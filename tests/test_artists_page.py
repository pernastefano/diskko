import re

import pytest

from app.models import ReleaseGroup
from app.services.library_views import PAGE_SIZE
from app.services.musicbrainz.matching import match_artist_albums
from tests.conftest import post
from tests.dbhelpers import bulk_library, make_album, make_artist, make_rg


def names_in(html):
    return re.findall(r'class="truncate font-medium[^"]*" title="([^"]*)"', html)


def row_names(html):
    return re.findall(r'<span class="block truncate font-medium">([^<]*)</span>', html)


@pytest.fixture
def lib(app, admin_client):
    """Alpha (owns 1 of 3), Beta (not synced, owns 0), Gamma (complete)."""
    with app.state.session_factory() as db:
        from app.models import utcnow

        alpha = make_artist(
            db, "Alpha", mbid="a" * 8 + "-0000-4000-8000-000000000001", status="resolved"
        )
        beta = make_artist(db, "Beta")
        gamma = make_artist(
            db, "Gamma", mbid="a" * 8 + "-0000-4000-8000-000000000002", status="resolved"
        )
        for artist in (alpha, gamma):
            artist.metadata_synced_at = utcnow()
        rgs = [make_rg(db, alpha, f"Alpha {i}", 2000 + i) for i in range(3)]
        make_rg(db, alpha, "Alpha Live", 2010, "Album", ("Live",))
        make_rg(db, gamma, "Gamma One", 1999)
        make_album(db, alpha, "Alpha 0", year=2000)
        make_album(db, gamma, "Gamma One", year=1999)
        make_album(db, beta, "Beta Local", year=2001)
        for artist in (alpha, gamma):
            match_artist_albums(db, artist.id)
        rgs[0].cover_status, rgs[0].cover_path = "ok", "cover-alpha.jpg"
        db.commit()
        return {"alpha": alpha.id, "beta": beta.id, "gamma": gamma.id}


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


# ----------------------------------------------------------------- access


def test_the_library_needs_a_login(client, admin_client, new_client):
    anon = new_client()
    for path in ("/artists", "/artists/items"):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_the_library_is_open_to_every_user(admin_client, user_client, lib):
    for client in (admin_client, user_client):
        assert "Alpha" in page(client, "/artists")


def test_the_home_page_is_the_dashboard(admin_client, new_client):
    r = admin_client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    anon = new_client().get("/")
    assert anon.status_code == 303 and anon.headers["location"].startswith("/login")


def test_menu_links(admin_client, user_client):
    admin, user = page(admin_client, "/artists"), page(user_client, "/artists")
    for text in (admin, user):
        for link in ("/dashboard", "/artists", "/albums", "/search", "/requests"):
            assert f'href="{link}"' in text, link
    assert 'href="/admin/artists"' in admin and "Artist management" in admin
    assert 'href="/admin/artists"' not in user and "/admin/library" not in admin + user


# ---------------------------------------------------------------- content


def test_empty_library(admin_client):
    text = page(admin_client, "/artists")
    assert "The library is empty" in text and "0 artist(s)" in text


def test_tiles_show_albums_and_missing_state(admin_client, lib):
    text = page(admin_client, "/artists")
    assert names_in(text) == ["Alpha", "Beta", "Gamma"]
    assert "3 artist(s)" in text
    chips = dict(
        zip(names_in(text), re.findall(r'data-ownership="(\w+)"[^>]*>([^<]*)<', text), strict=True)
    )
    # Alpha owns 1 and misses 2 (the live album is not a selected type): 1 of 3, yellow
    assert chips["Alpha"] == ("yellow", "1 / 3")
    assert chips["Beta"][0] == "unknown" and "No metadata" in chips["Beta"][1]  # not synced yet
    assert chips["Gamma"] == ("green", "1 / 1")  # owns everything: 1 of 1


def test_cover_or_initial(admin_client, lib):
    text = page(admin_client, "/artists")
    assert 'src="/covers/cover-alpha.jpg"' in text and 'loading="lazy"' in text
    assert text.count('aria-hidden="true">B</div>') == 1  # Beta has no cover: its initial


def test_each_tile_links_to_the_artist(admin_client, lib):
    text = page(admin_client, "/artists")
    for artist_id in lib.values():
        assert f'href="/artist/{artist_id}"' in text


def test_artist_names_are_escaped(app, admin_client):
    with app.state.session_factory() as db:
        make_artist(db, '<script>alert("x")</script>')
    text = page(admin_client, "/artists")
    assert "<script>alert" not in text and "&lt;script&gt;" in text


def test_the_totals_follow_the_system_wide_types(admin_client, user_client, lib):
    # Alpha owns 1 and misses 2 of the selected types: 1 of 3
    assert "1 of 3 album(s) owned" in page(admin_client, "/artists")
    assert "1 of 3 album(s) owned" in page(user_client, "/artists")
    post(admin_client, "/settings/albums/types", {"types": ["live"]}, page="/settings/albums")
    for client in (admin_client, user_client):  # the same for everybody
        text = page(client, "/artists")
        # Alpha has one live release group and owns none of it
        assert "0 of 1 album(s) owned" in text and "1 of 3 album(s) owned" not in text


# ---------------------------------------------------------------- filtering


def test_search_filters_the_page_and_the_fragment(admin_client, lib):
    assert names_in(page(admin_client, "/artists?q=alp")) == ["Alpha"]
    fragment = page(admin_client, "/artists/items?q=gam")
    assert names_in(fragment) == ["Gamma"] and "<html" not in fragment
    assert "No artists match these filters." in page(admin_client, "/artists/items?q=zzz")


def test_the_search_box_updates_the_tiles_by_htmx(admin_client, lib):
    text = page(admin_client, "/artists")
    box = re.search(r'<input id="q"[^>]*>', text, re.S).group(0)
    assert 'hx-get="/artists/items"' in box and 'hx-target="#tiles"' in box
    assert "delay:300ms" in box and 'hx-sync="this:replace"' in box


def test_letter_bar_and_filter(admin_client, lib):
    text = page(admin_client, "/artists")
    assert 'href="/artists?letter=A"' in text and 'href="/artists?letter=G"' in text
    assert 'href="/artists?letter=Z"' not in text  # no artist starts with Z: not a link
    assert names_in(page(admin_client, "/artists?letter=g")) == ["Gamma"]
    assert names_in(page(admin_client, "/artists?letter=%23")) == []
    assert "bg-indigo-600 text-white" in page(admin_client, "/artists?letter=A")


def test_only_missing(admin_client, lib):
    assert names_in(page(admin_client, "/artists?missing=1")) == ["Alpha"]
    assert 'name="missing" value="1" checked' in page(admin_client, "/artists?missing=1")


def test_sorting(admin_client, lib):
    assert names_in(page(admin_client, "/artists?sort=missing"))[0] == "Alpha"
    assert names_in(page(admin_client, "/artists?sort=recent"))[0] == "Gamma"  # added last
    assert names_in(page(admin_client, "/artists?sort=bogus")) == ["Alpha", "Beta", "Gamma"]
    assert '<option value="missing" selected>' in page(admin_client, "/artists?sort=missing")


def test_list_view(admin_client, lib):
    text = page(admin_client, "/artists?view=list")
    assert row_names(text) == ["Alpha", "Beta", "Gamma"] and "grid-cols-1" in text
    assert "sm:hidden" in text and "hidden w-28" in text  # details move under the name on phones
    assert names_in(text) == []  # no tiles in list mode
    assert row_names(page(admin_client, "/artists/items?view=list")) == ["Alpha", "Beta", "Gamma"]


def test_unknown_parameters_are_ignored(admin_client, lib):
    text = page(admin_client, "/artists?view=cube&letter=%C3%9F&page=0&sort=x&missing=maybe")
    assert names_in(text) == ["Alpha", "Beta", "Gamma"]


def test_links_keep_the_filters(admin_client, lib):
    text = page(admin_client, "/artists?q=a&sort=missing&view=list")
    assert "q=a&amp;sort=missing" in text or "q=a&sort=missing" in text
    assert 'href="/artists?q=a&sort=missing"' in text.replace(
        "&amp;", "&"
    )  # switch to grid keeps them


# ------------------------------------------------- pagination and infinite scroll


@pytest.fixture
def big(app, admin_client):
    with app.state.session_factory() as db:
        bulk_library(db, artists=130, rgs_per_artist=4, albums_per_artist=1)


def test_the_first_page_has_a_scroll_sentinel(admin_client, big):
    text = page(admin_client, "/artists")
    assert len(names_in(text)) == PAGE_SIZE and "130 artist(s)" in text
    sentinel = re.search(r'<div[^>]*hx-trigger="revealed"[^>]*>', text).group(0)
    assert 'hx-get="/artists/items?page=2"' in sentinel and 'hx-swap="outerHTML"' in sentinel


def test_scrolling_through_every_page_shows_each_artist_once(admin_client, big):
    seen, url = [], "/artists"
    for _ in range(10):
        html = page(admin_client, url)
        seen += names_in(html)
        sentinel = re.search(r'hx-get="(/artists/items\?[^"]+)"[^>]*hx-trigger="revealed"', html)
        if not sentinel:
            break
        url = sentinel.group(1).replace("&amp;", "&")
        assert 'hx-trigger="revealed"' in html
    assert len(seen) == len(set(seen)) == 130 and seen == sorted(seen)


def test_the_last_page_has_no_sentinel(admin_client, big):
    last = page(admin_client, "/artists/items?page=3")
    assert len(names_in(last)) == 130 - 2 * PAGE_SIZE and 'hx-trigger="revealed"' not in last
    beyond = page(admin_client, "/artists/items?page=9")
    assert names_in(beyond) == [] and "No artists match" not in beyond  # silently empty


def test_the_sentinel_keeps_the_filters(admin_client, big):
    html = page(admin_client, "/artists/items?q=artist&sort=missing&view=list")
    sentinel = (
        re.search(r'hx-get="([^"]+)"[^>]*hx-trigger="revealed"', html)
        .group(1)
        .replace("&amp;", "&")
    )
    assert (
        "q=artist" in sentinel
        and "sort=missing" in sentinel
        and "view=list" in sentinel
        and "page=2" in sentinel
    )


def test_plain_links_work_without_javascript(admin_client, big):
    second = page(admin_client, "/artists?page=2")
    assert 'href="/artists"' in second  # previous goes back to the first page, no page parameter
    assert "/artists?page=3" in second and names_in(second)[0] == "Artist 0048"
    assert "Previous" not in page(admin_client, "/artists") and "Next" in page(
        admin_client, "/artists"
    )


def test_a_thousand_five_hundred_artists(app, admin_client):
    with app.state.session_factory() as db:
        bulk_library(db, artists=1500, rgs_per_artist=4, albums_per_artist=2)
    import time

    start = time.perf_counter()
    first = page(admin_client, "/artists")
    deep = page(admin_client, "/artists/items?page=32&sort=missing")
    search = page(admin_client, "/artists/items?q=artist 07")
    assert "1500 artist(s)" in first and len(names_in(first)) == PAGE_SIZE
    assert len(names_in(deep)) == 1500 - 31 * PAGE_SIZE and len(names_in(search)) == 48
    assert time.perf_counter() - start < 3.0


# ---------------------------------------------------------------------- i18n


def test_italian(admin_client, lib):
    post(admin_client, "/lang", {"lang": "it", "next": "/artists"})
    text = page(admin_client, "/artists")
    assert "Cerca artisti" in text and "3 artisti" in text and "1 / 3" in text
    assert "Senza metadati" in text and "Più mancanti" in text


def test_every_release_group_type_is_counted_only_when_selected(app, admin_client, lib):
    with app.state.session_factory() as db:
        live = db.query(ReleaseGroup).filter_by(title="Alpha Live").one()
        assert live.category == "live"
    post(
        admin_client,
        "/settings/albums/types",
        {"types": ["album", "live"]},
        page="/settings/albums",
    )
    assert "1 / 4" in page(admin_client, "/artists")


def test_a_compilation_is_no_album_of_the_discography(admin_client, app):
    """Only a compilation owned, 11 albums known: the badge reads 0 / 11, not 1 / 12."""
    from app.models import utcnow

    with app.state.session_factory() as db:
        a = make_artist(
            db, "Tencc", mbid="b" * 8 + "-0000-4000-8000-000000000009", status="resolved"
        )
        a.metadata_synced_at = utcnow()
        for i in range(11):
            make_rg(db, a, f"Tencc {i}", 1970 + i)
        make_rg(db, a, "Greatest Hits", 1990, "Album", ("Compilation",))
        make_album(db, a, "Greatest Hits", year=1990)
        match_artist_albums(db, a.id)
        db.commit()
    text = page(admin_client, "/artists")
    assert "0 of 11 album(s) owned" in text and "0 / 11" in text


def test_the_footer_links_to_the_repository(admin_client, new_client):
    for text in (page(admin_client, "/artists"), new_client().get("/login").text):
        assert 'href="https://github.com/pernastefano/diskko"' in text and "data-footer" in text
