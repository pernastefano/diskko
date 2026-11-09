"""The search page lists what the library has (artists, albums, years) before MusicBrainz."""

import re

import pytest

from tests.dbhelpers import make_album, make_artist, make_rg


@pytest.fixture
def library(db):
    band = make_artist(db, "Dance Hall Crashers")
    other = make_artist(db, "Aretha Franklin")
    for artist, title, year in (
        (band, "Lady Soul", 1968),
        (other, "Lady Soul", 1968),
        (other, "Amazing Grace", 1972),
    ):
        album = make_album(db, artist, title, year)
        group = make_rg(db, artist, title, year)
        album.release_group_id, album.match_status = group.id, "matched"
    db.commit()
    return {"band": band, "other": other}


def block(html, name):
    found = re.search(rf"data-library-{name}>(.*?)(?=data-library-|</section>)", html, re.S)
    return found.group(1) if found else ""


def test_an_album_title_finds_the_album(user_client, library):
    html = user_client.get("/search?q=amazing").text
    assert "In your library" in html and "Amazing Grace" in block(html, "albums")
    assert "data-library-artists" not in html  # no artist is called that


def test_an_artist_name_finds_the_artist_and_their_albums(user_client, library):
    html = user_client.get("/search?q=aretha").text
    assert "Aretha Franklin" in block(html, "artists")
    albums = block(html, "albums")
    assert albums.count('data-album="') == 2 and "Amazing Grace" in albums


def test_a_year_finds_the_albums_of_that_year(user_client, library):
    html = user_client.get("/search?q=1968").text
    assert block(html, "albums").count('data-album="') == 2
    assert "Amazing Grace" not in html


def test_a_word_in_an_artist_name_is_not_a_genre_search(user_client, library):
    html = user_client.get("/search?q=dance").text
    assert "Dance Hall Crashers" in block(html, "artists")


def test_nothing_in_the_library_hides_the_section_and_a_short_text_searches_nothing(
    user_client, library
):
    assert "data-library-results" not in user_client.get("/search?q=zzzzqq").text
    assert "data-library-results" not in user_client.get("/search?q=a").text
    assert "data-library-results" not in user_client.get("/search").text


def test_musicbrainz_results_keep_their_own_heading(user_client, library):
    html = user_client.get("/search?q=amazing").text
    assert "Artists on MusicBrainz" in html
