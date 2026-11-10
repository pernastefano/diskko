"""Similar albums: the albums of the artists similar to the album's artist."""

from app.integrations.discovery_types import SimilarHit
from app.services import discovery as disc
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.mbdata import ARETHA
from tests.test_discovery_clients import no_waiting  # noqa: F401  (autouse fixture)
from tests.test_discovery_flow import settings  # noqa: F401

OTHER = "7" * 8 + "-0000-4000-8000-000000000007"


def world(db):
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA, in_library=True)
    group = make_rg(db, artist, "Lady Soul", 1968)
    twin = make_artist(db, "Gladys Knight", mbid=OTHER, in_library=False)
    old = make_rg(db, twin, "Imagination", 1973)
    new = make_rg(db, twin, "Still Together", 1994)
    third = make_rg(db, twin, "Another Year", 1980)
    make_rg(db, twin, "A Single", 1975, primary="Single")
    disc.store_similar(
        db,
        artist.id,
        [SimilarHit("Gladys Knight", OTHER, 0.9), SimilarHit("Nobody Known", None, 0.8)],
        "lastfm",
    )
    db.commit()
    return artist, group, twin, (old, new, third)


def test_the_page_asks_for_the_similar_albums_and_lists_those_it_knows(admin_client, db):
    _, group, _, (old, new, third) = world(db)
    assert f"/album/{group.id}/similar" in admin_client.get(f"/album/{group.id}").text
    html = post(admin_client, f"/album/{group.id}/similar").text
    assert 'data-similar-albums-state="ready"' in html
    # two albums of the similar artist (the newest), no single, none of the artist itself
    assert f'data-similar-album="{new.id}"' in html and f'data-similar-album="{third.id}"' in html
    assert f'data-similar-album="{old.id}"' not in html and "A Single" not in html
    assert f'href="/album/{new.id}"' in html


def test_an_owned_album_comes_first_and_is_marked(admin_client, db):
    _, group, twin, (old, new, third) = world(db)
    owned = make_album(db, twin, "Imagination", 1973)
    owned.release_group_id, owned.match_status = old.id, "matched"
    db.commit()
    html = post(admin_client, f"/album/{group.id}/similar").text
    assert f'data-similar-album="{old.id}"' in html and "data-in-library" in html
    assert html.index(f'data-similar-album="{old.id}"') < html.index(
        f'data-similar-album="{new.id}"'
    )


def test_without_known_albums_the_section_says_so(admin_client, db):
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA, in_library=True)
    group = make_rg(db, artist, "Lady Soul", 1968)
    disc.store_similar(db, artist.id, [SimilarHit("Nobody Known", None, 0.8)], "lastfm")
    db.commit()
    html = post(admin_client, f"/album/{group.id}/similar").text
    assert 'data-similar-albums-state="empty"' in html


def test_the_list_is_looked_up_by_a_job_when_missing(admin_client, db):
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA, in_library=True)
    group = make_rg(db, artist, "Lady Soul", 1968)
    html = post(admin_client, f"/album/{group.id}/similar").text
    assert 'data-similar-albums-state="loading"' in html and "every 3s" in html


def test_the_mobile_page_has_the_section_too(admin_client, db):
    _, group, _, (_, new, _) = world(db)
    assert f"/m/album/{group.id}/similar" in admin_client.get(f"/m/album/{group.id}").text
    html = post(admin_client, f"/m/album/{group.id}/similar", page="/m/albums").text
    assert f'href="/m/album/{new.id}"' in html


def test_an_unknown_album_is_a_404(admin_client):
    assert admin_client.get("/album/99999/similar").status_code == 404
