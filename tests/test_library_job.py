import pytest
from sqlalchemy import func, select

from app.jobs import engine
from app.jobs.errors import JobCancelled
from app.jobs.scheduler import JobScheduler, Schedule, schedules_for
from app.jobs.worker import JobWorker
from app.models import STATUS_FAILED, STATUS_SUCCEEDED, Job, LibraryTrack
from app.services.app_settings import get_setting
from app.services.library import build_provider
from app.services.library.filesystem import FilesystemProvider
from app.services.library.provider import LibraryError, ScanReporter
from app.services.library.scan import LAST_SCAN_KEY
from tests.audio import make_track
from tests.test_jobs_worker import enqueue, fetch, logs, wait_for_status


@pytest.fixture
def lib_settings(settings, tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    return settings.model_copy(update={"music_dir": root, "job_poll_interval": 0.02})


def fill(root):
    for artist in ("Aretha", "Prince"):
        for n in (1, 2):
            make_track(
                root / artist / "Album" / f"{n:02}.flac",
                artist=artist,
                albumartist=artist,
                album="Album",
                track=str(n),
            )


async def run_job(settings, factory, payload=None):
    worker = JobWorker(settings, factory)
    await worker.start()
    job_id = enqueue(factory, "scan_library", payload or {})
    worker.notify()
    return worker, job_id


async def test_scan_job_runs_to_completion_with_progress_and_logs(lib_settings, session_factory):
    fill(lib_settings.music_dir)
    worker, job_id = await run_job(lib_settings, session_factory)
    job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    assert job.progress_pct == 100.0 and job.queue == "library"
    messages = logs(session_factory, job_id)
    assert "Scanning the filesystem library" in messages
    assert any(m.startswith("Aretha: 1 album(s), 2 new track(s)") for m in messages)
    assert any(m.startswith("Scan finished") and "4 track(s) total" in m for m in messages)
    with session_factory() as db:
        assert db.scalar(select(func.count(LibraryTrack.id))) == 4
        assert get_setting(db, LAST_SCAN_KEY)["tracks"] == 4


async def test_second_scan_job_is_incremental(lib_settings, session_factory):
    fill(lib_settings.music_dir)
    worker, first = await run_job(lib_settings, session_factory)
    await wait_for_status(session_factory, first, STATUS_SUCCEEDED)
    second = enqueue(session_factory, "scan_library", {})
    worker.notify()
    await wait_for_status(session_factory, second, STATUS_SUCCEEDED)
    await worker.stop()
    assert any("4 unchanged" in m for m in logs(session_factory, second))
    with session_factory() as db:
        assert get_setting(db, LAST_SCAN_KEY)["files_read"] == 0


async def test_force_payload_rereads_everything(lib_settings, session_factory):
    fill(lib_settings.music_dir)
    worker, first = await run_job(lib_settings, session_factory)
    await wait_for_status(session_factory, first, STATUS_SUCCEEDED)
    forced = enqueue(session_factory, "scan_library", {"force": True})
    worker.notify()
    await wait_for_status(session_factory, forced, STATUS_SUCCEEDED)
    await worker.stop()
    assert "Scanning the filesystem library (full rescan)" in logs(session_factory, forced)
    with session_factory() as db:
        assert get_setting(db, LAST_SCAN_KEY)["files_read"] == 4


async def test_missing_library_folder_fails_without_retries(
    lib_settings, session_factory, tmp_path
):
    settings = lib_settings.model_copy(update={"music_dir": tmp_path / "not-mounted"})
    worker, job_id = await run_job(settings, session_factory)
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    await worker.stop()
    assert job.attempts == 1 and "not found" in job.error


async def test_unsafe_scan_fails_and_keeps_the_database(lib_settings, session_factory):
    import shutil

    for i in range(25):
        make_track(
            lib_settings.music_dir / f"A/Album/{i:02}.mp3",
            artist="A",
            albumartist="A",
            album="Album",
        )
    worker, first = await run_job(lib_settings, session_factory)
    await wait_for_status(session_factory, first, STATUS_SUCCEEDED)
    shutil.rmtree(lib_settings.music_dir / "A")
    second = enqueue(session_factory, "scan_library", {})
    worker.notify()
    job = await wait_for_status(session_factory, second, STATUS_FAILED)
    await worker.stop()
    assert "would remove 25 of 25" in job.error and job.attempts == 1
    with session_factory() as db:
        assert db.scalar(select(func.count(LibraryTrack.id))) == 25


async def test_unknown_library_source_fails(lib_settings, session_factory):
    settings = lib_settings.model_copy(update={"library_source": "spotify"})
    worker, job_id = await run_job(settings, session_factory)
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    await worker.stop()
    assert "Unknown library source" in job.error


def test_scans_are_deduplicated_and_use_their_own_queue(session_factory):
    first = enqueue(session_factory, "scan_library", {}, dedupe_key="scan_library")
    second = enqueue(session_factory, "scan_library", {}, dedupe_key="scan_library")
    assert first == second
    assert fetch(session_factory, first).queue == "library"


def test_library_queue_runs_one_scan_at_a_time(settings):
    assert settings.queue_concurrency["library"] == 1


def test_build_provider_selects_filesystem(lib_settings):
    provider = build_provider(lib_settings)
    assert isinstance(provider, FilesystemProvider) and provider.root == lib_settings.music_dir
    with pytest.raises(LibraryError):
        build_provider(lib_settings.model_copy(update={"library_source": "nope"}))


def test_scan_reporter_honours_cancellation_requests(session_factory):
    job_id = enqueue(session_factory, "scan_library", {})
    with session_factory() as db:
        engine.claim_next(db, "library")
    reporter = ScanReporter(session_factory, job_id, cancel_interval=0)
    reporter.checkpoint()  # nothing requested yet
    with session_factory() as db:
        engine.request_cancel(db, job_id)
    with pytest.raises(JobCancelled):
        reporter.checkpoint()


def test_scan_reporter_writes_progress_and_logs(session_factory):
    job_id = enqueue(session_factory, "scan_library", {})
    reporter = ScanReporter(session_factory, job_id)
    reporter.progress(42, "Reading tags 4/10")
    reporter.log("hello", "warning")
    job = fetch(session_factory, job_id)
    assert (job.progress_pct, job.step) == (42.0, "Reading tags 4/10")
    assert "hello" in logs(session_factory, job_id)


# ----------------------------------------------------------------------- schedule


def test_nightly_scan_is_scheduled_by_default(settings):
    schedules = schedules_for(settings)
    assert Schedule("scan_library", "0 2 * * *") in schedules
    assert Schedule("housekeeping", "30 3 * * *") in schedules


def test_nightly_scan_can_be_disabled_or_changed(settings):
    off = schedules_for(settings.model_copy(update={"library_scan_cron": " "}))
    assert all(s.job_type != "scan_library" for s in off)
    custom = schedules_for(settings.model_copy(update={"library_scan_cron": "15 4 * * 1"}))
    assert Schedule("scan_library", "15 4 * * 1") in custom


async def test_scheduler_registers_the_scan_and_survives_a_bad_cron(settings, session_factory):
    good = JobScheduler(settings, session_factory)
    good.start()
    try:
        assert "scheduled:scan_library" in good.job_ids()
    finally:
        good.shutdown()
    bad = JobScheduler(
        settings.model_copy(update={"library_scan_cron": "not a cron"}), session_factory
    )
    bad.start()  # must not raise
    try:
        assert "scheduled:scan_library" not in bad.job_ids()
        assert "scheduled:housekeeping" in bad.job_ids()
    finally:
        bad.shutdown()


def test_no_job_row_leaks_between_tests(session_factory):
    with session_factory() as db:
        assert db.scalar(select(func.count(Job.id))) == 0
