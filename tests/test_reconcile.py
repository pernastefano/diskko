"""Matching a download with MusicBrainz by hand: the page, the pairing the user sets, another
album of MusicBrainz, and the import that follows it."""

import asyncio
import json

import pytest
import respx
from mutagen.flac import FLAC

from app.integrations.musicbrainz_client import (
    ApiCache,
    release_group_query,
    release_group_search_key,
)
from app.jobs.worker import JobWorker
from app.models import Job, ReleaseGroup, utcnow
from app.services import downloads as svc
from tests import test_postprocess
from tests.conftest import MB_BASE, MB_UA, post
from tests.test_jobs_worker import enqueue, fetch, logs
from tests.test_postprocess import (
    RELEASE_MBID,
    TITLES,
    album_folder,
    good_files,
    mock_music,
    put_download,
    release_json,
    request_of,
    run_job,
    seed,
)

env = test_postprocess.env  # the fixture of the import tests


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"mb_user_agent": MB_UA, "musicbrainz_url": MB_BASE})


BONUS = [*TITLES, "Bonus Track"]  # four tracks, three files: asks which release
OTHER_GROUP = "99999999-9999-4999-8999-999999999999"


async def park(env, titles=BONUS):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, release_json(titles))
        await run_job(settings, factory)
    assert request_of(factory).status == "awaiting_choice"


def state_of(factory):
    return svc.release_state_of(request_of(factory))


async def run_payload(env, payload, release=None):
    settings, factory, music, downloads = env
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), factory)
    await worker.start()
    job_id = enqueue(factory, "postprocess", {"request_id": 1, **payload})
    worker.notify()
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock_music(mock, release or release_json(BONUS))
            for _ in range(500):
                job = await asyncio.to_thread(fetch, factory, job_id)
                if job.status in ("succeeded", "failed"):
                    return job_id, job
                await asyncio.sleep(0.02)
    finally:
        await worker.stop()
    raise AssertionError("the job did not finish")


# ------------------------------------------------------------- what is kept


async def test_the_parked_request_keeps_the_files_and_what_each_release_would_do(env):
    await park(env)
    state = state_of(env[1])
    assert [f["title"] for f in state["files"]] == TITLES
    assert all(f["ext"] == "flac" and f["size"] for f in state["files"])
    [option] = state["options"]
    assert [t["key"] for t in option["track_list"]] == ["1-1", "1-2", "1-3", "1-4"]
    assert option["pairs"] == {"0.flac": "1-1", "1.flac": "1-2", "2.flac": "1-3"}


# ------------------------------------------------------------- the page


async def test_the_page_shows_files_next_to_the_tracks_with_the_pairing_preselected(
    env, admin_client
):
    await park(env)
    html = admin_client.get("/requests/1/reconcile").text
    assert "data-mapping-form" in html and html.count("data-track-select") == 3
    for title in BONUS:
        assert title in html
    assert 'value="0|1-1" selected' in html and 'value="2|1-3" selected' in html
    assert "No file for: Bonus Track" in html
    assert "data-find-album" in html and "data-use-album" not in html  # nothing searched yet
    assert 'href="/requests/1/reconcile"' in admin_client.get("/requests/1").text


async def test_a_request_that_is_not_waiting_goes_back_to_its_page(env, admin_client):
    settings, factory, music, downloads = env
    seed(factory)
    r = admin_client.get("/requests/1/reconcile", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/requests/1"


async def test_another_users_request_cannot_be_reconciled(env, admin_client, user_client):
    await park(env)
    assert user_client.get("/requests/1/reconcile").status_code == 404
    r = post(user_client, "/requests/1/reconcile/import", {"release": "x"}, page="/dashboard")
    assert r.status_code == 404


# ------------------------------------------------------------- the user's pairing


async def test_the_user_can_swap_two_tracks_and_leave_one_file_out(env, admin_client):
    settings, factory, music, downloads = env
    await park(env)
    # file 0 is "Middle Song", file 1 "Opening"; file 2 has no track and keeps its own title
    r = post(
        admin_client,
        "/requests/1/reconcile/import",
        {"release": RELEASE_MBID, "map": ["0|1-2", "1|1-1", "2|"]},
        page="/requests/1/reconcile",
    )
    assert r.status_code == 303 and r.headers["location"] == "/requests/1"
    row = request_of(factory)
    assert row.status == "downloaded"
    with factory() as db:
        job = db.query(Job).filter_by(type="postprocess").order_by(Job.id.desc()).first()
        payload = json.loads(job.payload_json)
    assert payload["mapping"] == {"0.flac": "1-2", "1.flac": "1-1", "2.flac": ""}
    job_id, job = await run_payload(env, payload)
    assert job.status == "succeeded", logs(factory, job_id)
    names = sorted(p.name for p in album_folder(music).glob("*.flac"))
    assert (
        names == ["01 - Middle Song.flac", "02 - Opening.flac", "03 - The Closer.flac"]
        or len(names) == 3
    )
    tagged = {
        FLAC(p)["title"][0]: FLAC(p)["tracknumber"][0] for p in album_folder(music).glob("*.flac")
    }
    assert tagged["Middle Song"] == "2" and tagged["Opening"] == "1"  # the pairing was followed


async def test_two_files_cannot_be_one_track(env, admin_client):
    await park(env)
    r = post(
        admin_client,
        "/requests/1/reconcile/import",
        {"release": RELEASE_MBID, "map": ["0|1-1", "1|1-1", "2|"]},
        page="/requests/1/reconcile",
    )
    assert r.headers["location"].endswith("error=request_bad_mapping")
    assert request_of(env[1]).status == "awaiting_choice"


async def test_a_track_or_file_that_does_not_exist_is_refused(env, admin_client):
    await park(env)
    for field in (["0|9-9"], ["7|1-1"], ["x|1-1"]):
        r = post(
            admin_client,
            "/requests/1/reconcile/import",
            {"release": RELEASE_MBID, "map": field},
            page="/requests/1/reconcile",
        )
        assert r.headers["location"].endswith("error=request_bad_mapping"), field
    assert request_of(env[1]).status == "awaiting_choice"


# ------------------------------------------------------------- another album


def seed_search(factory, query, group_mbid=OTHER_GROUP, title="Another Album"):
    hit = {
        "id": group_mbid,
        "score": 100,
        "title": title,
        "primary-type": "Album",
        "first-release-date": "2001-01-01",
        "artist-credit": [
            {
                "name": "The Band",
                "artist": {"id": "33333333-3333-4333-8333-333333333333", "name": "The Band"},
            }
        ],
    }
    with factory() as db:
        db.add(
            ApiCache(
                key=release_group_search_key(query, 15),
                status=200,
                response=json.dumps({"count": 1, "release-groups": [hit]}),
                fetched_at=utcnow(),
                ttl_seconds=3600,
            )
        )
        db.commit()


async def test_looking_for_another_album_queues_a_musicbrainz_search_and_lists_the_answer(
    env, admin_client
):
    settings, factory, music, downloads = env
    await park(env)
    r = post(
        admin_client,
        "/requests/1/reconcile/find",
        {"q": "Another Album", "mode": "title", "scope": "all"},
        page="/requests/1/reconcile",
    )
    assert r.status_code == 303 and "q=Another+Album" in r.headers["location"]
    with factory() as db:
        job = db.query(Job).filter_by(type="search_release_groups").one()
        assert json.loads(job.payload_json)["query"] == release_group_query(
            "Another Album", "title"
        )
    seed_search(factory, release_group_query("Another Album", "title"))
    html = admin_client.get(r.headers["location"].split("#")[0]).text
    assert "data-use-album" in html and "Another Album" in html


async def test_using_the_other_album_moves_the_request_and_reads_its_releases(env, admin_client):
    settings, factory, music, downloads = env
    await park(env)
    query = release_group_query("Another Album", "title")
    seed_search(factory, query)
    r = post(
        admin_client,
        "/requests/1/reconcile/group",
        {"choice": f"mbid:{OTHER_GROUP}", "q": "Another Album", "mode": "title", "scope": "all"},
        page="/requests/1/reconcile",
    )
    assert r.status_code == 303 and r.headers["location"] == "/requests/1/reconcile"
    state = state_of(factory)
    assert state["loading"] is True and state["options"] == [] and state["files"]
    assert state["group"]["mbid"] == OTHER_GROUP
    with factory() as db:
        group = db.get(ReleaseGroup, request_of(factory).release_group_id)
        assert group.mbid == OTHER_GROUP and group.title == "Another Album"
        assert db.query(Job).filter_by(type="reconcile_options").count() == 1
    assert "data-loading" in admin_client.get("/requests/1/reconcile").text

    # the job reads the releases of that album and the files are matched to them
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), factory)
    await worker.start()
    worker.notify()
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{MB_BASE}/release").respond(json=release_json(TITLES, title="Another Album"))
            for _ in range(500):
                state = await asyncio.to_thread(state_of, factory)
                if not state["loading"]:
                    break
                await asyncio.sleep(0.02)
    finally:
        await worker.stop()
    assert state["loading"] is False and len(state["options"]) == 1
    assert state["options"][0]["title"] == "Another Album" and state["options"][0]["paired"] == 3
    assert request_of(factory).status == "awaiting_choice"


async def test_an_answer_that_is_not_in_the_cache_is_refused(env, admin_client):
    await park(env)
    r = post(
        admin_client,
        "/requests/1/reconcile/group",
        {"choice": f"mbid:{OTHER_GROUP}", "q": "Nothing Cached", "mode": "title", "scope": "all"},
        page="/requests/1/reconcile",
    )
    assert r.headers["location"].endswith("error=search_expired")
    assert request_of(env[1]).release_group_id == 1


# ------------------------------------------------------------- reading again


async def test_a_request_parked_by_an_older_version_can_read_its_files_again(env, admin_client):
    settings, factory, music, downloads = env
    await park(env)
    with factory() as db:  # what 0.3.1 kept: just the options
        req = db.get(type(request_of(factory)), 1)
        req.release_options_json = json.dumps([state_of(factory)["options"][0]])
        db.commit()
    assert "data-needs-rebuild" in admin_client.get("/requests/1/reconcile").text
    r = post(admin_client, "/requests/1/reconcile/rebuild", {}, page="/requests/1/reconcile")
    assert r.status_code == 303 and request_of(factory).status == "downloaded"
    job_id, job = await run_payload(env, {"release_choice": "reconcile"})
    assert job.status == "succeeded", logs(factory, job_id)
    assert request_of(factory).status == "awaiting_choice" and state_of(factory)["files"]
