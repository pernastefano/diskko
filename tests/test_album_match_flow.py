"""The match page: the artist found on MusicBrainz, the versions of an album, tags updated."""

import json

import respx
from sqlalchemy import select

from app.integrations.musicbrainz_client import peek_artist_search, peek_group_releases
from app.models import Artist, Job, LibraryAlbum, RetagReport
from tests import test_album_compare as base
from tests.conftest import post
from tests.mbdata import ARETHA, LADY_SOUL_RG
from tests.mbdata import load as mb_load
from tests.test_discovery_flow import (
    MB,
    drain,
    jobs,
    mount_releases,
    no_waiting,  # noqa: F401  (autouse fixture)
)

settings = base.settings
world = base.world

SEARCH_Q = "Lady Soul"


def page(world):
    return f"/admin/albums/{world['album'].id}/match"


OTHER_ARETHA = "3" * 8 + "-0000-4000-8000-000000000003"


def artist_hits(mock):
    """Aretha Franklin, and another artist with the same name."""
    data = mb_load("artist_search_aretha.json")
    twin = {**data["artists"][0], "id": OTHER_ARETHA, "disambiguation": "the other one"}
    mock.get(f"{MB}/artist").respond(json={**data, "artists": [data["artists"][0], twin]})


def search_artist(app, client, world, text="Aretha"):
    post(
        client,
        page(world).replace("/match", "/artist/search"),
        {"artist_q": text},
        page=page(world),
    )
    with respx.mock(assert_all_called=False) as mock:
        artist_hits(mock)
        drain(app)


# ------------------------------------------------------------------------ the artist


def test_the_artist_search_is_a_job_and_lists_what_musicbrainz_found(app, admin_client, world, db):
    r = post(
        admin_client,
        page(world).replace("/match", "/artist/search"),
        {"artist_q": " Aretha "},
        page=page(world),
    )
    assert r.status_code == 303 and "artist_q=Aretha" in r.headers["location"]
    assert 'data-artist-state="searching"' in admin_client.get(r.headers["location"]).text
    [job] = jobs(db, "search_artists")
    assert json.loads(job.payload_json) == {"query": "Aretha"}
    with respx.mock(assert_all_called=False) as mock:
        artist_hits(mock)
        drain(app)
    html = admin_client.get(page(world) + "?artist_q=Aretha").text
    assert "data-artist-choices" in html and 'data-artist-row="mb"' in html
    assert 'data-artist-row="new"' in html and "Create a new artist “Aretha”" in html
    assert 'name="target"' in html and "data-artist-cancel" in html
    assert "Cancel (back to the original artist)" in html


def test_a_search_result_becomes_the_albums_artist(app, admin_client, world, db):
    search_artist(app, admin_client, world, "Aretha Franklin")
    with app.state.session_factory() as s:
        hits = peek_artist_search(s, "Aretha Franklin", 15)
    other = next(h for h in hits if h.mbid == OTHER_ARETHA)
    r = post(
        admin_client,
        page(world).replace("/match", "/artist"),
        {"target": f"mb:{other.mbid}", "artist_q": "Aretha Franklin"},
        page=page(world),
    )
    assert r.status_code == 303, r.text
    db.expire_all()
    moved = db.get(Artist, db.get(LibraryAlbum, world["album"].id).artist_id)
    assert moved.mbid == other.mbid and moved.name == other.name and moved.in_library
    assert db.get(LibraryAlbum, world["album"].id).artist_id != world["artist"].id


def test_a_new_artist_without_musicbrainz_can_be_created_from_the_field(
    app, admin_client, world, db
):
    r = post(
        admin_client,
        page(world).replace("/match", "/artist"),
        {"target": "new", "artist_q": "Aretha Franklin & Band"},
        page=page(world),
    )
    assert r.status_code == 303
    db.expire_all()
    moved = db.get(Artist, db.get(LibraryAlbum, world["album"].id).artist_id)
    assert moved.name == "Aretha Franklin & Band" and moved.mbid is None


def test_an_answer_that_is_not_cached_is_refused_and_nothing_moves(admin_client, world, db):
    r = post(
        admin_client,
        page(world).replace("/match", "/artist"),
        {"target": f"mb:{ARETHA}", "artist_q": "Nobody here"},
        page=page(world),
    )
    assert r.status_code == 400
    db.expire_all()
    assert db.get(LibraryAlbum, world["album"].id).artist_id == world["artist"].id


def test_the_page_starts_again_from_the_original_artist(admin_client, world):
    html = admin_client.get(page(world)).text
    assert 'value="Aretha Franklin"' in html and "data-artist-reset" not in html
    searched = admin_client.get(page(world) + "?artist_q=Someone").text
    assert 'value="Someone"' in searched and f'href="{page(world)}#artist"' in searched


# ------------------------------------------------------------------------- the album


def start_versions(client, world, rg=LADY_SOUL_RG):
    return post(
        client,
        page(world) + "/releases",
        {"rg": rg, "q": SEARCH_Q, "mode": "title", "scope": "all"},
        page=page(world),
    )


def test_the_versions_are_read_by_a_job_and_each_is_compared_with_the_files(
    app, admin_client, world, db
):
    r = start_versions(admin_client, world)
    assert r.status_code == 200 and 'data-releases-state="loading"' in r.text
    start_versions(admin_client, world)  # asking again does not queue it again
    with respx.mock(assert_all_called=False) as mock:
        route = mount_releases(mock) or mock.routes[0]
        drain(app)
        assert route.called
    assert len(jobs(db, "fetch_group_releases")) == 1
    with app.state.session_factory() as s:
        assert len(peek_group_releases(s, LADY_SOUL_RG)) == 15
    html = admin_client.get(
        page(world) + f"/releases?rg={LADY_SOUL_RG}&q=Lady+Soul&mode=title&scope=all"
    ).text
    assert 'data-releases-state="ready"' in html and html.count("data-release=") == 15
    assert "15 release(s) of this album" in html and "data-use-release" in html
    assert "have a track here" in html and "data-compare-tracks" in html
    # the best fitting come first: no release has fewer matches than the one after it
    fits = [int(x) for x in __import__("re").findall(r"(\d+) of your 10 file", html)]
    assert fits == sorted(fits, reverse=True) and fits[0] >= 7


def test_the_versions_are_never_asked_by_a_get(admin_client, world, db):
    html = admin_client.get(page(world) + f"/releases?rg={LADY_SOUL_RG}").text
    assert 'data-releases-state="idle"' in html and not jobs(db, "fetch_group_releases")
    assert admin_client.get(page(world) + "/releases?rg=nope").status_code == 404


def test_using_a_release_matches_the_album_and_queues_the_tag_update(app, admin_client, world, db):
    from tests.test_album_match_page import fill_cache, rg_json

    query = __import__("app.integrations.musicbrainz_client", fromlist=["x"]).release_group_query(
        SEARCH_Q, "title", None
    )
    fill_cache(app.state.settings, app, query, [rg_json(LADY_SOUL_RG, "Lady Soul")])
    release = "1" * 8 + "-0000-4000-8000-000000000001"
    r = post(
        admin_client,
        page(world),
        {
            "choice": f"mbid:{LADY_SOUL_RG}",
            "q": SEARCH_Q,
            "mode": "title",
            "scope": "all",
            "release": release,
            "retag": "1",
        },
        page=page(world),
    )
    assert r.status_code == 303 and r.headers["location"].endswith("flash=match_saved_retag")
    db.expire_all()
    album = db.get(LibraryAlbum, world["album"].id)
    assert album.match_status == "manual" and album.release_group_id is not None
    [job] = db.scalars(select(Job).where(Job.type == "retag_album")).all()
    payload = json.loads(job.payload_json)
    assert payload["apply"] is True and payload["rename"] is True
    assert payload["release_mbid"] == release and payload["album_id"] == album.id
    assert db.scalar(select(RetagReport).where(RetagReport.album_id == album.id)) is not None


def test_without_the_box_the_tags_are_left_alone(app, admin_client, world, db):
    group = world["group"]
    r = post(
        admin_client,
        page(world),
        {"choice": str(group.id), "release": "1" * 8 + "-0000-4000-8000-000000000001"},
        page=page(world),
    )
    assert r.status_code == 303 and r.headers["location"].endswith("flash=match_saved")
    assert not jobs(db, "retag_album")


def test_an_album_of_another_source_is_listed_and_can_be_used(admin_client, world, db):
    from tests.dbhelpers import make_rg

    fallback = make_rg(db, world["artist"], "Lady Soul (Deluxe Edition)", 2013, mbid="deezer:123")
    db.commit()
    html = admin_client.get(page(world) + "?q=Lady+Soul&mode=title&scope=all").text
    assert "data-fallbacks" in html and f'data-rg="{fallback.id}"' in html
    r = post(admin_client, page(world), {"choice": str(fallback.id)}, page=page(world))
    assert r.status_code == 303
    db.expire_all()
    assert db.get(LibraryAlbum, world["album"].id).release_group_id == fallback.id


def test_only_an_admin_uses_these_routes(user_client, world):
    assert user_client.get(page(world) + f"/releases?rg={LADY_SOUL_RG}").status_code == 403
    assert user_client.get(page(world) + "/../artist/results").status_code in (403, 404)
