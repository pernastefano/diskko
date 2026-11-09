"""Your album next to a MusicBrainz release group: what agrees and what does not.

Used by the matching page so the choice is made with both in front: the facts of the album
(title, year, number of tracks) and the tracks one against the other. A local track and a
MusicBrainz track are paired by title (and by position when the title is close), one to one;
what is left on either side is shown as "only here" / "only on MusicBrainz".
"""

from dataclasses import dataclass, field
from itertools import product

from app.models import LibraryAlbum, LibraryTrack, ReleaseGroup, ReleaseTrack
from app.services.importer.matching import POSITION_TITLE_MIN, TITLE_MIN, _similar
from app.services.musicbrainz.titles import album_key

# Row states
SAME = "same"  # paired, same title and same place
DIFFERS = "differs"  # paired, but the title or the place is not the same
LOCAL_ONLY = "local_only"  # a file with no track on MusicBrainz
REMOTE_ONLY = "remote_only"  # a track on MusicBrainz with no file


@dataclass
class Fact:
    """One line of the summary: the same thing, here and on MusicBrainz."""

    key: str  # i18n suffix: title | year | tracks
    local: str
    remote: str
    same: bool


@dataclass
class Row:
    local: LibraryTrack | None
    remote: ReleaseTrack | None
    state: str
    reasons: list[str] = field(default_factory=list)  # title | place | length


@dataclass
class Comparison:
    facts: list[Fact]
    rows: list[Row]
    local_total: int
    remote_total: int

    @property
    def paired(self) -> int:
        return len([r for r in self.rows if r.local and r.remote])

    @property
    def agreeing(self) -> int:
        return len([r for r in self.rows if r.state == SAME])

    @property
    def local_only(self) -> int:
        return len([r for r in self.rows if r.state == LOCAL_ONLY])

    @property
    def remote_only(self) -> int:
        return len([r for r in self.rows if r.state == REMOTE_ONLY])

    @property
    def verdict(self) -> str:
        """good: every track agrees; close: all paired but some differ or are missing; far: the
        track lists have little in common (a different edition, or another album)."""
        if not self.remote_total:
            return "unknown"
        if self.agreeing == self.remote_total == self.local_total:
            return "good"
        return "close" if self.paired >= max(self.remote_total, self.local_total) * 0.6 else "far"


def _score(local: LibraryTrack, remote: ReleaseTrack) -> float:
    similarity = _similar(local.title, remote.title)
    same_place = (local.disc_no or 1) == remote.disc and local.track_no == remote.position
    if same_place and similarity >= POSITION_TITLE_MIN:
        return similarity + 0.5
    return similarity if similarity >= TITLE_MIN else 0.0


def pair(
    local: list[LibraryTrack], remote: list[ReleaseTrack]
) -> list[tuple[LibraryTrack, ReleaseTrack]]:
    """One to one, the best pairs first."""
    scored = sorted(
        (
            (score, i, j)
            for (i, a), (j, b) in product(enumerate(local), enumerate(remote))
            if (score := _score(a, b)) > 0
        ),
        reverse=True,
    )
    used_local: set[int] = set()
    used_remote: set[int] = set()
    pairs = []
    for _, i, j in scored:
        if i in used_local or j in used_remote:
            continue
        used_local.add(i)
        used_remote.add(j)
        pairs.append((local[i], remote[j]))
    return pairs


def _length_differs(local: LibraryTrack, remote: ReleaseTrack) -> bool:
    if not local.duration or not remote.length_ms:
        return False
    return abs(local.duration - remote.length_ms / 1000) > 5


def _order(row: Row) -> tuple:
    if row.remote is not None:
        return (row.remote.disc, row.remote.position, 0)
    return (row.local.disc_no or 1, row.local.track_no or 999, 1)


def compare(
    album: LibraryAlbum,
    local_tracks: list[LibraryTrack],
    group: ReleaseGroup,
    remote_tracks: list[ReleaseTrack],
) -> Comparison:
    rows: list[Row] = []
    paired_local, paired_remote = set(), set()
    for local, remote in pair(local_tracks, remote_tracks):
        paired_local.add(local.id)
        paired_remote.add(remote.id)
        reasons = []
        if album_key(local.title) != album_key(remote.title):
            reasons.append("title")
        if (local.disc_no or 1, local.track_no) != (remote.disc, remote.position):
            reasons.append("place")
        if _length_differs(local, remote):
            reasons.append("length")
        rows.append(Row(local, remote, DIFFERS if reasons else SAME, reasons))
    rows += [Row(t, None, LOCAL_ONLY) for t in local_tracks if t.id not in paired_local]
    rows += [Row(None, t, REMOTE_ONLY) for t in remote_tracks if t.id not in paired_remote]
    rows.sort(key=_order)

    local_count = len(local_tracks) or album.track_count
    remote_count = len(remote_tracks)
    facts = [
        Fact("title", album.title, group.title, album_key(album.title) == album_key(group.title)),
        Fact(
            "year",
            str(album.year or "—"),
            str(group.year or "—"),
            bool(album.year and group.year and album.year == group.year),
        ),
        Fact(
            "tracks",
            str(local_count),
            str(remote_count) if remote_tracks else "—",
            bool(remote_tracks) and local_count == remote_count,
        ),
    ]
    return Comparison(facts, rows, local_count, remote_count)
