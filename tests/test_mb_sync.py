import asyncio
import json

import pytest
import respx
from sqlalchemy import func, select

from app.jobs.worker import JobWorker
from app.models import (
    MATCH_MATCHED,
    MB_IGNORED,
    MB_PENDING,
    MB_RESOLVED,
    MB_REVIEW,
    MB_UNMATCHED,
    Artist,
    ArtistReview,
    Job,
    LibraryAlbum,
    ReleaseGroup,
    utcnow,
)
from app.services.musicbrainz import sync
from app.services.musicbrainz.resolution import VARIOUS_ARTISTS_MBID
from tests.conftest import MB_BASE as BASE
from tests.dbhelpers import make_album, make_artist
from tests.mbdata import (
    ARETHA,
    NIRVANA_UK,
    NIRVANA_US,
    aretha_discography,
    browse_router,
    load,
    nirvana_discographies,
)
from tests.test_jobs_worker import enqueue, fetch, logs, wait_for_status

STATUS_SUCCEEDED, STATUS_FAILED, STATUS_QUEUED = "succeeded", "failed", "queued"


def seed(factory, name="Aretha Franklin", mbid=None, status=MB_PENDING, albums=()):
    with factory() as db:
        artist = make_artist(db, name, mbid=mbid, status=status)
        for title, year in albums:
            make_album(db, artist, title, year=year)
        return artist.id


ARETHA_ALBUMS = (
    ("Lady Soul", 1968),
    ("Aretha Now", 1968),
    ("Spirit in the Dark", 1970),
    ("A Record Nobody Catalogued", 1999),
)


async def run_job(settings, factory, artist_id, **payload):
    worker = JobWorker(settings, factory)
    await worker.start()
    job_id = enqueue(factory, "sync_artist_metadata", {"artist_id": artist_id, **payload})
    worker.notify()
    return worker, job_id


def mount(mock, search=None, discographies=None, missing=frozenset()):
    routes = {}
    if search is not None:
        routes["search"] = mock.get("/artist").respond(json=search)
    routes["browse"] = mock.get("/release-group").mock(
        side_effect=browse_router(discographies or {}, missing)
    )
    return routes


def state(factory, artist_id):
    with factory() as db:
        artist = db.get(Artist, artist_id)
        db.expunge(artist)
        return artist


def count(factory, model, *where):
    with factory() as db:
        return db.scalar(select(func.count(model.id)).where(*where))


# ---------------------------------------------------------------- full sync


async def test_sync_resolves_the_artist_stores_the_discography_and_matches_albums(
    mbsettings, session_factory
):
    artist_id = seed(session_factory, albums=ARETHA_ALBUMS)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        routes = mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert job.progress_pct == 100.0 and job.step == "Done"
    artist = state(session_factory, artist_id)
    assert (artist.mbid, artist.mb_status, artist.mb_source) == (ARETHA, MB_RESOLVED, "auto")
    assert artist.mb_confidence == 1.0 and artist.metadata_synced_at is not None
    assert count(session_factory, ReleaseGroup, ReleaseGroup.artist_id == artist_id) == 200
    with session_factory() as db:
        albums = {a.title: a for a in db.scalars(select(LibraryAlbum))}
        assert albums["Lady Soul"].match_status == MATCH_MATCHED
        assert albums["Lady Soul"].release_group_id is not None
        assert albums["A Record Nobody Catalogued"].match_status == "unmatched"
    assert routes["search"].call_count == 1 and routes["browse"].call_count == 2  # 2 pages of 100
    messages = logs(session_factory, job_id)
    assert any("200 release group(s)" in m and "matched" in m for m in messages)


async def test_a_job_queues_the_cover_download_as_a_child(mbsettings, session_factory):
    artist_id = seed(session_factory, albums=ARETHA_ALBUMS)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    with session_factory() as db:
        child = db.scalar(select(Job).where(Job.type == "fetch_covers"))
    assert child.parent_job_id == job_id and child.dedupe_key == f"fetch_covers:{artist_id}"
    assert child.queue == "coverart" and json.loads(child.payload_json) == {"artist_id": artist_id}


async def test_covers_can_be_switched_off(mbsettings, session_factory):
    artist_id = seed(session_factory)
    settings = mbsettings.model_copy(update={"coverart_enabled": False})
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert count(session_factory, Job, Job.type == "fetch_covers") == 0


async def test_a_tagged_artist_needs_no_search(mbsettings, session_factory):
    artist_id = seed(session_factory, mbid=ARETHA, status=MB_PENDING, albums=ARETHA_ALBUMS)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        routes = mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert routes["search"].call_count == 0 and routes["browse"].call_count == 2
    assert state(session_factory, artist_id).mb_status == MB_RESOLVED


async def test_second_sync_comes_from_the_cache_and_force_refreshes(mbsettings, session_factory):
    artist_id = seed(session_factory, mbid=ARETHA, albums=ARETHA_ALBUMS)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        routes = mount(mock, None, {ARETHA: aretha_discography()})
        worker, first = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, first, STATUS_SUCCEEDED)
        calls = routes["browse"].call_count
        second = enqueue(session_factory, "sync_artist_metadata", {"artist_id": artist_id})
        worker.notify()
        await wait_for_status(session_factory, second, STATUS_SUCCEEDED)
        assert routes["browse"].call_count == calls  # nothing hit the network
        forced = enqueue(
            session_factory, "sync_artist_metadata", {"artist_id": artist_id, "force": True}
        )
        worker.notify()
        await wait_for_status(session_factory, forced, STATUS_SUCCEEDED)
        await worker.stop()
    assert routes["browse"].call_count == calls * 2
    assert count(session_factory, ReleaseGroup) == 200  # no duplicates after three syncs


async def test_release_groups_removed_upstream_are_removed_and_albums_rematched(
    mbsettings, session_factory
):
    artist_id = seed(session_factory, mbid=ARETHA, albums=ARETHA_ALBUMS)
    pages = aretha_discography()
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mount(mock, None, {ARETHA: pages})
        worker, first = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, first, STATUS_SUCCEEDED)
        with session_factory() as db:
            before = db.scalar(
                select(LibraryAlbum).where(LibraryAlbum.title == "Lady Soul")
            ).release_group_id
        lady = next(g for g in pages if g["title"] == "Lady Soul")
        mock.routes.clear()
        mount(mock, None, {ARETHA: [g for g in pages if g is not lady]})
        again = enqueue(
            session_factory, "sync_artist_metadata", {"artist_id": artist_id, "force": True}
        )
        worker.notify()
        await wait_for_status(session_factory, again, STATUS_SUCCEEDED)
        await worker.stop()
    assert count(session_factory, ReleaseGroup) == 199
    with session_factory() as db:
        album = db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Lady Soul"))
        assert before is not None and album.release_group_id != before
        assert album.match_status != MATCH_MATCHED or album.release_group_id is not None


# ------------------------------------------------------------ resolution via job


async def test_namesake_is_resolved_with_the_library_albums(mbsettings, session_factory):
    artist_id = seed(
        session_factory,
        "Nirvana",
        albums=(("Nevermind", 1991), ("In Utero", 1993), ("Bleach", 1989)),
    )
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_nirvana.json"), nirvana_discographies())
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    artist = state(session_factory, artist_id)
    assert (artist.mbid, artist.mb_status) == (NIRVANA_US, MB_RESOLVED)
    with session_factory() as db:
        statuses = {a.title: a.match_status for a in db.scalars(select(LibraryAlbum))}
    assert statuses == {"Nevermind": "matched", "In Utero": "matched", "Bleach": "matched"}


async def test_unresolvable_artist_goes_to_the_review_queue(mbsettings, session_factory):
    artist_id = seed(session_factory, "Nirvana", albums=(("Some Obscure Demo", 1990),))
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_nirvana.json"), nirvana_discographies())
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert job.step == "Needs review"
    artist = state(session_factory, artist_id)
    assert artist.mb_status == MB_REVIEW and artist.mbid is None
    with session_factory() as db:
        review = db.scalar(select(ArtistReview))
        candidates = sync.pending_review_candidates(review)
    assert (review.artist_id, review.reason, review.status) == (artist_id, "ambiguous", "pending")
    assert {NIRVANA_US, NIRVANA_UK} <= {c["mbid"] for c in candidates}
    assert count(session_factory, ReleaseGroup) == 0


async def test_no_match_goes_to_review_with_no_candidates(mbsettings, session_factory):
    artist_id = seed(session_factory, "Zzqxv Nonexistent Quartet")
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_none.json"))
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    with session_factory() as db:
        review = db.scalar(select(ArtistReview))
    assert review.reason == "no_match" and sync.pending_review_candidates(review) == []


async def test_an_unknown_mbid_goes_to_review(mbsettings, session_factory):
    artist_id = seed(session_factory, mbid=ARETHA)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mount(mock, None, {}, missing=frozenset({ARETHA}))
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_REVIEW
    with session_factory() as db:
        assert db.scalar(select(ArtistReview)).reason == "invalid_mbid"


async def test_artists_in_review_are_not_searched_again(mbsettings, session_factory):
    artist_id = seed(session_factory, status=MB_REVIEW)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        routes = mount(mock, load("artist_search_aretha.json"))
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert routes["search"].call_count == 0
    assert any("waiting for a manual decision" in m for m in logs(session_factory, job_id))


@pytest.mark.parametrize("status", [MB_UNMATCHED, MB_IGNORED])
async def test_decided_artists_are_skipped_unless_forced(mbsettings, session_factory, status):
    artist_id = seed(session_factory, status=status)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        routes = mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        assert routes["search"].call_count == 0
        forced = enqueue(
            session_factory, "sync_artist_metadata", {"artist_id": artist_id, "force": True}
        )
        worker.notify()
        await wait_for_status(session_factory, forced, STATUS_SUCCEEDED)
        await worker.stop()
    assert routes["search"].call_count == 1
    assert state(session_factory, artist_id).mb_status == MB_RESOLVED


async def test_unknown_artist_is_ignored_and_various_artists_gets_its_fixed_mbid(
    mbsettings, session_factory
):
    unknown = seed(session_factory, "Unknown Artist")
    various = seed(session_factory, "Various Artists", albums=(("Summer Hits", 2001),))
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        routes = mount(mock, {"artists": []}, {VARIOUS_ARTISTS_MBID: aretha_discography()})
        worker = JobWorker(mbsettings, session_factory)
        await worker.start()
        jobs = [
            enqueue(session_factory, "sync_artist_metadata", {"artist_id": i})
            for i in (unknown, various)
        ]
        worker.notify()
        for job_id in jobs:
            await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert state(session_factory, unknown).mb_status == MB_IGNORED
    artist = state(session_factory, various)
    assert (artist.mbid, artist.mb_status) == (VARIOUS_ARTISTS_MBID, MB_RESOLVED)
    assert artist.metadata_synced_at is not None
    # no search and, above all, no browsing of the (huge) Various Artists discography
    assert routes["search"].call_count == 0 and routes["browse"].call_count == 0
    assert count(session_factory, ReleaseGroup) == 0
    assert count(session_factory, Job, Job.type == "fetch_covers") == 0
    assert any(
        "Various Artists: discography not synced" in m for m in logs(session_factory, jobs[1])
    )


# ----------------------------------------------------------------------- failures


async def test_a_missing_user_agent_fails_the_job_without_retries(mbsettings, session_factory):
    artist_id = seed(session_factory)
    settings = mbsettings.model_copy(update={"mb_user_agent": ""})
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        worker, job_id = await run_job(settings, session_factory, artist_id)
        job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
        await worker.stop()
    assert job.attempts == 1 and "MB_USER_AGENT" in job.error and mock.calls.call_count == 0


async def test_an_outage_leaves_the_job_for_a_later_retry(mbsettings, session_factory):
    artist_id = seed(session_factory, mbid=ARETHA)
    settings = mbsettings.model_copy(update={"mb_max_retries": 0})
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/release-group").respond(503)
        worker, job_id = await run_job(settings, session_factory, artist_id)
        for _ in range(200):
            job = fetch(session_factory, job_id)
            if job.error:
                break
            await asyncio.sleep(0.02)
        await worker.stop()
    job = fetch(session_factory, job_id)
    assert job.status == STATUS_QUEUED and "HTTP 503" in job.error and job.next_run_at is not None
    assert state(session_factory, artist_id).metadata_synced_at is None  # nothing half-saved


async def test_a_missing_artist_fails_cleanly(mbsettings, session_factory):
    worker = JobWorker(mbsettings, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "sync_artist_metadata", {"artist_id": 9999})
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    await worker.stop()
    assert "not found" in job.error and job.attempts == 1


# ------------------------------------------------------------- admin decisions


def test_choosing_a_candidate_resolves_and_closes_the_review(jdb):
    artist = make_artist(jdb, status=MB_PENDING)
    sync.open_review(jdb, artist.id, "ambiguous", [{"mbid": NIRVANA_US}])
    jdb.commit()
    assert artist.mb_status == MB_REVIEW
    sync.choose_candidate(jdb, artist.id, NIRVANA_US, user_id=None)
    jdb.commit()
    jdb.refresh(artist)
    assert (artist.mbid, artist.mb_status, artist.mb_source, artist.mb_confidence) == (
        NIRVANA_US,
        MB_RESOLVED,
        "manual",
        1.0,
    )
    assert artist.metadata_synced_at is None  # the next sync verifies the choice
    assert jdb.scalar(select(ArtistReview.status)) == "resolved"


def test_two_artists_cannot_share_an_mbid(jdb):
    first = make_artist(jdb, "Nirvana", mbid=NIRVANA_US, status=MB_RESOLVED)
    second = make_artist(jdb, "Nirvana (live bootlegs)", status=MB_REVIEW)
    sync.choose_candidate(jdb, second.id, NIRVANA_US, user_id=None)
    jdb.commit()
    jdb.refresh(second)
    assert second.mbid is None and second.mb_status == MB_REVIEW
    assert jdb.get(Artist, first.id).mbid == NIRVANA_US
    assert (
        jdb.scalar(select(ArtistReview.reason).where(ArtistReview.artist_id == second.id))
        == "ambiguous"
    )


def test_dismissing_marks_the_artist_as_not_on_musicbrainz(jdb):
    artist = make_artist(jdb, status=MB_PENDING)
    sync.open_review(jdb, artist.id, "no_match", [])
    jdb.commit()
    sync.dismiss_artist(jdb, artist.id, user_id=None)
    jdb.commit()
    jdb.refresh(artist)
    assert artist.mb_status == MB_UNMATCHED
    assert jdb.scalar(select(ArtistReview.status)) == "dismissed"


def test_reset_forgets_the_identity(jdb):
    artist = make_artist(jdb, mbid=NIRVANA_US, status=MB_RESOLVED)
    artist.mb_source, artist.metadata_synced_at = "manual", utcnow()
    sync.reset_artist(jdb, artist.id, user_id=None)
    jdb.commit()
    jdb.refresh(artist)
    assert (artist.mbid, artist.mb_status, artist.mb_source, artist.metadata_synced_at) == (
        None,
        MB_PENDING,
        None,
        None,
    )


def test_only_one_review_is_open_per_artist(jdb):
    artist = make_artist(jdb)
    sync.open_review(jdb, artist.id, "no_match", [])
    sync.open_review(jdb, artist.id, "ambiguous", [{"mbid": "x"}])
    jdb.commit()
    statuses = [r.status for r in jdb.scalars(select(ArtistReview).order_by(ArtistReview.id))]
    assert statuses == ["dismissed", "pending"]


# --------------------------------------------------------------- pending artists


def test_which_artists_need_a_sync(jdb):
    from datetime import timedelta

    fresh = make_artist(
        jdb, "Fresh", mbid="a" * 8 + "-0000-4000-8000-000000000001", status=MB_RESOLVED
    )
    fresh.metadata_synced_at = utcnow() - timedelta(days=2)
    stale = make_artist(
        jdb, "Stale", mbid="a" * 8 + "-0000-4000-8000-000000000002", status=MB_RESOLVED
    )
    stale.metadata_synced_at = utcnow() - timedelta(days=45)
    never = make_artist(
        jdb, "Never", mbid="a" * 8 + "-0000-4000-8000-000000000003", status=MB_RESOLVED
    )
    new = make_artist(jdb, "New", status=MB_PENDING)
    for name, status in (("Rev", MB_REVIEW), ("Unm", MB_UNMATCHED), ("Ign", MB_IGNORED)):
        make_artist(jdb, name, status=status)
    make_artist(jdb, "Gone", status=MB_PENDING, in_library=False)
    jdb.commit()
    wanted = sync.artists_needing_sync(jdb, 30)
    assert wanted == [stale.id, never.id, new.id]


async def test_sync_pending_artists_queues_one_job_per_artist(mbsettings, session_factory):
    ids = [seed(session_factory, f"Artist {i}") for i in range(3)]
    worker = JobWorker(mbsettings, session_factory)
    await worker.start()
    with respx.mock(base_url=BASE, assert_all_called=False):
        parent = enqueue(session_factory, "sync_pending_artists", {})
        worker.notify()
        await wait_for_status(session_factory, parent, STATUS_SUCCEEDED)
        await worker.stop()  # stop before the children start working
    with session_factory() as db:
        children = list(db.scalars(select(Job).where(Job.type == "sync_artist_metadata")))
    assert sorted(json.loads(c.payload_json)["artist_id"] for c in children) == sorted(ids)
    assert all(
        c.parent_job_id == parent and c.dedupe_key.startswith("sync_artist:") for c in children
    )
    assert "Queued 3 artist(s)" in " ".join(logs(session_factory, parent))


async def test_sync_pending_artists_without_a_user_agent_does_nothing(mbsettings, session_factory):
    seed(session_factory)
    settings = mbsettings.model_copy(update={"mb_user_agent": ""})
    worker = JobWorker(settings, session_factory)
    await worker.start()
    parent = enqueue(session_factory, "sync_pending_artists", {})
    worker.notify()
    await wait_for_status(session_factory, parent, STATUS_SUCCEEDED)
    await worker.stop()
    assert count(session_factory, Job, Job.type == "sync_artist_metadata") == 0
    assert any("MB_USER_AGENT is not set" in m for m in logs(session_factory, parent))


async def test_no_duplicate_sync_jobs_for_the_same_artist(mbsettings, session_factory):
    seed(session_factory)
    worker = JobWorker(mbsettings.model_copy(update={"jobs_enabled": True}), session_factory)
    with respx.mock(assert_all_called=False) as mock:  # the queued syncs run: nothing real is asked
        mock.get(url__regex=r".*").respond(404)
        await worker.start()
        for _ in range(2):
            job = enqueue(session_factory, "sync_pending_artists", {})
            worker.notify()
            await wait_for_status(session_factory, job, STATUS_SUCCEEDED)
        await worker.stop()
    assert count(session_factory, Job, Job.type == "sync_artist_metadata") == 1
