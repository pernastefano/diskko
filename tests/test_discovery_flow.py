"""Search, similar artists and previews: the jobs, the pages and the whole path together."""

import asyncio
import copy
import re
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.integrations.musicbrainz_client import peek_artist_search
from app.jobs.worker import JobWorker
from app.models import (
    ACTIVE_STATUSES,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Artist,
    Job,
    LibraryAlbum,
    PreviewLookup,
    PreviewTrack,
    ReleaseGroup,
    SimilarArtist,
    utcnow,
)
from app.services import discovery as disc
from app.services.previews import preview_state, store_previews
from tests.conftest import post
from tests.dbhelpers import make_artist, make_rg
from tests.discovery_data import NIRVANA, RADIOHEAD, THOM_YORKE, load
from tests.mbdata import ARETHA, LADY_SOUL_RG
from tests.mbdata import load as mb_load
from tests.test_discovery_clients import no_waiting  # noqa: F401  (autouse fixture)

MB = "https://musicbrainz.test/ws/2"
LASTFM = "https://ws.audioscrobbler.com/2.0/"
LB = "https://labs.api.listenbrainz.org/similar-artists/json"
ITUNES = "https://itunes.apple.com"
DEEZER = "https://api.deezer.com"
UA = "Diskko/0.1 ( test@example.com )"


@pytest.fixture
def settings(settings):
    return settings.model_copy(
        update={
            "mb_user_agent": UA,
            "musicbrainz_url": MB,
            "lastfm_api_key": "test-lastfm-key-123",
            "job_poll_interval": 0.02,
        }
    )


# ---------------------------------------------------------------------- helpers


def drain(app, timeout=30.0):
    """Run the queued jobs (the app under test has no workers) until none is due."""

    async def run():
        worker = JobWorker(app.state.settings, app.state.session_factory)
        await worker.start()
        worker.notify()
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            with app.state.session_factory() as db:
                due = db.scalar(
                    select(func.count(Job.id)).where(
                        (Job.status == STATUS_RUNNING)
                        | (
                            (Job.status == STATUS_QUEUED)
                            & (Job.next_run_at.is_(None) | (Job.next_run_at <= utcnow()))
                        )
                    )
                )
            if not due:
                break
            await asyncio.sleep(0.05)
        await worker.stop()

    asyncio.run(run())


def jobs(db, type_=None):
    stmt = select(Job).order_by(Job.id)
    if type_:
        stmt = stmt.where(Job.type == type_)
    db.expire_all()
    return db.scalars(stmt).all()


def aretha_discography():
    data = copy.deepcopy(mb_load("rg_browse_aretha_p0.json"))
    data["release-group-count"] = len(data["release-groups"])  # one page only
    return data


def mount_musicbrainz(mock):
    mock.get(f"{MB}/artist").respond(json=mb_load("artist_search_aretha.json"))
    mock.get(f"{MB}/release-group").respond(json=aretha_discography())


def itunes_for_lady_soul(mock):
    album = {
        "wrapperType": "collection",
        "collectionType": "Album",
        "collectionId": 111,
        "collectionName": "Lady Soul",
        "artistName": "Aretha Franklin",
        "trackCount": 2,
        "releaseDate": "1968-01-22T08:00:00Z",
        "collectionViewUrl": "https://music.apple.com/us/album/lady-soul/111",
    }
    songs = [
        {
            "wrapperType": "track",
            "kind": "song",
            "trackId": 1000 + n,
            "trackName": name,
            "trackNumber": n,
            "discNumber": 1,
            "previewUrl": f"https://audio-ssl.itunes.apple.com/p/{n}.m4a",
        }
        for n, name in ((1, "Chain of Fools"), (2, "Money Won't Change You"))
    ]
    mock.get(f"{ITUNES}/search").respond(json={"resultCount": 1, "results": [album]})
    mock.get(f"{ITUNES}/lookup").respond(json={"resultCount": 3, "results": [album, *songs]})


def lady_soul_id(db):
    return db.scalar(select(ReleaseGroup.id).where(ReleaseGroup.mbid == LADY_SOUL_RG))


def seed_search(db, query="Aretha"):
    """Put an artist search into api_cache the way the job does."""
    import json

    from app.integrations.musicbrainz_client import ApiCache, search_cache_key

    db.add(
        ApiCache(
            key=search_cache_key(query, 15),
            status=200,
            response=json.dumps(mb_load("artist_search_aretha.json")),
            fetched_at=utcnow(),
            ttl_seconds=3600,
        )
    )
    db.commit()


# ------------------------------------------------------------- the whole path


def test_search_an_unowned_artist_then_see_albums_similar_artists_and_a_preview(
    app, admin_client, db
):
    make_artist(db, "Thom Yorke", mbid=THOM_YORKE)  # owned: the similar list must say so
    similar = load("lastfm_similar")
    similar["similarartists"]["artist"][0]["mbid"] = THOM_YORKE

    with respx.mock(assert_all_called=False) as mock:
        mount_musicbrainz(mock)
        mock.get(LASTFM).respond(json=similar)
        itunes_for_lady_soul(mock)

        # 1. the search page asks for a job and shows that it is searching
        r = admin_client.get("/search?q=Aretha")
        assert r.status_code == 200 and 'data-search-state="searching"' in r.text
        assert [j.type for j in jobs(db)] == ["search_artists"]
        drain(app)

        # 2. the results: the artist is not in the library
        text = admin_client.get("/search/results?q=Aretha").text
        assert 'data-search-state="results"' in text and "Aretha Franklin" in text
        assert f'data-artist-mbid="{ARETHA}"' in text and "data-in-library" not in text

        # 3. opening it creates the external artist and syncs its discography
        r = post(admin_client, "/search/open", {"q": "Aretha", "mbid": ARETHA}, page="/search")
        assert r.status_code == 303
        artist = db.scalar(select(Artist).where(Artist.mbid == ARETHA))
        assert r.headers["location"] == f"/artist/{artist.id}"
        assert artist.in_library is False
        assert {j.type for j in jobs(db)} >= {"sync_artist_metadata", "fetch_similar_artists"}
        drain(app)

        # 4. the artist page: external, with every release group as not owned
        text = admin_client.get(f"/artist/{artist.id}?all=1").text
        assert "data-external" in text and "Lady Soul" in text
        assert db.scalar(select(func.count(LibraryAlbum.id))) == 0

        # 5. similar artists, with the "in library" badge on the one we own
        sim = post(admin_client, f"/artist/{artist.id}/similar", page=f"/artist/{artist.id}")
        assert 'data-similar-state="ready"' in sim.text and "Atoms for Peace" in sim.text
        owned = re.search(r'data-similar="Thom Yorke".*?</li>', sim.text, re.S).group(0)
        assert "data-in-library" in owned
        assert "data-in-library" not in re.search(
            r'data-similar="Atoms for Peace".*?</li>', sim.text, re.S
        ).group(0)

        # 6. the album page: the tracks from MusicBrainz and a preview where one exists
        rg_id = lady_soul_id(db)
        mock.get(f"{MB}/release").respond(json=mb_load("releases_lady_soul.json"))
        first = post(admin_client, f"/album/{rg_id}/tracks", page=f"/artist/{artist.id}")
        assert "data-loading" in first.text
        drain(app)
        player = admin_client.get(f"/album/{rg_id}/tracks").text
        assert player.count('data-source="preview"') == 2 and player.count("<audio") == 2
        assert "Chain of Fools" in player and "courtesy of iTunes" in player
        assert "https://music.apple.com/us/album/lady-soul/111" in player


# ------------------------------------------------------------------------ search


def test_short_or_empty_searches_do_nothing(admin_client, db):
    for q in ("", "a", "  "):
        assert "data-search-state" not in admin_client.get(f"/search?q={q}").text
    assert jobs(db) == []


def test_search_needs_the_musicbrainz_contact(settings, admin_client, app, db):
    app.state.settings = settings.model_copy(update={"mb_user_agent": ""})
    r = admin_client.get("/search?q=Aretha")
    assert 'data-search-state="not_configured"' in r.text and jobs(db) == []


def test_a_cached_search_answers_without_a_job(admin_client, db):
    seed_search(db)
    text = admin_client.get("/search?q=Aretha").text
    assert 'data-search-state="results"' in text and jobs(db) == []


def test_searching_twice_queues_one_job(admin_client, db):
    admin_client.get("/search?q=Aretha")
    admin_client.get("/search?q=Aretha")
    admin_client.get("/search/results?q=Aretha")
    assert len(jobs(db, "search_artists")) == 1


def test_a_failed_search_is_reported_and_can_be_retried(admin_client, db):
    admin_client.get("/search?q=Aretha")
    job = jobs(db, "search_artists")[0]
    job.status, job.finished_at, job.error = STATUS_FAILED, utcnow(), "boom"
    db.commit()
    text = admin_client.get("/search?q=Aretha").text
    assert 'data-search-state="failed"' in text and "retry=1" in text
    assert len(jobs(db, "search_artists")) == 1  # not queued again in a loop
    assert 'data-search-state="searching"' in admin_client.get("/search?q=Aretha&retry=1").text
    assert len(jobs(db, "search_artists")) == 2


def test_the_search_job_fills_the_cache(app, admin_client, db):
    with respx.mock() as mock:
        mock.get(f"{MB}/artist").respond(json=mb_load("artist_search_aretha.json"))
        admin_client.get("/search?q=Aretha")
        drain(app)
    hits = peek_artist_search(db, "Aretha", 15)
    assert [h.name for h in hits] == ["Aretha Franklin"]
    assert jobs(db, "search_artists")[0].status == "succeeded"


def test_results_mark_artists_already_in_the_library(admin_client, db):
    make_artist(db, "Aretha Franklin", mbid=ARETHA)
    seed_search(db)
    text = admin_client.get("/search?q=Aretha").text
    assert "data-in-library" in text and 'action="/search/open"' not in text


def test_opening_uses_the_cached_result_not_the_form(admin_client, db):
    seed_search(db)
    r = post(
        admin_client,
        "/search/open",
        {"q": "Aretha", "mbid": ARETHA, "name": "Hacked Name"},
        page="/search",
    )
    assert r.status_code == 303
    assert db.scalar(select(Artist.name).where(Artist.mbid == ARETHA)) == "Aretha Franklin"


def test_opening_an_unknown_mbid_goes_back_to_the_search(admin_client, db):
    seed_search(db)
    for mbid in (NIRVANA, "not-an-mbid"):
        r = post(admin_client, "/search/open", {"q": "Aretha", "mbid": mbid}, page="/search")
        assert r.status_code == 303 and r.headers["location"].startswith("/search?q=")
    assert db.scalar(select(func.count(Artist.id))) == 0  # nothing was created


def test_opening_twice_reuses_the_artist(admin_client, db):
    seed_search(db)
    for _ in range(2):
        post(admin_client, "/search/open", {"q": "Aretha", "mbid": ARETHA}, page="/search")
    assert db.scalar(select(func.count(Artist.id))) == 1
    assert len(jobs(db, "sync_artist_metadata")) == 1


def test_discovery_pages_need_a_login_and_a_csrf_token(client, admin_client, db):
    seed_search(db)
    assert admin_client.post("/search/open", data={"mbid": ARETHA}).status_code == 403
    assert admin_client.post("/album/1/tracks").status_code == 403
    assert admin_client.post("/artist/1/similar").status_code == 403


def test_discovery_pages_redirect_anonymous_visitors(admin_client, new_client):
    anon = new_client()
    for path in (
        "/search",
        "/search/results",
        "/artist/1/similar",
        "/album/1",
        "/album/1/tracks",
        "/stream/1",
    ):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_ordinary_users_can_search_and_preview(user_client, db):
    seed_search(db)
    assert 'data-search-state="results"' in user_client.get("/search?q=Aretha").text


# ---------------------------------------------------------------- similar artists


def make_external(db):
    artist = make_artist(db, "Radiohead", mbid=RADIOHEAD, in_library=False)
    return artist


def test_similar_artists_load_through_a_job(app, admin_client, db):
    artist = make_external(db)
    first = post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    assert 'data-similar-state="loading"' in first.text and 'hx-trigger="every 3s"' in first.text
    again = post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    assert 'data-similar-state="loading"' in again.text
    assert len(jobs(db, "fetch_similar_artists")) == 1
    with respx.mock() as mock:
        route = mock.get(LASTFM).respond(json=load("lastfm_similar"))
        drain(app)
    assert route.calls.last.request.url.params["mbid"] == RADIOHEAD
    text = admin_client.get(f"/artist/{artist.id}/similar").text
    assert 'data-similar-state="ready"' in text and "Thom Yorke" in text and "Last.fm" in text
    rows = db.scalars(select(SimilarArtist).where(SimilarArtist.artist_id == artist.id)).all()
    assert len(rows) == 5 and {r.source for r in rows} == {"lastfm"}


def test_listenbrainz_steps_in_when_lastfm_has_nothing(app, admin_client, db):
    artist = make_external(db)
    post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    with respx.mock() as mock:
        mock.get(LASTFM).respond(json=load("lastfm_error_6"))
        mock.get(LB).respond(json=load("listenbrainz_similar"))
        drain(app)
    rows = db.scalars(select(SimilarArtist).where(SimilarArtist.artist_id == artist.id)).all()
    assert len(rows) == 8 and {r.source for r in rows} == {"listenbrainz"}
    assert "ListenBrainz" in admin_client.get(f"/artist/{artist.id}/similar").text


def test_listenbrainz_steps_in_when_the_lastfm_key_is_refused(app, admin_client, db):
    artist = make_external(db)
    post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    with respx.mock() as mock:
        mock.get(LASTFM).respond(403, json={"error": 10, "message": "Invalid API key"})
        mock.get(LB).respond(json=load("listenbrainz_similar"))
        drain(app)
    job = jobs(db, "fetch_similar_artists")[0]
    assert job.status == "succeeded"
    from app.models import JobLog

    messages = [m for (m,) in db.execute(select(JobLog.message).where(JobLog.job_id == job.id))]
    assert any("Last.fm failed" in m for m in messages)
    assert not any("test-lastfm-key-123" in m for m in messages)


def test_without_lastfm_the_fallback_is_used(settings, app, admin_client, db):
    app.state.settings = settings.model_copy(update={"lastfm_api_key": ""})
    artist = make_external(db)
    post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    with respx.mock() as mock:
        mock.get(LB).respond(json=load("listenbrainz_similar"))
        drain(app)
    assert db.scalar(select(func.count(SimilarArtist.id))) == 8


def test_nothing_found_anywhere_is_remembered(app, admin_client, db):
    artist = make_external(db)
    post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    with respx.mock() as mock:
        mock.get(LASTFM).respond(json=load("lastfm_error_6"))
        mock.get(LB).respond(json=[])
        drain(app)
    text = post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard").text
    assert 'data-similar-state="empty"' in text
    assert len(jobs(db, "fetch_similar_artists")) == 1  # fresh: not asked again


def test_when_every_provider_fails_the_job_is_retried_not_stored(app, admin_client, db):
    artist = make_external(db)
    post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    with respx.mock() as mock:
        mock.get(LASTFM).respond(503)
        mock.get(LB).respond(503)
        drain(app)
    job = jobs(db, "fetch_similar_artists")[0]
    assert job.status == STATUS_QUEUED and job.next_run_at is not None  # a retry is scheduled
    assert db.scalar(select(func.count(SimilarArtist.id))) == 0


def test_similar_artists_need_a_provider(settings, app, admin_client, db):
    app.state.settings = settings.model_copy(update={"lastfm_api_key": "", "mb_user_agent": ""})
    artist = make_external(db)
    text = post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard").text
    assert 'data-similar-state="unavailable"' in text and jobs(db) == []


def test_a_failed_similar_job_is_reported(admin_client, db):
    artist = make_external(db)
    post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard")
    job = jobs(db, "fetch_similar_artists")[0]
    job.status, job.finished_at = STATUS_FAILED, utcnow()
    db.commit()
    text = post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard").text
    assert 'data-similar-state="failed"' in text
    retried = post(admin_client, f"/artist/{artist.id}/similar?retry=1", page="/dashboard").text
    assert 'data-similar-state="loading"' in retried


def test_the_similar_list_links_and_opens_artists(admin_client, db):
    artist = make_external(db)
    owned = make_artist(db, "Atoms for Peace", in_library=True)  # matched by name only
    hits = load("lastfm_similar")["similarartists"]["artist"]
    from app.integrations.lastfm import parse_similar

    disc.store_similar(db, artist.id, parse_similar({"similarartists": {"artist": hits}}), "lastfm")
    db.commit()
    text = admin_client.get(f"/artist/{artist.id}/similar").text
    assert f'href="/artist/{owned.id}"' in text  # known artist: a plain link
    assert f'action="/artist/{artist.id}/similar/open"' in text  # unknown, with MBID: opens it
    r = post(
        admin_client, f"/artist/{artist.id}/similar/open", {"mbid": THOM_YORKE}, page="/dashboard"
    )
    new = db.scalar(select(Artist).where(Artist.mbid == THOM_YORKE))
    assert r.status_code == 303 and r.headers["location"] == f"/artist/{new.id}"
    assert new.in_library is False and new.name == "Thom Yorke"
    # only artists of this list can be opened
    other = post(
        admin_client, f"/artist/{artist.id}/similar/open", {"mbid": NIRVANA}, page="/dashboard"
    )
    assert other.status_code == 404


def test_a_similar_artist_without_an_mbid_links_to_a_search(admin_client, db):
    from app.integrations.discovery_types import SimilarHit

    artist = make_external(db)
    disc.store_similar(db, artist.id, [SimilarHit("Mystery Band", None, 0.4)], "lastfm")
    db.commit()
    assert (
        'href="/search?q=Mystery%20Band"' in admin_client.get(f"/artist/{artist.id}/similar").text
    )


def test_store_similar_drops_the_artist_itself_duplicates_and_extras(db):
    from app.integrations.discovery_types import SimilarHit

    artist = make_external(db)
    hits = [SimilarHit("Radiohead", RADIOHEAD, 1.0), SimilarHit("Muse", "m" * 8, 0.5)]
    hits += [SimilarHit("Muse", None, 0.4), SimilarHit("Muse", None, 0.3)]
    hits += [SimilarHit(f"Band {i}", None, 0.1) for i in range(50)]
    kept = disc.store_similar(db, artist.id, hits, "lastfm")
    db.commit()
    assert kept == disc.SIMILAR_MAX
    names = [r.name for r in db.scalars(select(SimilarArtist).order_by(SimilarArtist.position))]
    assert "Radiohead" not in names and names.count("Muse") == 2  # one by mbid, one by name
    assert disc.similar_is_fresh(db, artist.id)
    later = disc.SIMILAR_REFRESH + timedelta(seconds=1)
    assert not disc.similar_is_fresh(db, artist.id, now=lambda: utcnow() + later)


def test_stale_lists_are_shown_and_refreshed_quietly(admin_client, db):
    from app.integrations.discovery_types import SimilarHit
    from app.models import SimilarFetch

    artist = make_external(db)
    disc.store_similar(db, artist.id, [SimilarHit("Muse", None, 0.5)], "lastfm")
    db.get(SimilarFetch, artist.id).fetched_at = utcnow() - timedelta(days=30)
    db.commit()
    text = post(admin_client, f"/artist/{artist.id}/similar", page="/dashboard").text
    assert 'data-similar-state="ready"' in text and "Muse" in text
    assert len(jobs(db, "fetch_similar_artists")) == 1  # the refresh


# ------------------------------------------------------------------------ previews


def album_with_group(db, title="Lady Soul"):
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
    return artist, make_rg(db, artist, title, 1968)


def tracks_of(client, group_id, **kw):
    """What the album page asks when it opens: queue what is missing, return the tracks."""
    return post(client, f"/album/{group_id}/tracks", page="/dashboard", **kw)


def mount_releases(mock, empty=False):
    mock.get(f"{MB}/release").respond(
        json={"releases": []} if empty else mb_load("releases_lady_soul.json")
    )


def test_previews_are_looked_up_by_a_job_and_shown_on_the_tracks(app, admin_client, db):
    _, group = album_with_group(db)
    with respx.mock(assert_all_called=False) as mock:
        itunes_for_lady_soul(mock)
        mount_releases(mock)
        deezer = mock.get(url__startswith=DEEZER)
        tracks_of(admin_client, group.id)
        tracks_of(admin_client, group.id)
        drain(app)
        assert deezer.call_count == 0  # iTunes had it: Deezer is only a fallback
    assert len(jobs(db, "resolve_previews")) == 1 and len(jobs(db, "fetch_tracklist")) == 1
    state = preview_state(db, group.id)
    assert state.state == "ready" and state.provider == "itunes" and len(state.tracks) == 2
    text = admin_client.get(f"/album/{group.id}/tracks").text
    assert text.count('data-track="') == 10  # every track of the album, not just the previewed
    assert text.count('data-source="preview"') == 2 and text.count("<audio") == 2
    assert "https://audio-ssl.itunes.apple.com/p/1.m4a" in text
    assert "Chain of Fools" in text and "People Get Ready" in text and "Niki Hoeky" in text


def test_deezer_is_the_fallback_when_itunes_has_no_such_album(app, admin_client, db):
    _, group = album_with_group(db, "OK Computer")
    db.get(Artist, group.artist_id).name = "Radiohead"
    db.commit()
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock, empty=True)  # MusicBrainz lists no tracks: the previews are shown
        mock.get(f"{ITUNES}/search").respond(json={"resultCount": 0, "results": []})
        mock.get(f"{DEEZER}/search/album").respond(json=load("deezer_album_search"))
        mock.get(f"{DEEZER}/album/14879699/tracks").respond(json=load("deezer_album_tracks"))
        tracks_of(admin_client, group.id)
        drain(app)
    state = preview_state(db, group.id)
    assert state.state == "ready" and state.provider == "deezer" and len(state.tracks) == 12
    text = admin_client.get(f"/album/{group.id}/tracks").text
    assert text.count('data-source="preview"') == 12
    assert "courtesy of Deezer" in text and "https://www.deezer.com/" in text


def test_an_album_nobody_has_is_marked_and_not_asked_again(app, admin_client, db):
    _, group = album_with_group(db, "Obscure Demo Tape")
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock)
        mock.get(f"{ITUNES}/search").respond(json={"resultCount": 0, "results": []})
        mock.get(f"{DEEZER}/search/album").respond(json={"data": [], "total": 0})
        tracks_of(admin_client, group.id)
        drain(app)
    assert preview_state(db, group.id).state == "missing"
    text = tracks_of(admin_client, group.id).text
    assert "data-no-previews" in text and 'data-source="preview"' not in text
    assert len(jobs(db, "resolve_previews")) == 1


def test_a_wrong_album_is_never_used_as_a_preview(app, admin_client, db):
    _, group = album_with_group(db, "Lady Soul")
    wrong = {
        "wrapperType": "collection",
        "collectionId": 5,
        "collectionName": "Lady Soul (Karaoke Version)",
        "artistName": "Some Cover Band",
        "trackCount": 10,
    }
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock)
        mock.get(f"{ITUNES}/search").respond(json={"resultCount": 1, "results": [wrong]})
        mock.get(f"{DEEZER}/search/album").respond(json={"data": []})
        tracks_of(admin_client, group.id)
        drain(app)
    assert preview_state(db, group.id).state == "missing"
    assert db.scalar(select(func.count(PreviewTrack.id))) == 0


def test_providers_that_only_fail_do_not_mark_the_album_missing(app, admin_client, db):
    _, group = album_with_group(db)
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock)
        mock.get(f"{ITUNES}/search").respond(503)
        mock.get(f"{DEEZER}/search/album").respond(503)
        tracks_of(admin_client, group.id)
        drain(app)
    assert db.get(PreviewLookup, group.id) is None  # nothing stored: a retry is scheduled
    assert jobs(db, "resolve_previews")[0].next_run_at is not None


def test_one_provider_failing_still_lets_the_other_answer(app, admin_client, db):
    _, group = album_with_group(db, "OK Computer")
    db.get(Artist, group.artist_id).name = "Radiohead"
    db.commit()
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock, empty=True)
        mock.get(f"{ITUNES}/search").mock(side_effect=httpx.ConnectError("down"))
        mock.get(f"{DEEZER}/search/album").respond(json=load("deezer_album_search"))
        mock.get(f"{DEEZER}/album/14879699/tracks").respond(json=load("deezer_album_tracks"))
        tracks_of(admin_client, group.id)
        drain(app)
    assert preview_state(db, group.id).provider == "deezer"


def test_deezer_addresses_expire_and_are_looked_up_again(db, admin_client):
    from app.integrations.discovery_types import PreviewData

    _, group = album_with_group(db)
    store_previews(db, group.id, "deezer", "9", [PreviewData(1, 1, "Song", "1", "https://x/1.mp3")])
    db.commit()
    assert preview_state(db, group.id).state == "ready"
    later = utcnow() + timedelta(minutes=11)
    assert preview_state(db, group.id, now=lambda: later).state == "stale"
    # an iTunes address does not expire for a month
    store_previews(db, group.id, "itunes", "9", [PreviewData(1, 1, "Song", "1", "https://x/1.m4a")])
    db.commit()
    assert preview_state(db, group.id, now=lambda: later).state == "ready"
    assert preview_state(db, group.id, now=lambda: utcnow() + timedelta(days=31)).state == "stale"


def test_a_stale_lookup_is_resolved_again_with_force(db, admin_client):
    from app.integrations.discovery_types import PreviewData

    _, group = album_with_group(db)
    store_previews(db, group.id, "deezer", "9", [PreviewData(1, 1, "Song", "1", "https://x/1.mp3")])
    lookup = db.get(PreviewLookup, group.id)
    lookup.checked_at = utcnow() - timedelta(minutes=30)
    db.commit()
    text = tracks_of(admin_client, group.id).text
    assert "data-loading" in text
    assert '"force": true' in jobs(db, "resolve_previews")[0].payload_json


def test_reloading_an_expired_preview_asks_again(db, admin_client):
    from app.integrations.discovery_types import PreviewData

    _, group = album_with_group(db)
    store_previews(db, group.id, "deezer", "9", [PreviewData(1, 1, "Song", "1", "https://x/1.mp3")])
    db.commit()
    assert jobs(db, "resolve_previews") == []  # the lookup is still fresh
    post(admin_client, f"/album/{group.id}/tracks?refresh=1", page="/dashboard")
    assert '"force": true' in jobs(db, "resolve_previews")[0].payload_json


def test_a_failed_preview_job_is_reported_and_not_retried_in_a_loop(admin_client, db):
    _, group = album_with_group(db)
    from app.services.tracklists import store_tracklist

    tracks_of(admin_client, group.id)
    job = jobs(db, "resolve_previews")[0]
    job.status, job.finished_at = STATUS_FAILED, utcnow()
    store_tracklist(db, group.id, None)  # the track list is settled: only the previews failed
    db.commit()
    assert "data-failed" in tracks_of(admin_client, group.id).text
    assert len(jobs(db, "resolve_previews")) == 1
    post(admin_client, f"/album/{group.id}/tracks?refresh=1", page="/dashboard")
    assert len(jobs(db, "resolve_previews")) == 2  # "reload" tries again


def test_unknown_albums_are_404(admin_client):
    assert tracks_of(admin_client, 9999).status_code == 404
    assert admin_client.get("/album/9999/tracks").status_code == 404
    assert post(admin_client, "/artist/9999/similar", page="/dashboard").status_code == 404


# -------------------------------------------------------------------- housekeeping


def test_forgotten_external_artists_are_purged(db):
    old = utcnow() - timedelta(days=100)
    forgotten = make_artist(db, "Forgotten", mbid=ARETHA, in_library=False)
    forgotten.mb_source, forgotten.created_at = disc.SOURCE_SEARCH, old
    recent = make_artist(db, "Recent", mbid=NIRVANA, in_library=False)
    recent.mb_source = disc.SOURCE_SEARCH
    owner = make_artist(db, "Owns Albums", in_library=False)
    owner.mb_source, owner.created_at = disc.SOURCE_SEARCH, old
    lib = make_artist(db, "Library", in_library=True)
    lib.created_at = old
    from tests.dbhelpers import make_album

    make_album(db, owner, "Kept")
    db.commit()
    disc.store_similar(db, forgotten.id, [], None)
    db.commit()
    assert disc.purge_external_artists(db, 90) == 1
    names = {a.name for a in db.scalars(select(Artist))}
    assert names == {"Recent", "Owns Albums", "Library"}


def test_the_nav_has_a_search_link(admin_client, user_client):
    for c in (admin_client, user_client):
        assert 'href="/search"' in c.get("/dashboard").text


def test_active_statuses_cover_queued_and_running():
    assert STATUS_QUEUED in ACTIVE_STATUSES and STATUS_RUNNING in ACTIVE_STATUSES
