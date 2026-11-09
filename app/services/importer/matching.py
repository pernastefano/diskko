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
# a vinyl rip names its files after the side: "A1 - Title", "B4. Title" (the same "A1" that
# MusicBrainz gives the track of a vinyl release)
_SIDE_NUMBER = re.compile(r"^\s*([A-Ha-h]\d{1,2})\s*[-._ )]+\s*")


@dataclass
class FileFacts:
    """What is known about a downloaded file, from its tags and its path."""

    file: DownloadedFile
    title: str
    track_no: int | None
    disc_no: int
    side: str | None = None  # "A1": the number on a vinyl release


def _title_from_name(stem: str) -> tuple[str, int | None, str | None]:
    """(title, track number, side number like "A1") a file name tells."""
    side = _SIDE_NUMBER.match(stem)
    if side and stem[side.end() :].strip():
        return stem[side.end() :].strip(), None, side.group(1).upper()
    match = _LEADING_NUMBER.match(stem)
    if match and stem[match.end() :].strip():
        return stem[match.end() :].strip(), int(match.group(2)), None
    return stem, None, None


def facts_of(files: list[DownloadedFile]) -> list[FileFacts]:
    result = []
    for f in files:
        stem_title, stem_no, side = _title_from_name(f.path.stem)
        disc = f.info.disc_no
        if not disc:
            folder = f.relative.rsplit("/", 1)[0] if "/" in f.relative else ""
            found = _DISC_DIR.search(folder)
            disc = int(found.group(1)) if found else 1
        result.append(
            FileFacts(f, f.info.title or stem_title, f.info.track_no or stem_no, disc, side)
        )
    return result


_FEATURING = re.compile(
    r"\s*[(\[]\s*(?:feat|ft|featuring)\b[^)\]]*[)\]]|\s+(?:feat|ft|featuring)\.?\s.*$",
    re.IGNORECASE,
)


def without_guests(title: str) -> str:
    """The title without its "(feat. ...)" part: files and MusicBrainz do not always agree on
    whether the guests belong to the title, and a long list of them drowns a short title."""
    return _FEATURING.sub("", title).strip() or title


def _similar(a: str, b: str) -> float:
    ka, kb = album_key(without_guests(a)), album_key(without_guests(b))
    if not ka or not kb:
        return 0.0
    return max(fuzz.ratio(ka, kb), fuzz.token_sort_ratio(ka, kb)) / 100


def _score(fact: FileFacts, track: TrackData) -> float:
    similarity = _similar(fact.title, track.title)
    same_position = (fact.track_no == track.position and fact.disc_no == track.disc) or bool(
        fact.side and track.number and fact.side == track.number.strip().upper()
    )
    if same_position and similarity >= POSITION_TITLE_MIN:
        return similarity + 0.5
    return similarity if similarity >= TITLE_MIN else 0.0


def pair_partial(facts: list[FileFacts], release: ReleaseData) -> dict[int, TrackData]:
    """{index into `facts`: its track}, one to one, for the files that have a track they match
    (some files may have none, some tracks may have no file)."""
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
    return pairs


def pair_tracks(facts: list[FileFacts], release: ReleaseData) -> dict[int, TrackData] | None:
    """{index into `facts`: its track}, one to one, or None when some file has no certain track."""
    pairs = pair_partial(facts, release)
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


@dataclass
class Candidate:
    """A release of the album and how well the files fit it."""

    release: ReleaseData
    pairs: dict[int, TrackData]  # file index -> track

    @property
    def missing(self) -> int:
        """Tracks of the release no file stands for."""
        return len(self.release.tracks) - len(self.pairs)


def rank_releases(
    releases: list[ReleaseData],
    facts: list[FileFacts],
    album_title: str,
    *,
    tagged: str | None = None,
) -> list[Candidate]:
    """Every release with its pairing, the most likely first: the one that pairs the most files,
    then the release the files are already tagged with, then the closest in size, the one named
    like the album, the earliest. Unlike `choose_release` the files and tracks need not agree in
    number: a missing track or an extra file is something to show, not a reason to stop."""
    candidates = [Candidate(r, pair_partial(facts, r)) for r in releases]

    def key(c: Candidate):
        return (
            -len(c.pairs),
            c.release.mbid != tagged,
            abs(len(c.release.tracks) - len(facts)),
            name_key(c.release.title) != name_key(album_title),
            c.release.date or "9999",
            c.release.mbid,
        )

    return sorted(candidates, key=key)
