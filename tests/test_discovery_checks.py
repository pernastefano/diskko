"""Connection tests of Last.fm, ListenBrainz, iTunes and Deezer, and the pages that show them."""

import pytest
import respx
from sqlalchemy import func, select

from app.integrations.base import run_check
from app.integrations.discovery_checks import Deezer, Itunes, LastFm, ListenBrainz
from app.integrations.status import INTEGRATION_NAMES, build_integration
from app.models import SimilarArtist, SimilarFetch
from tests.dbhelpers import make_artist
from tests.discovery_data import RADIOHEAD, load

LASTFM = "https://ws.audioscrobbler.com/2.0/"
LB = "https://labs.api.listenbrainz.org/similar-artists/json"
ITUNES = "https://itunes.apple.com/search"
DEEZER = "https://api.deezer.com/album/302127"


@pytest.fixture
def dsettings(settings):
    return settings.model_copy(
        update={"mb_user_agent": "Diskko/0.1 ( test@example.com )", "lastfm_api_key": "k-123456"}
    )


def test_the_new_integrations_are_registered():
    assert {"lastfm", "listenbrainz", "itunes", "deezer"} <= set(INTEGRATION_NAMES)


def test_what_needs_configuration(settings, dsettings):
    assert not LastFm(settings).is_configured() and LastFm(dsettings).is_configured()
    assert not ListenBrainz(settings).is_configured() and ListenBrainz(dsettings).is_configured()
    assert Itunes(settings).is_configured() and Deezer(settings).is_configured()
    assert build_integration("deezer", settings).name == "deezer"


async def test_lastfm_check(dsettings):
    with respx.mock() as mock:
        mock.get(LASTFM).respond(json=load("lastfm_similar"))
        assert (await run_check(LastFm(dsettings))).code == "ok"
    with respx.mock() as mock:
        mock.get(LASTFM).respond(403, json={"error": 10, "message": "Invalid API key"})
        result = await run_check(LastFm(dsettings))
    assert result.code == "auth_failed" and "k-123456" not in str(result.to_dict())
    with respx.mock() as mock:
        mock.get(LASTFM).respond(json={"error": 29})
        assert (await run_check(LastFm(dsettings))).code == "rate_limited"


async def test_listenbrainz_check(dsettings):
    with respx.mock() as mock:
        route = mock.get(LB).respond(json=load("listenbrainz_similar"))
        assert (await run_check(ListenBrainz(dsettings))).code == "ok"
    assert route.calls.last.request.headers["User-Agent"].startswith("Diskko/0.1")
    with respx.mock() as mock:
        mock.get(LB).respond(429)
        assert (await run_check(ListenBrainz(dsettings))).code == "rate_limited"
    with respx.mock() as mock:
        mock.get(LB).respond(json={"unexpected": True})
        assert (await run_check(ListenBrainz(dsettings))).code == "invalid_response"


async def test_itunes_and_deezer_checks(settings):
    with respx.mock() as mock:
        mock.get(ITUNES).respond(json=load("itunes_album_search"))
        assert (await run_check(Itunes(settings))).code == "ok"
    with respx.mock() as mock:
        mock.get(ITUNES).respond(500)
        assert (await run_check(Itunes(settings))).code == "http_error"
    with respx.mock() as mock:
        mock.get(DEEZER).respond(json={"id": 302127, "title": "Discovery"})
        assert (await run_check(Deezer(settings))).code == "ok"
    with respx.mock() as mock:
        mock.get(DEEZER).respond(json=load("deezer_error"))  # an error with HTTP 200
        assert (await run_check(Deezer(settings))).code == "invalid_response"


def test_the_status_page_shows_them(admin_client):
    text = admin_client.get("/admin/status").text
    for name in ("Last.fm", "ListenBrainz", "iTunes (previews)", "Deezer (previews)"):
        assert name in text


def test_the_discovery_queue_is_configurable(admin_client, app):
    text = admin_client.get("/settings/jobs").text
    assert "Discovery" in text and 'name="queue_discovery"' in text
    assert app.state.settings.queue_concurrency["discovery"] == 2


def test_deleting_an_artist_removes_its_similar_list(db):
    from app.integrations.discovery_types import SimilarHit
    from app.services import discovery as disc

    artist = make_artist(db, "Radiohead", mbid=RADIOHEAD, in_library=False)
    disc.store_similar(db, artist.id, [SimilarHit("Muse", None, 0.5)], "lastfm")
    db.commit()
    db.delete(artist)
    db.commit()
    assert db.scalar(select(func.count(SimilarArtist.id))) == 0
    assert db.scalar(select(func.count(SimilarFetch.artist_id))) == 0
