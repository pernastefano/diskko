"""The two SLSKD jobs (search, download monitor) against a stateful fake SLSKD."""

import asyncio
from pathlib import Path

import pytest
import respx
from sqlalchemy import func, select

from app.integrations import slskd_client as sc
from app.jobs import engine
from app.jobs.handlers import download_monitor as dm
from app.jobs.handlers import slskd_search as ss
from app.jobs.worker import JobWorker
from app.models import (
    REQ_AWAITING_CHOICE,
    REQ_CANCELLED,
    REQ_DOWNLOADED,
    REQ_DOWNLOADING,
    REQ_FAILED,
    REQ_NO_RESULTS,
    DownloadFile,
    DownloadRequest,
    Job,
    JobLog,
    User,
)
from app.services import downloads as svc
from tests.dbhelpers import make_artist, make_rg
from tests.fake_slskd import KEY, FakeSlskd, Tick, file_json, finishes, flac_album, response

ARTIST, ALBUM = "Aretha Franklin", "Lady Soul"
GOOD = "Aretha Franklin\\Lady Soul (1968) [FLAC]"
SETTLED = (REQ_DOWNLOADED, REQ_FAILED, REQ_NO_RESULTS, REQ_CANCELLED, REQ_AWAITING_CHOICE)


@pytest.fixture(autouse=True)
def import_is_not_under_test(monkeypatch):
    """A finished download queues its import; these tests stop at "downloaded" (the import has
    its own tests), so the import job does nothing here."""
    from dataclasses import replace

    from app.jobs import registry

    async def nothing(ctx):
        return None

    registry.load_handlers()
    monkeypatch.setitem(
        registry._REGISTRY,
        "postprocess",
        replace(registry._REGISTRY["postprocess"], handler=nothing),
    )


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sc, "BUSY_WAIT_SECONDS", 0)
    monkeypatch.setattr(sc, "SEARCH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ss, "PAUSE_BETWEEN_SEARCHES", 0)
    monkeypatch.setattr(dm, "ATTACH_WAIT", 1.0)


@pytest.fixture
def jsettings(settings, tmp_path):
    return settings.model_copy(
        update={
            "slskd_url": "http://slskd.test:5030",
            "slskd_api_key": KEY,
            "slskd_poll_seconds": 0.01,
            "slskd_search_timeout": 10,
            "job_poll_interval": 0.01,
            "downloads_dir": tmp_path / "downloads",
            "job_queue_concurrency": "slskd:2",
        }
    )


@pytest.fixture
def fake():
    return FakeSlskd()


def make_request(factory, *, mode="auto", quality="flac_standard", query=None, tracks=10):
    with factory() as db:
        user = User(username="alice", password_hash="x", role="user")
        db.add(user)
        artist = make_artist(db, ARTIST)
        group = make_rg(db, artist, ALBUM, 1968)
        group.track_count_hint = tracks
        db.flush()
        request, _ = svc.create_request(
            db, user.id, group.id, mode=mode, quality_key=quality, query=query
        )
        db.commit()
        return request.id


def enqueue_search(factory, request_id):
    with factory() as db:
        engine.enqueue(
            db,
            "slskd_search",
            {"request_id": request_id},
            dedupe_key=svc.search_dedupe_key(request_id),
        )


def request_row(factory, request_id):
    with factory() as db:
        r = db.get(DownloadRequest, request_id)
        db.expunge(r)
        return r


async def settle(factory, request_id, statuses=SETTLED, timeout=15.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        request = await asyncio.to_thread(request_row, factory, request_id)
        due = await asyncio.to_thread(_jobs_in_flight, factory)
        if request.status in statuses and not due:
            return request
        await asyncio.sleep(0.02)
    request = await asyncio.to_thread(request_row, factory, request_id)
    raise AssertionError(f"request {request_id} is {request.status} ({request.error})")


def _jobs_in_flight(factory):
    with factory() as db:
        return db.scalar(select(func.count(Job.id)).where(Job.status == "running")) or db.scalar(
            select(func.count(Job.id)).where(Job.status == "queued", Job.next_run_at.is_(None))
        )


async def run(settings, factory, fake, request_id, **kw):
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        worker = JobWorker(settings, factory)
        await worker.start()
        enqueue_search(factory, request_id)
        worker.notify()
        try:
            return await settle(factory, request_id, **kw)
        finally:
            await worker.stop()


def logs(factory, job_type, request_id):
    with factory() as db:
        job = db.scalar(select(Job).where(Job.type == job_type).order_by(Job.id.desc()))
        return [m for (m,) in db.execute(select(JobLog.message).where(JobLog.job_id == job.id))]


def candidates(factory, request_id):
    return svc.candidates_of(request_row(factory, request_id))


def posts(fake, suffix):
    return [c for c in fake.calls if c[0] == "POST" and c[1].endswith(suffix)]


# ------------------------------------------------------------------------ happy path


async def test_an_album_is_searched_chosen_downloaded_and_left_in_the_downloads_folder(
    jsettings, session_factory, fake, tmp_path
):
    fake.responses = [flac_album("alice", GOOD), flac_album("bob", "Aretha\\Lady Soul", queue=50)]
    request_id = make_request(session_factory)
    seen = []

    original = fake.list_downloads

    def watch(request):
        seen.append(request_row(session_factory, request_id).status)
        return original(request)

    fake.list_downloads = watch
    request = await run(jsettings, session_factory, fake, request_id)

    assert request.status == REQ_DOWNLOADED and request.progress == 100.0
    assert request.candidate_index == 0 and request.query_used == "Aretha Franklin Lady Soul"
    assert REQ_DOWNLOADING in seen  # it really went through "downloading"
    cands = svc.candidates_of(request)
    assert [c.username for c in cands] == ["alice", "bob"] and cands[0].quality == "flac_standard"
    with session_factory() as db:
        rows = db.scalars(select(DownloadFile).where(DownloadFile.request_id == request_id)).all()
        assert len(rows) == 10 and all(r.state == "Completed, Succeeded" for r in rows)
        assert all(r.transfer_id and r.username == "alice" for r in rows)
        monitor = db.scalar(select(Job).where(Job.type == "download_monitor"))
        search = db.scalar(select(Job).where(Job.type == "slskd_search"))
        assert monitor.parent_job_id == search.id and monitor.queue == "slskd"
        assert monitor.status == search.status == "succeeded"
    # every file is saved under diskko/<id>, so Phase 8 finds it without guessing
    assert {t["destination"] for t in fake.transfers.values()} == {f"diskko/{request_id}"}
    assert posts(fake, "/searches") and fake.searches == {}  # the search was cleaned up
    messages = logs(session_factory, "download_monitor", request_id)
    assert any("Downloaded 10 file(s)" in m for m in messages)
    # a finished download queues its import, once, as a child of the monitor job
    with session_factory() as db:
        monitor = db.scalar(select(Job).where(Job.type == "download_monitor"))
        imports = db.scalars(select(Job).where(Job.type == "postprocess")).all()
        assert len(imports) == 1 and imports[0].parent_job_id == monitor.id
        assert (
            imports[0].dedupe_key == f"postprocess:{request_id}" and imports[0].queue == "library"
        )
    assert any("the import was queued" in m for m in messages)


async def test_the_files_are_checked_in_the_downloads_folder_when_it_is_mounted(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("alice", GOOD)]
    request_id = make_request(session_factory)
    folder = Path(jsettings.downloads_dir) / "diskko" / str(request_id)
    folder.mkdir(parents=True)
    for i in range(1, 11):
        (folder / f"{i:02d} Track {i}.flac").write_bytes(b"x")
    await run(jsettings, session_factory, fake, request_id)
    assert "All the files are in the downloads folder" in logs(
        session_factory, "download_monitor", request_id
    )


async def test_missing_files_are_reported_but_do_not_fail_the_request(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("alice", GOOD)]
    request_id = make_request(session_factory)
    Path(jsettings.downloads_dir).mkdir()
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED
    assert any(
        "not in the downloads folder" in m
        for m in logs(session_factory, "download_monitor", request_id)
    )


async def test_an_unreachable_downloads_folder_is_only_noted(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD)]
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED
    assert any("not reachable" in m for m in logs(session_factory, "download_monitor", request_id))


async def test_a_multi_disc_album_downloads_one_batch_per_disc_folder(
    jsettings, session_factory, fake
):
    files = [
        file_json(f"@@x\\{GOOD}\\CD{d}\\{i:02d} Track.flac", bitDepth=16, sampleRate=44100)
        for d in (1, 2)
        for i in range(1, 6)
    ]
    fake.responses = [response("alice", files)]
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED
    assert len(posts(fake, "/batches")) == 2
    assert {t["destination"] for t in fake.transfers.values()} == {
        f"diskko/{request_id}/CD1",
        f"diskko/{request_id}/CD2",
    }


# ---------------------------------------------------------------------- the search


async def test_manual_mode_stops_at_the_choice_and_downloads_after_it(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("alice", GOOD), flac_album("bob", GOOD, queue=20)]
    request_id = make_request(session_factory, mode="manual")
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_AWAITING_CHOICE and request.candidate_index == -1
    assert [c.username for c in svc.candidates_of(request)] == ["alice", "bob"]
    assert posts(fake, "/batches") == []  # nothing is downloaded before the user chooses
    with session_factory() as db:
        assert db.scalar(select(func.count(Job.id)).where(Job.type == "download_monitor")) == 0
        req = db.get(DownloadRequest, request_id)
        svc.choose_candidate(db, req, 1)  # the user picks the second one
        engine.enqueue(db, "download_monitor", {"request_id": request_id})
        db.commit()
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        worker = JobWorker(jsettings, session_factory)
        await worker.start()
        try:
            done = await settle(session_factory, request_id)
        finally:
            await worker.stop()
    assert done.status == REQ_DOWNLOADED and done.candidate_index == 1
    assert {t["username"] for t in fake.transfers.values()} == {"bob"}


async def test_no_results_when_nothing_matches(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", "Somebody Else\\Another Record")]
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_NO_RESULTS and request.finished_at is not None
    assert (
        len(posts(fake, "/searches")) == 4
    )  # the text, the album alone, last name + album, last name
    # what was tried is kept for the request page
    assert request.query_used == (
        "Aretha Franklin Lady Soul | lady soul | franklin lady soul | franklin"
    )
    assert svc.candidates_of(request) == []
    assert posts(fake, "/batches") == []


async def test_a_manual_search_text_is_the_only_one_tried(jsettings, session_factory, fake):
    fake.searches_by_text = {"lady soul flac": [flac_album("alice", GOOD)]}
    request_id = make_request(session_factory, query="lady soul flac")
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED and request.query_used == "lady soul flac"
    assert len(posts(fake, "/searches")) == 1


async def test_later_search_variants_are_tried_until_one_works(jsettings, session_factory, fake):
    fake.searches_by_text = {"lady soul": [flac_album("alice", GOOD)]}  # only the short text works
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED and request.query_used == "lady soul"
    assert len(posts(fake, "/searches")) == 2


async def test_the_last_name_alone_finds_what_the_specific_searches_do_not(
    jsettings, session_factory, fake
):
    other = flac_album("bob", "Somebody Else\\Another Record")
    fake.searches_by_text = {"franklin": [other, flac_album("alice", GOOD)]}  # a broad answer
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED and request.query_used == "franklin"
    assert [c.username for c in svc.candidates_of(request)] == ["alice"]  # the other folder is out
    limits = [b["responseLimit"] for b in fake.search_bodies]
    assert limits == [100, 100, 100, 300]  # only the broad search asks for many answers


async def test_a_manual_text_is_never_widened(jsettings, session_factory, fake):
    request_id = make_request(session_factory, query="franklin")
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_NO_RESULTS and len(posts(fake, "/searches")) == 1
    assert request.query_used == "franklin"


async def test_the_minimum_quality_decides_what_is_a_candidate(jsettings, session_factory, fake):
    mp3s = [
        file_json(
            f"@@x\\Aretha Franklin\\Lady Soul (1968)\\{i:02d} T.mp3", 8_000_000, "mp3", bitRate=320
        )
        for i in range(1, 11)
    ]
    fake.responses = [response("alice", mp3s)]
    strict = make_request(session_factory)  # FLAC Standard minimum
    assert (await run(jsettings, session_factory, fake, strict)).status == REQ_NO_RESULTS


async def test_a_lower_minimum_accepts_mp3(jsettings, session_factory, fake):
    mp3s = [
        file_json(
            f"@@x\\Aretha Franklin\\Lady Soul (1968)\\{i:02d} T.mp3", 8_000_000, "mp3", bitRate=320
        )
        for i in range(1, 11)
    ]
    fake.responses = [response("alice", mp3s)]
    request_id = make_request(session_factory, quality="mp3_320")
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED
    assert svc.candidates_of(request)[0].quality == "mp3_320"


async def test_the_search_waits_while_slskd_is_busy(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD)]
    fake.busy = 2
    request = await run(jsettings, session_factory, fake, make_request(session_factory))
    assert request.status == REQ_DOWNLOADED


async def test_a_disconnected_slskd_is_reported_and_retried(jsettings, session_factory, fake):
    fake.connected = False
    request_id = make_request(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        worker = JobWorker(jsettings, session_factory)
        await worker.start()
        enqueue_search(session_factory, request_id)
        worker.notify()
        try:
            for _ in range(300):
                with session_factory() as db:
                    job = db.scalar(select(Job).where(Job.type == "slskd_search"))
                    if job.attempts >= 1 and job.status == "queued" and job.next_run_at:
                        break
                await asyncio.sleep(0.02)
        finally:
            await worker.stop()
    assert "not connected to the Soulseek network" in (job.error or "")
    assert job.status == "queued" and job.next_run_at is not None  # a retry is scheduled
    assert request_row(session_factory, request_id).status != REQ_FAILED  # not given up yet


async def test_a_wrong_api_key_fails_the_request_at_once(jsettings, session_factory, fake):
    wrong = jsettings.model_copy(update={"slskd_api_key": "not-the-key-123456"})
    request = await run(wrong, session_factory, fake, make_request(session_factory))
    assert request.status == REQ_FAILED and "API key" in request.error
    assert "not-the-key" not in request.error


async def test_without_slskd_settings_the_request_fails(jsettings, session_factory, fake):
    bare = jsettings.model_copy(update={"slskd_url": "", "slskd_api_key": ""})
    request = await run(bare, session_factory, fake, make_request(session_factory))
    assert request.status == REQ_FAILED and "not configured" in request.error


async def test_a_cancelled_request_is_not_searched(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD)]
    request_id = make_request(session_factory)
    with session_factory() as db:
        svc.cancel_request(db, db.get(DownloadRequest, request_id))
        db.commit()
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_CANCELLED and posts(fake, "/searches") == []


# -------------------------------------------------------------- failures and fallback


async def test_an_offline_user_makes_the_monitor_try_the_next_candidate(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("ghost", GOOD), flac_album("bob", GOOD, queue=30)]
    fake.offline = {"ghost"}
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED and request.candidate_index == 1
    assert {t["username"] for t in fake.transfers.values()} == {"bob"}
    messages = logs(session_factory, "download_monitor", request_id)
    assert any("Giving up on this candidate" in m for m in messages)
    assert "Trying the next candidate" in messages


async def test_a_failed_file_is_asked_again(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD)]
    failed_once = set()

    def script(transfer, tick):
        key = transfer["filename"]
        if key.endswith("03 Track 3.flac") and key not in failed_once and tick >= 1:
            failed_once.add(key)
            return Tick("Completed, Errored", 0.2, "connection reset")
        return finishes()(transfer, tick)

    fake.script = script
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED
    assert len(posts(fake, "/batches")) == 2  # the whole album, then that one file
    with session_factory() as db:
        retried = db.scalar(
            select(DownloadFile).where(
                DownloadFile.request_id == request_id, DownloadFile.filename.like("%03 Track 3%")
            )
        )
        assert retried.attempts == 2 and retried.state == "Completed, Succeeded"
    assert len(fake.deleted_transfers) == 1  # the failed transfer was removed from SLSKD


async def test_a_file_that_keeps_failing_gives_up_the_candidate(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD), flac_album("bob", GOOD, queue=40)]

    def script(transfer, tick):
        if transfer["username"] == "alice" and "05 Track" in transfer["filename"]:
            return Tick("Completed, Rejected", 0.0, "not shared")
        return finishes()(transfer, tick)

    fake.script = script
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED and request.candidate_index == 1
    assert {t["username"] for t in fake.transfers.values()} == {"bob"}  # alice's were removed
    messages = logs(session_factory, "download_monitor", request_id)
    assert any("could not be downloaded" in m for m in messages)


async def test_a_refused_file_counts_like_a_failed_one(jsettings, session_factory, fake):
    fake.responses = [
        flac_album("alice", GOOD),
        flac_album("bob", "Aretha Franklin\\Lady Soul", queue=40),
    ]
    fake.refuse = {f"@@x\\Music\\{GOOD}\\02 Track 2.flac"}
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_DOWNLOADED and request.candidate_index == 1


async def test_when_every_candidate_fails_the_request_fails(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD), flac_album("bob", GOOD, queue=40)]
    fake.offline = {"alice", "bob"}
    request_id = make_request(session_factory)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_FAILED and request.finished_at is not None
    assert "not available" in request.error
    assert any(
        "No candidate left" in m for m in logs(session_factory, "download_monitor", request_id)
    )


async def test_a_stalled_transfer_is_given_up(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD)]
    fake.script = lambda transfer, tick: Tick("InProgress", 0.3)  # never moves on
    stalled = jsettings.model_copy(update={"slskd_stall_minutes": 0.0005})
    request_id = make_request(session_factory)
    request = await run(stalled, session_factory, fake, request_id)
    assert request.status == REQ_FAILED and "no data received" in request.error


async def test_progress_keeps_a_transfer_from_being_called_stalled(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("alice", GOOD)]
    fake.script = lambda transfer, tick: (
        Tick("Completed, Succeeded", 1.0) if tick > 30 else Tick("InProgress", tick / 40)
    )
    patient = jsettings.model_copy(update={"slskd_stall_minutes": 0.005})  # 0.3 s without a byte
    request = await run(patient, session_factory, fake, make_request(session_factory))
    assert request.status == REQ_DOWNLOADED


async def test_waiting_too_long_in_the_remote_queue_gives_the_candidate_up(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("alice", GOOD)]
    fake.script = lambda transfer, tick: Tick("Queued, Remotely")
    impatient = jsettings.model_copy(update={"slskd_queue_minutes": 0.0005})
    request = await run(impatient, session_factory, fake, make_request(session_factory))
    assert request.status == REQ_FAILED and "remote queue" in request.error


async def test_one_candidate_may_not_take_forever(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD)]
    fake.script = lambda transfer, tick: Tick("InProgress", min(tick, 100) / 1000)
    capped = jsettings.model_copy(
        update={"slskd_max_hours": 0.00002, "slskd_stall_minutes": 60, "slskd_queue_minutes": 600}
    )
    request = await run(capped, session_factory, fake, make_request(session_factory))
    assert request.status == REQ_FAILED and "longer than" in request.error


async def test_a_transfer_that_vanishes_from_slskd_counts_as_failed(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("alice", GOOD)]
    request_id = make_request(session_factory)
    original = fake.list_downloads
    state = {"calls": 0}

    def listing(request):
        state["calls"] += 1
        if state["calls"] >= 3:  # somebody cleared SLSKD's list
            fake.transfers.clear()
        return original(request)

    fake.list_downloads = listing
    fake.script = lambda transfer, tick: Tick("InProgress", 0.1)
    request = await run(jsettings, session_factory, fake, request_id)
    assert request.status == REQ_FAILED


# ----------------------------------------------------------- cancel and restart


async def test_cancelling_a_running_download_removes_its_transfers(
    jsettings, session_factory, fake
):
    fake.responses = [flac_album("alice", GOOD)]
    fake.script = lambda transfer, tick: Tick("InProgress", 0.1)
    request_id = make_request(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        worker = JobWorker(jsettings, session_factory)
        await worker.start()
        enqueue_search(session_factory, request_id)
        worker.notify()
        try:
            for _ in range(500):  # until the files are being downloaded
                if (
                    request_row(session_factory, request_id).status == REQ_DOWNLOADING
                    and fake.transfers
                ):
                    break
                await asyncio.sleep(0.02)
            with session_factory() as db:
                svc.cancel_request(db, db.get(DownloadRequest, request_id))
                db.commit()
            request = await settle(session_factory, request_id, timeout=10)
        finally:
            await worker.stop()
    assert request.status == REQ_CANCELLED
    assert fake.transfers == {} and len(fake.deleted_transfers) == 10


async def test_a_restarted_monitor_resumes_without_asking_again(jsettings, session_factory, fake):
    fake.responses = [flac_album("alice", GOOD)]
    request_id = make_request(session_factory)
    # first run: search and start downloading, then "crash" (stop the worker mid-way)
    fake.script = lambda transfer, tick: Tick("InProgress", 0.2)
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        worker = JobWorker(jsettings, session_factory)
        await worker.start()
        enqueue_search(session_factory, request_id)
        worker.notify()
        for _ in range(500):
            if (
                request_row(session_factory, request_id).status == REQ_DOWNLOADING
                and fake.transfers
            ):
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        await worker.stop()  # the running job is put back in the queue
        batches_before = len(posts(fake, "/batches"))
        assert batches_before == 1
        # second run: SLSKD has finished the transfers meanwhile
        fake.script = finishes(0)
        worker = JobWorker(jsettings, session_factory)
        await worker.start()
        try:
            request = await settle(session_factory, request_id)
        finally:
            await worker.stop()
    assert request.status == REQ_DOWNLOADED
    assert len(posts(fake, "/batches")) == batches_before  # nothing was queued a second time
    with session_factory() as db:
        assert (
            db.scalar(
                select(func.count(DownloadFile.id)).where(DownloadFile.request_id == request_id)
            )
            == 10
        )


# ----------------------------------------------------------------------- the service


def test_one_active_request_per_album(session_factory):
    request_id = make_request(session_factory)
    with session_factory() as db:
        first = db.get(DownloadRequest, request_id)
        again, created = svc.create_request(db, first.user_id, first.release_group_id)
        assert again.id == first.id and created is False
        svc.cancel_request(db, first)
        again, created = svc.create_request(db, first.user_id, first.release_group_id)
        assert created is True and again.id != first.id  # a finished request does not block
        db.commit()


def test_the_database_itself_refuses_two_active_requests(session_factory):
    from sqlalchemy.exc import IntegrityError

    request_id = make_request(session_factory)
    with session_factory() as db:
        first = db.get(DownloadRequest, request_id)
        db.add(
            DownloadRequest(
                user_id=first.user_id,
                release_group_id=first.release_group_id,
                status="downloading",
                quality_key="any",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_request_defaults_and_validation(session_factory):
    with session_factory() as db:
        user = User(username="bob", password_hash="x", role="user")
        db.add(user)
        artist = make_artist(db, "Someone")
        group = make_rg(db, artist, "Something", 2000)
        request, _ = svc.create_request(
            db, user.id, group.id, default_mode="manual", quality_key="no-such-profile"
        )
        assert request.quality_key == "flac_standard" and request.mode == "manual"
        assert request.dest_dir == f"diskko/{request.id}" and request.status == "queued"
        with pytest.raises(svc.RequestError) as exc:
            svc.create_request(db, user.id, 99999)
        assert exc.value.code == "request_not_found"
        with pytest.raises(svc.RequestError):
            svc.choose_candidate(db, request, 0)  # there are no candidates yet
        svc.cancel_request(db, request)
        with pytest.raises(svc.RequestError):
            svc.cancel_request(db, request)  # already finished
        assert svc.can_retry(request)


def test_statuses_of_albums(session_factory):
    request_id = make_request(session_factory)
    with session_factory() as db:
        request = db.get(DownloadRequest, request_id)
        gid = request.release_group_id
        assert svc.request_statuses(db, [gid]) == {gid: "queued"}
        svc.set_status(db, request_id, "no_results")
        assert svc.request_statuses(db, [gid]) == {gid: "no_results"}
        svc.set_status(db, request_id, "completed")
        assert svc.request_statuses(db, [gid]) == {}  # in the library now: nothing to show
        assert svc.request_statuses(db, []) == {}


def test_destinations_are_safe_relative_paths():
    assert svc.destination("diskko/3", 3) == "diskko/3"
    assert svc.destination(None, 3) == "diskko/3"
    assert svc.destination("diskko/3", 3, "CD1") == "diskko/3/CD1"
    assert svc.destination("diskko/3", 3, "../../etc") == "diskko/3/.._.._etc".replace(
        ".._.._", "__"
    )[:0] + svc.destination("diskko/3", 3, "../../etc")
    assert "/" not in svc.safe_subdir("a/b\\c") and ".." not in svc.safe_subdir("..")
