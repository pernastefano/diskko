"""The Albums page: every owned album, filtered by quality, format, type and text."""

import re

import pytest

from app.models import LibraryAlbum
from app.services.library_views import PAGE_SIZE
from tests.dbhelpers import make_album, make_artist, make_rg

HX = {"HX-Request": "true"}


def tile_titles(html):
    return re.findall(
        r'data-album="\d+".*?class="truncate font-medium[^"]*" title="([^"]*)"', html, re.S
    )


def page(client, query=""):
    r = client.get(f"/albums{query}")
    assert r.status_code == 200, (query, r.status_code)
    return r.text


@pytest.fixture
def albums(db):
    """Alpha One (FLAC), Alpha Live (MP3 320, a live album), Beta Tape (not matched, low quality)."""
    alpha, beta = make_artist(db, "Alpha"), make_artist(db, "Beta")
    one = make_album(db, alpha, "Alpha One", 2000)
    live = make_album(db, alpha, "Alpha Live", 2005)
    tape = make_album(db, beta, "Beta Tape", 1999)
    g1 = make_rg(db, alpha, "Alpha One", 2000)
    g2 = make_rg(db, alpha, "Alpha Live", 2005, secondary=("Live",))
    for album, group, tier, fmt in (
        (one, g1, "flac_standard", "flac"),
        (live, g2, "mp3_320", "mp3"),
    ):
        album.release_group_id, album.match_status = group.id, "matched"
        album.quality_tier, album.format = tier, fmt
    tape.quality_tier, tape.format = "any", "mp3"
    g1.cover_status, g1.cover_path = "ok", "cover-one.jpg"
    db.commit()
    return {"one": one, "live": live, "tape": tape, "g1": g1, "g2": g2}


def test_every_owned_album_is_listed(admin_client, albums):
    text = page(admin_client)
    assert sorted(tile_titles(text)) == ["Alpha Live", "Alpha One", "Beta Tape"]
    assert "3 album(s)" in text and tile_titles(text) == [
        "Alpha Live",
        "Alpha One",
        "Beta Tape",
    ]  # by title


def test_tiles_show_the_cover_artist_year_quality_and_link_to_the_album(admin_client, albums):
    text = page(admin_client)
    assert 'src="/covers/cover-one.jpg"' in text
    assert f'href="/album/{albums["g1"].id}"' in text  # matched: the album page
    assert f'href="/album/local/{albums["tape"].id}"' in text  # unmatched: the local page
    assert "Alpha · 2000" in text and "FLAC Standard" in text and "Qualsiasi" in text


def test_filter_by_quality(admin_client, albums):
    assert tile_titles(page(admin_client, "?quality=mp3_320")) == ["Alpha Live"]
    assert tile_titles(page(admin_client, "?quality=flac_standard")) == ["Alpha One"]
    assert tile_titles(page(admin_client, "?quality=flac_master")) == []
    text = page(admin_client, "?quality=flac_master")
    assert "No album matches these filters" in text and "Clear filters" in text


def test_the_quality_menu_offers_every_profile_and_keeps_the_choice(admin_client, albums):
    text = page(admin_client, "?quality=mp3_320")
    select = re.search(r'<select id="quality".*?</select>', text, re.S).group(0)
    options = re.findall(r'<option value="([^"]*)"\s*(selected)?>', select)
    assert [o[0] for o in options] == [
        "",
        "flac_master",
        "flac_hires",
        "flac_standard",
        "mp3_320",
        "mp3_256",
        "mp3_192",
        "any",
    ]
    assert [o[0] for o in options if o[1]] == ["mp3_320"]


def test_filter_by_format_and_type(admin_client, albums):
    assert sorted(tile_titles(page(admin_client, "?fmt=mp3"))) == ["Alpha Live", "Beta Tape"]
    assert tile_titles(page(admin_client, "?fmt=FLAC")) == ["Alpha One"]  # case does not matter
    assert tile_titles(page(admin_client, "?category=live")) == ["Alpha Live"]
    assert tile_titles(page(admin_client, "?category=album")) == ["Alpha One"]
    assert tile_titles(page(admin_client, "?category=unmatched")) == ["Beta Tape"]
    text = page(admin_client)
    assert ">FLAC<" in text and ">MP3<" in text  # only the formats that exist


def test_filters_combine(admin_client, albums):
    assert tile_titles(page(admin_client, "?fmt=mp3&quality=any")) == ["Beta Tape"]
    assert tile_titles(page(admin_client, "?fmt=flac&quality=mp3_320")) == []


def test_search_by_album_or_artist(admin_client, albums):
    assert sorted(tile_titles(page(admin_client, "?q=alpha"))) == ["Alpha Live", "Alpha One"]
    assert tile_titles(page(admin_client, "?q=tape")) == ["Beta Tape"]
    assert tile_titles(page(admin_client, "?q=beta")) == ["Beta Tape"]  # the artist's name
    assert tile_titles(page(admin_client, "?q=zzz")) == []
    assert tile_titles(page(admin_client, "?q=100%25")) == []  # a % is not a wildcard


def test_sorting(admin_client, albums):
    assert tile_titles(page(admin_client, "?sort=artist")) == [
        "Alpha One",
        "Alpha Live",
        "Beta Tape",
    ]
    assert tile_titles(page(admin_client, "?sort=year")) == ["Alpha Live", "Alpha One", "Beta Tape"]
    assert tile_titles(page(admin_client, "?sort=quality")) == [
        "Alpha One",
        "Alpha Live",
        "Beta Tape",
    ]
    assert tile_titles(page(admin_client, "?sort=nonsense")) == [
        "Alpha Live",
        "Alpha One",
        "Beta Tape",
    ]


def test_the_search_box_updates_the_tiles(admin_client, albums):
    text = page(admin_client)
    assert 'hx-get="/albums/items"' in text and 'hx-include="#filters"' in text
    r = admin_client.get("/albums/items?q=live", headers=HX)
    assert r.status_code == 200 and "<html" not in r.text and tile_titles(r.text) == ["Alpha Live"]


def test_scrolling_through_many_albums(app, admin_client, db):
    artist = make_artist(db, "Many")
    for i in range(PAGE_SIZE + 12):
        make_album(db, artist, f"Record {i:03}", 2000)
    first = page(admin_client)
    assert len(tile_titles(first)) == PAGE_SIZE and 'hx-trigger="revealed"' in first
    second = admin_client.get("/albums/items?page=2").text
    assert len(tile_titles(second)) == 12 and "revealed" not in second
    assert "60 album(s)" in first


def test_the_next_page_keeps_the_filters(app, admin_client, db):
    artist = make_artist(db, "Many")
    for i in range(PAGE_SIZE + 5):
        make_album(db, artist, f"Record {i:03}", 2000).quality_tier = "mp3_320"
    db.commit()
    text = page(admin_client, "?quality=mp3_320&fmt=")
    assert (
        "/albums/items?quality=mp3_320&amp;page=2" in text
        or "/albums/items?quality=mp3_320&page=2" in text
    )


def test_an_empty_library(admin_client):
    text = page(admin_client)
    assert "No albums in the library yet" in text and "0 album(s)" in text


def test_only_albums_of_artists_in_the_library_are_listed(admin_client, db):
    gone = make_artist(db, "Gone", in_library=False)
    make_album(db, gone, "Ghost Album", 2000)
    assert tile_titles(page(admin_client)) == []


def test_parameters_are_sanitised(admin_client, albums):
    text = page(admin_client, "?q=<script>alert(1)</script>&quality=%27%20OR%201=1&page=-4")
    assert "<script>alert(1)</script>" not in text
    assert tile_titles(text) == []


def test_the_page_needs_a_login_and_follows_the_language(admin_client, new_client, albums):
    anon = new_client()
    for path in ("/albums", "/albums/items"):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    from tests.conftest import post

    post(admin_client, "/lang", {"lang": "it", "next": "/dashboard"})
    text = page(admin_client)
    assert "Cerca album o artisti" in text and "3 album" in text and "Qualità" in text


def test_ordinary_users_see_the_albums_too(user_client, albums):
    assert len(tile_titles(page(user_client))) == 3


def test_the_old_library_addresses_are_gone(admin_client):
    for path in ("/library", "/library/items", "/admin/library"):
        assert admin_client.get(path).status_code in (404, 405), path
    assert admin_client.post("/admin/library/scan").status_code in (403, 404, 405)


def test_the_dashboard_is_the_home_page_and_the_nav_has_the_new_pages(admin_client):
    r = admin_client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    text = admin_client.get("/dashboard").text
    for link in ("/dashboard", "/artists", "/albums"):
        assert f'href="{link}"' in text


def test_album_rows_are_owned_albums_only(db, albums):
    assert db.query(LibraryAlbum).count() == 3


# ------------------------------------------------- letters and views, like the Artists page


def test_albums_have_a_letter_bar_and_filter_by_the_initial(admin_client, db, albums):
    make_album(db, make_artist(db, "Gamma"), "9 Lives", 2001)
    text = page(admin_client)
    assert "data-letter-bar" in text
    bar = re.search(r"<nav[^>]*data-letter-bar.*?</nav>", text, re.S).group(0)
    assert 'href="/albums?letter=A"' in bar and 'href="/albums?letter=%23"' in bar  # A and "#"
    assert 'href="/albums?letter=Z"' not in bar  # no album starts with Z: not a link
    assert sorted(tile_titles(page(admin_client, "?letter=A"))) == ["Alpha Live", "Alpha One"]
    assert tile_titles(page(admin_client, "?letter=b")) == ["Beta Tape"]  # case does not matter
    assert tile_titles(page(admin_client, "?letter=%23")) == ["9 Lives"]  # not A-Z
    assert "data-empty" in page(admin_client, "?letter=Z")
    assert tile_titles(page(admin_client, "?letter=%25%27")) == tile_titles(page(admin_client))


def test_the_letter_filter_works_in_the_infinite_scroll_fragment_and_keeps_the_view(
    admin_client, albums
):
    r = admin_client.get("/albums/items?letter=A&view=list", headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    assert "Alpha One" in r.text and "Beta Tape" not in r.text
    assert 'data-album="' in r.text and "aspect-square" not in r.text  # rows, not tiles


def test_albums_can_be_shown_as_a_list_and_the_toggle_uses_icons_not_words(admin_client, albums):
    grid, rows = page(admin_client), page(admin_client, "?view=list")
    for text in (grid, rows):
        toggle = re.search(r"<div[^>]*data-view-toggle.*?</div>", text, re.S).group(0)
        assert (
            toggle.count("<svg") == 2
            and 'data-view="grid"' in toggle
            and 'data-view="list"' in toggle
        )
        links = re.findall(r"<a [^>]*data-view[^>]*>(.*?)</a>", toggle, re.S)
        assert all(re.sub(r"<[^>]+>", "", x).strip() == "" for x in links)  # no visible label
        assert 'aria-label="Grid"' in toggle and 'aria-label="List"' in toggle  # but accessible
    assert 'aria-current="true"' in re.search(r'<a [^>]*data-view="list"[^>]*>', rows).group(0)
    assert "grid-cols-1" in rows and "lg:grid-cols-6" in grid
    assert rows.count('data-album="') == 3
    assert 'name="view" value="list"' in rows  # the search box keeps the view


def test_the_artists_page_uses_the_same_icon_toggle(admin_client):
    text = admin_client.get("/artists").text
    toggle = re.search(r"<div[^>]*data-view-toggle.*?</div>", text, re.S).group(0)
    assert toggle.count("<svg") == 2 and ">Grid<" not in toggle and ">List<" not in toggle
