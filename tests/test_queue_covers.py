"""The nightly pass that queues the covers of artists nobody opens."""

import json

import httpx
import pytest
import respx
from sqlalchemy import select

from app.jobs import engine
from app.jobs.handlers import queue_covers
from app.jobs.scheduler import SCHEDULES, Schedule
from app.jobs.worker import JobWorker
from app.models import STATUS_SUCCEEDED, Job, ReleaseGroup, utcnow
from app.services.covers import artists_needing_covers
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.test_jobs_worker import enqueue, logs, wait_for_status

TYPES = ["album", "ep"]


def synced(db, name, **kw):
    artist = make_artist(db, name, **kw)
    artist.metadata_synced_at = utcnow()
    return artist


def ids(rows):
    return [name for _, name in rows]


@pytest.fixture
def library(db):
    out = {}
    for name in ("Todo", "Done", "Missing", "Unwanted", "Owned Live", "Not Synced", "Outside"):
        out[name] = synced(db, name, in_library=name != "Outside")
    out["Not Synced"].metadata_synced_at = None
    make_rg(db, out["Todo"], "Todo 1", 2000)
    done = make_rg(db, out["Done"], "Done 1", 2000)
    done.cover_status, done.cover_path = "ok", "x.jpg"
    gone = make_rg(db, out["Missing"], "Missing 1", 2000)
    gone.cover_status = "missing"  # the archive has none: not asked again
    make_rg(db, out["Unwanted"], "Unwanted Live", 2000, secondary=("Live",))  # a type nobody shows
    live = make_rg(db, out["Owned Live"], "Owned Live", 2000, secondary=("Live",))
    album = make_album(db, out["Owned Live"], "Owned Live", 2000, mbid_rg=live.mbid)
    album.release_group_id, album.match_status = live.id, "matched"  # owned: its cover matters
    make_rg(db, out["Not Synced"], "Ns 1", 2000)
    make_rg(db, out["Outside"], "Out 1", 2000)
    db.commit()
    return out


def test_only_artists_with_something_to_fetch_are_picked(db, library):
    assert sorted(ids(artists_needing_covers(db, TYPES, 50))) == ["Owned Live", "Todo"]


def test_a_type_nobody_selected_counts_only_when_owned(db, library):
    assert "Unwanted" not in ids(artists_needing_covers(db, TYPES, 50))
    assert "Unwanted" in ids(artists_needing_covers(db, [*TYPES, "live"], 50))
    assert sorted(ids(artists_needing_covers(db, [], 50))) == ["Owned Live"]  # nothing wanted


def test_each_artist_appears_once_and_the_limit_applies(db):
    artist = synced(db, "Many")
    for i in range(5):
        make_rg(db, artist, f"Album {i}", 2000 + i)
    for i in range(4):
        make_rg(db, synced(db, f"Other {i}"), f"Other album {i}", 2000)
    db.commit()
    rows = artists_needing_covers(db, TYPES, 50)
    assert len(rows) == 5 and len({i for i, _ in rows}) == 5
    assert len(artists_needing_covers(db, TYPES, 3)) == 3


def test_a_failed_cover_is_asked_for_again(db, library):
    group = db.scalar(select(ReleaseGroup).where(ReleaseGroup.title == "Done 1"))
    group.cover_status, group.cover_path = "unknown", None  # a cancelled or failed download
    db.commit()
    assert "Done" in ids(artists_needing_covers(db, TYPES, 50))


def test_it_runs_every_night_and_counts_as_routine():
    assert Schedule("queue_pending_covers", "50 4 * * *") in SCHEDULES
    assert "queue_pending_covers" in engine.ROUTINE_TYPES


async def run_pass(settings, factory, **update):
    s = settings.model_copy(
        update={
            "job_poll_interval": 0.02,
            "coverart_min_interval": 0.0,
            "providers_covers": "coverartarchive",
            **update,
        }
    )
    worker = JobWorker(s, factory)
    await worker.start()
    job_id = enqueue(factory, "queue_pending_covers")
    worker.notify()
    return worker, job_id


async def test_the_job_queues_a_cover_job_per_artist(settings, session_factory):
    with session_factory() as db:
        a = synced(db, "Aretha Franklin")
        make_rg(db, a, "Lady Soul", 1968)
        b = synced(db, "Nirvana")
        make_rg(db, b, "Nevermind", 1991)
        done = synced(db, "Complete")
        g = make_rg(db, done, "All Done", 2000)
        g.cover_status = "ok"
        db.commit()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://coverartarchive\.org/.*").mock(
            return_value=httpx.Response(404, text="none")
        )
        worker, job_id = await run_pass(settings, session_factory)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        for _ in range(200):  # let the queued cover jobs run
            with session_factory() as db:
                jobs = db.scalars(select(Job).where(Job.type == "fetch_covers")).all()
                if len(jobs) == 2 and all(j.status == STATUS_SUCCEEDED for j in jobs):
                    break
            import asyncio

            await asyncio.sleep(0.05)
        await worker.stop()
    with session_factory() as db:
        jobs = db.scalars(select(Job).where(Job.type == "fetch_covers").order_by(Job.id)).all()
        assert [j.label for j in jobs] == ["Aretha Franklin", "Nirvana"]  # not the complete one
        assert all(
            j.parent_job_id == job_id
            and j.dedupe_key == f"fetch_covers:{json.loads(j.payload_json)['artist_id']}"
            for j in jobs
        )
        states = {g.title: g.cover_status for g in db.scalars(select(ReleaseGroup))}
    assert states == {"Lady Soul": "missing", "Nevermind": "missing", "All Done": "ok"}
    messages = logs(session_factory, job_id)
    assert any(
        m.startswith("Queued the covers of 2 artist(s): Aretha Franklin, Nirvana") for m in messages
    )


async def test_an_artist_whose_covers_are_already_queued_is_not_queued_twice(
    settings, session_factory
):
    with session_factory() as db:
        a = synced(db, "Busy")
        make_rg(db, a, "Busy 1", 2000)
        db.commit()
        artist_id = a.id
        engine.enqueue(
            db, "fetch_covers", {"artist_id": artist_id}, dedupe_key=f"fetch_covers:{artist_id}"
        )
    worker, job_id = await run_pass(
        settings.model_copy(update={"job_queue_concurrency": "default:2"}), session_factory
    )
    await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    with session_factory() as db:
        assert len(db.scalars(select(Job).where(Job.type == "fetch_covers")).all()) == 1


async def test_with_cover_art_off_nothing_is_queued(settings, session_factory):
    with session_factory() as db:
        make_rg(db, synced(db, "Anyone"), "Album", 2000)
        db.commit()
    worker, job_id = await run_pass(settings, session_factory, coverart_enabled=False)
    await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    with session_factory() as db:
        assert db.scalars(select(Job).where(Job.type == "fetch_covers")).all() == []
    assert "Cover art is turned off: nothing to queue" in logs(session_factory, job_id)


async def test_when_everything_is_done_it_says_so(settings, session_factory):
    worker, job_id = await run_pass(settings, session_factory)
    await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    assert "Every artist has all the covers it can have" in logs(session_factory, job_id)


async def test_a_big_library_continues_the_next_night(settings, session_factory, monkeypatch):
    monkeypatch.setattr(queue_covers, "BATCH", 2)
    with session_factory() as db:
        for i in range(3):
            make_rg(db, synced(db, f"Artist {i}"), f"Album {i}", 2000)
        db.commit()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://coverartarchive\.org/.*").mock(
            return_value=httpx.Response(404)
        )
        worker, job_id = await run_pass(settings, session_factory)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    with session_factory() as db:
        assert len(db.scalars(select(Job).where(Job.type == "fetch_covers")).all()) == 2
    assert "There may be more: the next night continues" in logs(session_factory, job_id)
