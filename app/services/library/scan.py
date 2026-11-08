"""Run a library scan: ask the provider, then persist the result per artist."""

import time
from collections import Counter
from dataclasses import asdict, dataclass, field

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    MATCH_UNMATCHED,
    MB_RESOLVED,
    AlbumArtistOverride,
    Artist,
    LibraryAlbum,
    LibraryTrack,
    utcnow,
)
from app.services.app_settings import get_setting, set_setting
from app.services.library.grouping import name_key, sort_name_for
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
from app.services.musicbrainz.matching import match_artist_albums

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
    other_source_albums_removed: int = 0  # rows of the source that is no longer the active one
    duration_s: float = 0.0
    finished_at: str = ""
    # Artists with new tracks whose albums MusicBrainz data we have does not know yet (a very new
    # release): their discography is synced again. Not saved with the last scan.
    resync_artist_ids: list[int] = field(default_factory=list)


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
    if scanned.artist_id:
        chosen = db.get(Artist, scanned.artist_id)
        if chosen is not None:
            return chosen
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
            twin = db.scalar(
                select(LibraryAlbum).where(
                    LibraryAlbum.source == source,
                    LibraryAlbum.path == album.path,
                    LibraryAlbum.title == album.title,
                )
            )
            if twin is not None:
                # Another album of this very scan has the same folder and title (the key of an
                # album): they are one album. Its tracks join it instead of breaking the key.
                added += _persist_tracks(db, source, twin, album)
                db.flush()
                _refresh_totals(db, twin)
                continue
            row = LibraryAlbum(
                source=source, path=album.path, title=album.title, artist_id=artist.id
            )
            db.add(row)
        _apply_album(row, album, artist.id)
        db.flush()
        claimed.add(row.id)
        added += _persist_tracks(db, source, row, album)
    return len(scanned.albums), added


def _refresh_totals(db: Session, album: LibraryAlbum) -> None:
    """The counts of an album that took tracks from another album of the same scan."""
    count = func.count(LibraryTrack.id)
    size = func.coalesce(func.sum(LibraryTrack.file_size), 0)
    album.track_count, album.total_size = db.execute(
        select(count, size).where(LibraryTrack.album_id == album.id)
    ).one()


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
    if row.artist_id is not None and row.artist_id != artist_id:
        # moved to another artist: what it was matched to was that artist's release group
        row.release_group_id, row.matched_confidence = None, None
        row.match_status = MATCH_UNMATCHED
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
    deactivated = _deactivate_empty_artists(db)
    db.commit()
    return len(stale_paths), empty_albums, deactivated


def _deactivate_empty_artists(db: Session) -> int:
    return db.execute(
        update(Artist)
        .where(
            Artist.in_library.is_(True),
            ~select(LibraryAlbum.id).where(LibraryAlbum.artist_id == Artist.id).exists(),
        )
        .values(in_library=False)
    ).rowcount


def _purge_other_sources(
    db: Session, source: str, max_removal_pct: int, reporter: Reporter
) -> tuple[int, int]:
    """The source that was scanned is the one that counts: what the other sources left behind
    (an earlier folder scan after switching to Plex, or the reverse) goes, so that every album
    is listed once. Artists, their MusicBrainz data and manual decisions stay.

    Not when the scanned source is much smaller than what would go (an unmounted folder looks
    like that): the rows stay and the log says why. Returns (albums removed, tracks removed)."""
    other_tracks = db.scalar(
        select(func.count(LibraryTrack.id)).where(LibraryTrack.source != source)
    )
    if not other_tracks:
        return 0, 0
    own_tracks = db.scalar(select(func.count(LibraryTrack.id)).where(LibraryTrack.source == source))
    if own_tracks * 100 < other_tracks * (100 - max_removal_pct):
        reporter.log(
            f"The {source} library has {own_tracks} track(s) and an earlier source {other_tracks}: "
            "the earlier rows were kept (is the library mounted and complete?)",
            "warning",
        )
        return 0, 0
    tracks = db.execute(delete(LibraryTrack).where(LibraryTrack.source != source)).rowcount
    albums = db.execute(delete(LibraryAlbum).where(LibraryAlbum.source != source)).rowcount
    # Commit before logging: the reporter writes the job log through its own connection, which
    # would wait for the write lock held here ("database is locked") and undo the whole purge.
    db.commit()
    reporter.log(f"Removed {albums} album(s) and {tracks} track(s) left by another library source")
    return albums, tracks


def _unmatched_artists(db: Session) -> set[int]:
    """Artists we know from MusicBrainz that have a library album with no match."""
    return set(
        db.scalars(
            select(LibraryAlbum.artist_id)
            .join(Artist, Artist.id == LibraryAlbum.artist_id)
            .where(
                LibraryAlbum.match_status == MATCH_UNMATCHED, Artist.metadata_synced_at.is_not(None)
            )
            .distinct()
        )
    )


def match_new_albums(db: Session, changed: list[int]) -> list[int]:
    """Match albums to MusicBrainz right after a scan (it is local work, no request): the albums
    of the artists that got new tracks, and any album of a known artist still without a match (a
    scan that stopped halfway, a source switch). Returns the artists that got new tracks and
    still have an unmatched album: the release is probably newer than their stored discography,
    so the caller syncs them again. Artists never synced wait for their first sync."""
    todo = set(changed) | _unmatched_artists(db)
    resync: list[int] = []
    for artist_id in sorted(todo):
        artist = db.get(Artist, artist_id)
        if artist is None or artist.metadata_synced_at is None:
            continue
        match_artist_albums(db, artist_id)
        if artist_id in changed and artist.mb_status == MB_RESOLVED:
            left = db.scalar(
                select(func.count(LibraryAlbum.id)).where(
                    LibraryAlbum.artist_id == artist_id,
                    LibraryAlbum.match_status == MATCH_UNMATCHED,
                )
            )
            if left:
                resync.append(artist_id)
    return resync


def apply_artist_overrides(db: Session, artists: list[ScannedArtist]) -> list[ScannedArtist]:
    """Put the albums a person gave to another artist under that artist, whatever the tags or
    Plex say about it. Albums are told by their folder and title."""
    rows = db.scalars(select(AlbumArtistOverride)).all()
    if not rows:
        return artists
    wanted = {(r.path, r.title): r.artist_id for r in rows}
    targets = {
        a.id: a for a in db.scalars(select(Artist).where(Artist.id.in_(set(wanted.values()))))
    }
    kept: list[ScannedArtist] = []
    moved: dict[int, ScannedArtist] = {}
    for artist in artists:
        stay = []
        for album in artist.albums:
            target = targets.get(wanted.get((album.path, album.title)))
            if target is None:
                stay.append(album)
                continue
            slot = moved.setdefault(
                target.id,
                ScannedArtist(
                    name=target.name,
                    sort_name=target.sort_name or sort_name_for(target.name),
                    mbid=target.mbid,
                    artist_id=target.id,
                ),
            )
            slot.albums.append(album)
        if stay:
            artist.albums = stay
            kept.append(artist)
    return kept + list(moved.values())


def save_artists(db: Session, source: str, artists: list[ScannedArtist]) -> list[int]:
    """Save scanned artists with their albums and tracks (the caller commits). Returns the ids of
    the artists. Used to record one imported album without scanning the whole library."""
    claimed: set[int] = set()
    ids = []
    for scanned in apply_artist_overrides(db, artists):
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
    with factory() as db:
        output.artists = apply_artist_overrides(db, output.artists)
    claimed: set[int] = set()
    changed: list[int] = []  # artists that got new tracks: their albums are matched again
    total = len(output.artists)
    for i, artist in enumerate(output.artists, start=1):
        reporter.checkpoint()
        reporter.progress(60 + 40 * i / total, f"Artist {i}/{total}: {artist.name}")
        with factory() as db:
            albums, added = _persist_artist(db, source, artist, album_of, claimed)
            if added:
                changed.append(_find_artist(db, artist).id)
            db.commit()
        summary.albums += albums
        summary.tracks_added += added
        if added:
            reporter.log(f"{artist.name}: {albums} album(s), {added} new track(s)")

    reporter.checkpoint()
    with factory() as db:
        removed, albums_removed, deactivated = _remove_stale(db, source, stale)
        summary.tracks_removed, summary.albums_removed = removed, albums_removed
        summary.other_source_albums_removed, _ = _purge_other_sources(
            db, source, max_removal_pct, reporter
        )
        summary.artists_deactivated = deactivated + _deactivate_empty_artists(db)
        summary.resync_artist_ids = match_new_albums(db, changed)
        summary.artists = db.scalar(
            select(func.count(Artist.id)).where(Artist.in_library.is_(True))
        )
        summary.tracks = db.scalar(
            select(func.count(LibraryTrack.id)).where(LibraryTrack.source == source)
        )
        summary.duration_s = round(time.monotonic() - started, 2)
        summary.finished_at = utcnow().isoformat(timespec="seconds")
        saved = asdict(summary)
        saved.pop("resync_artist_ids", None)
        set_setting(db, LAST_SCAN_KEY, saved)
        db.commit()
    reporter.progress(100, "Done")
    return summary


def last_scan(db: Session) -> dict | None:
    return get_setting(db, LAST_SCAN_KEY)
