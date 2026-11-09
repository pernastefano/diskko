import re
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import inspect, select, text

from app.db import make_engine, run_migrations
from app.integrations import ratelimit
from app.integrations.cached_http import CachedJsonClient
from app.integrations.lastfm import LastFmClient, parse_bio, parse_top_tracks
from app.integrations.previews import DeezerClient, ItunesClient
from app.integrations.wikipedia import WikidataClient, WikipediaClient, wikidata_id
from app.jobs.worker import JobWorker
from app.models import Artist, ArtistBio, ArtistTopTrack, Job, utcnow
from app.services.artist_profile import (
    list_top_tracks,
    pick_deezer_artist,
    preview_alive,
    short_bio,
    top_state,
)
from tests.conftest import MB_BASE, post
from tests.dbhelpers import make_artist
from tests.test_jobs_worker import enqueue, logs, wait_for_status

MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
DEEZER = "https://api.deezer.com"
ITUNES = "https://itunes.apple.com"
LASTFM = "https://ws.audioscrobbler.com/2.0/"
WIKIDATA = "https://www.wikidata.org/w/api.php"
PREVIEW = "https://cdnt-preview.dzcdn.net/api/1/1/a/b/c/x.mp3?hdnea=exp=1~hmac=abc"
IMG = "https://cdn-images.dzcdn.net/images/artist/abc/250x250-000000-80-0-0.jpg"
KEY = "lastfm-test-key"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    for cls in (LastFmClient, ItunesClient, DeezerClient, WikidataClient, WikipediaClient):
        monkeypatch.setattr(cls, "min_interval", 0)
    monkeypatch.setattr(CachedJsonClient, "BACKOFF_BASE", 0)
    ratelimit._LIMITERS.clear()
    yield
    ratelimit._LIMITERS.clear()


@pytest.fixture
def psettings(mbsettings):
    return mbsettings.model_copy(update={"lastfm_api_key": KEY})


def make(factory, name="Radiohead", mbid=MBID, **fields):
    with factory() as db:
        artist = make_artist(db, name, mbid=mbid, status="resolved" if mbid else "pending")
        for key, value in fields.items():
            setattr(artist, key, value)
        db.commit()
        return artist.id


async def run(settings, factory, job_type, artist_id, **payload):
    worker = JobWorker(settings, factory)
    await worker.start()
    job_id = enqueue(factory, job_type, {"artist_id": artist_id, **payload})
    worker.notify()
    return worker, job_id


# ---------------------------------------------------------------- the short text


def test_the_biography_is_cut_to_two_short_sentences():
    long = (
        "Radiohead are an English rock band formed in Abingdon, Oxfordshire, in 1985. "
        "The band consists of Thom Yorke, Jonny Greenwood, Colin Greenwood, Ed O'Brien and "
        "Philip Selway. They are known for experimentation."
    )
    assert short_bio(long) == " ".join(long.split(". ")[:1]) + ". " + (
        "The band consists of Thom Yorke, Jonny Greenwood, Colin Greenwood, Ed O'Brien and "
        "Philip Selway."
    )
    assert short_bio("Short one.") == "Short one." and short_bio("") == ""
    assert short_bio("Dr. Dre is a rapper. He was born in 1965. Another one.").endswith("1965.")
    assert short_bio("A. B. Smith è un musicista. Nato a Roma, suona. Poi altro.").startswith(
        "A. B. Smith è un musicista. Nato a Roma"
    )
    one_huge = short_bio("word " * 300)
    assert len(one_huge) <= 301 and one_huge.endswith("…")


def test_only_the_exact_name_is_taken_as_the_deezer_artist():
    from app.integrations.discovery_types import ArtistCandidate

    found = [
        ArtistCandidate("Radiohead Tribute Band", None, 9999, "1"),
        ArtistCandidate("Radiohead", None, 10, "2"),
        ArtistCandidate("radiohead", None, 500, "3"),
        ArtistCandidate("Radiohead", None, 50000, None),
    ]
    assert pick_deezer_artist(found, "Radiohead").provider_id == "3"
    assert pick_deezer_artist(found[:1], "Radiohead") is None


# ---------------------------------------------------------------------- parsers


def test_the_wikidata_id_comes_from_the_musicbrainz_relations():
    rels = [
        {"type": "official homepage", "url": {"resource": "https://example.org"}},
        {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q44190"}},
    ]
    assert wikidata_id(rels) == "Q44190"
    assert wikidata_id([{"type": "wikidata", "url": {"resource": "https://x/y/notq"}}]) is None
    assert wikidata_id([]) is None


def test_lastfm_bio_loses_its_html_and_the_read_more_link():
    data = {
        "artist": {
            "url": "https://www.last.fm/music/Radiohead",
            "bio": {
                "summary": 'Radiohead are a band &amp; more. <a href="https://www.last.fm/'
                'music/Radiohead">Read more on Last.fm</a>'
            },
        }
    }
    bio = parse_bio(data)
    assert bio.text == "Radiohead are a band & more." and bio.url.startswith("https://www.last.fm/")
    assert parse_bio({"artist": {"bio": {"summary": ""}}}) is None
    evil = {"artist": {"url": "https://evil.example/x", "bio": {"summary": "Text."}}}
    assert parse_bio(evil).url is None  # only a Last.fm address is ever linked


def test_lastfm_top_tracks_keep_the_rank_order():
    data = {
        "toptracks": {
            "track": [
                {"name": "Creep", "url": "https://www.last.fm/music/x/_/Creep"},
                {"name": "Karma Police"},
            ]
        }
    }
    assert [t.title for t in parse_top_tracks(data)] == ["Creep", "Karma Police"]
    one = {"toptracks": {"track": {"name": "Only"}}}  # one result may come as a bare object
    assert [t.title for t in parse_top_tracks(one)] == ["Only"]
    assert parse_top_tracks({}) == []


# ---------------------------------------------------------------------- clients


async def test_deezer_top_tracks_are_not_cached_and_keep_their_preview(settings, session_factory):
    payload = {
        "data": [
            {
                "id": 1,
                "title": "Creep",
                "duration": 238,
                "preview": PREVIEW,
                "link": "https://www.deezer.com/track/1",
                "album": {"title": "Pablo Honey"},
            },
            {"id": 2, "title": "No preview", "duration": 100, "preview": "http://evil.example/x"},
        ],
        "total": 2,
    }
    with respx.mock() as mock:
        route = mock.get(f"{DEEZER}/artist/399/top").respond(json=payload)
        async with DeezerClient(settings, session_factory) as client:
            tracks = await client.artist_top("399", limit=10)
            await client.artist_top("399", limit=10)
            with pytest.raises(ValueError):
                await client.artist_top("../x")
    assert route.call_count == 2  # signed addresses expire: never cached
    assert tracks[0].title == "Creep" and tracks[0].preview_url == PREVIEW
    assert tracks[0].album == "Pablo Honey" and tracks[0].duration_s == 238
    assert tracks[1].preview_url is None  # not an https address at an audio host
    assert route.calls[0].request.url.params["limit"] == "10"


async def test_itunes_song_search_returns_previews(settings, session_factory):
    payload = {
        "results": [
            {
                "wrapperType": "track",
                "kind": "song",
                "trackName": "Creep",
                "artistName": "Radiohead",
                "collectionName": "Pablo Honey",
                "trackTimeMillis": 238000,
                "trackViewUrl": "https://music.apple.com/it/album/creep/1",
                "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a",
            },
            {"wrapperType": "collection", "collectionName": "ignored"},
        ]
    }
    with respx.mock() as mock:
        route = mock.get(f"{ITUNES}/search").respond(json=payload)
        async with ItunesClient(settings, session_factory) as client:
            songs = await client.search_songs("Radiohead", "Creep")
    params = route.calls.last.request.url.params
    assert (params["entity"], params["media"], params["limit"]) == ("song", "music", "5")
    assert len(songs) == 1 and songs[0].artist == "Radiohead"
    assert songs[0].preview_url.endswith("x.m4a") and songs[0].duration_s == 238


async def test_wikidata_and_wikipedia(psettings, session_factory):
    with respx.mock() as mock:
        wd = mock.get(WIKIDATA).respond(
            json={
                "entities": {
                    "Q44190": {
                        "sitelinks": {
                            "itwiki": {"title": "Radiohead"},
                            "enwiki": {"title": "Radiohead"},
                        }
                    }
                }
            }
        )
        mock.get("https://it.wikipedia.org/api/rest_v1/page/summary/Radiohead").respond(
            json={
                "type": "standard",
                "extract": "I Radiohead sono un gruppo.",
                "content_urls": {"desktop": {"page": "https://it.wikipedia.org/wiki/Radiohead"}},
            }
        )
        mock.get("https://en.wikipedia.org/api/rest_v1/page/summary/Mercury").respond(
            json={"type": "disambiguation", "extract": "Mercury may refer to"}
        )
        mock.get("https://en.wikipedia.org/api/rest_v1/page/summary/Nobody").respond(404)
        async with WikidataClient(psettings, session_factory) as wikidata:
            links = await wikidata.sitelinks("Q44190", ("en", "it"))
            with pytest.raises(ValueError):
                await wikidata.sitelinks("Q1; DROP", ("en",))
        async with WikipediaClient(psettings, session_factory) as wiki:
            it = await wiki.summary("it", "Radiohead")
            assert await wiki.summary("en", "Mercury") is None  # not one thing
            assert await wiki.summary("en", "Nobody") is None
            with pytest.raises(ValueError):
                await wiki.summary("en/evil", "x")
    assert links == {"en": "Radiohead", "it": "Radiohead"}
    params = wd.calls.last.request.url.params
    assert params["sitefilter"] == "enwiki|itwiki" and params["props"] == "sitelinks"
    assert it.text.startswith("I Radiohead") and it.url == "https://it.wikipedia.org/wiki/Radiohead"


async def test_lastfm_top_tracks_and_bio_requests(psettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(LASTFM).mock(
            side_effect=lambda r: httpx.Response(
                200,
                json=(
                    {"toptracks": {"track": [{"name": "Creep"}]}}
                    if r.url.params["method"] == "artist.gettoptracks"
                    else {"artist": {"bio": {"summary": "Una band."}}}
                ),
            )
        )
        async with LastFmClient(psettings, session_factory) as client:
            top = await client.top_tracks(mbid=MBID, limit=10)
            bio = await client.bio(mbid=MBID, lang="it")
    first, second = (c.request.url.params for c in route.calls)
    assert first["method"] == "artist.gettoptracks" and first["mbid"] == MBID
    assert first["limit"] == "10" and first["api_key"] == KEY
    assert second["method"] == "artist.getinfo" and second["lang"] == "it"
    assert [t.title for t in top] == ["Creep"] and bio.text == "Una band."


# --------------------------------------------------------------------- the jobs


def mock_wikipedia_chain(mock, langs=("it", "en")):
    mock.get(f"{MB_BASE}/artist/{MBID}").respond(
        json={
            "relations": [
                {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q44190"}}
            ]
        }
    )
    mock.get(WIKIDATA).respond(
        json={
            "entities": {
                "Q44190": {"sitelinks": {f"{lang}wiki": {"title": "Radiohead"} for lang in langs}}
            }
        }
    )
    for lang in langs:
        mock.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/Radiohead").respond(
            json={
                "type": "standard",
                "extract": f"Text in {lang}.",
                "content_urls": {"desktop": {"page": f"https://{lang}.wikipedia.org/wiki/R"}},
            }
        )


def mock_deezer_artist(mock, artist_id=399, top=None):
    mock.get(f"{DEEZER}/search/artist").respond(
        json={
            "data": [{"id": artist_id, "name": "Radiohead", "nb_fan": 100, "picture_medium": IMG}]
        }
    )
    if top is not None:
        mock.get(f"{DEEZER}/artist/{artist_id}/top").respond(json={"data": top})


def bios_of(factory, artist_id):
    with factory() as db:
        return {
            b.lang: b for b in db.scalars(select(ArtistBio).where(ArtistBio.artist_id == artist_id))
        }


async def test_the_profile_job_finds_the_picture_and_the_biography(psettings, session_factory):
    artist_id = make(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock_wikipedia_chain(mock)
        mock_deezer_artist(mock)
        worker, job_id = await run(psettings, session_factory, "fetch_artist_profile", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    with session_factory() as db:
        artist = db.get(Artist, artist_id)
        assert artist.deezer_artist_id == "399" and artist.image_url == IMG
        assert artist.profile_checked_at is not None
    bios = bios_of(session_factory, artist_id)
    assert bios["it"].text == "Text in it." and bios["it"].source == "wikipedia"
    assert bios["en"].url == "https://en.wikipedia.org/wiki/R"


async def test_the_profile_job_keeps_an_existing_picture(psettings, session_factory):
    artist_id = make(session_factory, image_url="https://cdn-images.dzcdn.net/mine.jpg")
    with respx.mock(assert_all_called=False) as mock:
        mock_wikipedia_chain(mock)
        mock_deezer_artist(mock)
        worker, job_id = await run(psettings, session_factory, "fetch_artist_profile", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    with session_factory() as db:
        assert db.get(Artist, artist_id).image_url.endswith("mine.jpg")


async def test_lastfm_gives_the_biography_wikipedia_does_not_have(psettings, session_factory):
    artist_id = make(session_factory, mbid=None)  # no MBID: Wikipedia cannot even be asked
    with respx.mock(assert_all_called=False) as mock:
        mock_deezer_artist(mock)
        lastfm = mock.get(LASTFM).respond(
            json={
                "artist": {
                    "url": "https://www.last.fm/music/Radiohead",
                    "bio": {"summary": 'Una band. <a href="x">Read more on Last.fm</a>'},
                }
            }
        )
        worker, job_id = await run(psettings, session_factory, "fetch_artist_profile", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    bios = bios_of(session_factory, artist_id)
    assert {b.source for b in bios.values()} == {"lastfm"} and bios["it"].text == "Una band."
    assert lastfm.call_count == 2  # one per language
    assert {c.request.url.params["lang"] for c in lastfm.calls} == {"en", "it"}


async def test_a_language_nobody_has_is_remembered_as_empty(psettings, session_factory):
    artist_id = make(session_factory)
    s = psettings.model_copy(update={"lastfm_api_key": ""})
    with respx.mock(assert_all_called=False) as mock:
        mock_wikipedia_chain(mock, langs=("en",))  # no Italian article
        mock_deezer_artist(mock)
        worker, job_id = await run(s, session_factory, "fetch_artist_profile", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    bios = bios_of(session_factory, artist_id)
    assert bios["en"].text and bios["it"].text == ""


async def test_the_profile_job_retries_when_nobody_answers(psettings, session_factory):
    artist_id = make(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*").respond(503)
        worker, job_id = await run(psettings, session_factory, "fetch_artist_profile", artist_id)
        for _ in range(500):
            if any("Attempt failed" in m for m in logs(session_factory, job_id)):
                break
            import asyncio

            await asyncio.sleep(0.02)
        await worker.stop()
    await wait_for_status(
        session_factory, job_id, "queued"
    )  # the write of the failure may still be in flight
    with session_factory() as db:
        assert db.get(Artist, artist_id).profile_checked_at is None  # will be asked again


def rows_of(factory, artist_id):
    with factory() as db:
        rows = list_top_tracks(db, artist_id)
        db.expunge_all()
        return rows


DEEZER_TOP = [
    {
        "id": 1,
        "title": "Creep",
        "duration": 238,
        "preview": PREVIEW,
        "link": "https://www.deezer.com/track/1",
        "album": {"title": "Pablo Honey"},
    },
    {
        "id": 2,
        "title": "Karma Police",
        "duration": 264,
        "preview": PREVIEW,
        "album": {"title": "OK Computer"},
    },
]


async def test_the_top_tracks_come_from_deezer_with_their_previews(psettings, session_factory):
    artist_id = make(session_factory, deezer_artist_id="399")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{DEEZER}/artist/399/top").respond(json={"data": DEEZER_TOP})
        lastfm = mock.get(LASTFM).respond(json={})
        worker, job_id = await run(psettings, session_factory, "fetch_top_tracks", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    rows = rows_of(session_factory, artist_id)
    assert [(r.position, r.title, r.provider, r.preview_provider) for r in rows] == [
        (1, "Creep", "deezer", "deezer"),
        (2, "Karma Police", "deezer", "deezer"),
    ]
    assert rows[0].album_title == "Pablo Honey" and rows[0].preview_url == PREVIEW
    assert lastfm.call_count == 0  # the default provider had the list
    with session_factory() as db:
        assert db.get(Artist, artist_id).top_checked_at is not None


async def test_the_deezer_artist_is_looked_up_by_name_when_unknown(psettings, session_factory):
    artist_id = make(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock_deezer_artist(mock, 399, DEEZER_TOP)
        worker, job_id = await run(psettings, session_factory, "fetch_top_tracks", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    with session_factory() as db:
        assert db.get(Artist, artist_id).deezer_artist_id == "399"
    assert len(rows_of(session_factory, artist_id)) == 2


async def test_lastfm_lists_and_itunes_adds_only_the_previews_it_is_sure_about(
    psettings, session_factory
):
    artist_id = make(session_factory, deezer_artist_id="399")
    itunes_audio = "https://audio-ssl.itunes.apple.com/creep.m4a"

    def songs(request):
        term = request.url.params["term"]
        if "Creep" in term:
            rows = [
                {
                    "wrapperType": "track",
                    "kind": "song",
                    "trackName": "Creep",
                    "artistName": "Radiohead",
                    "collectionName": "Pablo Honey",
                    "trackTimeMillis": 238000,
                    "previewUrl": itunes_audio,
                    "trackViewUrl": "https://music.apple.com/x",
                },
            ]
        else:  # another artist's song with a similar title: never accepted
            rows = [
                {
                    "wrapperType": "track",
                    "kind": "song",
                    "trackName": "Karma Police",
                    "artistName": "Some Cover Band",
                    "previewUrl": itunes_audio,
                }
            ]
        return httpx.Response(200, json={"results": rows})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{DEEZER}/artist/399/top").respond(503)  # Deezer is down
        mock.get(LASTFM).respond(
            json={"toptracks": {"track": [{"name": "Creep"}, {"name": "Karma Police"}]}}
        )
        mock.get(f"{ITUNES}/search").mock(side_effect=songs)
        worker, job_id = await run(psettings, session_factory, "fetch_top_tracks", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    rows = rows_of(session_factory, artist_id)
    assert [(r.title, r.provider) for r in rows] == [
        ("Creep", "lastfm"),
        ("Karma Police", "lastfm"),
    ]
    assert rows[0].preview_url == itunes_audio and rows[0].preview_provider == "itunes"
    assert rows[0].album_title == "Pablo Honey" and rows[0].duration_s == 238
    assert rows[1].preview_url is None and rows[1].preview_provider is None


async def test_without_a_lastfm_key_deezer_is_the_only_source(mbsettings, session_factory):
    artist_id = make(session_factory, deezer_artist_id="399")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{DEEZER}/artist/399/top").respond(json={"data": []})
        worker, job_id = await run(mbsettings, session_factory, "fetch_top_tracks", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert rows_of(session_factory, artist_id) == []
    with session_factory() as db:
        assert db.get(Artist, artist_id).top_checked_at is not None  # "none", remembered
    assert any("Last.fm" in m or "lastfm" in m for m in logs(session_factory, job_id))


# -------------------------------------------------------------------- freshness


def test_preview_links_and_lists_age_differently(session_factory):
    artist_id = make(session_factory)
    now = utcnow()
    with session_factory() as db:
        artist = db.get(Artist, artist_id)
        assert top_state(artist, []) == "unknown"
        artist.top_checked_at = now
        db.add(
            ArtistTopTrack(
                artist_id=artist_id,
                position=1,
                title="Creep",
                provider="deezer",
                preview_url=PREVIEW,
                preview_provider="deezer",
                fetched_at=now,
            )
        )
        db.add(
            ArtistTopTrack(
                artist_id=artist_id,
                position=2,
                title="Itunes",
                provider="lastfm",
                preview_url="https://audio-ssl.itunes.apple.com/a.m4a",
                preview_provider="itunes",
                fetched_at=now,
            )
        )
        db.commit()
        tracks = list_top_tracks(db, artist_id)
        assert top_state(artist, tracks, now=now) == "ready"
        later = now + timedelta(minutes=11)
        assert [preview_alive(t, now=later) for t in tracks] == [False, True]  # Deezer expires
        assert top_state(artist, tracks, now=later) == "expired"
        assert top_state(artist, tracks, now=now + timedelta(days=8)) == "stale"


# --------------------------------------------------------------------- the page


def test_the_page_has_a_picture_and_loads_the_description_and_the_top_tracks(app, admin_client):
    with app.state.session_factory() as db:
        artist = make_artist(db, "Radiohead", mbid=MBID, status="resolved")
        artist.image_url = IMG
        db.commit()
        artist_id = artist.id
    html = admin_client.get(f"/artist/{artist_id}").text
    assert "data-artist-image" in html and "500x500" in html and 'alt="Radiohead"' in html
    assert f'hx-post="/artist/{artist_id}/bio"' in html
    assert f'hx-post="/artist/{artist_id}/top"' in html
    assert html.index('id="artist-top"') < html.index('id="artist-sections"')  # before the albums


def test_the_artist_page_shows_an_initial_without_a_picture(app, admin_client):
    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Radiohead").id
    html = admin_client.get(f"/artist/{artist_id}").text
    assert "data-artist-image" not in html and 'aria-hidden="true">R</div>' in html


def seed_bios(app, artist_id, **texts):
    with app.state.session_factory() as db:
        for lang, body in texts.items():
            db.add(
                ArtistBio(
                    artist_id=artist_id,
                    lang=lang,
                    text=body,
                    source="wikipedia",
                    url=f"https://{lang}.wikipedia.org/wiki/R",
                )
            )
        artist = db.get(Artist, artist_id)
        artist.profile_checked_at = utcnow()
        db.commit()


def test_the_description_is_short_credited_and_in_the_users_language(app, admin_client):
    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Radiohead", mbid=MBID).id
    long_en = "First sentence of the band. " + "Second sentence about the band. " * 3
    seed_bios(app, artist_id, en=long_en, it="Una frase sola.")
    html = admin_client.post(
        f"/artist/{artist_id}/bio",
        data={
            "csrf_token": re.search(
                r'name="csrf_token" value="([^"]+)"', admin_client.get("/dashboard").text
            ).group(1)
        },
    ).text
    assert "First sentence of the band. Second sentence about the band." in html
    assert html.count("Second sentence") == 1  # two sentences at most
    assert "Text from Wikipedia (CC BY-SA)." in html
    assert 'href="https://en.wikipedia.org/wiki/R"' in html and "Read more" in html
    post(admin_client, "/lang", {"lang": "it", "next": "/dashboard"})
    italian = admin_client.get(f"/artist/{artist_id}/bio").text
    assert "Una frase sola." in italian and "Testo da Wikipedia" in italian


def test_the_description_is_requested_once_and_polled_while_it_loads(app, admin_client):
    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Radiohead", mbid=MBID).id
    first = admin_client.get(f"/artist/{artist_id}/bio").text  # a GET queues it too
    assert 'hx-trigger="every 3s"' in first and "Loading the description" in first
    admin_client.get(f"/artist/{artist_id}/bio")
    with app.state.session_factory() as db:
        jobs = db.scalars(select(Job).where(Job.type == "fetch_artist_profile")).all()
    assert len(jobs) == 1  # deduplicated


def seed_tracks(app, artist_id, *, age_minutes=0, with_preview=True):
    when = utcnow() - timedelta(minutes=age_minutes)
    with app.state.session_factory() as db:
        artist = db.get(Artist, artist_id)
        artist.top_checked_at = when
        for pos, title in enumerate(("Creep", "Karma Police"), start=1):
            db.add(
                ArtistTopTrack(
                    artist_id=artist_id,
                    position=pos,
                    title=title,
                    album_title="Album",
                    duration_s=238,
                    provider="deezer",
                    preview_url=PREVIEW if with_preview else None,
                    preview_provider="deezer" if with_preview else None,
                    fetched_at=when,
                )
            )
        db.commit()


def test_the_top_tracks_are_listed_with_players_and_credit(app, admin_client):
    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Radiohead", mbid=MBID).id
    seed_tracks(app, artist_id)
    html = admin_client.get(f"/artist/{artist_id}/top").text
    assert html.count("data-top-track=") == 2 and "Top 10 tracks" in html
    assert html.count("<audio") == 2 and PREVIEW in html
    assert "3:58" in html  # 238 s
    assert "Preview courtesy of Deezer." in html
    assert 'hx-trigger="every 3s [dkIdle()]"' not in html
    with app.state.session_factory() as db:
        assert db.scalar(select(Job).where(Job.type == "fetch_top_tracks")) is None


def test_old_deezer_previews_are_renewed_by_a_job_and_not_offered_meanwhile(app, admin_client):
    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Radiohead", mbid=MBID).id
    seed_tracks(app, artist_id, age_minutes=30)
    html = admin_client.get(f"/artist/{artist_id}/top").text
    assert "<audio" not in html and "Renewing" in html  # the expired address is not played
    assert 'hx-trigger="every 3s [dkIdle()]"' in html and html.count("data-top-track=") == 2
    with app.state.session_factory() as db:
        assert db.scalars(select(Job).where(Job.type == "fetch_top_tracks")).all()


def test_an_artist_never_looked_up_gets_a_job_and_a_loading_message(app, admin_client):
    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Radiohead", mbid=MBID).id
    html = admin_client.get(f"/artist/{artist_id}/top").text
    assert 'data-top-state="loading"' in html and 'hx-trigger="every 3s [dkIdle()]"' in html
    admin_client.get(f"/artist/{artist_id}/top")
    with app.state.session_factory() as db:
        assert len(db.scalars(select(Job).where(Job.type == "fetch_top_tracks")).all()) == 1


def test_no_section_at_all_when_there_are_no_top_tracks(app, admin_client):
    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Obscure", mbid=None).id
        db.get(Artist, artist_id).top_checked_at = utcnow()
        db.commit()
    html = admin_client.get(f"/artist/{artist_id}/top").text
    assert "Top 10 tracks" not in html and "data-top-track" not in html


def test_the_profile_pages_need_a_login(app, new_client):
    anon = new_client()
    for path in ("bio", "top"):
        assert anon.get(f"/artist/1/{path}", follow_redirects=False).status_code == 303
    assert new_client().get("/artist/999/top").status_code in (303, 404)


# ------------------------------------------------------- settings and migration


def test_the_new_capabilities_are_in_the_services_section(app, admin_client):
    page = "/settings/services"
    html = admin_client.get(page).text
    for key in ("providers_toptracks", "providers_bio"):
        assert f'data-slot="{key}-1"' in html and f'data-slot="{key}-2"' in html
    base = {
        "providers_covers_1": "coverartarchive",
        "providers_previews_1": "itunes",
        "providers_similar_1": "lastfm",
        "providers_metadata_1": "musicbrainz",
    }
    r = post(
        admin_client,
        "/settings/services",
        {
            **base,
            "providers_toptracks_1": "lastfm",
            "providers_toptracks_2": "deezer",
            "providers_bio_1": "lastfm",
            "providers_bio_2": "",
        },
        page=page,
    )
    assert r.status_code == 303
    s = app.state.settings
    assert (s.providers_toptracks, s.providers_bio) == ("lastfm,deezer", "lastfm")


def test_migration_0014_adds_the_tables_and_columns(settings):
    url = settings.database_url
    run_migrations(url, "0013")
    run_migrations(url)
    run_migrations(url)  # idempotent
    engine = make_engine(url)
    found = inspect(engine)
    assert {"artist_bios", "artist_top_tracks"} <= set(found.get_table_names())
    assert {"deezer_artist_id", "profile_checked_at", "top_checked_at"} <= {
        c["name"] for c in found.get_columns("artists")
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artists (id, name, sort_name, name_key, mb_status, in_library, created_at,"
                " updated_at) VALUES (1, 'A', 'A', 'a', 'pending', 1, '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO artist_bios (artist_id, lang, text, checked_at)"
                " VALUES (1, 'en', 'x', '2026-01-01')"
            )
        )
        conn.execute(text("DELETE FROM artists WHERE id = 1"))  # the bio goes with the artist
        assert conn.execute(text("SELECT COUNT(*) FROM artist_bios")).scalar_one() == 0
    engine.dispose()


# ------------------------------------------------ pictures when metadata is updated


async def test_deezer_gives_the_picture_of_a_known_artist(settings, session_factory):
    with respx.mock() as mock:
        route = mock.get(f"{DEEZER}/artist/399").respond(
            json={"id": 399, "name": "Radiohead", "picture_medium": IMG}
        )
        async with DeezerClient(settings, session_factory) as client:
            assert await client.artist_picture("399") == IMG
            with pytest.raises(ValueError):
                await client.artist_picture("../x")
        mock.get(f"{DEEZER}/artist/5").respond(json={"picture_medium": "http://evil.example/p.jpg"})
        async with DeezerClient(settings, session_factory) as client:
            assert await client.artist_picture("5") is None  # only Deezer's own image servers
    assert route.call_count == 1


async def test_the_profile_job_finds_the_picture_of_an_artist_whose_deezer_id_is_known(
    psettings, session_factory
):
    artist_id = make(session_factory, deezer_artist_id="399")
    with respx.mock(assert_all_called=False) as mock:
        picture = mock.get(f"{DEEZER}/artist/399").respond(json={"picture_medium": IMG})
        mock.get(f"{DEEZER}/search/artist").respond(json={"data": []})
        mock_wikipedia_chain(mock)
        worker, job_id = await run(psettings, session_factory, "fetch_artist_profile", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    with session_factory() as db:
        artist = db.get(Artist, artist_id)
        assert artist.image_url == IMG and artist.image_checked_at is not None
    assert picture.call_count == 1
    assert any("picture found" in m for m in logs(session_factory, job_id))


async def test_the_profile_job_does_not_ask_again_for_a_picture_it_has(psettings, session_factory):
    artist_id = make(
        session_factory, deezer_artist_id="399", image_url="https://cdn-images.dzcdn.net/m.jpg"
    )
    with respx.mock(assert_all_called=False) as mock:
        picture = mock.get(f"{DEEZER}/artist/399").respond(json={"picture_medium": IMG})
        mock_wikipedia_chain(mock)
        worker, job_id = await run(psettings, session_factory, "fetch_artist_profile", artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert picture.call_count == 0
    with session_factory() as db:
        assert db.get(Artist, artist_id).image_url.endswith("m.jpg")


async def _sync_artist(settings, factory, artist_id):
    from tests.mbdata import ARETHA, aretha_discography, load
    from tests.test_mb_sync import mount, run_job

    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        worker, job_id = await run_job(settings, factory, artist_id)
        await wait_for_status(factory, job_id, "succeeded")
        with factory() as db:
            children = [j for j in db.scalars(select(Job)) if j.parent_job_id == job_id]
        await worker.stop()
    return job_id, children


async def test_updating_an_artist_without_a_picture_looks_for_it(mbsettings, session_factory):
    from tests.test_mb_sync import seed

    artist_id = seed(session_factory)
    job_id, children = await _sync_artist(mbsettings, session_factory, artist_id)
    profile = [j for j in children if j.type == "fetch_artist_profile"]
    assert len(profile) == 1 and profile[0].dedupe_key == f"artist_profile:{artist_id}"
    assert any("no picture yet" in m for m in logs(session_factory, job_id))


async def test_updating_an_artist_that_has_a_picture_does_not(mbsettings, session_factory):
    from tests.test_mb_sync import seed

    artist_id = seed(session_factory)
    with session_factory() as db:
        db.get(Artist, artist_id).image_url = "https://cdn-images.dzcdn.net/m.jpg"
        db.commit()
    job_id, children = await _sync_artist(mbsettings, session_factory, artist_id)
    assert not [j for j in children if j.type == "fetch_artist_profile"]
    assert not any("no picture yet" in m for m in logs(session_factory, job_id))


async def test_the_picture_is_looked_for_even_when_musicbrainz_does_not_have_the_artist(
    mbsettings, session_factory
):
    from tests.test_mb_sync import mount, run_job, seed

    artist_id = seed(session_factory, "Obscure Band")
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, {"created": "2026-01-01T00:00:00Z", "count": 0, "offset": 0, "artists": []})
        worker, job_id = await run_job(mbsettings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        with session_factory() as db:
            kinds = {j.type for j in db.scalars(select(Job)) if j.parent_job_id == job_id}
        await worker.stop()
    assert "fetch_artist_profile" in kinds
