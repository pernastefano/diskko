import httpx
import pytest
import respx
from sqlalchemy import select

from app.integrations import breaker
from app.integrations.breaker import BREAKERS, CircuitBreakers, ServiceBreakerOpen
from app.integrations.cached_http import CachedJsonClient
from app.integrations.previews import DeezerClient
from app.integrations.providers import CAPABILITIES, provider_order
from app.jobs import engine
from app.models import ReleaseGroup
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.test_jobs_worker import enqueue, fetch, logs, wait_for_status
from tests.test_mb_covers import CAA, JPEG, covers_of, run_covers

DEEZER = "https://api.deezer.com"
DEEZER_IMG = "https://cdn-images.dzcdn.net/images/cover/abc/250x250-000000-80-0-0.jpg"
PNG_HEADERS = {"content-type": "image/jpeg"}


# ------------------------------------------------------------------ the breaker


def test_a_service_is_paused_after_enough_failures_and_tried_again_later():
    now = [100.0]
    b = CircuitBreakers(clock=lambda: now[0])
    for _ in range(2):
        b.failure("svc", threshold=3, cooldown=60)
        b.check("svc", 3)  # not yet
    b.failure("svc", threshold=3, cooldown=60)
    with pytest.raises(ServiceBreakerOpen) as caught:
        b.check("svc", 3)
    assert caught.value.service == "svc" and 59 <= caught.value.delay <= 60
    assert caught.value.delay >= 1 and b.remaining("svc") > 0
    now[0] += 61
    b.check("svc", 3)  # one request is let through
    b.failure("svc", threshold=3, cooldown=60)  # ... and fails: paused again at once
    with pytest.raises(ServiceBreakerOpen):
        b.check("svc", 3)


def test_a_success_resets_the_count_and_services_are_independent():
    b = CircuitBreakers(clock=lambda: 0.0)
    b.failure("a", 2, 10)
    b.success("a")
    b.failure("a", 2, 10)
    b.check("a", 2)
    b.failure("a", 2, 10)
    with pytest.raises(ServiceBreakerOpen):
        b.check("a", 2)
    b.check("b", 2)


async def test_a_client_stops_asking_a_service_that_keeps_failing(settings, session_factory):
    s = settings.model_copy(update={"provider_breaker_threshold": 2})
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=rf"{DEEZER}/.*").respond(503)
        async with DeezerClient(s, session_factory, max_retries=0) as client:
            for name in ("one", "two"):
                with pytest.raises(Exception, match="not answering"):
                    await client.search_albums("Band", name)
            calls = route.call_count
            with pytest.raises(ServiceBreakerOpen):
                await client.search_albums("Band", "three")
            assert route.call_count == calls  # no request while paused


# ------------------------------------------------------------- provider order


def test_the_order_comes_from_the_settings_and_bad_values_fall_back(settings):
    def order(capability, value):
        key = CAPABILITIES[capability][0]
        return provider_order(settings.model_copy(update={key: value}), capability)

    assert order("covers", "") == ["coverartarchive", "deezer"]
    assert order("covers", "deezer,coverartarchive") == ["deezer", "coverartarchive"]
    assert order("covers", "deezer, deezer ,nope") == ["deezer"]
    assert order("covers", "nope") == ["coverartarchive", "deezer"]
    assert order("previews", "deezer") == ["deezer"]
    assert order("similar", "listenbrainz,lastfm") == ["listenbrainz", "lastfm"]


# ----------------------------------------------------------------- the covers


def fallback_settings(mbsettings, **update):
    return mbsettings.model_copy(
        update={
            "coverart_url": CAA,
            "coverart_min_interval": 0.0,
            "providers_covers": "coverartarchive,deezer",
            **update,
        }
    )


def seed_one(factory):
    with factory() as db:
        artist = make_artist(db, "Band", mbid="a" * 8 + "-0000-4000-8000-000000000001")
        artist.status = "resolved"
        rg = make_rg(db, artist, "Studio Album", 1990)
        make_album(db, artist, "Studio Album", year=1990)
        db.commit()
        return artist.id, rg.mbid


def deezer_album(title="Studio Album", artist="Band", cover=DEEZER_IMG):
    return {
        "data": [
            {
                "id": 7,
                "title": title,
                "nb_tracks": 10,
                "artist": {"name": artist},
                "cover_medium": cover,
            }
        ]
    }


def mock_deezer(mock, payload=None, image=None):
    mock.get(f"{DEEZER}/search/album").mock(
        return_value=httpx.Response(200, json=payload or deezer_album())
    )
    route = mock.get(DEEZER_IMG).mock(
        return_value=image or httpx.Response(200, content=JPEG, headers=PNG_HEADERS)
    )
    return route


async def test_when_the_cover_art_archive_fails_the_fallback_provides_the_cover(
    mbsettings, session_factory
):
    settings = fallback_settings(mbsettings)
    artist_id, mbid = seed_one(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{CAA}/release-group/{mbid}/front-250").respond(500)
        mock_deezer(mock)
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert covers_of(session_factory, artist_id)["Studio Album"] == ("ok", f"{mbid}.jpg")
    messages = logs(session_factory, job_id)
    assert any("Cover Art Archive failed" in m and "HTTP 500" in m for m in messages)
    assert any("cover saved" in m and "Deezer" in m for m in messages)
    assert (settings.data_dir / "covers" / f"{mbid}.jpg").read_bytes() == JPEG


async def test_the_fallback_is_also_asked_when_the_first_has_no_cover(mbsettings, session_factory):
    settings = fallback_settings(mbsettings)
    artist_id, mbid = seed_one(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{CAA}/release-group/{mbid}/front-250").respond(404)
        mock_deezer(mock)
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert covers_of(session_factory, artist_id)["Studio Album"][0] == "ok"


async def test_a_cover_of_a_different_album_is_never_taken(mbsettings, session_factory):
    settings = fallback_settings(mbsettings)
    artist_id, mbid = seed_one(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{CAA}/release-group/{mbid}/front-250").respond(404)
        image = mock_deezer(mock, deezer_album(artist="Somebody Else"))
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert covers_of(session_factory, artist_id)["Studio Album"] == ("missing", None)
    assert image.call_count == 0
    assert any(
        "no cover at Cover Art Archive or Deezer" in m for m in logs(session_factory, job_id)
    )


async def test_the_default_provider_can_be_changed(mbsettings, session_factory):
    settings = fallback_settings(mbsettings, providers_covers="deezer,coverartarchive")
    artist_id, mbid = seed_one(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        caa = mock.get(f"{CAA}/release-group/{mbid}/front-250").respond(
            200, content=JPEG, headers=PNG_HEADERS
        )
        mock_deezer(mock)
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert caa.call_count == 0  # Deezer had it: the second provider is not needed
    assert any("Deezer" in m and "cover saved" in m for m in logs(session_factory, job_id))


async def test_an_unanswered_cover_stays_unknown_to_be_retried(mbsettings, session_factory):
    settings = fallback_settings(mbsettings)
    artist_id, mbid = seed_one(session_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{CAA}/release-group/{mbid}/front-250").respond(500)
        mock.get(f"{DEEZER}/search/album").mock(
            return_value=httpx.Response(200, json=deezer_album(artist="Somebody Else"))
        )
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_log(session_factory, job_id, "Attempt failed")
        await worker.stop()
    # Deezer answered "not this album" but the Cover Art Archive could not: not "missing" yet
    assert covers_of(session_factory, artist_id)["Studio Album"] == ("unknown", None)
    await wait_for_status(
        session_factory, job_id, "queued"
    )  # the write of the failure may still be in flight


async def wait_for_log(factory, job_id, text, timeout=5.0):
    import asyncio

    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if any(text in m for m in logs(factory, job_id)):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{text!r} not logged: {logs(factory, job_id)}")


def deezer_by_query(request):
    import re

    q = request.url.params["q"]
    artist, album = re.search(r'artist:"(.*)" album:"(.*)"', q).groups()
    return httpx.Response(200, json=deezer_album(title=album, artist=artist))


def seed_many(factory, count, albums=1):
    with factory() as db:
        ids = []
        for n in range(count):
            artist = make_artist(db, f"Band {n}", mbid=f"{n:08x}-0000-4000-8000-000000000001")
            artist.status = "resolved"
            for k in range(albums):
                make_rg(db, artist, f"Album {n}-{k}", 1990 + k)
                make_album(db, artist, f"Album {n}-{k}", year=1990 + k)
            ids.append(artist.id)
        db.commit()
        return ids


async def test_with_every_provider_down_the_job_waits_without_using_an_attempt(
    mbsettings, session_factory, monkeypatch
):
    monkeypatch.setattr(CachedJsonClient, "BACKOFF_BASE", 0.0)
    settings = fallback_settings(mbsettings)
    (artist_id,) = seed_many(session_factory, 1, albums=4)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{CAA}/.*").respond(500)
        mock.get(url__regex=rf"{DEEZER}/.*").respond(503)
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_log(session_factory, job_id, "attempt not used")
        await worker.stop()
    job = fetch(session_factory, job_id)
    assert job.status == "queued" and job.attempts == 0 and job.next_run_at is not None
    assert all(v == ("unknown", None) for v in covers_of(session_factory, artist_id).values())
    assert any("no provider is answering" in m for m in logs(session_factory, job_id))


async def test_a_waiting_job_does_not_hold_up_the_rest_of_the_queue(mbsettings, session_factory):
    settings = fallback_settings(mbsettings, providers_covers="coverartarchive")
    stuck, fine = seed_many(session_factory, 2, albums=3)
    with factory_rgs(session_factory) as mbids, respx.mock(assert_all_called=False) as mock:
        for rg_mbid in mbids[stuck]:
            mock.get(f"{CAA}/release-group/{rg_mbid}/front-250").respond(500)
        for rg_mbid in mbids[fine]:
            mock.get(f"{CAA}/release-group/{rg_mbid}/front-250").respond(
                200, content=JPEG, headers=PNG_HEADERS
            )
        worker, first = await run_covers(settings, session_factory, stuck)
        await wait_for_log(session_factory, first, "attempt not used")
        second = enqueue(session_factory, "fetch_covers", {"artist_id": fine})
        worker.notify()
        await wait_for_status(session_factory, second, "succeeded")
        await worker.stop()
    assert fetch(session_factory, first).status == "queued"  # still waiting, not failed
    assert {v[0] for v in covers_of(session_factory, fine).values()} == {"ok"}


class factory_rgs:
    """{artist id: [release group mbids]} of the library seeded by seed_many."""

    def __init__(self, factory):
        self.factory = factory

    def __enter__(self):
        with self.factory() as db:
            found: dict[int, list[str]] = {}
            for g in db.scalars(select(ReleaseGroup)):
                found.setdefault(g.artist_id, []).append(g.mbid)
            return found

    def __exit__(self, *exc):
        return False


async def test_a_paused_service_is_skipped_by_the_next_jobs(mbsettings, session_factory):
    settings = fallback_settings(mbsettings, provider_breaker_threshold=2)
    ids = seed_many(session_factory, 4)
    with factory_rgs(session_factory) as mbids, respx.mock(assert_all_called=False) as mock:
        caa = [mock.get(f"{CAA}/release-group/{mbids[a][0]}/front-250").respond(500) for a in ids]
        mock.get(f"{DEEZER}/search/album").mock(side_effect=deezer_by_query)
        mock.get(DEEZER_IMG).respond(200, content=JPEG, headers=PNG_HEADERS)
        worker, first = await run_covers(settings, session_factory, ids[0])
        jobs = [first]
        await wait_for_status(session_factory, first, "succeeded")
        for a in ids[1:]:
            jobs.append(enqueue(session_factory, "fetch_covers", {"artist_id": a}))
            worker.notify()
            await wait_for_status(session_factory, jobs[-1], "succeeded")
        await worker.stop()
    assert sum(r.call_count for r in caa) == 2  # then the breaker paused the service
    assert not any("paused" in m for m in logs(session_factory, jobs[1]))
    assert any("paused after repeated failures" in m for m in logs(session_factory, jobs[2]))
    for artist_id in ids:  # every artist still got its cover, from the fallback
        assert {v[0] for v in covers_of(session_factory, artist_id).values()} == {"ok"}


# ----------------------------------------------------------------- the engine


def test_requeue_later_gives_the_attempt_back(session_factory):
    with session_factory() as db:
        job = engine.enqueue(db, "fetch_covers", {"artist_id": 1})
        db.commit()
        engine.claim_next(db, "coverart")
        engine.requeue_later(db, job.id, "Service paused", 90)
    after = fetch(session_factory, job.id)
    assert after.status == "queued" and after.attempts == 0
    assert after.next_run_at is not None
    with session_factory() as db:
        assert engine.claim_next(db, "coverart") is None  # not before the pause is over


# ------------------------------------------------------------------- settings


def test_the_provider_order_is_saved_from_the_settings_page(app, admin_client):
    page = "/settings/services"
    text = admin_client.get(page).text
    assert 'data-slot="providers_covers-1"' in text and 'data-slot="providers_covers-2"' in text
    r = post(
        admin_client,
        "/settings/services",
        {
            "providers_covers_1": "deezer",
            "providers_covers_2": "coverartarchive",
            "providers_previews_1": "itunes",
            "providers_previews_2": "",
            "providers_similar_1": "lastfm",
            "providers_similar_2": "listenbrainz",
            "provider_breaker_threshold": "3",
            "provider_breaker_cooldown_minutes": "20",
        },
        page=page,
    )
    assert r.status_code == 303
    settings = app.state.settings
    assert settings.providers_covers == "deezer,coverartarchive"
    assert settings.providers_previews == "itunes"
    assert (settings.provider_breaker_threshold, settings.provider_breaker_cooldown_minutes) == (
        3,
        20,
    )
    assert provider_order(settings, "covers") == ["deezer", "coverartarchive"]
    shown = admin_client.get(page).text
    assert '<option value="deezer" selected>' in shown


def test_the_provider_settings_are_validated(app, admin_client):
    page = "/settings/services"
    base = {
        "providers_covers_1": "deezer",
        "providers_previews_1": "itunes",
        "providers_similar_1": "lastfm",
    }
    for bad in (
        {"providers_covers_2": "deezer"},  # duplicate
        {"providers_covers_1": ""},  # no default
        {"providers_covers_2": "napster"},  # unknown provider
    ):
        r = post(
            admin_client,
            "/settings/services",
            {**base, **bad},
            page=page,
        )
        assert r.status_code == 400 and 'data-error="providers_covers"' in r.text, bad
    r = post(
        admin_client,
        "/settings/services",
        {**base, "provider_breaker_threshold": "0"},
        page=page,
    )
    assert r.status_code == 400
    assert app.state.settings.providers_covers == "coverartarchive,deezer"  # nothing was saved


def test_defaults_and_italian(app, admin_client):
    assert app.state.settings.provider_breaker_threshold == 5
    assert app.state.settings.provider_breaker_cooldown_minutes == 10
    post(admin_client, "/lang", {"lang": "it", "next": "/settings/services"})
    text = admin_client.get("/settings/services").text
    assert "Servizi di riserva" in text and "Riserva 1" in text and "Predefinito" in text


def test_the_breaker_helpers_use_the_settings(settings):
    s = settings.model_copy(update={"provider_breaker_threshold": 1})
    BREAKERS.reset()
    breaker.record_failure(s, "x")
    with pytest.raises(ServiceBreakerOpen):
        breaker.check(s, "x")
    breaker.record_success("x")
    breaker.check(s, "x")


# ------------------------------------------------- previews and metadata


async def test_previews_follow_the_configured_providers(mbsettings, session_factory):
    from app.jobs.worker import JobWorker

    settings = mbsettings.model_copy(update={"providers_previews": "deezer"})
    with session_factory() as db:
        artist = make_artist(db, "Band", mbid="a" * 8 + "-0000-4000-8000-000000000001")
        rg = make_rg(db, artist, "Studio Album", 1990)
        db.commit()
        rg_id = rg.id
    with respx.mock(assert_all_called=False) as mock:
        itunes = mock.get(url__regex=r"https://itunes\.apple\.com/.*").respond(200, json={})
        mock.get(f"{DEEZER}/search/album").mock(
            return_value=httpx.Response(200, json=deezer_album())
        )
        mock.get(f"{DEEZER}/album/7/tracks").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 1,
                            "title": "One",
                            "track_position": 1,
                            "disk_number": 1,
                            "preview": "https://cdnt-preview.dzcdn.net/x.mp3",
                        }
                    ]
                },
            )
        )
        worker = JobWorker(settings, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "resolve_previews", {"release_group_id": rg_id})
        worker.notify()
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert itunes.call_count == 0  # not in the list: never asked
    assert any("deezer: 1 preview(s) saved" in m for m in logs(session_factory, job_id))


async def test_previews_wait_when_every_provider_is_paused(mbsettings, session_factory):
    from app.jobs.worker import JobWorker

    with session_factory() as db:
        artist = make_artist(db, "Band", mbid="a" * 8 + "-0000-4000-8000-000000000001")
        rg = make_rg(db, artist, "Studio Album", 1990)
        db.commit()
        rg_id = rg.id
    for service in ("itunes", "deezer"):
        for _ in range(mbsettings.provider_breaker_threshold):
            breaker.record_failure(mbsettings, service)
    with respx.mock(assert_all_called=False) as mock:
        worker = JobWorker(mbsettings, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "resolve_previews", {"release_group_id": rg_id})
        worker.notify()
        await wait_for_log(session_factory, job_id, "attempt not used")
        await worker.stop()
        assert not mock.calls  # nothing was asked
    job = fetch(session_factory, job_id)
    assert job.status == "queued" and job.attempts == 0


async def test_metadata_requests_stop_while_musicbrainz_is_paused(make_client, mbsettings):
    from app.integrations.musicbrainz_client import MusicBrainzError

    s = mbsettings.model_copy(update={"provider_breaker_threshold": 1, "mb_max_retries": 0})
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*").respond(503)
        async with make_client(settings=s) as client:
            with pytest.raises(MusicBrainzError, match="not answering"):
                await client.lookup_artist("a" * 8 + "-0000-4000-8000-000000000001")
            calls = route.call_count
            with pytest.raises(
                ServiceBreakerOpen
            ):  # a RetryLater: the job waits, keeps its attempt
                await client.lookup_artist("b" * 8 + "-0000-4000-8000-000000000002")
            assert route.call_count == calls
