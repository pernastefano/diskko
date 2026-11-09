"""The search page also asks MusicBrainz for albums (and for any words, or a field such as tag:)."""

import json

import pytest

from app.integrations.musicbrainz_client import (
    ApiCache,
    release_group_query,
    release_group_search_key,
)
from app.models import Artist, utcnow
from tests.conftest import post
from tests.dbhelpers import make_artist
from tests.test_discovery_flow import (
    HX,
    MB,
    UA,
    jobs,
    no_waiting,  # noqa: F401  (autouse fixture)
)

ARTIST = "11111111-0000-4000-8000-000000000001"
GROUP = "22222222-0000-4000-8000-000000000002"


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"mb_user_agent": UA, "musicbrainz_url": MB})


def seed(db, text="lady soul"):
    hit = {
        "id": GROUP,
        "score": 100,
        "title": "Lady Soul",
        "primary-type": "Album",
        "first-release-date": "1968-01-22",
        "artist-credit": [
            {"name": "Aretha Franklin", "artist": {"id": ARTIST, "name": "Aretha Franklin"}}
        ],
    }
    key = release_group_search_key(release_group_query(text, "anything"), 15)
    db.add(
        ApiCache(
            key=key,
            status=200,
            response=json.dumps({"count": 1, "release-groups": [hit]}),
            fetched_at=utcnow(),
            ttl_seconds=3600,
        )
    )
    db.commit()


def start(client, q="lady soul"):
    return post(client, f"/search/albums/start?q={q}", {}, page="/search", headers=HX)


def test_the_page_asks_for_the_albums_once_it_loads_and_looking_queues_nothing(admin_client, db):
    html = admin_client.get("/search?q=lady soul").text
    assert 'data-album-search-state="starting"' in html and "Albums on MusicBrainz" in html
    assert admin_client.get("/search/albums/results?q=lady soul").status_code == 200
    assert jobs(db, "search_release_groups") == []


def test_the_start_queues_one_job_with_the_words_as_a_title_or_artist_query(admin_client, db):
    assert 'data-album-search-state="searching"' in start(admin_client).text
    start(admin_client)
    queued = jobs(db, "search_release_groups")
    assert len(queued) == 1
    assert json.loads(queued[0].payload_json)["query"] == release_group_query(
        "lady soul", "anything"
    )


def test_a_field_query_goes_through_as_typed(admin_client, db):
    start(admin_client, "tag:dance")
    assert json.loads(jobs(db, "search_release_groups")[0].payload_json)["query"] == "(tag:dance)"


def test_cached_albums_are_listed_with_their_artist(admin_client, db):
    seed(db)
    html = admin_client.get("/search?q=lady soul").text
    assert 'data-album-search-state="results"' in html and "Lady Soul" in html
    assert "Aretha Franklin" in html and "1968" in html and "/search/open-album" in html


def test_an_artist_we_have_links_straight_to_their_page(admin_client, db):
    artist = make_artist(db, "Aretha Franklin", mbid=ARTIST)
    seed(db)
    html = admin_client.get("/search?q=lady soul").text
    assert f'href="/artist/{artist.id}"' in html and "/search/open-album" not in html


def test_opening_the_artist_of_an_album_creates_the_artist_from_the_cached_result(admin_client, db):
    seed(db)
    r = post(
        admin_client,
        "/search/open-album",
        {"q": "lady soul", "mbid": GROUP},
        page="/search?q=lady soul",
    )
    artist = db.query(Artist).filter_by(mbid=ARTIST).one()
    assert r.status_code == 303 and r.headers["location"] == f"/artist/{artist.id}"
    assert artist.name == "Aretha Franklin" and not artist.in_library


def test_an_unknown_album_goes_back_to_the_search(admin_client, db):
    r = post(
        admin_client,
        "/search/open-album",
        {"q": "lady soul", "mbid": GROUP},
        page="/search?q=lady soul",
    )
    assert r.headers["location"] == "/search?q=lady+soul"
    assert db.query(Artist).count() == 0


def test_a_person_cannot_fill_the_queue_with_album_searches(admin_client, db):
    for name in ("Ab", "Cd", "Ef", "Gh"):
        text = start(admin_client, name).text
    assert len(jobs(db, "search_release_groups")) == 3 and 'data-album-search-state="busy"' in text


def test_short_searches_show_no_album_section(admin_client):
    assert "Albums on MusicBrainz" not in admin_client.get("/search?q=a").text
