"""Last.fm, ListenBrainz, iTunes and Deezer clients, album matching and the cache."""

import httpx
import pytest
import respx
from sqlalchemy import select

from app.integrations import ratelimit
from app.integrations.cached_http import CachedJsonClient, NotConfigured, ServiceError
from app.integrations.lastfm import LastFmClient
from app.integrations.listenbrainz import ListenBrainzClient
from app.integrations.listenbrainz import parse_similar as lb_parse
from app.integrations.previews import DeezerClient, ItunesClient, store_url
from app.models import ApiCache
from app.services.previews import best_candidate, title_core
from tests.discovery_data import DEEZER_OKC, ITUNES_OKC, NIRVANA, RADIOHEAD, THOM_YORKE, load

LASTFM_KEY = "test-lastfm-key-123"
UA = "Diskko/0.1 ( test@example.com )"
LASTFM = "https://ws.audioscrobbler.com/2.0/"
LB = "https://labs.api.listenbrainz.org/similar-artists/json"
ITUNES = "https://itunes.apple.com"
DEEZER = "https://api.deezer.com"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    for cls in (LastFmClient, ListenBrainzClient, ItunesClient, DeezerClient):
        monkeypatch.setattr(cls, "min_interval", 0)
    monkeypatch.setattr(CachedJsonClient, "BACKOFF_BASE", 0)
    ratelimit._LIMITERS.clear()
    yield
    ratelimit._LIMITERS.clear()


@pytest.fixture
def dsettings(settings):
    return settings.model_copy(update={"mb_user_agent": UA, "lastfm_api_key": LASTFM_KEY})


def cache_rows(factory):
    with factory() as db:
        return db.scalars(select(ApiCache)).all()


# ---------------------------------------------------------------------- Last.fm


async def test_lastfm_similar_artists(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(LASTFM).respond(json=load("lastfm_similar"))
        async with LastFmClient(dsettings, session_factory) as client:
            hits = await client.similar(mbid=RADIOHEAD, limit=5)
    assert [h.name for h in hits][:2] == ["Thom Yorke", "Atoms for Peace"]
    assert hits[0].mbid == THOM_YORKE and hits[0].score == 1.0
    assert 0 < hits[1].score < 1  # "match" arrives as a string
    request = route.calls.last.request
    assert request.url.params["method"] == "artist.getsimilar"
    assert request.url.params["mbid"] == RADIOHEAD and request.url.params["format"] == "json"
    assert request.url.params["api_key"] == LASTFM_KEY
    assert request.headers["User-Agent"] == UA


async def test_lastfm_key_never_reaches_the_cache(dsettings, session_factory):
    with respx.mock() as mock:
        mock.get(LASTFM).respond(json=load("lastfm_similar"))
        async with LastFmClient(dsettings, session_factory) as client:
            await client.similar(mbid=RADIOHEAD)
    rows = cache_rows(session_factory)
    assert rows and all(LASTFM_KEY not in r.key and LASTFM_KEY not in r.response for r in rows)


async def test_lastfm_answers_are_cached(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(LASTFM).respond(json=load("lastfm_similar"))
        async with LastFmClient(dsettings, session_factory) as client:
            first = await client.similar(mbid=RADIOHEAD)
            second = await client.similar(mbid=RADIOHEAD)
            forced = await client.similar(mbid=RADIOHEAD, force=True)
    assert first == second == forced and route.call_count == 2  # the forced one asked again


async def test_lastfm_unknown_artist_is_an_empty_list_remembered(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(LASTFM).respond(json=load("lastfm_error_6"))
        async with LastFmClient(dsettings, session_factory) as client:
            assert await client.similar(name="zzqxjv none") == []
            assert await client.similar(name="zzqxjv none") == []
    assert route.call_count == 1
    assert [r.status for r in cache_rows(session_factory)] == [404]


async def test_lastfm_invalid_key_is_a_permanent_error(dsettings, session_factory):
    body = {"message": "Invalid API key - You must be granted a valid key by last.fm", "error": 10}
    with respx.mock() as mock:
        route = mock.get(LASTFM).respond(403, json=body)
        async with LastFmClient(dsettings, session_factory) as client:
            with pytest.raises(ServiceError) as exc:
                await client.similar(name="Cher")
    assert route.call_count == 1  # no retries for a refused key
    assert LASTFM_KEY not in str(exc.value)


async def test_lastfm_temporary_errors_are_retried(dsettings, session_factory):
    with respx.mock() as mock:
        mock.get(LASTFM).mock(
            side_effect=[
                httpx.Response(200, json={"error": 29, "message": "Rate Limit Exceded"}),
                httpx.Response(503),
                httpx.Response(200, json=load("lastfm_similar")),
            ]
        )
        async with LastFmClient(dsettings, session_factory) as client:
            hits = await client.similar(name="Radiohead")
    assert len(hits) == 5


async def test_lastfm_gives_up_after_the_retries(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(LASTFM).respond(503)
        async with LastFmClient(dsettings, session_factory, max_retries=2) as client:
            with pytest.raises(ServiceError):
                await client.similar(name="Radiohead")
    assert route.call_count == 3


async def test_lastfm_network_errors_never_leak_the_url(dsettings, session_factory):
    with respx.mock() as mock:
        mock.get(LASTFM).mock(side_effect=httpx.ConnectError("boom"))
        async with LastFmClient(dsettings, session_factory, max_retries=1) as client:
            with pytest.raises(ServiceError) as exc:
                await client.similar(name="Radiohead")
    assert LASTFM_KEY not in str(exc.value) and "audioscrobbler" not in str(exc.value)


def test_lastfm_needs_a_key(settings, session_factory):
    with pytest.raises(NotConfigured):
        LastFmClient(settings, session_factory)


# ----------------------------------------------------------------- ListenBrainz


async def test_listenbrainz_similar_artists(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(LB).respond(json=load("listenbrainz_similar"))
        async with ListenBrainzClient(dsettings, session_factory) as client:
            hits = await client.similar(RADIOHEAD, limit=5)
    assert len(hits) == 5 and hits[0].name == "Nirvana" and hits[0].mbid == NIRVANA
    assert hits[0].score == 1.0 and all(0 < h.score <= 1 for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    params = route.calls.last.request.url.params
    assert params["artist_mbids"] == RADIOHEAD and params["algorithm"].startswith("session_based")
    assert route.calls.last.request.headers["User-Agent"] == UA


def test_listenbrainz_parsing_is_tolerant():
    rows = [
        {"artist_mbid": RADIOHEAD, "name": "Radiohead", "score": 100},  # the seed itself
        {"artist_mbid": "not-a-uuid", "name": "Broken", "score": 90},
        {"artist_mbid": NIRVANA, "name": "Nirvana", "score": "50"},
        {"artist_mbid": THOM_YORKE, "name": "", "score": 10},
        "garbage",
    ]
    hits = lb_parse([r for r in rows if isinstance(r, dict)], RADIOHEAD)
    assert [(h.name, h.score) for h in hits] == [("Nirvana", 1.0)]
    assert lb_parse(None) == [] and lb_parse({"error": 1}) == []


async def test_listenbrainz_needs_a_contact(settings, session_factory):
    with pytest.raises(NotConfigured):
        ListenBrainzClient(settings, session_factory)


async def test_listenbrainz_rate_limited_then_ok(dsettings, session_factory):
    with respx.mock() as mock:
        mock.get(LB).mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, json=load("listenbrainz_similar")),
            ]
        )
        async with ListenBrainzClient(dsettings, session_factory) as client:
            assert len(await client.similar(RADIOHEAD)) == 8


# ----------------------------------------------------------------------- iTunes


async def test_itunes_album_search_and_tracks(dsettings, session_factory):
    with respx.mock() as mock:
        search = mock.get(f"{ITUNES}/search").respond(json=load("itunes_album_search"))
        lookup = mock.get(f"{ITUNES}/lookup").respond(json=load("itunes_lookup_songs"))
        async with ItunesClient(dsettings, session_factory) as client:
            albums = await client.search_albums("Radiohead", "OK Computer")
            tracks = await client.album_tracks(ITUNES_OKC)
    assert albums[0].provider_id == ITUNES_OKC and albums[0].track_count == 12
    assert albums[0].year == 1997 and albums[0].url.startswith("https://")
    params = search.calls.last.request.url.params
    assert params["term"] == "Radiohead OK Computer" and params["media"] == "music"
    assert params["entity"] == "album" and params["country"] == "US"
    assert lookup.calls.last.request.url.params["entity"] == "song"
    assert len(tracks) == 12 and tracks[0].title == "Airbag" and tracks[0].position == 1
    assert tracks[0].url.startswith("https://audio-ssl.itunes.apple.com/")


async def test_itunes_results_are_cached(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(f"{ITUNES}/search").respond(json=load("itunes_album_search"))
        async with ItunesClient(dsettings, session_factory) as client:
            await client.search_albums("Radiohead", "OK Computer")
            await client.search_albums("Radiohead", "OK Computer")
    assert route.call_count == 1


async def test_itunes_skips_songs_without_a_preview(dsettings, session_factory):
    data = load("itunes_lookup_songs")
    for row in data["results"][1:4]:
        row.pop("previewUrl")
    with respx.mock() as mock:
        mock.get(f"{ITUNES}/lookup").respond(json=data)
        async with ItunesClient(dsettings, session_factory) as client:
            assert len(await client.album_tracks(ITUNES_OKC)) == 9


def test_store_links_must_be_the_providers_own_pages():
    assert store_url("https://music.apple.com/us/album/ok-computer/1097861387")
    assert store_url("https://www.deezer.com/album/14879699")
    for bad in (
        None,
        "",
        "http://music.apple.com/x",
        "https://evil.example/apple.com",
        "javascript:1",
    ):
        assert store_url(bad) is None


# ----------------------------------------------------------------------- Deezer


async def test_deezer_album_search_and_tracks(dsettings, session_factory):
    with respx.mock() as mock:
        search = mock.get(f"{DEEZER}/search/album").respond(json=load("deezer_album_search"))
        mock.get(f"{DEEZER}/album/{DEEZER_OKC}/tracks").respond(json=load("deezer_album_tracks"))
        async with DeezerClient(dsettings, session_factory) as client:
            albums = await client.search_albums("Radiohead", 'OK "Computer"')
            tracks = await client.album_tracks(DEEZER_OKC)
    assert albums[0].provider_id == DEEZER_OKC and albums[0].artist == "Radiohead"
    assert albums[0].track_count == 12 and albums[0].url.startswith("https://www.deezer.com/")
    assert search.calls.last.request.url.params["q"] == 'artist:"Radiohead" album:"OK Computer"'
    assert len(tracks) == 12 and tracks[0].position == 1 and "dzcdn.net" in tracks[0].url


async def test_deezer_no_data_error_is_an_empty_answer(dsettings, session_factory):
    with respx.mock() as mock:
        mock.get(f"{DEEZER}/search/album").respond(json=load("deezer_error"))  # HTTP 200!
        async with DeezerClient(dsettings, session_factory) as client:
            assert await client.search_albums("Nobody", "Nothing") == []


async def test_deezer_quota_errors_are_retried(dsettings, session_factory):
    quota = {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}
    with respx.mock() as mock:
        mock.get(f"{DEEZER}/search/album").mock(
            side_effect=[
                httpx.Response(200, json=quota),
                httpx.Response(200, json=load("deezer_album_search")),
            ]
        )
        async with DeezerClient(dsettings, session_factory) as client:
            assert len(await client.search_albums("Radiohead", "OK Computer")) == 3


async def test_deezer_other_errors_are_permanent(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(f"{DEEZER}/search/album").respond(
            json={"error": {"type": "OAuthException", "message": "x", "code": 300}}
        )
        async with DeezerClient(dsettings, session_factory) as client:
            with pytest.raises(ServiceError):
                await client.search_albums("A", "B")
    assert route.call_count == 1


async def test_deezer_preview_addresses_are_never_cached(dsettings, session_factory):
    """The signed addresses expire after about 15 minutes."""
    with respx.mock() as mock:
        route = mock.get(f"{DEEZER}/album/{DEEZER_OKC}/tracks").respond(
            json=load("deezer_album_tracks")
        )
        async with DeezerClient(dsettings, session_factory) as client:
            await client.album_tracks(DEEZER_OKC)
            await client.album_tracks(DEEZER_OKC)
    assert route.call_count == 2 and cache_rows(session_factory) == []


# --------------------------------------------------------------------- matching


def itunes_candidates():
    data = load("itunes_album_search")
    from app.integrations.discovery_types import AlbumCandidate

    return [
        AlbumCandidate(
            str(r["collectionId"]), r["collectionName"], r["artistName"], r["trackCount"]
        )
        for r in data["results"]
    ]


def test_the_right_album_is_chosen_and_look_alikes_are_not():
    best = best_candidate(itunes_candidates(), "Radiohead", "OK Computer", 12)
    assert best.provider_id == ITUNES_OKC
    # "(8-bit)", "OKNOTOK" and tribute albums are other records
    from app.integrations.discovery_types import AlbumCandidate

    others = [
        AlbumCandidate("1", "OK Computer (8-bit)", "Radiohead", 11),
        AlbumCandidate("2", "OK Computer OKNOTOK 1997 2017", "Radiohead", 23),
        AlbumCandidate("3", "OK Computer", "A Tribute Band", 12),
    ]
    assert best_candidate(others, "Radiohead", "OK Computer", 12) is None


def test_editions_of_the_same_album_match():
    from app.integrations.discovery_types import AlbumCandidate

    cands = [AlbumCandidate("9", "OK Computer (Deluxe Edition)", "Radiohead", 20)]
    assert best_candidate(cands, "Radiohead", "OK Computer")
    assert title_core("Abbey Road (Remastered 2019)") == title_core("Abbey Road")
    assert title_core("Abbey Road (Live)") != title_core("Abbey Road")


def test_track_count_breaks_ties():
    from app.integrations.discovery_types import AlbumCandidate

    cands = [
        AlbumCandidate("deluxe", "Nevermind", "Nirvana", 30),
        AlbumCandidate("plain", "Nevermind", "Nirvana", 12),
    ]
    assert best_candidate(cands, "Nirvana", "Nevermind", 12).provider_id == "plain"
    assert best_candidate([], "Nirvana", "Nevermind") is None
