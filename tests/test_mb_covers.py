import httpx
import pytest
import respx
from sqlalchemy import select

from app.jobs.handlers.fetch_covers import write_atomic
from app.jobs.worker import JobWorker
from app.models import ReleaseGroup
from app.services.musicbrainz.matching import match_artist_albums
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.test_jobs_worker import enqueue, logs, wait_for_status

CAA = "https://coverartarchive.test"
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64
PNG = b"\x89PNG" + b"\0" * 32


ROUTES: list = []


def front(mock, mbid, response=None):
    route = mock.get(f"{CAA}/release-group/{mbid}/front-250")
    if response is not None:
        route.mock(return_value=response)
    ROUTES.append(route)
    return route


def total_calls():
    return sum(route.call_count for route in ROUTES)


async def run_covers(settings, factory, artist_id, **payload):
    worker = JobWorker(settings, factory)
    await worker.start()
    job_id = enqueue(factory, "fetch_covers", {"artist_id": artist_id, **payload})
    worker.notify()
    return worker, job_id


def cover_settings(mbsettings):
    return mbsettings.model_copy(update={"coverart_url": CAA, "coverart_min_interval": 0.0})


def covers_of(factory, artist_id):
    with factory() as db:
        return {
            g.title: (g.cover_status, g.cover_path)
            for g in db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == artist_id))
        }


@pytest.fixture(autouse=True)
def _reset_routes():
    ROUTES.clear()


def seed_discography(factory):
    with factory() as db:
        artist = make_artist(
            db, "Band", mbid="a" * 8 + "-0000-4000-8000-000000000001", status="resolved"
        )
        rgs = {
            "wanted": make_rg(db, artist, "Studio Album", 1990),
            "ep": make_rg(db, artist, "Studio EP", 1991, "EP"),
            "single": make_rg(db, artist, "A Single", 1992, "Single"),
            "owned_comp": make_rg(db, artist, "Best Of", 1999, "Album", ("Compilation",)),
            "other_comp": make_rg(db, artist, "More Hits", 2001, "Album", ("Compilation",)),
        }
        make_album(db, artist, "Best Of", year=1999, compilation=True)
        match_artist_albums(db, artist.id)
        db.commit()
        return artist.id, {k: v.mbid for k, v in rgs.items()}


async def test_covers_are_saved_for_wanted_and_owned_release_groups(mbsettings, session_factory):
    settings = cover_settings(mbsettings)
    artist_id, mbids = seed_discography(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        for key in ("wanted", "ep", "owned_comp"):
            front(
                mock,
                mbids[key],
                httpx.Response(
                    200,
                    content=JPEG if key != "ep" else PNG,
                    headers={"content-type": "image/jpeg" if key != "ep" else "image/png"},
                ),
            )
        skipped = [
            front(mock, mbids[k]).respond(200, content=JPEG, headers={"content-type": "image/jpeg"})
            for k in ("single", "other_comp")
        ]
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    covers = covers_of(session_factory, artist_id)
    assert covers["Studio Album"] == ("ok", f"{mbids['wanted']}.jpg")
    assert covers["Studio EP"] == ("ok", f"{mbids['ep']}.png")
    assert covers["Best Of"][0] == "ok"  # owned, even though compilations are not a wanted type
    assert covers["A Single"] == ("unknown", None) and covers["More Hits"] == ("unknown", None)
    assert all(route.call_count == 0 for route in skipped)  # no requests for unwanted types
    directory = settings.config_dir / "covers"
    assert (directory / f"{mbids['wanted']}.jpg").read_bytes() == JPEG
    assert (directory / f"{mbids['ep']}.png").read_bytes() == PNG
    assert (
        sorted(p.name for p in directory.iterdir() if p.name.startswith(".")) == []
    )  # no temp files


async def test_release_groups_without_a_cover_are_remembered(mbsettings, session_factory):
    settings = cover_settings(mbsettings)
    artist_id, mbids = seed_discography(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        for key in ("wanted", "ep", "owned_comp"):
            front(mock, mbids[key], httpx.Response(404, text="Not found"))
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        calls = total_calls()
        again = enqueue(session_factory, "fetch_covers", {"artist_id": artist_id})
        worker.notify()
        await wait_for_status(session_factory, again, "succeeded")
        assert total_calls() == calls  # not asked again
        forced = enqueue(session_factory, "fetch_covers", {"artist_id": artist_id, "force": True})
        worker.notify()
        await wait_for_status(session_factory, forced, "succeeded")
        await worker.stop()
    assert total_calls() == calls * 2  # force retries the missing ones
    assert covers_of(session_factory, artist_id)["Studio Album"] == ("missing", None)


async def test_covers_already_saved_are_not_downloaded_again(mbsettings, session_factory):
    settings = cover_settings(mbsettings)
    artist_id, mbids = seed_discography(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        for key in ("wanted", "ep", "owned_comp"):
            front(
                mock,
                mbids[key],
                httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}),
            )
        worker, first = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, first, "succeeded")
        calls = total_calls()
        second = enqueue(session_factory, "fetch_covers", {"artist_id": artist_id})
        worker.notify()
        await wait_for_status(session_factory, second, "succeeded")
        await worker.stop()
    assert total_calls() == calls
    assert "No covers to fetch" in logs(session_factory, second)


async def test_a_failing_cover_does_not_stop_the_others(mbsettings, session_factory):
    settings = cover_settings(mbsettings)
    artist_id, mbids = seed_discography(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        front(mock, mbids["wanted"], httpx.Response(503))
        front(
            mock,
            mbids["ep"],
            httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}),
        )
        front(
            mock,
            mbids["owned_comp"],
            httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}),
        )
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    covers = covers_of(session_factory, artist_id)
    assert covers["Studio Album"] == ("unknown", None)  # will be retried by a later job
    assert covers["Studio EP"][0] == "ok" and covers["Best Of"][0] == "ok"
    assert any("HTTP 503" in m for m in logs(session_factory, job_id))


async def test_repeated_failures_make_the_job_retry_later(mbsettings, session_factory):
    settings = cover_settings(mbsettings)
    artist_id, _ = seed_discography(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{CAA}/.*").respond(503)
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "queued")  # re-queued with backoff
        await worker.stop()
    assert all(v == ("unknown", None) for v in covers_of(session_factory, artist_id).values())


def test_write_atomic_replaces_and_cleans_up(tmp_path):
    directory = tmp_path / "covers"
    write_atomic(directory, "x.jpg", b"one")
    write_atomic(directory, "x.jpg", b"two")
    assert (directory / "x.jpg").read_bytes() == b"two"
    assert [p.name for p in directory.iterdir()] == ["x.jpg"]


async def test_the_job_also_fetches_the_types_a_user_asked_for(mbsettings, session_factory):
    """A user who selected singles gets those covers too, on top of the shared choice."""
    settings = cover_settings(mbsettings)
    artist_id, mbids = seed_discography(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        for key in ("wanted", "ep", "owned_comp", "single", "other_comp"):
            front(
                mock,
                mbids[key],
                httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}),
            )
        worker, job_id = await run_covers(
            settings, session_factory, artist_id, types=["single", "bogus"]
        )
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    covers = covers_of(session_factory, artist_id)
    assert covers["A Single"][0] == "ok"  # asked for
    assert covers["More Hits"] == ("unknown", None)  # compilation nobody asked for and nobody owns
