"""An album of the library against each release of a MusicBrainz release group.

A release group has several releases (editions) and they do not have the same tracks: the
person matching an album sees, for each, how many of the files have a track there and which
tracks and files are left over, and picks the release that is right. Pure: no disk, no network.
"""

from dataclasses import dataclass
from types import SimpleNamespace

from app.integrations.musicbrainz_client import ReleaseData
from app.models import LibraryAlbum, LibraryTrack
from app.services import album_compare as compare_svc
from app.services.musicbrainz.titles import album_key

MAX_RELEASES = 40


@dataclass
class ReleaseFit:
    release: ReleaseData
    cmp: compare_svc.Comparison

    @property
    def paired(self) -> int:
        return self.cmp.paired

    @property
    def exact(self) -> bool:
        """Every file has a track and every track has a file."""
        return self.cmp.local_only == 0 and self.cmp.remote_only == 0


def compare_release(
    album: LibraryAlbum, local: list[LibraryTrack], release: ReleaseData
) -> compare_svc.Comparison:
    remote = [
        SimpleNamespace(
            id=i, disc=t.disc, position=t.position, title=t.title, length_ms=t.length_ms
        )
        for i, t in enumerate(release.tracks)
    ]
    group = SimpleNamespace(title=release.title, year=_year(release.date))
    return compare_svc.compare(album, local, group, remote)  # type: ignore[arg-type]


def _year(date: str | None) -> int | None:
    head = (date or "")[:4]
    return int(head) if head.isdigit() else None


def rank(
    album: LibraryAlbum, local: list[LibraryTrack], releases: list[ReleaseData]
) -> list[ReleaseFit]:
    """The releases the files fit best first: the most files with a track, then the fewest
    tracks and files left over, the title like the album's, the earliest."""
    fits = [ReleaseFit(r, compare_release(album, local, r)) for r in releases]
    fits.sort(
        key=lambda f: (
            -f.paired,
            f.cmp.local_only + f.cmp.remote_only,
            album_key(f.release.title) != album_key(album.title),
            f.release.date or "9999",
            f.release.mbid,
        )
    )
    return fits[:MAX_RELEASES]
