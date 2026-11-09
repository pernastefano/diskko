"""An album that is in the library more than once: show the copies, keep one, delete the others.

Copies come from imports and scans that left the old files behind (an upgrade into another
folder, a Plex library read next to a folder library...). Deleting is for the administrator, who
chooses the copy to keep; the files of the others are removed from the disk, the cover and the
folders that are left empty with them.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LibraryAlbum, LibraryTrack, User
from app.services import audit
from app.services.importer import library_import as lib

OWNED = ("matched", "tentative", "manual")


@dataclass
class Copy:
    album: LibraryAlbum
    old: lib.OldAlbum
    tracks: int
    size: int


def copies_of(db: Session, music_dir: Path, release_group_id: int) -> list[Copy]:
    """The owned copies of an album with what tells them apart (a single one: no duplicate)."""
    found = []
    olds = {o.id: o for o in lib.owned_albums(db, music_dir, release_group_id)}
    albums = db.scalars(
        select(LibraryAlbum)
        .where(
            LibraryAlbum.release_group_id == release_group_id, LibraryAlbum.match_status.in_(OWNED)
        )
        .order_by(LibraryAlbum.id)
    )
    for album in albums:
        old = olds.get(album.id)
        if old is None:
            continue
        tracks = db.scalars(select(LibraryTrack).where(LibraryTrack.album_id == album.id)).all()
        found.append(Copy(album, old, len(tracks), sum(t.file_size or 0 for t in tracks)))
    return found


def keep_one(
    db: Session, music_dir: Path, release_group_id: int, keep_album_id: int, user: User
) -> tuple[int, list[Path]]:
    """Delete every owned copy but the chosen one. The caller commits. Returns (how many copies
    were removed, the folders to tell Plex about)."""
    copies = copies_of(db, music_dir, release_group_id)
    chosen = next((c for c in copies if c.album.id == keep_album_id), None)
    if chosen is None or len(copies) < 2:
        return 0, []
    keep = {p.resolve() for p in chosen.old.files}
    removed, plex_paths = 0, []
    for copy in copies:
        if copy is chosen:
            continue
        # a file that is also one of the kept copy's must stay (the same file listed twice)
        lib.retire_album(copy.old, music_dir, keep, chosen.old.folder)
        if copy.old.folder != chosen.old.folder:
            plex_paths.append(copy.old.folder.parent)
        db.delete(copy.album)
        removed += 1
    audit.record(
        db,
        user,
        "album.dedupe",
        target_type="album",
        target_id=chosen.album.id,
        label=chosen.album.title,
        details={"removed": removed},
    )
    return removed, plex_paths
