"""Run a library scan: ask the provider, then persist the result per artist."""

import time
from collections import Counter
from dataclasses import asdict, dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import Artist, LibraryAlbum, LibraryTrack, utcnow
from app.services.app_settings import get_setting, set_setting
from app.services.library.grouping import name_key
from app.services.library.provider import (
    LibraryError,
    LibraryProvider,
    RecordingReporter,
    ScannedAlbum,
    ScannedArtist,
    ScannedTrack,
    ScanReporter,
    track_fields,
)

LAST_SCAN_KEY = "library.last_scan"
MIN_TRACKS_FOR_REMOVAL_GUARD = 20
CHUNK = 500

Reporter = ScanReporter | RecordingReporter


@dataclass
class ScanSummary:
    source: str = ""
    forced: bool = False
    files_seen: int = 0
    files_read: int = 0
    files_skipped: int = 0
    errors: int = 0
    artists: int = 0
    albums: int = 0
    tracks: int = 0
    tracks_added: int = 0
    tracks_removed: int = 0
    albums_removed: int = 0
    artists_deactivated: int = 0
    duration_s: float = 0.0
    finished_at: str = ""


def _track_to_scanned(row: LibraryTrack) -> ScannedTrack:
    fields = {k: getattr(row, k) for k in track_fields(ScannedTrack(path="")) if hasattr(row, k)}
    return ScannedTrack(path=row.path, **fields)


def load_known(db: Session, source: str) -> tuple[dict[str, ScannedTrack], dict[str, int]]:
    """Tracks already in the DB for `source`: path -> data, and path -> album id."""
    known: dict[str, ScannedTrack] = {}
    album_of: dict[str, int] = {}
    for row in db.scalars(select(LibraryTrack).where(LibraryTrack.source == source)):
        known[row.path] = _track_to_scanned(row)
        album_of[row.path] = row.album_id
    return known, album_of


def _find_artist(db: Session, scanned: ScannedArtist) -> Artist | None:
    if scanned.mbid:
        artist = db.scalar(select(Artist).where(Artist.mbid == scanned.mbid))
        if artist:
            return artist
        # An artist stored without MBID gets it now.
        return db.scalar(
            select(Artist).where(Artist.name_key == name_key(scanned.name), Artist.mbid.is_(None))
        )
    return db.scalar(
        select(Artist)
        .where(Artist.name_key == name_key(scanned.name))
        .order_by(Artist.mbid.is_(None).desc(), Artist.id)
    )


def _persist_artist(
    db: Session,
    source: str,
    scanned: ScannedArtist,
    album_of: dict[str, int],
    claimed: set[int],
) -> tuple[int, int]:
    """Save one artist with albums and tracks. Returns (albums, tracks added)."""
    artist = _find_artist(db, scanned)
    if artist is None:
        artist = Artist(
            name=scanned.name, sort_name=scanned.sort_name, name_key=name_key(scanned.name)
        )
        db.add(artist)
    artist.name, artist.sort_name = scanned.name, scanned.sort_name
    artist.name_key = name_key(scanned.name)
    if scanned.mbid:
        artist.mbid = scanned.mbid
    artist.in_library = True
    db.flush()

    added = 0
    for album in scanned.albums:
        row = _find_album(db, source, album, album_of, claimed)
        if row is None:
            row = LibraryAlbum(
                source=source, path=album.path, title=album.title, artist_id=artist.id
            )
            db.add(row)
        _apply_album(row, album, artist.id)
        db.flush()
        claimed.add(row.id)
        added += _persist_tracks(db, source, row, album)
    return len(scanned.albums), added


def _find_album(
    db: Session, source: str, album: ScannedAlbum, album_of: dict[str, int], claimed: set[int]
) -> LibraryAlbum | None:
    """Reuse the row that already owns these tracks (so a retitled album keeps its identity)."""
    owners = Counter(album_of[t.path] for t in album.tracks if t.path in album_of)
    for album_id, _ in owners.most_common():
        if album_id not in claimed:
            row = db.get(LibraryAlbum, album_id)
            if row is not None:
                return row
    row = db.scalar(
        select(LibraryAlbum).where(
            LibraryAlbum.source == source,
            LibraryAlbum.path == album.path,
            LibraryAlbum.title == album.title,
        )
    )
    return row if row is not None and row.id not in claimed else None


def _apply_album(row: LibraryAlbum, album: ScannedAlbum, artist_id: int) -> None:
    row.artist_id = artist_id
    row.path, row.title, row.year = album.path, album.title, album.year
    row.format, row.quality_tier, row.bitrate = album.format, album.quality_tier, album.bitrate
    row.track_count, row.disc_count = len(album.tracks), album.disc_count
    row.total_size, row.is_compilation = album.total_size, album.is_compilation
    row.mbid_release, row.mbid_release_group = album.mbid_release, album.mbid_release_group
    row.plex_rating_key = album.plex_rating_key
    row.scanned_at = utcnow()


def _persist_tracks(db: Session, source: str, album: LibraryAlbum, scanned: ScannedAlbum) -> int:
    paths = [t.path for t in scanned.tracks]
    existing: dict[str, LibraryTrack] = {}
    for i in range(0, len(paths), CHUNK):
        rows = db.scalars(
            select(LibraryTrack).where(
                LibraryTrack.source == source, LibraryTrack.path.in_(paths[i : i + CHUNK])
            )
        )
        existing.update({r.path: r for r in rows})
    added = 0
    for track in scanned.tracks:
        row = existing.get(track.path)
        if row is None:
            row = LibraryTrack(source=source, path=track.path, album_id=album.id)
            db.add(row)
            added += 1
        row.album_id = album.id
        for key, value in track_fields(track).items():
            setattr(row, key, value)
    return added


def _remove_stale(db: Session, source: str, stale_paths: list[str]) -> tuple[int, int, int]:
    """Delete vanished tracks, then empty albums, then flag artists without albums."""
    for i in range(0, len(stale_paths), CHUNK):
        db.execute(
            delete(LibraryTrack).where(
                LibraryTrack.source == source, LibraryTrack.path.in_(stale_paths[i : i + CHUNK])
            )
        )
    empty_albums = db.execute(
        delete(LibraryAlbum).where(
            LibraryAlbum.source == source,
            ~select(LibraryTrack.id).where(LibraryTrack.album_id == LibraryAlbum.id).exists(),
        )
    ).rowcount
    deactivated = db.execute(
        update(Artist)
        .where(
            Artist.in_library.is_(True),
            ~select(LibraryAlbum.id).where(LibraryAlbum.artist_id == Artist.id).exists(),
        )
        .values(in_library=False)
    ).rowcount
    db.commit()
    return len(stale_paths), empty_albums, deactivated


def save_artists(db: Session, source: str, artists: list[ScannedArtist]) -> list[int]:
    """Save scanned artists with their albums and tracks (the caller commits). Returns the ids of
    the artists. Used to record one imported album without scanning the whole library."""
    claimed: set[int] = set()
    ids = []
    for scanned in artists:
        _persist_artist(db, source, scanned, {}, claimed)
        ids.append(_find_artist(db, scanned).id)
    return ids


def run_scan(
    factory: sessionmaker[Session],
    provider: LibraryProvider,
    reporter: Reporter,
    *,
    force: bool = False,
    max_removal_pct: int = 50,
) -> ScanSummary:
    """Blocking: run it in a worker thread. Raises LibraryError or JobCancelled."""
    started = time.monotonic()
    source = provider.source
    with factory() as db:
        known, album_of = load_known(db, source)

    output = provider.scan(known, reporter, force=force)

    seen = {t.path for artist in output.artists for album in artist.albums for t in album.tracks}
    stale = sorted(set(known) - seen)
    if len(known) >= MIN_TRACKS_FOR_REMOVAL_GUARD and len(stale) * 100 > max_removal_pct * len(
        known
    ):
        raise LibraryError(
            f"The scan would remove {len(stale)} of {len(known)} known tracks. "
            "Is the music folder mounted? Nothing was changed."
        )

    summary = ScanSummary(
        source=source,
        forced=force,
        files_seen=output.stats.files_seen,
        files_read=output.stats.files_read,
        files_skipped=output.stats.files_skipped,
        errors=output.stats.errors,
    )
    claimed: set[int] = set()
    total = len(output.artists)
    for i, artist in enumerate(output.artists, start=1):
        reporter.checkpoint()
        reporter.progress(60 + 40 * i / total, f"Artist {i}/{total}: {artist.name}")
        with factory() as db:
            albums, added = _persist_artist(db, source, artist, album_of, claimed)
            db.commit()
        summary.albums += albums
        summary.tracks_added += added
        if added:
            reporter.log(f"{artist.name}: {albums} album(s), {added} new track(s)")

    reporter.checkpoint()
    with factory() as db:
        removed, albums_removed, deactivated = _remove_stale(db, source, stale)
        summary.tracks_removed, summary.albums_removed = removed, albums_removed
        summary.artists_deactivated = deactivated
        summary.artists = db.scalar(
            select(func.count(Artist.id)).where(Artist.in_library.is_(True))
        )
        summary.tracks = db.scalar(
            select(func.count(LibraryTrack.id)).where(LibraryTrack.source == source)
        )
        summary.duration_s = round(time.monotonic() - started, 2)
        summary.finished_at = utcnow().isoformat(timespec="seconds")
        set_setting(db, LAST_SCAN_KEY, asdict(summary))
        db.commit()
    reporter.progress(100, "Done")
    return summary


def last_scan(db: Session) -> dict | None:
    return get_setting(db, LAST_SCAN_KEY)
