"""Re-tagging an album that is already in the library: what the tags are now, what they would
become, which tracks of the MusicBrainz release have no file, and the writing itself.

`build` only reads (the files and the MusicBrainz release), so the same code serves the preview
and the real run. Only the files the library tracks for the album are looked at, only the tags
this program writes are changed (genre, lyrics, comments and ReplayGain stay) and every file is
written as a copy that replaces the original once it has been checked.
"""

import contextlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from app.integrations.musicbrainz_client import ReleaseData, TrackData
from app.services.importer.integrity import DownloadedFile
from app.services.importer.matching import Candidate, FileFacts, facts_of, rank_releases
from app.services.importer.naming import NameValues, file_stem
from app.services.importer.tagging import Cover, TaggingError, TrackTags, can_tag, write_tags
from app.services.library.tags import (
    UnreadableAudio,
    clean,
    parse_int,
    read_audio_file,
)

# What is compared and shown, in this order (translations: retag.field.<name>).
FIELDS = (
    "title",
    "artist",
    "albumartist",
    "album",
    "track",
    "tracktotal",
    "disc",
    "disctotal",
    "date",
    "compilation",
    "releasetype",
    "mb_release",
    "mb_releasegroup",
    "mb_artist",
    "mb_albumartist",
    "mb_recording",
)
MAX_CANDIDATES = 12
TMP_PREFIX = ".diskko-retag-"


# ------------------------------------------------------------------------ reading


def _join(values: Any) -> str:
    items = [clean(v) for v in (values if isinstance(values, list | tuple) else [values])]
    return "; ".join(v for v in items if v)


def _text(frame: Any) -> str:
    return clean(frame.text[0]) or "" if frame is not None and getattr(frame, "text", None) else ""


def _total(value: Any) -> str:
    """'3/12' -> '12'."""
    _, _, total = str(value or "").partition("/")
    return str(parse_int(total) or "")


def _vorbis_snapshot(tags: Any) -> dict[str, str]:
    def get(*keys: str) -> str:
        for key in keys:
            values = tags.get(key) if tags is not None else None
            if values:
                return _join(values)
        return ""

    return {
        "title": get("title"),
        "artist": get("artist"),
        "albumartist": get("albumartist", "album artist"),
        "album": get("album"),
        "track": str(parse_int(get("tracknumber")) or ""),
        "tracktotal": get("tracktotal", "totaltracks") or _total(get("tracknumber")),
        "disc": str(parse_int(get("discnumber")) or ""),
        "disctotal": get("disctotal", "totaldiscs") or _total(get("discnumber")),
        "date": get("date", "year"),
        "compilation": "1" if get("compilation").lower() in ("1", "true", "yes") else "",
        "releasetype": ", ".join(tags.get("releasetype", [])) if tags is not None else "",
        "mb_release": get("musicbrainz_albumid"),
        "mb_releasegroup": get("musicbrainz_releasegroupid"),
        "mb_artist": get("musicbrainz_artistid"),
        "mb_albumartist": get("musicbrainz_albumartistid"),
        "mb_recording": get("musicbrainz_trackid"),
    }


def _id3_snapshot(tags: Any) -> dict[str, str]:
    free = {f.desc.lower(): _join(f.text) for f in tags.getall("TXXX")} if tags else {}
    ufid = ""
    for frame in tags.getall("UFID") if tags else []:
        if frame.owner == "http://musicbrainz.org":
            ufid = frame.data.decode("ascii", "ignore")
    text = (lambda key: _text(tags.get(key))) if tags else (lambda key: "")
    return {
        "title": text("TIT2"),
        "artist": text("TPE1"),
        "albumartist": text("TPE2"),
        "album": text("TALB"),
        "track": str(parse_int(text("TRCK")) or ""),
        "tracktotal": _total(text("TRCK")),
        "disc": str(parse_int(text("TPOS")) or ""),
        "disctotal": _total(text("TPOS")),
        "date": text("TDRC") or text("TYER"),
        "compilation": "1" if text("TCMP").lower() in ("1", "true", "yes") else "",
        "releasetype": ", ".join(free.get("musicbrainz album type", "").split("; ")).strip(", "),
        "mb_release": free.get("musicbrainz album id", ""),
        "mb_releasegroup": free.get("musicbrainz release group id", ""),
        "mb_artist": free.get("musicbrainz artist id", ""),
        "mb_albumartist": free.get("musicbrainz album artist id", ""),
        "mb_recording": ufid,
    }


def _mp4_snapshot(tags: Any) -> dict[str, str]:
    def get(key: str) -> str:
        values = tags.get(key) if tags is not None else None
        if not values:
            return ""
        value = values[0]
        return clean(value.decode("utf-8", "ignore") if isinstance(value, bytes) else value) or ""

    def pair(key: str) -> tuple[str, str]:
        values = tags.get(key) if tags is not None else None
        if not values or not values[0]:
            return "", ""
        number, total = (list(values[0]) + [0, 0])[:2]
        return str(number or ""), str(total or "")

    def free(name: str) -> str:
        values = tags.get("----:com.apple.iTunes:" + name) if tags is not None else None
        return _join(
            [v.decode("utf-8", "ignore") if isinstance(v, bytes) else v for v in values or []]
        )

    track, tracktotal = pair("trkn")
    disc, disctotal = pair("disk")
    return {
        "title": get("\xa9nam"),
        "artist": get("\xa9ART"),
        "albumartist": get("aART"),
        "album": get("\xa9alb"),
        "track": track,
        "tracktotal": tracktotal,
        "disc": disc,
        "disctotal": disctotal,
        "date": get("\xa9day"),
        "compilation": "1" if tags is not None and tags.get("cpil") else "",
        "releasetype": ", ".join(free("MusicBrainz Album Type").split("; ")).strip(", "),
        "mb_release": free("MusicBrainz Album Id"),
        "mb_releasegroup": free("MusicBrainz Release Group Id"),
        "mb_artist": free("MusicBrainz Artist Id"),
        "mb_albumartist": free("MusicBrainz Album Artist Id"),
        "mb_recording": free("MusicBrainz Track Id"),
    }


def read_snapshot(path: Path) -> tuple[dict[str, str], bool]:
    """(the tags shown and compared, whether the file has an embedded cover). Raises
    UnreadableAudio."""
    try:
        audio = mutagen.File(path)
    except (mutagen.MutagenError, OSError, ValueError, TypeError, EOFError) as exc:
        raise UnreadableAudio(f"{type(exc).__name__}: {exc}") from exc
    if audio is None:
        raise UnreadableAudio("unsupported or corrupt audio file")
    tags = audio.tags
    if isinstance(audio, FLAC):
        return _vorbis_snapshot(tags), bool(audio.pictures)
    if isinstance(audio, OggVorbis | OggOpus):
        return _vorbis_snapshot(tags), bool(tags and tags.get("metadata_block_picture"))
    if isinstance(audio, MP4):
        return _mp4_snapshot(tags), bool(tags and tags.get("covr"))
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = None
    except (mutagen.MutagenError, OSError) as exc:
        raise UnreadableAudio(str(exc)) from exc
    return _id3_snapshot(id3), bool(id3 and id3.getall("APIC"))


def planned_snapshot(tags: TrackTags) -> dict[str, str]:
    """The same fields as `read_snapshot`, for the tags that would be written."""
    return {
        "title": tags.title,
        "artist": tags.artist,
        "albumartist": tags.album_artist,
        "album": tags.album,
        "track": str(tags.track),
        "tracktotal": str(tags.track_total or ""),
        "disc": str(tags.disc),
        "disctotal": str(tags.disc_total or ""),
        "date": tags.date or "",
        "compilation": "1" if tags.compilation else "",
        "releasetype": ", ".join(tags.release_types),
        "mb_release": tags.mbid_release or "",
        "mb_releasegroup": tags.mbid_release_group or "",
        "mb_artist": "; ".join(tags.mbid_artists),
        "mb_albumartist": "; ".join(tags.mbid_album_artists),
        "mb_recording": tags.mbid_recording or "",
    }


def differences(current: dict[str, str], planned: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"field": name, "old": current.get(name, ""), "new": planned.get(name, "")}
        for name in FIELDS
        if current.get(name, "") != planned.get(name, "")
    ]


# --------------------------------------------------------------------------- plan


@dataclass
class Item:
    """One file of the album."""

    path: Path
    relative: str
    taggable: bool
    # same | change | unmatched | unreadable | untaggable | written | failed (after a run)
    state: str = "same"
    track: TrackData | None = None
    tags: TrackTags | None = None
    name: NameValues | None = None
    has_cover: bool = False
    current: dict[str, str] = field(default_factory=dict)
    changes: list[dict[str, str]] = field(default_factory=list)
    set_cover: bool = False
    new_name: str | None = None
    error: str | None = None


@dataclass
class Plan:
    items: list[Item]
    candidates: list[Candidate]
    chosen: Candidate | None
    missing: list[TrackData]
    folder_target: Path | None = None
    folder_values: NameValues | None = None
    cover: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def to_write(self) -> list[Item]:
        return [i for i in self.items if i.state == "change" or i.set_cover]

    def report(self) -> dict[str, Any]:
        release = self.chosen.release if self.chosen else None
        return {
            "release": _release_row(self.chosen) if self.chosen else None,
            "candidates": [_release_row(c) for c in self.candidates[:MAX_CANDIDATES]],
            "files": [
                {
                    "name": i.relative,
                    "state": i.state,
                    "track": _track_row(i.track),
                    "changes": i.changes,
                    "cover": i.set_cover,
                    "rename": i.new_name,
                    "error": i.error,
                }
                for i in self.items
            ],
            "missing": [_track_row(t) for t in self.missing],
            "extra": [i.relative for i in self.items if i.state == "unmatched"],
            "cover": self.cover,
            "folder": (
                {"to": self.folder_target.name, "parent": self.folder_target.parent.name}
                if self.folder_target
                else None
            ),
            "notes": self.notes,
            "summary": {
                "files": len(self.items),
                "to_change": len([i for i in self.items if i.state == "change"]),
                "same": len([i for i in self.items if i.state == "same"]),
                "unmatched": len([i for i in self.items if i.state == "unmatched"]),
                "written": len([i for i in self.items if i.state == "written"]),
                "failed": len([i for i in self.items if i.state == "failed"]),
                "tracks": len(release.tracks) if release else 0,
                "missing": len(self.missing),
                "cover_to_embed": len([i for i in self.items if i.set_cover]),
            },
        }


def _release_row(c: Candidate) -> dict[str, Any]:
    r: ReleaseData = c.release
    return {
        "mbid": r.mbid,
        "title": r.title,
        "date": r.date or "",
        "country": r.country or "",
        "status": r.status or "",
        "tracks": len(r.tracks),
        "paired": len(c.pairs),
    }


def _track_row(t: TrackData | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {
        "disc": t.disc,
        "position": t.position,
        "title": t.title,
        "seconds": round(t.length_ms / 1000) if t.length_ms else None,
    }


def read_files(folder: Path, paths: list[Path]) -> tuple[list[DownloadedFile], dict[Path, str]]:
    """The tracked files that can be read, and the reason for those that cannot."""
    good, bad = [], {}
    for path in paths:
        try:
            info = read_audio_file(path)
            good.append(DownloadedFile(path, _relative(path, folder), info, path.stat().st_size))
        except (UnreadableAudio, OSError) as exc:
            bad[path] = str(exc)
    return good, bad


def _relative(path: Path, folder: Path) -> str:
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return path.name


def build(
    folder: Path,
    paths: list[Path],
    releases: list[ReleaseData],
    group: dict,
    artist_name: str,
    build_plan,
    *,
    release_mbid: str | None = None,
    rename: bool = False,
    file_template: str = "",
    missing_files: int = 0,
) -> Plan:
    """What re-tagging the files of `folder` would do. `build_plan` is the import's function
    that turns paired files into their tags (passed in: the import handler owns it)."""
    files, unreadable = read_files(folder, paths)
    items: list[Item] = [
        Item(p, _relative(p, folder), can_tag(p), state="unreadable", error=why)
        for p, why in unreadable.items()
    ]
    notes: list[str] = []
    if missing_files:
        notes.append(f"files_missing_on_disk:{missing_files}")
    facts: list[FileFacts] = facts_of(files)
    tagged = _majority([f.file.info.mbid_release for f in facts])
    ranked = rank_releases(releases, facts, group["title"], tagged=tagged)
    chosen = None
    if release_mbid:
        chosen = next((c for c in ranked if c.release.mbid == release_mbid), None)
    if chosen is None and ranked:
        chosen = ranked[0]
    if chosen is None or not facts:
        items += [
            Item(f.file.path, f.file.relative, can_tag(f.file.path), "unmatched") for f in facts
        ]
        return Plan(_sorted(items), ranked, chosen, [], notes=notes)

    paired = sorted(chosen.pairs)
    sub_facts = [facts[i] for i in paired]
    sub_pairs = {k: chosen.pairs[i] for k, i in enumerate(paired)}
    planned, folder_values = build_plan(sub_facts, chosen.release, sub_pairs, group, artist_name)
    multi_disc = len({p.tags.disc for p in planned}) > 1
    for k, p in enumerate(planned):
        path = sub_facts[k].file.path
        item = Item(path, sub_facts[k].file.relative, p.taggable, track=sub_pairs[k])
        item.tags, item.name = p.tags, p.name
        if not p.taggable:
            item.state = "untaggable"
        else:
            try:
                item.current, item.has_cover = read_snapshot(path)
            except UnreadableAudio as exc:
                item.state, item.error = "unreadable", str(exc)
            else:
                item.changes = differences(item.current, planned_snapshot(p.tags))
                item.state = "change" if item.changes else "same"
        items.append(item)
    for i, fact in enumerate(facts):
        if i not in chosen.pairs:
            item = Item(fact.file.path, fact.file.relative, can_tag(fact.file.path), "unmatched")
            with contextlib.suppress(UnreadableAudio):
                item.current, item.has_cover = read_snapshot(fact.file.path)
            items.append(item)
    used = {(t.disc, t.position) for t in chosen.pairs.values()}
    missing = [t for t in chosen.release.tracks if (t.disc, t.position) not in used]
    plan = Plan(_sorted(items), ranked, chosen, missing, notes=notes)
    if rename and file_template:
        _plan_renames(plan, folder, file_template, multi_disc)
    plan.folder_values = folder_values
    return plan


def _sorted(items: list[Item]) -> list[Item]:
    return sorted(
        items,
        key=lambda i: (
            i.track.disc if i.track else 99,
            i.track.position if i.track else 999,
            i.relative,
        ),
    )


def _majority(values: list[str | None]) -> str | None:
    found = [v for v in values if v]
    return max(set(found), key=found.count) if found else None


def _plan_renames(plan: Plan, folder: Path, template: str, multi_disc: bool) -> None:
    """The new name of every file whose name differs from the template's. A name that two files
    would share, or that another file of the folder already has, is not used."""
    taken = {p.name.lower() for p in folder.iterdir()} if folder.is_dir() else set()
    wanted: dict[str, Item] = {}
    for item in plan.items:
        if item.name is None or item.state in ("unreadable", "unmatched"):
            continue
        new = file_stem(template, item.name, multi_disc=multi_disc) + item.path.suffix.lower()
        if new == item.path.name:
            continue
        key = new.lower()
        if key in wanted or (key in taken and key != item.path.name.lower()):
            plan.notes.append(f"rename_skipped:{item.path.name}")
            continue
        wanted[key] = item
        item.new_name = new
    renamed = {i.path.name.lower() for i in wanted.values()}
    for key, item in list(wanted.items()):  # a target that is the current name of a file staying
        if key in taken and key not in renamed and key != item.path.name.lower():
            item.new_name = None


def choose_cover_targets(plan: Plan, replace: bool) -> int:
    """Mark the files that get the new cover (all of them, or the ones without) and return how
    many."""
    count = 0
    for item in plan.items:
        item.set_cover = bool(
            item.taggable and item.state in ("change", "same") and (replace or not item.has_cover)
        )
        count += item.set_cover
    return count


# -------------------------------------------------------------------------- writing


def write_item(item: Item, cover: Cover | None) -> None:
    """Re-tag one file: a copy next to it is tagged, checked and then replaces the original."""
    assert item.tags is not None
    tmp = item.path.with_name(f"{TMP_PREFIX}{item.path.name}")
    try:
        shutil.copy2(item.path, tmp)
        write_tags(tmp, item.tags, cover if item.set_cover else None, keep_other=True)
        before, after = read_audio_file(item.path), read_audio_file(tmp)
        if not after.duration or abs((before.duration or 0) - after.duration) > 1.0:
            raise TaggingError(f"{item.path.name}: the audio changed while tagging")
        wrote, _ = read_snapshot(tmp)
        wrong = differences(wrote, planned_snapshot(item.tags))
        if wrong:
            raise TaggingError(f"{item.path.name}: {wrong[0]['field']} was not written")
        os.replace(tmp, item.path)
    except (UnreadableAudio, OSError) as exc:
        raise TaggingError(f"{item.path.name}: {type(exc).__name__}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def rename_item(item: Item) -> Path:
    """Rename a file inside its folder (never over another file). Returns the new path."""
    assert item.new_name
    target = item.path.with_name(item.new_name)
    if target.exists() and target.resolve() != item.path.resolve():
        raise OSError(f"{target.name} already exists")
    os.rename(item.path, target)
    return target
