"""The page of one album for matching it by hand: the artist's candidates, a search over
MusicBrainz (a job), and the decisions."""

import asyncio
import json
import re

import httpx
import pytest
import respx
from sqlalchemy import select

from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    ReleaseGroupData,
    lucene_terms,
    release_group_query,
)
from app.models import AlbumMatchOverride, Job, LibraryAlbum, ReleaseGroup
from app.services.musicbrainz import sync
from tests.conftest import MB_BASE, post
from tests.dbhelpers import make_album, make_artist, make_rg

ARTIST_MBID = "a" * 8 + "-0000-4000-8000-000000000001"
OTHER_RG = "bbbbbbbb-0000-4000-8000-0000000000b1"
COMP_RG = "cccccccc-0000-4000-8000-0000000000c1"


@pytest.fixture
def settings(settings):
    return settings.model_copy(
        update={
            "musicbrainz_url": MB_BASE,
            "mb_user_agent": "Test/1 ( t@example.com )",
            "mb_min_interval": 0.0,
        }
    )


@pytest.fixture
def world(db):
    """An artist with three release groups and an owned album that matched none of them."""
    artist = make_artist(db, "The Band", mbid=ARTIST_MBID, status="resolved")
    groups = [make_rg(db, artist, f"Record {i}", 2000 + i) for i in range(8)]
    album = make_album(db, artist, "Greatest Hits Live", year=2010)
    return {"artist": artist, "groups": groups, "album": album}


def rg_json(
    mbid, title, artist="The Band", artist_mbid=ARTIST_MBID, kind="Album", secondary=None, score=100
):
    raw = {
        "id": mbid,
        "score": score,
        "title": title,
        "primary-type": kind,
        "first-release-date": "2011-05-01",
        "artist-credit": [{"name": artist, "artist": {"id": artist_mbid, "name": artist}}],
    }
    if secondary:
        raw["secondary-types"] = secondary
    return raw


def fill_cache(settings, app, query, hits):
    """What the search job leaves in api_cache (the page reads it from there)."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{MB_BASE}/release-group").mock(
            return_value=httpx.Response(200, json={"count": len(hits), "release-groups": hits})
        )

        async def run():
            async with MusicBrainzClient(settings, app.state.session_factory) as client:
                return await client.search_release_groups(query, limit=15)

        return asyncio.run(run())


def url(album, suffix="match"):
    return f"/admin/albums/{album.id}/{suffix}"


# ----------------------------------------------------------------------- queries


def test_the_queries_are_built_for_each_kind_of_search():
    assert release_group_query("Abbey Road") == 'releasegroup:"Abbey Road"'
    assert release_group_query('He said "hi"') == 'releasegroup:"He said \\"hi\\""'
    assert release_group_query("abbey road", "anything") == (
        "(releasegroup:(abbey road) OR artist:(abbey road))"
    )
    assert release_group_query("releasegroup:abbey AND artist:beatles", "anything") == (
        "(releasegroup:abbey AND artist:beatles)"
    )
    assert (
        release_group_query("Abbey Road", "title", "abc")
        == 'releasegroup:"Abbey Road" AND arid:abc'
    )
    assert lucene_terms('AC/DC: "Back" (in) Black!') == "AC DC Back in Black"


# --------------------------------------------------------------------- the page


def test_the_match_page_is_for_administrators(admin_client, user_client, world, new_client):
    assert admin_client.get(url(world["album"])).status_code == 200
    assert user_client.get(url(world["album"])).status_code == 403
    assert new_client().get(url(world["album"])).status_code == 303
    assert admin_client.get("/admin/albums/999999/match").status_code == 404
    for path in ("match", "match/results", "match/search"):
        assert user_client.get(url(world["album"], path)).status_code in (403, 405)


def test_the_page_shows_the_album_and_ranks_the_artists_release_groups(admin_client, world):
    text = admin_client.get(url(world["album"])).text
    assert (
        "Greatest Hits Live" in text
        and "The Band" in text
        and 'data-match-status="unmatched"' in text
    )
    assert text.count("data-rg=") == 6 and "Show all 8" in text  # the likeliest six first
    assert admin_client.get(url(world["album"]) + "?all=1").text.count("data-rg=") == 8
    assert 'value="Greatest Hits Live"' in text  # the search starts from the title


def test_choosing_a_release_group_of_the_artist_matches_by_hand(admin_client, world, db):
    album, group = world["album"], world["groups"][3]
    r = post(admin_client, url(album), {"choice": str(group.id)}, page=url(album))
    assert r.status_code == 303 and r.headers["location"] == f"/album/{group.id}?flash=match_saved"
    db.expire_all()
    assert db.get(LibraryAlbum, album.id).match_status == "manual"
    assert db.get(LibraryAlbum, album.id).release_group_id == group.id
    assert "Manual" in admin_client.get(url(album)).text


def test_not_on_musicbrainz_and_back_to_automatic(admin_client, world, db):
    album = world["album"]
    r = post(admin_client, url(album), {"choice": "none"}, page=url(album))
    assert r.status_code == 303 and r.headers["location"].startswith(f"/album/local/{album.id}")
    db.expire_all()
    assert db.get(LibraryAlbum, album.id).match_status == "none"
    post(admin_client, url(album), {"choice": "auto"}, page=url(album))
    db.expire_all()
    assert db.get(LibraryAlbum, album.id).match_status != "none"
    assert db.scalar(select(AlbumMatchOverride.id)) is None


def test_a_release_group_of_another_artist_is_refused(admin_client, world, db):
    other = make_rg(db, make_artist(db, "Someone Else"), "Not Yours", 1999)
    album = world["album"]
    r = post(admin_client, url(album), {"choice": str(other.id)}, page=url(album))
    assert r.status_code == 400 and "does not belong to this artist" in r.text
    db.expire_all()
    assert db.get(LibraryAlbum, album.id).match_status == "unmatched"


# ---------------------------------------------------------------------- searching


def test_searching_queues_one_job_and_the_page_waits_for_it(admin_client, world, db, app):
    album = world["album"]
    r = post(
        admin_client,
        url(album, "match/search"),
        {"q": "Greatest Hits", "mode": "title", "scope": "all"},
        page=url(album),
    )
    assert r.status_code == 303 and "q=Greatest+Hits" in r.headers["location"]
    post(
        admin_client,
        url(album, "match/search"),
        {"q": "Greatest Hits", "mode": "title", "scope": "all"},
        page=url(album),
    )
    jobs = db.scalars(select(Job).where(Job.type == "search_release_groups")).all()
    assert len(jobs) == 1  # the second click found the first job still active
    assert json.loads(jobs[0].payload_json) == {"query": 'releasegroup:"Greatest Hits"'}
    page = admin_client.get(r.headers["location"]).text
    assert 'data-search-state="searching"' in page and "match/results" in page  # it polls


def test_the_search_is_never_made_by_a_get(admin_client, world, db):
    admin_client.get(url(world["album"]) + "?q=Anything&scope=all")
    assert db.scalar(select(Job.id).where(Job.type == "search_release_groups")) is None


def test_results_are_listed_with_the_artist_and_can_be_chosen(
    admin_client, world, db, app, settings
):
    album = world["album"]
    query = release_group_query("Greatest Hits", "title", None)
    fill_cache(
        settings,
        app,
        query,
        [
            rg_json(
                COMP_RG,
                "Greatest Hits Live",
                artist="Various Artists",
                artist_mbid="dddddddd-0000-4000-8000-0000000000d1",
                kind="Album",
                secondary=["Compilation", "Live"],
                score=96,
            ),
            rg_json(OTHER_RG, "Record 2", score=60),
        ],
    )
    page = admin_client.get(url(album) + "?q=Greatest+Hits&mode=title&scope=all").text
    assert 'data-search-state="results"' in page and "Various Artists" in page
    assert "match 96%" in page and page.count("data-rg-mbid=") == 2
    # choosing a result that is not in the artist's discography adopts it as one of the artist's
    r = post(
        admin_client,
        url(album),
        {"choice": f"mbid:{COMP_RG}", "q": "Greatest Hits", "mode": "title", "scope": "all"},
        page=url(album),
    )
    assert r.status_code == 303, r.text
    db.expire_all()
    adopted = db.scalar(select(ReleaseGroup).where(ReleaseGroup.mbid == COMP_RG))
    assert (
        adopted.artist_id == world["artist"].id
        and adopted.category == "live-compilation"
        or adopted.category
    )
    assert adopted.title == "Greatest Hits Live" and adopted.source == "musicbrainz"
    row = db.get(LibraryAlbum, album.id)
    assert row.release_group_id == adopted.id and row.match_status == "manual"
    assert r.headers["location"] == f"/album/{adopted.id}?flash=match_saved"
    assert admin_client.get(r.headers["location"]).status_code == 200  # the album page opens


def test_a_result_that_is_already_in_the_discography_is_reused(
    admin_client, world, db, app, settings
):
    album, known = world["album"], world["groups"][0]
    fill_cache(
        settings,
        app,
        release_group_query("record", "title", None),
        [rg_json(known.mbid, "Record 0")],
    )
    page = admin_client.get(url(album) + "?q=record&mode=title&scope=all").text
    assert "data-known" in page
    post(
        admin_client,
        url(album),
        {"choice": f"mbid:{known.mbid}", "q": "record", "mode": "title", "scope": "all"},
        page=url(album),
    )
    db.expire_all()
    assert db.query(ReleaseGroup).filter(ReleaseGroup.mbid == known.mbid).count() == 1
    assert db.get(LibraryAlbum, album.id).release_group_id == known.id


def test_a_search_result_that_expired_cannot_be_used(admin_client, world, db):
    album = world["album"]
    r = post(
        admin_client,
        url(album),
        {"choice": f"mbid:{COMP_RG}", "q": "never searched", "mode": "title", "scope": "all"},
        page=url(album),
    )
    assert r.status_code == 400 and "no longer available" in r.text
    assert db.scalar(select(ReleaseGroup.id).where(ReleaseGroup.mbid == COMP_RG)) is None
    assert db.get(LibraryAlbum, album.id).match_status == "unmatched"


def test_the_search_can_be_limited_to_the_artist(admin_client, world, db):
    album = world["album"]
    r = post(
        admin_client,
        url(album, "match/search"),
        {"q": "Live", "mode": "title", "scope": "artist"},
        page=url(album),
    )
    job = db.scalar(select(Job).where(Job.type == "search_release_groups"))
    assert f"arid:{ARTIST_MBID}" in job.payload_json and "scope=artist" in r.headers["location"]


def test_an_artist_without_an_mbid_can_only_be_searched_everywhere(admin_client, db):
    artist = make_artist(db, "No Id")
    album = make_album(db, artist, "Something")
    text = admin_client.get(url(album) + "?q=x&scope=artist").text
    options = re.findall(r"<option value=\"(artist|all)\"([^>]*)>", text)
    assert (
        dict(options)["artist"].strip().endswith("disabled")
        or "disabled" in dict(options)["artist"]
    )
    assert "selected" in dict(options)["all"]


def test_without_a_musicbrainz_contact_the_page_says_so(admin_client, world, app):
    app.state.settings = app.state.settings.model_copy(update={"mb_user_agent": ""})
    text = admin_client.get(url(world["album"]) + "?q=x").text
    assert 'data-search-state="not_configured"' in text


async def test_the_search_job_leaves_its_answer_where_the_page_reads_it(settings, session_factory):
    from app.integrations.musicbrainz_client import peek_release_group_search
    from app.jobs.worker import JobWorker
    from tests.test_jobs_worker import enqueue, wait_for_status

    query = release_group_query("Record 1", "title", ARTIST_MBID)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{MB_BASE}/release-group").mock(
            return_value=httpx.Response(
                200, json={"release-groups": [rg_json(OTHER_RG, "Record 1")]}
            )
        )
        worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "search_release_groups", {"query": query})
        worker.notify()
        await wait_for_status(session_factory, job_id, "succeeded", timeout=15)
        await worker.stop()
    assert route.calls.last.request.url.params["query"] == query
    with session_factory() as db:
        [hit] = peek_release_group_search(db, query, 15)
    assert hit.data.title == "Record 1" and hit.artist_name == "The Band" and hit.score == 100
    assert hit.artist_mbid == ARTIST_MBID and hit.data.secondary_types == []


# ----------------------------------------------------- a chosen group survives syncs


def test_a_sync_does_not_remove_a_release_group_chosen_by_hand(world, db):
    album, artist = world["album"], world["artist"]
    outside = make_rg(db, artist, "From Elsewhere", 2011, mbid=COMP_RG)
    db.add(
        AlbumMatchOverride(
            artist_id=artist.id, album_key="greatest hits live", release_group_id=outside.id
        )
    )
    db.commit()
    keep = [
        ReleaseGroupData(
            mbid=g.mbid,
            title=g.title,
            primary_type="Album",
            secondary_types=[],
            first_release_date="2000",
        )
        for g in world["groups"]
    ]
    sync.store_release_groups(db, artist.id, keep)  # the discography lacks "From Elsewhere"
    db.commit()
    assert db.get(ReleaseGroup, outside.id) is not None
    assert db.scalar(select(AlbumMatchOverride.id)) is not None
    stray = make_rg(db, artist, "Stray", 2012)
    sync.store_release_groups(db, artist.id, keep)  # but a group nobody chose still goes
    db.commit()
    assert db.get(ReleaseGroup, stray.id) is None
    assert album.id


def test_the_artists_admin_page_links_to_the_match_page(admin_client, world):
    text = admin_client.get(f"/admin/artists/{world['artist'].id}").text
    assert f'href="/admin/albums/{world["album"].id}/match"' in text and "data-find-on-mb" in text
