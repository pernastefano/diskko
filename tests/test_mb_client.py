from collections import Counter
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
    cache_key,
    lucene_phrase,
)
from app.models import ApiCache
from tests.conftest import MB_BASE as BASE
from tests.conftest import MB_UA as UA
from tests.mbdata import (
    ARETHA,
    NIRVANA_US,
    load,
    synthetic_release_groups,
)


def aretha_pages():
    p0, p1 = load("rg_browse_aretha_p0.json"), load("rg_browse_aretha_p1.json")
    total = p0["release-group-count"]
    pages = {0: p0["release-groups"], 100: p1["release-groups"]}
    pages[200] = synthetic_release_groups(100, "synthetic-a")
    pages[300] = synthetic_release_groups(total - 300, "synthetic-b")
    return total, pages


# ---------------------------------------------------------------------- search


async def test_search_parses_the_recorded_answer(make_client):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(json=load("artist_search_aretha.json"))
        async with make_client() as client:
            hits = await client.search_artists("Aretha Franklin")
    [hit] = hits
    assert (hit.mbid, hit.name, hit.score, hit.country, hit.type) == (
        ARETHA,
        "Aretha Franklin",
        100,
        "US",
        "Person",
    )
    request = route.calls.last.request
    assert request.headers["User-Agent"] == UA
    assert request.headers["Accept"] == "application/json"
    assert request.url.params["fmt"] == "json" and request.url.params["limit"] == "10"
    assert request.url.params["query"] == 'artist:"Aretha Franklin"'


async def test_search_with_ambiguous_names(make_client):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").respond(json=load("artist_search_nirvana.json"))
        async with make_client() as client:
            hits = await client.search_artists("Nirvana")
    assert len(hits) == 10 and [h.name for h in hits[:2]] == ["Nirvana", "Nirvana"]
    assert hits[0].mbid == NIRVANA_US and hits[0].score == 100 and hits[1].score < 100
    assert "grunge" in hits[0].disambiguation


async def test_search_without_results(make_client):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").respond(json=load("artist_search_none.json"))
        async with make_client() as client:
            assert await client.search_artists("Zzqxv Nonexistent Quartet") == []


def test_query_values_are_escaped():
    assert lucene_phrase('Guns "N" Roses') == '"Guns \\"N\\" Roses"'
    assert lucene_phrase("AC\\DC") == '"AC\\\\DC"'
    assert lucene_phrase("Sigur Rós") == '"Sigur Rós"'


async def test_search_sends_the_escaped_query(make_client):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(json={"artists": []})
        async with make_client() as client:
            await client.search_artists('Guns "N" Roses')
    assert route.calls.last.request.url.params["query"] == 'artist:"Guns \\"N\\" Roses"'


async def test_lookup_artist(make_client):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get(f"/artist/{ARETHA}").respond(json=load("artist_lookup_aretha.json"))
        async with make_client() as client:
            hit = await client.lookup_artist(ARETHA)
    assert hit.mbid == ARETHA and hit.name == "Aretha Franklin" and route.called


async def test_lookup_of_an_unknown_artist_is_none(make_client):
    with respx.mock(base_url=BASE) as mock:
        mock.get(url__regex=r".*/artist/.*").respond(404, json={"error": "Not Found"})
        async with make_client() as client:
            assert await client.lookup_artist("00000000-0000-4000-8000-000000000000") is None


# ---------------------------------------------------------------------- browse


async def test_browse_follows_the_pages(make_client):
    total, pages = aretha_pages()
    seen = []

    def answer(request):
        offset = int(request.url.params["offset"])
        seen.append((offset, request.url.params["limit"], request.url.params["artist"]))
        return httpx.Response(
            200,
            json={
                "release-group-count": total,
                "release-group-offset": offset,
                "release-groups": pages[offset],
            },
        )

    progress = []
    with respx.mock(base_url=BASE) as mock:
        mock.get("/release-group").mock(side_effect=answer)
        async with make_client() as client:
            groups = await client.browse_release_groups(
                ARETHA, on_page=lambda *a: progress.append(a)
            )
    assert len(groups) == total == 356
    assert seen == [
        (0, "100", ARETHA),
        (100, "100", ARETHA),
        (200, "100", ARETHA),
        (300, "100", ARETHA),
    ]
    assert progress[0] == (100, 356) and progress[-1] == (356, 356)
    assert len({g.mbid for g in groups}) == 356


async def test_browse_parses_types_dates_and_categories(make_client):
    p0 = load("rg_browse_aretha_p0.json")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/release-group").respond(json={**p0, "release-group-count": 100})
        async with make_client() as client:
            groups = await client.browse_release_groups(ARETHA)
    assert Counter(g.category for g in groups) == {"compilation": 58, "album": 41, "other": 1}
    rose = next(g for g in groups if g.title == "A Rose Is Still a Rose")
    assert (rose.primary_type, rose.first_release_date, rose.year) == ("Album", "1998-03-24", 1998)
    no_primary = next(g for g in groups if g.primary_type is None)
    assert no_primary.category == "other"
    assert all(len(g.mbid) == 36 for g in groups)


async def test_browse_of_an_unknown_artist_is_none(make_client):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/release-group").respond(404, json={"error": "Not Found"})
        async with make_client() as client:
            assert await client.browse_release_groups(ARETHA) is None


async def test_browse_of_an_artist_without_release_groups(make_client):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/release-group").respond(json={"release-group-count": 0, "release-groups": []})
        async with make_client() as client:
            assert await client.browse_release_groups(ARETHA) == []


async def test_malformed_entries_are_skipped(make_client):
    body = {
        "release-group-count": 3,
        "release-groups": [
            {"id": "x" * 36, "title": "Ok", "primary-type": "Album"},
            {"title": "no id"},
            {"id": "y" * 36},
        ],
    }
    with respx.mock(base_url=BASE) as mock:
        mock.get("/release-group").respond(json=body)
        async with make_client() as client:
            groups = await client.browse_release_groups(ARETHA)
    assert [g.title for g in groups] == ["Ok"]


# --------------------------------------------------------------- rate and retry


async def test_requests_are_spaced_by_the_limiter(make_client, ft):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").respond(json={"artists": []})
        async with make_client() as client:
            for name in ("A", "B", "C"):
                await client.search_artists(name)
    assert len(ft.sleeps) == 2 and all(s >= 1.1 - 1e-9 for s in ft.sleeps)


async def test_503_is_retried_with_a_growing_pause(make_client, ft):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(200, json=load("artist_search_aretha.json")),
            ]
        )
        async with make_client() as client:
            hits = await client.search_artists("Aretha Franklin")
    assert len(hits) == 1 and route.call_count == 3
    assert ft.sleeps[0] >= 2.0 - 1e-9 and ft.sleeps[1] >= 4.0 - 1e-9  # 2 s, then 4 s


async def test_retry_after_is_honoured_up_to_a_cap(make_client, ft):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").mock(
            side_effect=[
                httpx.Response(503, headers={"Retry-After": "7"}),
                httpx.Response(200, json={"artists": []}),
            ]
        )
        async with make_client() as client:
            await client.search_artists("A")
    assert ft.sleeps[0] >= 7 - 1e-9


async def test_persistent_503_gives_up_after_the_retries(make_client):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(503)
        async with make_client() as client:
            with pytest.raises(MusicBrainzError, match="HTTP 503"):
                await client.search_artists("A")
    assert route.call_count == 4  # first try + 3 retries


async def test_network_errors_are_retried_without_leaking_the_url(make_client):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").mock(side_effect=httpx.ConnectError("refused musicbrainz.test"))
        async with make_client() as client:
            with pytest.raises(MusicBrainzError) as exc:
                await client.search_artists("A")
    assert route.call_count == 4 and "ConnectError" in str(exc.value)
    assert "musicbrainz.test" not in str(exc.value)


async def test_network_error_then_success(make_client):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"artists": []})]
        )
        async with make_client() as client:
            assert await client.search_artists("A") == []


async def test_bad_request_is_not_retried(make_client):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(400)
        async with make_client() as client:
            with pytest.raises(MusicBrainzError, match="400"):
                await client.search_artists("A")
    assert route.call_count == 1


async def test_invalid_json_is_an_error(make_client):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").respond(200, text="<html>nope</html>")
        async with make_client() as client:
            with pytest.raises(MusicBrainzError, match="invalid JSON"):
                await client.search_artists("A")


async def test_user_agent_is_mandatory(mbsettings, session_factory):
    with pytest.raises(MusicBrainzNotConfigured, match="MB_USER_AGENT"):
        MusicBrainzClient(mbsettings.model_copy(update={"mb_user_agent": "  "}), session_factory)


# ------------------------------------------------------------------------ cache


async def test_second_call_is_served_from_the_cache(make_client, ft):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(json=load("artist_search_aretha.json"))
        async with make_client() as client:
            first = await client.search_artists("Aretha Franklin")
            slept = list(ft.sleeps)
            second = await client.search_artists("Aretha Franklin")
            assert client.requests_made == 1
    assert first == second and route.call_count == 1 and ft.sleeps == slept  # no limiter use


async def test_cache_is_shared_between_clients_through_the_database(make_client, session_factory):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(json=load("artist_search_aretha.json"))
        async with make_client() as client:
            await client.search_artists("Aretha Franklin")
        async with make_client() as client:
            await client.search_artists("Aretha Franklin")
    assert route.call_count == 1
    with session_factory() as db:
        assert db.scalar(select(func.count(ApiCache.key))) == 1


async def test_cache_expires_after_the_ttl(make_client, clock):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(json={"artists": []})
        async with make_client() as client:
            await client.search_artists("A")
            clock.state["now"] += timedelta(days=6)
            await client.search_artists("A")
            assert route.call_count == 1
            clock.state["now"] += timedelta(days=2)
            await client.search_artists("A")
    assert route.call_count == 2


async def test_force_bypasses_the_cache(make_client):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/artist").respond(json={"artists": []})
        async with make_client() as client:
            await client.search_artists("A")
            await client.search_artists("A", force=True)
    assert route.call_count == 2


async def test_not_found_is_cached_for_a_day(make_client, clock):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get(f"/artist/{ARETHA}").respond(404)
        async with make_client() as client:
            assert await client.lookup_artist(ARETHA) is None
            assert await client.lookup_artist(ARETHA) is None
            assert route.call_count == 1
            clock.state["now"] += timedelta(days=2)
            assert await client.lookup_artist(ARETHA) is None
    assert route.call_count == 2


async def test_failures_are_never_cached(make_client, session_factory):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").respond(503)
        async with make_client() as client:
            with pytest.raises(MusicBrainzError):
                await client.search_artists("A")
    with session_factory() as db:
        assert db.scalar(select(func.count(ApiCache.key))) == 0


def test_cache_key_ignores_parameter_order_and_user_agent():
    assert cache_key("artist", {"a": 1, "b": 2}) == cache_key("artist", {"b": 2, "a": 1})
    assert cache_key("artist", {"a": 1}) != cache_key("artist", {"a": 2})
    assert cache_key("artist", {"a": 1}) != cache_key("release-group", {"a": 1})


async def test_nirvana_browse_pages_are_cached_per_artist(make_client):
    p = load("rg_browse_nirvana_us.json")
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/release-group").respond(json={**p, "release-group-count": 100})
        async with make_client() as client:
            await client.browse_release_groups(NIRVANA_US)
            await client.browse_release_groups(NIRVANA_US)
    assert route.call_count == 1
