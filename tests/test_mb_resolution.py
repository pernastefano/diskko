import httpx
import pytest
import respx

from app.models import MB_IGNORED, MB_RESOLVED, MB_REVIEW
from app.services.musicbrainz.resolution import (
    NAMESAKES_TO_COMPARE,
    VARIOUS_ARTISTS_MBID,
    resolve_artist,
)
from tests.conftest import MB_BASE as BASE
from tests.mbdata import (
    ARETHA,
    NIRVANA_UK,
    NIRVANA_US,
    browse_router,
    load,
    nirvana_discographies,
)

US_ALBUMS = ["Nevermind", "In Utero", "Bleach"]
UK_ALBUMS = ["Local Anaesthetic", "Songs of Love and Praise"]


def search_json(*artists):
    return {"count": len(artists), "offset": 0, "artists": list(artists)}


def artist(mbid, name, score=100, aliases=(), **kw):
    return {
        "id": mbid,
        "name": name,
        "sort-name": name,
        "score": str(score),
        "aliases": [{"name": a} for a in aliases],
        **kw,
    }


async def resolve(make_client, name, albums, search, discographies=None, mock_extra=None):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/artist").respond(json=search)
        route = mock.get("/release-group").mock(side_effect=browse_router(discographies or {}))
        async with make_client() as client:
            result = await resolve_artist(client, name, albums)
    return result, route


async def test_a_single_exact_match_is_accepted(make_client):
    result, route = await resolve(
        make_client, "Aretha Franklin", ["Lady Soul"], load("artist_search_aretha.json")
    )
    assert (result.status, result.mbid, result.source, result.confidence) == (
        MB_RESOLVED,
        ARETHA,
        "auto",
        1.0,
    )
    assert route.call_count == 0  # no discography needed


@pytest.mark.parametrize("written", ["ARETHA FRANKLIN", "aretha  franklin", "Aretha Franklin "])
async def test_case_and_spacing_do_not_matter(make_client, written):
    result, _ = await resolve(make_client, written, [], load("artist_search_aretha.json"))
    assert result.mbid == ARETHA


async def test_accents_and_punctuation_do_not_matter(make_client):
    search = search_json(artist("a" * 8 + "-0000-4000-8000-" + "0" * 12, "Beyoncé"))
    result, _ = await resolve(make_client, "BEYONCE", [], search)
    assert result.status == MB_RESOLVED


async def test_an_alias_counts_as_an_exact_name(make_client):
    search = search_json(
        artist(
            "b" * 8 + "-0000-4000-8000-" + "0" * 12, "Prince Rogers Nelson", aliases=["The Artist"]
        )
    )
    result, _ = await resolve(make_client, "The Artist", [], search)
    assert result.status == MB_RESOLVED


async def test_namesakes_are_told_apart_by_the_library_albums(make_client):
    result, route = await resolve(
        make_client,
        "Nirvana",
        US_ALBUMS,
        load("artist_search_nirvana.json"),
        nirvana_discographies(),
    )
    assert (result.status, result.mbid, result.confidence) == (MB_RESOLVED, NIRVANA_US, 0.9)
    assert route.call_count == NAMESAKES_TO_COMPARE  # only the top contenders are browsed


async def test_the_less_famous_namesake_wins_when_its_albums_are_in_the_library(make_client):
    """The UK band has a lower search score than the US one, but its albums are the ones owned."""
    result, _ = await resolve(
        make_client,
        "Nirvana",
        UK_ALBUMS,
        load("artist_search_nirvana.json"),
        nirvana_discographies(),
    )
    assert (result.status, result.mbid) == (MB_RESOLVED, NIRVANA_UK)


async def test_namesakes_without_evidence_go_to_review(make_client):
    result, _ = await resolve(
        make_client,
        "Nirvana",
        ["Some Unknown Record"],
        load("artist_search_nirvana.json"),
        nirvana_discographies(),
    )
    assert (result.status, result.reason, result.mbid) == (MB_REVIEW, "ambiguous", None)
    assert len(result.candidates) >= 3
    first = result.candidates[0]
    assert {
        "mbid",
        "name",
        "disambiguation",
        "country",
        "type",
        "score",
        "similarity",
        "overlap",
    } <= set(first)
    assert {c["mbid"] for c in result.candidates} >= {NIRVANA_US, NIRVANA_UK}


async def test_a_tie_between_namesakes_goes_to_review(make_client):
    shared = [
        {
            "id": "c" * 8 + "-0000-4000-8000-" + "0" * 12,
            "title": "Shared Title",
            "primary-type": "Album",
        }
    ]
    discographies = {NIRVANA_US: shared, NIRVANA_UK: shared}
    result, _ = await resolve(
        make_client, "Nirvana", ["Shared Title"], load("artist_search_nirvana.json"), discographies
    )
    assert result.status == MB_REVIEW and result.reason == "ambiguous"


async def test_no_library_albums_means_no_way_to_choose_between_namesakes(make_client):
    result, _ = await resolve(
        make_client, "Nirvana", [], load("artist_search_nirvana.json"), nirvana_discographies()
    )
    assert result.status == MB_REVIEW and result.reason == "ambiguous"


async def test_no_search_result_goes_to_review(make_client):
    result, _ = await resolve(
        make_client, "Zzqxv Nonexistent Quartet", ["X"], load("artist_search_none.json")
    )
    assert (result.status, result.reason, result.candidates) == (MB_REVIEW, "no_match", [])


async def test_a_close_misspelling_of_a_clear_best_hit_is_accepted(make_client):
    result, _ = await resolve(make_client, "Aretha Frankiln", [], load("artist_search_aretha.json"))
    assert (result.status, result.mbid, result.confidence) == (MB_RESOLVED, ARETHA, 0.8)


async def test_a_vague_name_is_never_guessed(make_client):
    result, _ = await resolve(
        make_client, "Franklin", ["Lady Soul"], load("artist_search_aretha.json")
    )
    assert (result.status, result.reason) == (MB_REVIEW, "low_score")
    assert result.candidates[0]["mbid"] == ARETHA


async def test_two_equally_close_hits_are_not_guessed(make_client):
    search = search_json(
        artist("d" * 8 + "-0000-4000-8000-" + "0" * 12, "The Smithe", 95),
        artist("e" * 8 + "-0000-4000-8000-" + "0" * 12, "The Smithy", 94),
    )
    result, _ = await resolve(make_client, "The Smiths", [], search)
    assert result.status == MB_REVIEW and result.reason == "low_score"
    assert len(result.candidates) == 2


async def test_review_candidates_are_limited_and_ordered_by_name_similarity(make_client):
    hits = [
        artist(f"{i:08x}-0000-4000-8000-000000000000", f"Franklin Fan {'x' * i}", 80)
        for i in range(9)
    ]
    result, _ = await resolve(make_client, "Franklin", [], search_json(*hits))
    sims = [c["similarity"] for c in result.candidates]
    assert len(sims) == 5 and sims == sorted(sims, reverse=True)


async def test_various_artists_needs_no_search(make_client):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        async with make_client() as client:
            result = await resolve_artist(client, "Various Artists", [])
    assert (result.status, result.mbid) == (MB_RESOLVED, VARIOUS_ARTISTS_MBID)
    assert mock.calls.call_count == 0


async def test_unknown_artist_is_ignored_without_a_search(make_client):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        async with make_client() as client:
            result = await resolve_artist(client, "Unknown Artist", ["Some Album"])
    assert result.status == MB_IGNORED and mock.calls.call_count == 0


async def test_musicbrainz_errors_propagate_to_the_job(make_client):
    from app.integrations.musicbrainz_client import MusicBrainzError

    with respx.mock(base_url=BASE) as mock:
        mock.get("/artist").mock(return_value=httpx.Response(400))
        async with make_client() as client:
            with pytest.raises(MusicBrainzError):
                await resolve_artist(client, "Anyone", [])
