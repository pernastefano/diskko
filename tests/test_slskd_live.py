"""Tests against a REAL SLSKD instance. They are skipped unless these are set:

    SLSKD_LIVE_URL=http://host:port  SLSKD_LIVE_KEY=<api key>        (search only)
    SLSKD_LIVE_DOWNLOAD=1 [SLSKD_LIVE_ARTIST=... SLSKD_LIVE_ALBUM=... SLSKD_LIVE_TRACKS=10]

The first test searches the Soulseek network and checks the engine on the real answers; the second
runs the whole path (request -> search -> candidate -> download -> files in the downloads folder).
Never run in CI: nothing here is mocked. Run it by hand: `pytest tests/test_slskd_live.py -s`.
"""

import asyncio
import os

import pytest
from sqlalchemy import select

from app.integrations.slskd_client import SlskdClient
from app.jobs import engine
from app.jobs.worker import JobWorker
from app.models import DownloadFile, DownloadRequest, Job, JobLog, User
from app.quality import DEFAULT_PROFILES
from app.services import downloads as svc
from app.services.candidate_selection import Wanted, query_variants, select_candidates
from tests.dbhelpers import make_artist, make_rg

URL = os.environ.get("SLSKD_LIVE_URL", "")
KEY = os.environ.get("SLSKD_LIVE_KEY", "")
ARTIST = os.environ.get("SLSKD_LIVE_ARTIST", "Nine Inch Nails")
ALBUM = os.environ.get("SLSKD_LIVE_ALBUM", "The Slip")
TRACKS = int(os.environ.get("SLSKD_LIVE_TRACKS", "10"))
QUALITY = os.environ.get("SLSKD_LIVE_QUALITY", "mp3_320")

live = pytest.mark.skipif(not (URL and KEY), reason="needs SLSKD_LIVE_URL and SLSKD_LIVE_KEY")
download = pytest.mark.skipif(
    os.environ.get("SLSKD_LIVE_DOWNLOAD") != "1", reason="needs SLSKD_LIVE_DOWNLOAD=1"
)


@pytest.fixture
def lsettings(settings, tmp_path):
    return settings.model_copy(
        update={
            "slskd_url": URL,
            "slskd_api_key": KEY,
            "slskd_search_timeout": 20,
            "slskd_poll_seconds": 3.0,
            "slskd_stall_minutes": 5,
            "slskd_queue_minutes": 15,
            "slskd_max_hours": 0.75,
            "job_poll_interval": 0.2,
            "job_queue_concurrency": "slskd:2",
        }
    )


@live
async def test_a_real_search_gives_usable_candidates(lsettings):
    wanted = Wanted(ARTIST, ALBUM, None, TRACKS, QUALITY)
    async with SlskdClient(lsettings) as client:
        state = await client.server_state()
        print("\nSLSKD", state)
        assert state["connected"] and state["logged_in"]
        found = []
        for query in query_variants(ARTIST, ALBUM):
            sid = await client.create_search(query, timeout_s=20)
            try:
                info = await client.wait_for_search(sid, hard_timeout=90)
                responses = await client.get_responses(sid)
            finally:
                await client.delete_search(sid)
            print(
                f"search {query!r}: {len(responses)} users, {info.file_count} files, {info.state}"
            )
            found = select_candidates(responses, wanted, DEFAULT_PROFILES, limit=5)
            if found:
                break
        for i, c in enumerate(found):
            print(
                f"  #{i + 1} {c.username:<20} score {c.score:.2f} {c.quality:<14} "
                f"{c.track_count:>3} tracks  free={c.has_free_slot} queue={c.queue_length}  "
                f"{c.directory.split(chr(92))[-1][:50]}"
            )
        assert found, "no candidate: try other SLSKD_LIVE_ARTIST/ALBUM"
        assert found[0].score >= 0.55 and all(c.files for c in found)


@live
@download
async def test_a_real_album_is_downloaded_through_the_whole_pipeline(lsettings, session_factory):
    with session_factory() as db:
        user = User(username="live", password_hash="x", role="user")
        db.add(user)
        artist = make_artist(db, ARTIST)
        group = make_rg(db, artist, ALBUM, None)
        group.track_count_hint = TRACKS
        db.flush()
        request, _ = svc.create_request(db, user.id, group.id, quality_key=QUALITY)
        engine.enqueue(
            db,
            "slskd_search",
            {"request_id": request.id},
            dedupe_key=svc.search_dedupe_key(request.id),
        )
        db.commit()
        request_id = request.id

    worker = JobWorker(lsettings, session_factory)
    await worker.start()
    worker.notify()
    last = ""
    try:
        for _ in range(45 * 60 // 5):  # up to 45 minutes
            with session_factory() as db:
                req = db.get(DownloadRequest, request_id)
                line = f"{req.status} {req.progress:.0f}% candidate={req.candidate_index}"
                if line != last:
                    print(line, flush=True)
                    last = line
                if req.status in ("downloaded", "failed", "no_results", "cancelled"):
                    break
            await asyncio.sleep(5)
    finally:
        await worker.stop()

    with session_factory() as db:
        req = db.get(DownloadRequest, request_id)
        files = db.scalars(select(DownloadFile).where(DownloadFile.request_id == request_id)).all()
        print("final:", req.status, req.error, "| files:", len(files))
        for job in db.scalars(
            select(Job).where(Job.type.in_(("slskd_search", "download_monitor")))
        ):
            for (m,) in db.execute(select(JobLog.message).where(JobLog.job_id == job.id)):
                print(f"  [{job.type}] {m}")
        print("saved by SLSKD under its downloads folder:", req.dest_dir)
        assert req.status == "downloaded", req.error
        assert files and all(f.state == "Completed, Succeeded" for f in files)

    # leave SLSKD tidy: forget the finished transfers (the files stay where they were saved)
    async with SlskdClient(lsettings) as client:
        for t in await client.list_downloads():
            if any(f.transfer_id == t.id for f in files):
                await client.cancel_transfer(t.username, t.id, remove=True)
