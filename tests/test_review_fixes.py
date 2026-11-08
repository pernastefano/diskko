"""Fixes from the external code review: jobs that give up, retries, pruning, audit volume."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.integrations.musicbrainz_client import ReleaseGroupData
from app.jobs import engine
from app.jobs.worker import JobWorker
from app.models import AuditLog, DownloadRequest, Job, ReleaseGroup, User, utcnow
from app.services.library.provider import LibraryError, LibraryUnavailable
from app.services.musicbrainz import sync
from tests.conftest import login
from tests.dbhelpers import make_artist, make_rg
from tests.test_jobs_worker import enqueue, logs, wait_for_status


def test_a_job_that_waited_a_day_for_a_service_is_failed_not_kept_for_ever(session_factory):
    with session_factory() as db:
        young = engine.enqueue(db, "housekeeping")
        old = engine.enqueue(db, "backup_db")
        old.created_at = utcnow() - engine.DEFER_LIMIT - timedelta(minutes=1)
        young.status = old.status = "running"
        db.commit()
        engine.requeue_later(db, young.id, "MusicBrainz is paused", 60)
        engine.requeue_later(db, old.id, "MusicBrainz is paused", 60)
        db.expire_all()
        assert db.get(Job, young.id).status == "queued"  # still waiting, attempt not used
        gone = db.get(Job, old.id)
        assert gone.status == "failed" and "Gave up after 24h" in gone.error


async def run_scan_job(settings, session_factory, monkeypatch, error):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr("app.jobs.handlers.scan_library.build_provider", broken)
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "scan_library")
    worker.notify()
    return worker, job_id


async def test_a_scan_with_plex_down_is_retried_later(settings, session_factory, monkeypatch):
    error = LibraryUnavailable("Cannot connect to Plex (ConnectionError)")
    worker, job_id = await run_scan_job(settings, session_factory, monkeypatch, error)
    for _ in range(500):  # the job starts "queued" too: wait for the failed attempt
        if any("Attempt failed" in m for m in logs(session_factory, job_id)):
            break
        await asyncio.sleep(0.02)
    row = await wait_for_status(session_factory, job_id, "queued", timeout=15)
    await worker.stop()
    assert row.next_run_at is not None and row.attempts == 1  # backoff, one attempt left


async def test_a_scan_with_a_wrong_setup_still_fails_at_once(
    settings, session_factory, monkeypatch
):
    worker, job_id = await run_scan_job(
        settings, session_factory, monkeypatch, LibraryError("Plex section 'Music' not found")
    )
    row = await wait_for_status(session_factory, job_id, "failed", timeout=15)
    await worker.stop()
    assert row.attempts == 1  # retrying will not help


async def test_a_cover_job_for_a_deleted_artist_fails_for_good(settings, session_factory):
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "fetch_covers", {"artist_id": 4242})
    worker.notify()
    row = await wait_for_status(session_factory, job_id, "failed", timeout=15)
    await worker.stop()
    assert "artist 4242 not found" in row.error and row.attempts == 1


def test_a_sync_never_deletes_a_release_group_that_has_a_download_request(jdb):
    artist = make_artist(jdb, "Band")
    wanted = make_rg(jdb, artist, "Wanted", 2000)
    gone = make_rg(jdb, artist, "Vanished", 2001)
    user = User(username="u", password_hash="x", role="user")
    jdb.add(user)
    jdb.flush()
    jdb.add(
        DownloadRequest(
            user_id=user.id,
            release_group_id=wanted.id,
            status="downloading",
            quality_key="flac_standard",
        )
    )
    jdb.commit()
    still_listed = ReleaseGroupData(
        "x" * 8 + "-0000-4000-8000-000000000001", "Other", "Album", [], "2002"
    )
    sync.store_release_groups(jdb, artist.id, [still_listed])  # MusicBrainz lists neither
    jdb.commit()
    assert jdb.get(ReleaseGroup, wanted.id) is not None  # the request keeps its album
    assert jdb.get(ReleaseGroup, gone.id) is None
    assert jdb.query(DownloadRequest).count() == 1


def test_hammering_a_paused_account_writes_one_audit_entry_not_one_per_try(
    admin_client, new_client, db
):
    browser = new_client()
    for _ in range(5):
        login(browser, "admin", "wrong-password")
    for _ in range(6):  # six more tries while it is paused
        assert login(browser, "admin", "wrong-password").status_code == 429
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "login.throttled")).all()
    assert len(rows) == 1
