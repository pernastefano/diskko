"""After a scan, albums are matched to MusicBrainz by themselves (no "Refresh metadata")."""

from sqlalchemy import select

from app.models import MATCH_UNMATCHED, Job, LibraryAlbum, utcnow
from app.services.app_settings import get_setting
from app.services.library.filesystem import FilesystemProvider
from app.services.library.provider import RecordingReporter
from app.services.library.scan import LAST_SCAN_KEY, match_new_albums, run_scan
from tests.audio import make_track
from tests.dbhelpers import make_album, make_artist, make_rg

MBID = "a" * 8 + "-0000-4000-8000-0000000000"


def synced_artist(db, name, n, status="resolved"):
    artist = make_artist(db, name, mbid=f"{MBID}{n:02d}", status=status)
    artist.metadata_synced_at = utcnow()
    db.commit()
    return artist


def test_albums_left_unmatched_are_matched_even_if_the_artist_got_no_new_tracks(session_factory):
    """A scan that stopped halfway, or a source switch, leaves albums without a match."""
    with session_factory() as db:
        artist = synced_artist(db, "Muse", 1)
        make_rg(db, artist, "Showbiz", 1999)
        album = make_album(db, artist, "Showbiz", year=1999)
        assert album.match_status == MATCH_UNMATCHED
        assert match_new_albums(db, []) == []  # nobody changed, nothing to sync again
        db.commit()
        assert db.get(LibraryAlbum, album.id).match_status != MATCH_UNMATCHED


def test_a_new_album_the_stored_discography_lacks_asks_for_a_sync(session_factory):
    with session_factory() as db:
        known = synced_artist(db, "Muse", 1)
        make_rg(db, known, "Showbiz", 1999)
        make_album(db, known, "Showbiz", year=1999)
        make_album(db, known, "Brand New Record", year=2026)  # not in the stored discography
        fresh = make_artist(db, "Never Synced")  # first sync will match it
        make_album(db, fresh, "Whatever")
        other = synced_artist(db, "Only Old", 2)
        make_rg(db, other, "Old One", 1990)
        make_album(db, other, "Old One", year=1990)  # fully matched: no sync needed
        resync = match_new_albums(db, [known.id, fresh.id, other.id])
        assert resync == [known.id]


def test_a_scan_matches_new_albums_and_reports_who_needs_a_sync(session_factory, tmp_path):
    root = tmp_path / "music"
    with session_factory() as db:
        artist = synced_artist(db, "Aretha Franklin", 3)
        make_rg(db, artist, "Lady Soul", 1968)
        artist_id = artist.id
    make_track(
        root / "Aretha Franklin/Lady Soul/01.flac",
        artist="Aretha Franklin",
        albumartist="Aretha Franklin",
        album="Lady Soul",
    )
    make_track(
        root / "Aretha Franklin/Brand New/01.flac",
        artist="Aretha Franklin",
        albumartist="Aretha Franklin",
        album="Brand New",
    )
    summary = run_scan(session_factory, FilesystemProvider(root), RecordingReporter())
    with session_factory() as db:
        status = {a.title: a.match_status for a in db.scalars(select(LibraryAlbum))}
        assert status["Lady Soul"] != MATCH_UNMATCHED  # matched by the scan itself
        assert status["Brand New"] == MATCH_UNMATCHED
        assert LAST_SCAN_KEY and "resync_artist_ids" not in get_setting(db, LAST_SCAN_KEY)
    assert summary.resync_artist_ids == [artist_id]


async def test_the_scan_job_queues_a_sync_for_those_artists(settings, session_factory, tmp_path):
    from app.jobs.worker import JobWorker
    from tests.test_jobs_worker import enqueue, wait_for_status

    root = tmp_path / "music"
    with session_factory() as db:
        artist = synced_artist(db, "Aretha Franklin", 3)
        make_rg(db, artist, "Lady Soul", 1968)
        artist_id = artist.id
    make_track(
        root / "Aretha Franklin/Brand New/01.flac",
        artist="Aretha Franklin",
        albumartist="Aretha Franklin",
        album="Brand New",
    )
    cfg = settings.model_copy(
        update={
            "music_dir": root,
            "library_source": "filesystem",
            "mb_user_agent": "Test/1 ( t@example.com )",
            "job_poll_interval": 0.02,
        }
    )
    worker = JobWorker(cfg, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "scan_library")
    worker.notify()
    await wait_for_status(session_factory, job_id, "succeeded", timeout=30)
    await worker.stop()
    with session_factory() as db:
        syncs = db.scalars(select(Job).where(Job.type == "sync_artist_metadata")).all()
        assert [j.payload_json for j in syncs] == [f'{{"artist_id": {artist_id}}}']
        assert syncs[0].parent_job_id == job_id
