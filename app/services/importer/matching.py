"""Which MusicBrainz release is this download, and which file is which track?

The release is the one with as many tracks as there are files (a deluxe edition and the plain
album have different counts), the best matching titles among those, then the earliest. Every file
is then paired with one track of the release, by title (and by disc and track number when they
agree). A file that cannot be paired with certainty stops the import: a wrong title or number in
the library is worse than a download waiting in the quarantine.
"""

import re
from dataclasses import dataclass
from itertools import product

from rapidfuzz import fuzz

from app.integrations.musicbrainz_client import ReleaseData, TrackData
from app.services.importer.integrity import DownloadedFile
from app.services.library.grouping import name_key
from app.services.musicbrainz.titles import album_key

TITLE_MIN = 0.80  # similarity of file title and track title
POSITION_TITLE_MIN = 0.55  # ... when disc and track number agree as well
_DISC_DIR = re.compile(r"(?:cd|disc|disk|dvd)\s*[-_. ]*(\d{1,2})", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"^\s*(?:(\d{1,2})\s*[-.]\s*)?(\d{1,3})\s*[-._ )]+\s*")


@dataclass
class FileFacts:
    """What is known about a downloaded file, from its tags and its path."""

    file: DownloadedFile
    title: str
    track_no: int | None
    disc_no: int


def _title_from_name(stem: str) -> tuple[str, int | None]:
    match = _LEADING_NUMBER.match(stem)
    if match and stem[match.end() :].strip():
        return stem[match.end() :].strip(), int(match.group(2))
    return stem, None


def facts_of(files: list[DownloadedFile]) -> list[FileFacts]:
    result = []
    for f in files:
        stem_title, stem_no = _title_from_name(f.path.stem)
        disc = f.info.disc_no
        if not disc:
            folder = f.relative.rsplit("/", 1)[0] if "/" in f.relative else ""
            found = _DISC_DIR.search(folder)
            disc = int(found.group(1)) if found else 1
        result.append(FileFacts(f, f.info.title or stem_title, f.info.track_no or stem_no, disc))
    return result


def _similar(a: str, b: str) -> float:
    ka, kb = album_key(a), album_key(b)
    if not ka or not kb:
        return 0.0
    return max(fuzz.ratio(ka, kb), fuzz.token_sort_ratio(ka, kb)) / 100


def _score(fact: FileFacts, track: TrackData) -> float:
    similarity = _similar(fact.title, track.title)
    same_position = fact.track_no == track.position and fact.disc_no == track.disc
    if same_position and similarity >= POSITION_TITLE_MIN:
        return similarity + 0.5
    return similarity if similarity >= TITLE_MIN else 0.0


def pair_tracks(facts: list[FileFacts], release: ReleaseData) -> dict[int, TrackData] | None:
    """{index into `facts`: its track}, one to one, or None when some file has no certain track."""
    scored = sorted(
        (
            (score, i, j)
            for (i, fact), (j, track) in product(enumerate(facts), enumerate(release.tracks))
            if (score := _score(fact, track)) > 0
        ),
        reverse=True,
    )
    used_files: set[int] = set()
    used_tracks: set[int] = set()
    pairs: dict[int, TrackData] = {}
    for _, i, j in scored:
        if i in used_files or j in used_tracks:
            continue
        used_files.add(i)
        used_tracks.add(j)
        pairs[i] = release.tracks[j]
    return pairs if len(pairs) == len(facts) else None


def choose_release(
    releases: list[ReleaseData], facts: list[FileFacts], album_title: str
) -> tuple[ReleaseData, dict[int, TrackData]] | None:
    """The release of the right size whose tracks all pair with the files, or None."""
    sized = [r for r in releases if len(r.tracks) == len(facts)]
    candidates = []
    for release in sized:
        pairs = pair_tracks(facts, release)
        if pairs is None:
            continue
        titles = sum(_similar(facts[i].title, t.title) for i, t in pairs.items())
        same_name = name_key(release.title) == name_key(album_title)
        candidates.append(
            (-titles, not same_name, release.date or "9999", release.mbid, release, pairs)
        )
    if not candidates:
        return None
    best = min(candidates, key=lambda c: c[:4])
    return best[4], best[5]
