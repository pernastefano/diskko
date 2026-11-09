"""How the clients keep to the limits of the services, and how the cover job copes when a
provider does not answer."""

import asyncio

import httpx
import pytest
import respx

from app.integrations import breaker, ratelimit
from app.integrations.cached_http import CachedJsonClient, ServiceError
from app.integrations.coverart import CoverArtClient, CoverArtError
from app.integrations.lastfm import LastFmClient
from app.integrations.listenbrainz import ListenBrainzClient
from app.integrations.previews import DeezerClient, ItunesClient
from app.integrations.wikipedia import WikidataClient, WikipediaClient
from app.models import Job
from tests.test_jobs_worker import fetch, logs, wait_for_status
from tests.test_mb_covers import CAA, covers_of, run_covers
from tests.test_service_fallback import DEEZER, deezer_album, fallback_settings, seed_one

UA = "Diskko/0.1 ( test@example.com )"
ITUNES = "https://itunes.apple.com"
LB = "https://labs.api.listenbrainz.org/similar-artists/json"
MBID = "a" * 8 + "-0000-4000-8000-000000000002"


@pytest.fixture
def dsettings(settings):
    return settings.model_copy(update={"mb_user_agent": UA})


@pytest.fixture(autouse=True)
def _limiters():
    ratelimit._LIMITERS.clear()
    yield
    ratelimit._LIMITERS.clear()


# ---------------------------------------------------------- what the services publish


def test_the_intervals_are_inside_the_published_limits():
    assert ItunesClient.min_interval >= 60 / 20  # "approximately 20 calls per minute"
    assert LastFmClient.min_interval >= 1.0  # "several calls per second" can get you suspended
    assert ListenBrainzClient.min_interval >= 1.0  # "never more than ONE call per second"
    assert (
        WikipediaClient.min_interval * 200 >= 60 - 1e-9 or 60 / WikipediaClient.min_interval <= 200
    )
    assert DeezerClient.min_interval >= 0.1  # 50 requests in 5 seconds is the figure reported


def test_wikipedia_and_wikidata_share_one_limiter():
    """Wikimedia counts a client across all its sites together, not per site."""
    a = ratelimit.get_limiter(WikipediaClient.limiter_name, WikipediaClient.min_interval)
    b = ratelimit.get_limiter(WikidataClient.limiter_name, WikidataClient.min_interval)
    assert WikipediaClient.limiter_name == WikidataClient.limiter_name == "wikimedia" and a is b


def test_a_429_without_retry_after_waits_at_least_five_seconds_for_wikimedia():
    assert WikipediaClient.BACKOFF_BASE >= 5.0 and WikidataClient.RATE_LIMIT_PAUSE >= 5.0


# ------------------------------------------------------------------- too many requests


async def test_itunes_refusing_with_403_holds_every_request_back_for_the_minute(
    dsettings, session_factory, monkeypatch
):
    monkeypatch.setattr(CachedJsonClient, "RATE_LIMIT_PAUSE", 0.0)  # (the conftest zeroes it)
    monkeypatch.setattr(ItunesClient, "RATE_LIMIT_PAUSE", 60.0)
    with respx.mock() as mock:
        route = mock.get(f"{ITUNES}/search").respond(403)
        async with ItunesClient(dsettings, session_factory) as client:
            with pytest.raises(ServiceError, match="limiting requests"):
                await client.search_albums("Band", "Album")
            limiter = client._limiter
    assert route.call_count == 1  # not asked again at once
    assert limiter._next_slot - limiter._clock() > 50  # and nobody asks it for a minute


async def test_a_429_with_a_short_retry_after_is_obeyed_and_retried(dsettings, session_factory):
    with respx.mock() as mock:
        route = mock.get(f"{DEEZER}/search/album").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"data": []}),
            ]
        )
        async with DeezerClient(dsettings, session_factory) as client:
            await client.search_albums("Band", "Album")
    assert route.call_count == 2


async def test_listenbrainz_headers_say_when_the_window_ends(dsettings, session_factory):
    with respx.mock() as mock:
        mock.get(LB).respond(
            json=[], headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "7"}
        )
        async with ListenBrainzClient(dsettings, session_factory) as client:
            await client.similar(MBID)
            limiter = client._limiter
    assert 5 < limiter._next_slot - limiter._clock() <= 7.5  # it waits for the window to end


# --------------------------------------------------------------- Cover Art Archive


async def test_a_timeout_or_503_at_the_cover_archive_is_tried_once_more(dsettings):
    async with CoverArtClient(dsettings.model_copy(update={"coverart_url": CAA})) as client:
        client._limiter = ratelimit.RateLimiter(0.0)
        with respx.mock() as mock:
            route = mock.get(f"{CAA}/release-group/{MBID}/front-250").mock(
                side_effect=[
                    httpx.ReadTimeout("slow"),
                    httpx.Response(
                        200, content=b"\xff\xd8img", headers={"content-type": "image/jpeg"}
                    ),
                ]
            )
            assert await client.fetch_front(MBID) == (b"\xff\xd8img", ".jpg")
    assert route.call_count == 2


async def test_a_503_with_a_long_retry_after_is_not_waited_for_in_the_request(dsettings):
    async with CoverArtClient(dsettings.model_copy(update={"coverart_url": CAA})) as client:
        client._limiter = ratelimit.RateLimiter(0.0)
        with respx.mock() as mock:
            route = mock.get(f"{CAA}/release-group/{MBID}/front-250").respond(
                503, headers={"Retry-After": "45"}
            )
            with pytest.raises(CoverArtError, match="HTTP 503"):
                await client.fetch_front(MBID)
        assert route.call_count == 1
        assert client._limiter._next_slot - client._limiter._clock() > 40  # held back for all


async def test_a_missing_cover_is_not_a_failure_and_is_not_retried(dsettings):
    async with CoverArtClient(dsettings.model_copy(update={"coverart_url": CAA})) as client:
        client._limiter = ratelimit.RateLimiter(0.0)
        with respx.mock() as mock:
            route = mock.get(f"{CAA}/release-group/{MBID}/front-250").respond(404)
            assert await client.fetch_front(MBID) is None
        assert route.call_count == 1


# ------------------------------------------------------------------ the cover job


async def test_a_paused_provider_makes_the_job_wait_without_using_an_attempt(
    mbsettings, session_factory
):
    settings = fallback_settings(mbsettings)
    artist_id, mbid = seed_one(session_factory)
    breaker.BREAKERS.failure("coverart", 1, 600)  # the Cover Art Archive is paused
    with respx.mock(assert_all_called=False) as mock:
        caa = mock.get(f"{CAA}/release-group/{mbid}/front-250").respond(500)
        mock.get(f"{DEEZER}/search/album").respond(200, json={"data": []})
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        for _ in range(500):
            if any("providers are paused" in m for m in logs(session_factory, job_id)):
                break
            await asyncio.sleep(0.02)
        await worker.stop()
    row = await wait_for_status(session_factory, job_id, "queued")
    assert caa.call_count == 0  # nothing was asked of a paused service
    assert row.attempts == 0 and row.next_run_at is not None  # the attempt is given back
    assert any("Deezer has no such cover" in m for m in logs(session_factory, job_id))
    assert covers_of(session_factory, artist_id)["Studio Album"] == ("unknown", None)


async def test_out_of_attempts_the_job_ends_without_failing_and_covers_stay_for_later(
    mbsettings, session_factory
):
    from app.jobs.handlers.fetch_covers import MAX_ATTEMPTS

    settings = fallback_settings(mbsettings)
    artist_id, mbid = seed_one(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{CAA}/release-group/{mbid}/front-250").respond(500)
        mock.get(f"{DEEZER}/search/album").mock(
            return_value=httpx.Response(200, json=deezer_album(artist="Somebody Else"))
        )
        from app.jobs.worker import JobWorker
        from tests.test_jobs_worker import enqueue

        job_id = enqueue(session_factory, "fetch_covers", {"artist_id": artist_id})
        with session_factory() as db:
            db.get(Job, job_id).attempts = MAX_ATTEMPTS - 1  # this run is the last attempt
            db.commit()
        worker = JobWorker(settings, session_factory)
        await worker.start()
        worker.notify()
        row = await wait_for_status(session_factory, job_id, "succeeded", timeout=15)
        await worker.stop()
    assert any("looked up again later" in m for m in logs(session_factory, job_id))
    assert row.error is None
    assert covers_of(session_factory, artist_id)["Studio Album"] == ("unknown", None)
    assert fetch(session_factory, job_id).status == "succeeded"
