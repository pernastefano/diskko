"""Which file is which track, when the import cannot tell on its own.

The state kept for a download that waits for the user: the files (name, tags, length), the
release group it is being matched to and the releases it may be, each with the pairing the engine
would make. The page shows it, the user changes the pairing, and the import then follows it.
Everything here is pure: no disk, no network.
"""

from types import SimpleNamespace

from app.integrations.musicbrainz_client import ReleaseData, TrackData
from app.services.importer.matching import FileFacts, pair_partial, rank_releases

MAX_RELEASE_OPTIONS = 6  # releases offered for one album


def track_key(track: TrackData) -> str:
    """The identity of a track inside a release: "disc-position" (a form field value)."""
    return f"{track.disc}-{track.position}"


def file_infos(facts: list[FileFacts]) -> list[dict]:
    """What the page shows of each file (JSON-able). `name` is the path inside the download."""
    return [
        {
            "name": f.file.relative,
            "title": f.title,
            "track_no": f.track_no,
            "disc_no": f.disc_no,
            "side": f.side,
            "seconds": round(f.file.info.duration) if f.file.info.duration else None,
            "size": f.file.size,
            "ext": f.file.path.suffix.lstrip(".").lower(),
        }
        for f in facts
    ]


def _facts_of(files: list[dict]) -> list:
    """Stand-ins for FileFacts: the pairing reads the title and the numbers, nothing else."""
    return [
        SimpleNamespace(
            title=f["title"], track_no=f["track_no"], disc_no=f["disc_no"], side=f.get("side")
        )
        for f in files
    ]


def _secs(track: TrackData) -> int | None:
    return round(track.length_ms / 1000) if track.length_ms else None


def release_option(release: ReleaseData, pairs: dict[int, TrackData], files: list[dict]) -> dict:
    """A release the files may be, with how they fit it: the tracks, which file stands for which
    (by file name), the tracks no file stands for and the files with no track."""
    paired = {id(t) for t in pairs.values()}
    return {
        "mbid": release.mbid,
        "title": release.title,
        "date": release.date,
        "country": release.country,
        "tracks": len(release.tracks),
        "paired": len(pairs),
        "track_list": [
            {
                "key": track_key(t),
                "disc": t.disc,
                "position": t.position,
                "number": t.number,
                "title": t.title,
                "seconds": _secs(t),
            }
            for t in release.tracks
        ],
        "pairs": {files[i]["name"]: track_key(t) for i, t in pairs.items()},
        "missing": [t.title for t in release.tracks if id(t) not in paired],
        "unmatched": [
            files[i]["name"].rsplit("/", 1)[-1] for i in range(len(files)) if i not in pairs
        ],
    }


def build_state(
    group: dict, files: list[dict], releases: list[ReleaseData], album_title: str
) -> dict:
    """The state to keep: the album (release group) being matched, the files and the releases that
    fit them best (the fullest pairing first)."""
    ranked = rank_releases(releases, _facts_of(files), album_title)
    return {
        "group": {"id": group["id"], "mbid": group["mbid"], "title": group["title"]},
        "files": files,
        "options": [
            release_option(c.release, c.pairs, files) for c in ranked[:MAX_RELEASE_OPTIONS]
        ],
        "loading": False,
    }


def pairs_from_mapping(
    files: list[dict], release: ReleaseData, mapping: dict[str, str]
) -> dict[int, TrackData]:
    """{index of file: track} as the user set it ({file name: track key}); a file that is not in
    the mapping, or maps to "", has no track."""
    by_key = {track_key(t): t for t in release.tracks}
    return {
        i: by_key[mapping[f["name"]]]
        for i, f in enumerate(files)
        if mapping.get(f["name"]) in by_key
    }


def automatic_pairs(files: list[dict], release: ReleaseData) -> dict[int, TrackData]:
    return pair_partial(_facts_of(files), release)
