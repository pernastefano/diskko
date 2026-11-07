"""Writing the tags of an imported file (mutagen).

The old tags are removed first (a downloaded file brings URLs, comments, other people's ids...),
except ReplayGain, which is still true. Then the standard fields are written, with the
MusicBrainz ids where the release is known, and the front cover is embedded.

Taggable: FLAC, MP3 (ID3v2.4), M4A/MP4, Ogg Vorbis and Opus. Other formats are imported as they
are. The field names are those Picard writes, so that Plex, Jellyfin and other tools read them.
"""

import base64
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCMP,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
    UFID,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

TAGGABLE = (".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".oga", ".opus")
_ITUNES = "----:com.apple.iTunes:"


class TaggingError(Exception):
    """The file could not be tagged."""


@dataclass
class TrackTags:
    title: str
    artist: str
    album_artist: str
    album: str
    track: int
    disc: int = 1
    date: str | None = None  # "1997", "1997-05" or "1997-05-21"
    track_total: int | None = None
    disc_total: int | None = None
    compilation: bool = False
    release_types: list[str] = field(default_factory=list)
    mbid_release: str | None = None
    mbid_release_group: str | None = None
    mbid_artists: list[str] = field(default_factory=list)
    mbid_album_artists: list[str] = field(default_factory=list)
    mbid_recording: str | None = None


@dataclass
class Cover:
    data: bytes
    mime: str  # image/jpeg or image/png


def can_tag(path: Path) -> bool:
    return path.suffix.lower() in TAGGABLE


def _number(value: int, total: int | None) -> str:
    return f"{value}/{total}" if total else str(value)


def _vorbis_fields(tags: TrackTags) -> dict[str, list[str]]:
    fields = {
        "title": [tags.title],
        "artist": [tags.artist],
        "albumartist": [tags.album_artist],
        "album": [tags.album],
        "tracknumber": [str(tags.track)],
        "discnumber": [str(tags.disc)],
    }
    if tags.date:
        fields["date"] = [tags.date]
    if tags.track_total:
        fields["tracktotal"] = fields["totaltracks"] = [str(tags.track_total)]
    if tags.disc_total:
        fields["disctotal"] = fields["totaldiscs"] = [str(tags.disc_total)]
    if tags.compilation:
        fields["compilation"] = ["1"]
    if tags.release_types:
        fields["releasetype"] = list(tags.release_types)
    if tags.mbid_release:
        fields["musicbrainz_albumid"] = [tags.mbid_release]
    if tags.mbid_release_group:
        fields["musicbrainz_releasegroupid"] = [tags.mbid_release_group]
    if tags.mbid_artists:
        fields["musicbrainz_artistid"] = list(tags.mbid_artists)
    if tags.mbid_album_artists:
        fields["musicbrainz_albumartistid"] = list(tags.mbid_album_artists)
    if tags.mbid_recording:
        fields["musicbrainz_trackid"] = [tags.mbid_recording]
    return fields


def _picture(cover: Cover) -> Picture:
    picture = Picture()
    picture.type, picture.mime, picture.desc, picture.data = 3, cover.mime, "Front", cover.data
    return picture


def _tag_flac(path: Path, tags: TrackTags, cover: Cover | None) -> None:
    audio = FLAC(path)
    replaygain = {
        k: list(v) for k, v in (audio.tags or {}).items() if k.lower().startswith("replaygain_")
    }
    audio.clear_pictures()
    audio.delete()
    audio = FLAC(path)
    if audio.tags is None:
        audio.add_tags()
    for key, values in {**_vorbis_fields(tags), **replaygain}.items():
        audio[key] = values
    if cover is not None:
        audio.add_picture(_picture(cover))
    audio.save()


def _tag_ogg(path: Path, tags: TrackTags, cover: Cover | None) -> None:
    opener = OggOpus if path.suffix.lower() == ".opus" else OggVorbis
    audio = opener(path)
    replaygain = {
        k: list(v) for k, v in (audio.tags or {}).items() if k.lower().startswith("replaygain_")
    }
    audio.delete()
    audio = opener(path)
    for key, values in {**_vorbis_fields(tags), **replaygain}.items():
        audio[key] = values
    if cover is not None:
        audio["metadata_block_picture"] = [
            base64.b64encode(_picture(cover).write()).decode("ascii")
        ]
    audio.save()


def _tag_mp3(path: Path, tags: TrackTags, cover: Cover | None) -> None:
    replaygain = []
    try:
        old = ID3(path)
        replaygain = [
            f
            for f in old.values()
            if isinstance(f, TXXX) and f.desc.lower().startswith("replaygain_")
        ]
    except ID3NoHeaderError:
        pass
    new = ID3()
    new.add(TIT2(encoding=3, text=tags.title))
    new.add(TPE1(encoding=3, text=tags.artist))
    new.add(TPE2(encoding=3, text=tags.album_artist))
    new.add(TALB(encoding=3, text=tags.album))
    new.add(TRCK(encoding=3, text=_number(tags.track, tags.track_total)))
    new.add(TPOS(encoding=3, text=_number(tags.disc, tags.disc_total)))
    if tags.date:
        new.add(TDRC(encoding=3, text=tags.date))
    if tags.compilation:
        new.add(TCMP(encoding=3, text="1"))

    def free(desc: str, values: list[str]) -> None:
        if values:
            new.add(TXXX(encoding=3, desc=desc, text=values))

    free("MusicBrainz Album Type", list(tags.release_types))
    free("MusicBrainz Album Id", [tags.mbid_release] if tags.mbid_release else [])
    free(
        "MusicBrainz Release Group Id", [tags.mbid_release_group] if tags.mbid_release_group else []
    )
    free("MusicBrainz Artist Id", list(tags.mbid_artists))
    free("MusicBrainz Album Artist Id", list(tags.mbid_album_artists))
    if tags.mbid_recording:
        new.add(UFID(owner="http://musicbrainz.org", data=tags.mbid_recording.encode("ascii")))
    for frame in replaygain:
        new.add(frame)
    if cover is not None:
        new.add(APIC(encoding=3, mime=cover.mime, type=3, desc="Front", data=cover.data))
    new.save(path, v1=0, v2_version=4)


def _tag_mp4(path: Path, tags: TrackTags, cover: Cover | None) -> None:
    audio = MP4(path)
    replaygain = {
        k: list(v)
        for k, v in (audio.tags or {}).items()
        if k.lower().startswith(_ITUNES.lower() + "replaygain_")
    }
    audio.delete()
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    audio["\xa9nam"] = [tags.title]
    audio["\xa9ART"] = [tags.artist]
    audio["aART"] = [tags.album_artist]
    audio["\xa9alb"] = [tags.album]
    if tags.date:
        audio["\xa9day"] = [tags.date]
    audio["trkn"] = [(tags.track, tags.track_total or 0)]
    audio["disk"] = [(tags.disc, tags.disc_total or 0)]
    if tags.compilation:
        audio["cpil"] = True

    def free(name: str, values: list[str]) -> None:
        if values:
            audio[_ITUNES + name] = [MP4FreeForm(v.encode("utf-8")) for v in values]

    free("MusicBrainz Album Type", list(tags.release_types))
    free("MusicBrainz Album Id", [tags.mbid_release] if tags.mbid_release else [])
    free(
        "MusicBrainz Release Group Id", [tags.mbid_release_group] if tags.mbid_release_group else []
    )
    free("MusicBrainz Artist Id", list(tags.mbid_artists))
    free("MusicBrainz Album Artist Id", list(tags.mbid_album_artists))
    free("MusicBrainz Track Id", [tags.mbid_recording] if tags.mbid_recording else [])
    for key, values in replaygain.items():
        audio[key] = values
    if cover is not None:
        fmt = MP4Cover.FORMAT_PNG if cover.mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover.data, imageformat=fmt)]
    audio.save()


def write_tags(path: Path, tags: TrackTags, cover: Cover | None = None) -> None:
    """Replace the tags of `path`. Raises TaggingError."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".flac":
            _tag_flac(path, tags, cover)
        elif suffix in (".ogg", ".oga", ".opus"):
            _tag_ogg(path, tags, cover)
        elif suffix == ".mp3":
            _tag_mp3(path, tags, cover)
        elif suffix in (".m4a", ".mp4"):
            _tag_mp4(path, tags, cover)
        else:
            raise TaggingError(f"cannot tag {suffix} files")
    except TaggingError:
        raise
    except (mutagen.MutagenError, OSError, ValueError, TypeError, KeyError) as exc:
        raise TaggingError(f"{path.name}: {type(exc).__name__}: {exc}") from exc
