"""Artist pictures (Deezer), the coloured "owned / missing" labels and the dashboard for everyone."""

import re
from datetime import timedelta

import pytest
import respx
from sqlalchemy import select

from app.integrations import ratelimit
from app.integrations.cached_http import CachedJsonClient
from app.integrations.discovery_types import ArtistCandidate
from app.integrations.previews import DeezerClient, picture_url
from app.jobs.handlers import fetch_artist_images as job_module
from app.jobs.worker import JobWorker
from app.models import STATUS_FAILED, STATUS_SUCCEEDED, Artist, utcnow
from app.services.artist_images import (
    RECHECK,
    artists_needing_image,
    pick_artist,
    store_image,
)
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.test_jobs_worker import enqueue, logs, wait_for_status

DEEZER = "https://api.deezer.com/search/artist"
PIC = "https://cdn-images.dzcdn.net/images/artist/f69dea01fc82fac4a64074e71774d053/250x250-000000-80-0-0.jpg"
UA = "Diskko/0.1 ( test@example.com )"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(DeezerClient, "min_interval", 0)
    monkeypatch.setattr(CachedJsonClient, "BACKOFF_BASE", 0)
    ratelimit._LIMITERS.clear()
    yield
    ratelimit._LIMITERS.clear()


def deezer_answer(*artists):
    """What Deezer answers: NOT sorted by relevance (a tribute band can come first)."""
    return {
        "data": [
            {"id": i, "name": name, "picture_medium": pic, "nb_fan": fans}
            for i, (name, pic, fans) in enumerate(artists, 1)
        ],
        "total": len(artists),
    }


# ------------------------------------------------------------------------ the client


async def test_deezer_artist_search(settings, session_factory):
    answer = deezer_answer(("Radiohead Tribute Band", PIC, 414), ("Radiohead", PIC, 9_000_000))
    with respx.mock() as mock:
        route = mock.get(DEEZER).respond(json=answer)
        async with DeezerClient(settings, session_factory) as client:
            found = await client.search_artists('Radio"head')
    assert [(c.name, c.fans) for c in found] == [
        ("Radiohead Tribute Band", 414),
        ("Radiohead", 9_000_000),
    ]
    assert found[0].picture_url == PIC
    assert route.calls.last.request.url.params["q"] == 'artist:"Radio head"'


def test_only_deezer_image_servers_are_trusted():
    assert picture_url(PIC) == PIC
    for bad in (
        None,
        "",
        "http://cdn-images.dzcdn.net/x.jpg",
        "https://evil.example/dzcdn.net/x.jpg",
        "javascript:1",
    ):
        assert picture_url(bad) is None


# ---------------------------------------------------------------------- the choice


def cand(name, fans=1, pic=PIC):
    return ArtistCandidate(name, pic, fans)


def test_the_artist_with_exactly_that_name_and_most_fans_wins():
    picked = pick_artist(
        [
            cand("Radiohead Tribute Band", 400),
            cand("Radiohead", 50),
            cand("Radiohead", 9000),
            cand("DJ Radiohead"),
        ],
        "Radiohead",
    )
    assert picked.fans == 9000


def test_names_match_ignoring_case_accents_and_punctuation():
    assert pick_artist([cand("BEYONCÉ")], "Beyonce") is not None
    assert pick_artist([cand("AC/DC")], "AC DC") is not None
    assert pick_artist([cand("Guns N’ Roses")], "Guns N' Roses") is not None


def test_nothing_looser_is_accepted():
    assert pick_artist([cand("Radiohead Tribute Band"), cand("DJ Radiohead")], "Radiohead") is None
    assert pick_artist([], "Radiohead") is None
    assert pick_artist([cand("Radiohead", pic=None)], "Radiohead") is None  # no picture to show


# ------------------------------------------------------------------------ what is due


def test_artists_without_a_picture_are_due_once_then_after_a_long_time(db):
    a = make_artist(db, "Alpha")
    b = make_artist(db, "Beta")
    make_artist(db, "Outside", in_library=False)
    assert {n for _, n in artists_needing_image(db, 10)} == {"Alpha", "Beta"}
    store_image(db, a.id, PIC)
    store_image(db, b.id, None)
    db.commit()
    assert artists_needing_image(db, 10) == []
    later = lambda: utcnow() + RECHECK + timedelta(days=1)  # noqa: E731
    assert [n for _, n in artists_needing_image(db, 10, now=later)] == ["Beta"]  # Alpha has one


def test_the_never_tried_come_before_the_retries(db):
    old = make_artist(db, "Tried Once")
    store_image(db, old.id, None)
    old.image_checked_at = utcnow() - RECHECK - timedelta(days=5)
    make_artist(db, "Never Tried")
    db.commit()
    assert [n for _, n in artists_needing_image(db, 10)] == ["Never Tried", "Tried Once"]


def test_the_limit(db):
    for i in range(5):
        make_artist(db, f"A{i}")
    assert len(artists_needing_image(db, 3)) == 3


# --------------------------------------------------------------------------- the job


async def run_job(settings, factory):
    s = settings.model_copy(update={"job_poll_interval": 0.02, "mb_user_agent": UA})
    worker = JobWorker(s, factory)
    await worker.start()
    job_id = enqueue(factory, "fetch_artist_images", {})
    worker.notify()
    return worker, job_id


async def test_the_job_stores_pictures_and_remembers_the_artists_without_one(
    settings, session_factory
):
    with session_factory() as db:
        make_artist(db, "Radiohead")
        make_artist(db, "Nobody Known")
    with respx.mock() as mock:

        def answer(request):
            name = request.url.params["q"]
            if "Radiohead" in name:
                return respx.MockResponse(
                    200,
                    json=deezer_answer(("Radiohead Tribute Band", PIC, 5), ("Radiohead", PIC, 900)),
                )
            return respx.MockResponse(200, json=deezer_answer(("Somebody Else", PIC, 1)))

        route = mock.get(DEEZER).mock(side_effect=answer)
        worker, job_id = await run_job(settings, session_factory)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        calls = route.call_count
        again = enqueue(session_factory, "fetch_artist_images", {})
        worker.notify()
        await wait_for_status(session_factory, again, STATUS_SUCCEEDED)
        await worker.stop()
        assert route.call_count == calls  # everyone was looked up: nothing more to ask
    with session_factory() as db:
        got = {
            a.name: (a.image_url, a.image_checked_at is not None)
            for a in db.scalars(select(Artist))
        }
    assert got["Radiohead"] == (PIC, True) and got["Nobody Known"] == (None, True)
    messages = logs(session_factory, job_id)
    assert "Radiohead: picture found (900 fans)" in messages
    assert any(m.startswith("Nobody Known: no artist named exactly so") for m in messages)
    assert "Done: 1 picture(s) found, 1 not found" in messages
    assert any(
        "every artist already has a picture" in m.lower() for m in logs(session_factory, again)
    )


async def test_a_deezer_failure_marks_nobody_and_is_retried(settings, session_factory):
    with session_factory() as db:
        make_artist(db, "Radiohead")
    with respx.mock() as mock:
        mock.get(DEEZER).respond(503)
        worker, job_id = await run_job(settings, session_factory)
        for _ in range(300):
            with session_factory() as db:
                from app.models import Job

                job = db.get(Job, job_id)
                if job.attempts >= 1 and job.status in ("queued", STATUS_FAILED):
                    break
            import asyncio

            await asyncio.sleep(0.02)
        await worker.stop()
    assert job.status == "queued" and job.next_run_at is not None  # a retry is planned
    with session_factory() as db:
        assert db.scalar(select(Artist.image_checked_at)) is None  # not marked: asked again later


async def test_a_long_library_is_done_in_batches(settings, session_factory, monkeypatch):
    monkeypatch.setattr(job_module, "BATCH", 3)
    with session_factory() as db:
        for i in range(5):
            make_artist(db, f"Artist {i}")
    with respx.mock() as mock:
        mock.get(DEEZER).respond(json=deezer_answer())
        worker, job_id = await run_job(settings, session_factory)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        for _ in range(300):
            with session_factory() as db:
                if not artists_needing_image(db, 10):
                    break
            import asyncio

            await asyncio.sleep(0.05)
        await worker.stop()
    with session_factory() as db:
        assert artists_needing_image(db, 10) == []  # the follow-up run did the rest


def test_the_picture_job_runs_daily_and_after_a_scan():
    from app.jobs.scheduler import SCHEDULES, Schedule

    assert Schedule("fetch_artist_images", "40 4 * * *") in SCHEDULES


# ------------------------------------------------------------------- the artist tiles


def tile(html, name):
    pattern = (
        r'<a href="/artist/\d+"[^>]*>(?:(?!</a>).)*?title="'
        + re.escape(name)
        + r'"(?:(?!</a>).)*?</a>'
    )
    m = re.search(pattern, html, re.S)
    assert m, name
    return m.group(0)


@pytest.fixture
def artists(db):
    """Red (owns nothing), Yellow (owns 1 of 3), Green (owns everything), Grey (not synced)."""
    out = {}
    for name in ("Red", "Yellow", "Green"):
        artist = make_artist(
            db, name, mbid=f"{ord(name[0]):08x}-0000-4000-8000-000000000000", status="resolved"
        )
        artist.metadata_synced_at = utcnow()
        out[name] = artist
    make_rg(db, out["Red"], "Red 1", 2000)
    make_rg(db, out["Red"], "Red 2", 2001)
    for i in range(3):
        make_rg(db, out["Yellow"], f"Yellow {i}", 2000 + i)
    make_rg(db, out["Green"], "Green One", 1999)
    make_album(db, out["Yellow"], "Yellow 0", year=2000)
    make_album(db, out["Green"], "Green One", year=1999)
    out["Grey"] = make_artist(db, "Grey")
    from app.services.musicbrainz.matching import match_artist_albums

    for name in ("Yellow", "Green"):
        match_artist_albums(db, out[name].id)
    db.commit()
    return out


def chips(html):
    return {
        n: (m.group(1), m.group(2))
        for n in ("Red", "Yellow", "Green", "Grey")
        if (m := re.search(r'data-ownership="(\w+)"[^>]*>([^<]*)<', tile(html, n)))
    }


def test_the_labels_are_red_yellow_or_green(admin_client, artists):
    text = admin_client.get("/artists").text
    found = chips(text)
    assert found["Red"] == ("red", "0 / 2")  # nothing owned
    assert found["Yellow"] == ("yellow", "1 / 2")  # some owned, some missing
    assert found["Green"] == ("green", "1 / 0")  # nothing missing
    assert found["Grey"][0] == "unknown"  # not synced: the number of missing albums is unknown
    assert 'class="rounded px-1.5 py-0.5 font-medium tabular-nums bg-red-100 text-red-800"' in tile(
        text, "Red"
    )
    assert "bg-amber-100" in tile(text, "Yellow") and "bg-green-100" in tile(text, "Green")


def test_the_label_explains_itself(admin_client, artists):
    assert 'title="1 album(s) owned / 2 missing"' in tile(
        admin_client.get("/artists").text, "Yellow"
    )


def test_the_tile_shows_picture_name_and_label_in_that_order(admin_client, db, artists):
    store_image(db, artists["Yellow"].id, PIC)
    db.commit()
    html = tile(admin_client.get("/artists").text, "Yellow")
    img, name, chip = html.index("<img"), html.index('title="Yellow"'), html.index("data-ownership")
    assert img < name < chip
    assert f'src="{PIC}"' in html and 'referrerpolicy="no-referrer"' in html


def test_without_a_picture_the_cover_then_the_initial_is_shown(admin_client, db, artists):
    group = make_rg(db, artists["Green"], "Covered", 2010)
    album = make_album(db, artists["Green"], "Covered", year=2010)
    album.release_group_id, album.match_status = group.id, "matched"
    group.cover_status, group.cover_path = "ok", "green-cover.jpg"
    db.commit()
    text = admin_client.get("/artists").text
    assert 'src="/covers/green-cover.jpg"' in tile(text, "Green")  # no picture yet: a cover
    assert 'aria-hidden="true">R</div>' in tile(text, "Red")  # nothing at all: the initial


def test_the_list_view_has_the_same_labels_and_pictures(admin_client, db, artists):
    store_image(db, artists["Red"].id, PIC)
    db.commit()
    text = admin_client.get("/artists?view=list").text
    assert (
        f'src="{PIC}"' in text
        and 'data-ownership="red"' in text
        and 'data-ownership="green"' in text
    )


# ------------------------------------------------------------------------ dashboard


def test_the_dashboard_is_open_to_everyone_but_the_controls_are_for_admins(
    admin_client, user_client, db, artists
):
    admin, user = admin_client.get("/dashboard").text, user_client.get("/dashboard").text
    assert "Scan library" in admin and 'action="/dashboard/scan"' in admin
    assert user_client.get("/dashboard").status_code == 200
    assert "Scan library" not in user and "/dashboard/scan" not in user
    assert "music-test" not in user and "Folder" not in user  # the library path stays private
    assert "Artists" in user and "Albums" in user


def test_the_quality_table_links_to_the_filtered_albums(admin_client, db):
    artist = make_artist(db, "Alpha")
    album = make_album(db, artist, "Alpha One", 2000)
    album.quality_tier = "mp3_320"
    db.commit()
    text = admin_client.get("/dashboard").text
    assert 'href="/albums?quality=mp3_320"' in text
    assert 'href="/albums?quality=flac_master"' not in text  # nothing there: no link
    assert 'href="/artists"' in text and 'href="/albums"' in text
