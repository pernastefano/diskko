"""The mobile interface (/m): a simplified, installable view of the library and the requests."""

import json
import re

import pytest
from sqlalchemy import select

from app.models import (
    Artist,
    ArtistTopTrack,
    DownloadRequest,
    LibraryTrack,
    ReleaseTrack,
    TracklistFetch,
    User,
    utcnow,
)
from app.services import downloads as svc
from app.services.app_settings import get_setting, set_setting
from app.services.downloads import USER_QUALITY_KEY
from tests.conftest import PASSWORD, login, post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.fake_slskd import KEY
from tests.test_requests_pages import jobs, requests_in, with_candidates

HX = {"HX-Request": "true"}


@pytest.fixture
def settings(settings):
    return settings.model_copy(
        update={
            "slskd_url": "http://slskd.test:5030",
            "slskd_api_key": KEY,
            "mb_user_agent": "Diskko/0.1 ( test@example.com )",
        }
    )


@pytest.fixture
def world(db):
    """Aretha owns Lady Soul (two files) and misses Aretha Now."""
    artist = make_artist(db, "Aretha Franklin")
    owned = make_rg(db, artist, "Lady Soul", 1968)
    missing = make_rg(db, artist, "Aretha Now", 1968)
    album = make_album(db, artist, "Lady Soul", 1968)
    album.release_group_id, album.match_status, album.quality_tier = (
        owned.id,
        "matched",
        "flac_standard",
    )
    for n, title in enumerate(("Chain of Fools", "Natural Woman"), start=1):
        db.add(
            LibraryTrack(
                album_id=album.id, source="filesystem", path=f"A/{n}.flac", title=title,
                track_no=n, disc_no=1, duration=180, quality_tier="flac_standard",
            )
        )  # fmt: skip
    db.commit()
    return {"artist": artist, "owned": owned, "missing": missing, "album": album}


def mobile_request(client, world, db):
    post(
        client,
        f"/m/request/{world['artist'].id}/{world['missing'].id}",
        {},
        page="/m/artists",
        headers=HX,
    )
    [req] = requests_in(db)
    return req


# ------------------------------------------------------------------ access and PWA


def test_every_page_needs_a_login_but_the_app_files_do_not(admin_client, new_client):
    client = new_client()  # nobody signed in (the administrator exists, so it is not the setup)
    for path in ("/m/", "/m/artists", "/m/albums", "/m/search", "/m/requests"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fm"), path
    assert client.get("/m/sw.js").status_code == 200
    assert client.get("/m/offline").status_code == 200
    assert client.get("/static/m/manifest.webmanifest").status_code == 200


def test_the_service_worker_has_the_mobile_scope_and_never_caches_pages(new_client):
    r = new_client().get("/m/sw.js")
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["service-worker-allowed"] == "/m/" and r.headers["cache-control"] == "no-cache"
    assert "'/m/offline'" in r.text and "request.method !== 'GET'" in r.text


def test_the_manifest_makes_it_an_installable_app_under_m(new_client):
    manifest = json.loads(new_client().get("/static/m/manifest.webmanifest").text)
    assert manifest["start_url"] == "/m/" and manifest["scope"] == "/m/"
    assert manifest["display"] == "standalone" and manifest["short_name"] == "DISKKO"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])


def test_the_pages_link_the_manifest_register_the_worker_and_have_four_tabs(user_client, world):
    html = user_client.get("/m/artists").text
    assert '<link rel="manifest" href="/static/m/manifest.webmanifest">' in html
    assert "viewport-fit=cover" in html and "register('/m/sw.js'" in html
    assert "apple-mobile-web-app-capable" in html
    for tab in ("artists", "albums", "search", "requests"):
        assert f'data-tab="{tab}"' in html
    assert 'data-tab="artists" aria-current="page"' in html
    for desktop_only in ("/admin/jobs", "/admin/status", "/settings", "/admin/artists"):
        assert desktop_only not in html


def test_the_two_interfaces_link_to_each_other(user_client):
    assert (
        'href="/m/" class="underline hover:text-slate-900" data-mobile-link'
        in user_client.get("/dashboard").text
    )
    assert 'href="/dashboard"' in user_client.get("/m/artists").text


def test_the_root_goes_to_the_artists(user_client):
    r = user_client.get("/m/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/m/artists"


# --------------------------------------------------------------------- lists


def test_the_artists_list_filters_as_you_type(user_client, world, db):
    make_artist(db, "Zebra Band")
    html = user_client.get("/m/artists").text
    assert f'href="/m/artist/{world["artist"].id}"' in html and "Zebra Band" in html
    found = user_client.get("/m/artists/items?q=zeb", headers=HX).text
    assert "Zebra Band" in found and "Aretha" not in found and "<html" not in found
    assert "No artist" in user_client.get("/m/artists/items?q=qqqq", headers=HX).text


def test_a_long_list_loads_more_on_demand(user_client, db):
    for i in range(50):
        make_artist(db, f"Artist {i:02d}")
    first = user_client.get("/m/artists").text
    assert first.count("data-artist=") == 48 and 'hx-get="/m/artists/items?page_no=2"' in first
    more = user_client.get("/m/artists/items?page_no=2", headers=HX).text
    assert more.count("data-artist=") == 2 and "data-more" not in more


def test_the_albums_list_links_to_the_mobile_album(user_client, world):
    html = user_client.get("/m/albums").text
    assert f'href="/m/album/{world["owned"].id}"' in html and "Lady Soul" in html
    assert "Lady" not in user_client.get("/m/albums/items?q=zzz", headers=HX).text


# --------------------------------------------------------------------- artist


def test_the_artist_page_has_the_four_parts_and_no_admin_tools(user_client, admin_client, world):
    for client in (user_client, admin_client):
        html = client.get(f"/m/artist/{world['artist'].id}").text
        aid = world["artist"].id
        assert (
            f'hx-post="/m/artist/{aid}/top"' in html
            and f'hx-post="/m/artist/{aid}/similar"' in html
        )
        assert f'href="/m/album/{world["owned"].id}"' in html  # owned
        assert 'data-group="missing-' in html and "Aretha Now" in html  # missing, with a button
        assert 'data-action="request-download"' in html
        assert "/admin/" not in html and "data-page-actions" not in html


def test_the_top_tracks_have_their_preview(user_client, world, db):
    artist = db.get(Artist, world["artist"].id)
    artist.top_checked_at = utcnow()
    db.add(
        ArtistTopTrack(
            artist_id=artist.id, position=1, title="Respect", provider="deezer",
            preview_url="https://cdn.deezer.test/p.mp3", preview_provider="deezer",
            fetched_at=utcnow(),
        )
    )  # fmt: skip
    db.commit()
    html = post(user_client, f"/m/artist/{artist.id}/top", {}, page="/m/artists", headers=HX).text
    assert "Respect" in html and 'src="https://cdn.deezer.test/p.mp3"' in html
    assert "<audio" in html and 'data-top-state="ready"' in html


def test_similar_artists_link_to_the_mobile_pages(user_client, world, db):
    from app.models import SimilarArtist

    other = make_artist(db, "Gladys Knight")
    db.add(
        SimilarArtist(
            artist_id=world["artist"].id, name="Gladys Knight", name_key="gladys knight",
            mbid=None, score=0.9, source="lastfm", position=1,
        )
    )  # fmt: skip
    db.commit()
    html = user_client.get(f"/m/artist/{world['artist'].id}/similar").text
    assert "Gladys Knight" in html and "/m/" in html and 'href="/artist/' not in html
    del other


def test_asking_for_an_album_queues_the_search_and_shows_the_status(user_client, world, db):
    req = mobile_request(user_client, world, db)
    assert req.status == "queued" and len(jobs(db, "slskd_search")) == 1
    html = user_client.get(f"/m/requests/{req.id}/control").text
    assert 'data-request-status="queued"' in html and 'hx-get="/m/requests/' in html
    assert f'href="/m/requests/{req.id}"' in html
    mobile_request(user_client, world, db)  # again: still one
    assert len(requests_in(db)) == 1


def test_without_slskd_the_button_says_so(app, user_client, world, db):
    app.state.settings.slskd_url = ""
    html = post(
        user_client,
        f"/m/request/{world['artist'].id}/{world['missing'].id}",
        {},
        page="/m/artists",
        headers=HX,
    ).text
    assert 'data-action="request-download"' in html and requests_in(db) == []


def test_a_group_of_another_artist_cannot_be_requested(user_client, world, db):
    other = make_artist(db, "Someone")
    r = post(
        user_client,
        f"/m/request/{other.id}/{world['missing'].id}",
        {},
        page="/m/artists",
        headers=HX,
    )
    assert r.status_code == 404 and requests_in(db) == []


# ---------------------------------------------------------------------- album


def test_an_owned_album_lists_its_tracks_with_a_player(user_client, world):
    gid = world["owned"].id
    page = user_client.get(f"/m/album/{gid}").text
    assert f'hx-post="/m/album/{gid}/tracks"' in page and 'data-ownership="owned"' in page
    html = post(user_client, f"/m/album/{gid}/tracks", {}, page="/m/albums", headers=HX).text
    assert "Chain of Fools" in html and "Natural Woman" in html
    assert html.count("data-play") == 2 and "<audio" not in html  # the player plays them


def test_a_missing_album_shows_its_tracks_and_the_request_button(user_client, world, db):
    gid = world["missing"].id
    for n, title in enumerate(("Think", "I Say a Little Prayer"), start=1):
        db.add(
            ReleaseTrack(release_group_id=gid, disc=1, position=n, title=title, length_ms=150000)
        )
    db.add(TracklistFetch(release_group_id=gid, fetched_at=utcnow(), track_count=2))
    db.commit()
    page = user_client.get(f"/m/album/{gid}").text
    assert 'data-ownership="remote"' in page and 'data-action="request-download"' in page
    html = user_client.get(f"/m/album/{gid}/tracks").text
    assert "Think" in html and "I Say a Little Prayer" in html and "2:30" in html


def test_an_album_without_a_match_has_its_own_mobile_page(user_client, world, db):
    loose = make_album(db, world["artist"], "Demo Tapes", 1960)
    db.add(LibraryTrack(album_id=loose.id, source="filesystem", path="D/1.flac", title="Demo",
                        track_no=1, disc_no=1, duration=100, quality_tier="any"))  # fmt: skip
    db.commit()
    html = user_client.get(f"/m/album/local/{loose.id}").text
    assert "Demo Tapes" in html and "Demo" in html and "data-player" in html
    matched = user_client.get(f"/m/album/local/{world['album'].id}", follow_redirects=False)
    assert matched.headers["location"] == f"/m/album/{world['owned'].id}"


# --------------------------------------------------------------------- search


def test_the_search_shows_the_library_first_and_asks_musicbrainz_for_artists(user_client, world):
    html = user_client.get("/m/search?q=aretha").text
    assert "data-library-artists" in html and f'href="/m/artist/{world["artist"].id}"' in html
    assert 'hx-post="/m/search/start?q=aretha"' in html and "data-search-state" in html


def test_the_search_job_is_queued_by_the_start_and_followed(user_client, db):
    html = post(user_client, "/m/search/start?q=Nirvana", {}, page="/m/search", headers=HX).text
    assert (
        'data-search-state="searching"' in html and 'hx-get="/m/search/results?q=Nirvana"' in html
    )
    assert len(jobs(db, "search_artists")) == 1
    assert user_client.get("/m/search/results?q=Nirvana").status_code == 200


def test_opening_an_unknown_search_result_goes_back_to_the_mobile_search(user_client):
    r = post(
        user_client,
        "/m/search/open",
        {"q": "nirvana", "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da"},
        page="/m/search",
    )
    assert r.status_code == 303 and r.headers["location"] == "/m/search?q=nirvana"


# ------------------------------------------------------------------- requests


def test_the_requests_list_shows_mine_with_filters_and_progress(
    user_client, admin_client, world, db
):
    req = mobile_request(user_client, world, db)
    svc.set_status(db, req.id, "downloading", progress=40.0)
    db.commit()
    html = user_client.get("/m/requests").text
    assert f'href="/m/requests/{req.id}"' in html and "Aretha Now" in html and "40%" in html
    assert 'hx-trigger="every 5s"' in html
    for f in ("all", "active", "finished", "failed"):
        assert f'data-filter="{f}"' in html
    assert "data-request=" not in user_client.get("/m/requests?status=failed").text
    assert "data-request=" in admin_client.get("/m/requests").text  # an administrator sees all


def test_a_request_waiting_for_a_choice_shows_simple_candidates(user_client, world, db):
    req = mobile_request(user_client, world, db)
    with_candidates(db, req)
    svc.awaiting_choice(db, req.id)
    db.commit()
    listing = user_client.get("/m/requests").text
    assert "Waiting for your choice" in listing
    html = user_client.get(f"/m/requests/{req.id}").text
    assert html.count("data-choose") == 3 and "user0" not in html  # no user names, no paths
    assert "/admin/jobs" not in html and "data-file=" not in html
    r = post(
        user_client, f"/m/requests/{req.id}/choose", {"index": "1"}, page=f"/m/requests/{req.id}"
    )
    assert r.status_code == 303 and r.headers["location"] == f"/m/requests/{req.id}"
    db.expire_all()
    assert db.get(DownloadRequest, req.id).status == "candidate_found"
    assert len(jobs(db, "download_monitor")) == 1


def test_a_refused_choice_comes_back_with_the_message(user_client, world, db):
    req = mobile_request(user_client, world, db)
    r = post(user_client, f"/m/requests/{req.id}/choose", {"index": "0"}, page="/m/requests")
    assert r.headers["location"] == f"/m/requests/{req.id}?error=request_not_choosing"
    assert "data-error" in user_client.get(r.headers["location"]).text


def test_cancel_retry_and_delete_stay_in_the_mobile_pages(user_client, world, db):
    req = mobile_request(user_client, world, db)
    detail = user_client.get(f"/m/requests/{req.id}").text
    assert "data-cancel" in detail and "data-retry" not in detail
    r = post(user_client, f"/m/requests/{req.id}/cancel", {}, page=f"/m/requests/{req.id}")
    assert r.headers["location"] == f"/m/requests/{req.id}"
    detail = user_client.get(f"/m/requests/{req.id}").text
    assert "data-retry" in detail and "data-delete" in detail
    r = post(user_client, f"/m/requests/{req.id}/retry", {}, page=f"/m/requests/{req.id}")
    new = r.headers["location"]
    assert new.startswith("/m/requests/") and new != f"/m/requests/{req.id}"
    old = requests_in(db)[0]
    r = post(user_client, f"/m/requests/{old.id}/delete", {}, page=f"/m/requests/{old.id}")
    assert r.status_code == 303 and r.headers["location"].startswith("/m/requests?")
    assert db.scalars(select(DownloadRequest).where(DownloadRequest.id == old.id)).first() is None


def test_one_cannot_see_or_touch_another_users_request(admin_client, user_client, world, db):
    req = mobile_request(admin_client, world, db)
    assert user_client.get(f"/m/requests/{req.id}").status_code == 404
    assert (
        post(user_client, f"/m/requests/{req.id}/cancel", {}, page="/m/requests").status_code == 404
    )


# ---------------------------------------------------------------- preferences


def test_the_menu_leads_to_the_preferences_with_everything_asked_for(user_client):
    assert 'href="/m/preferences"' in user_client.get("/m/artists").text
    html = user_client.get("/m/preferences").text
    assert 'name="language"' in html and html.count('name="theme"') == 3
    for theme in ("light", "dark", "auto"):
        assert f'value="{theme}"' in html
    for field in ("current_password", "new_password", "new_password_confirm"):
        assert f'name="{field}"' in html
    assert 'href="https://github.com/pernastefano/diskko"' in html and "data-credits" in html
    assert (
        "MusicBrainz, Cover Art Archive" in html and "quality" not in html.lower().split("<main")[1]
    )


def test_language_and_theme_are_saved_and_the_quality_preference_is_kept(user_client, db):
    me = db.query(User).filter_by(username="bob").one()
    set_setting(db, USER_QUALITY_KEY, "mp3_320", me.id)
    db.commit()
    r = post(
        user_client, "/m/preferences", {"language": "it", "theme": "dark"}, page="/m/preferences"
    )
    assert (
        r.status_code == 303 and r.headers["location"] == "/m/preferences?flash=preferences_saved"
    )
    db.expire_all()
    me = db.get(User, me.id)
    assert (me.language, me.theme) == ("it", "dark")
    assert get_setting(db, USER_QUALITY_KEY, "", me.id) == "mp3_320"
    html = user_client.get(r.headers["location"]).text
    assert 'lang="it"' in html and "Preferenze" in html


def test_a_language_or_theme_we_do_not_have_is_refused(user_client):
    r = post(
        user_client, "/m/preferences", {"language": "xx", "theme": "dark"}, page="/m/preferences"
    )
    assert r.status_code == 400 and "data-preferences" in r.text


def test_the_password_is_changed_from_the_phone(user_client, new_client):
    new = "a-brand-new-pass-1"

    def change(current, confirm=new):
        return post(
            user_client,
            "/m/preferences/password",
            {"current_password": current, "new_password": new, "new_password_confirm": confirm},
            page="/m/preferences",
        )

    assert change("wrong").status_code == 400 and "data-password" in change("wrong").text
    assert change(PASSWORD, "different").status_code == 400
    r = change(PASSWORD)
    assert r.status_code == 303 and r.headers["location"] == "/m/preferences?flash=password_changed"
    assert login(new_client(), "bob", PASSWORD).status_code != 303  # the old one no longer works
    assert login(new_client(), "bob", new).status_code == 303


# --------------------------------------------------------------------- the player


def test_an_owned_album_has_the_whole_album_player_above_the_tab_bar(user_client, world):
    html = user_client.get(f"/m/album/{world['owned'].id}").text
    assert 'x-data="albumPlayer"' in html and 'src="/static/player.js"' in html
    data = json.loads(re.search(r'id="player-data">(.*?)</script>', html, re.S).group(1))
    assert [t["title"] for t in data["queue"]] == ["Chain of Fools", "Natural Woman"]
    assert all(t["url"].startswith("/stream/") for t in data["queue"])
    assert data["labels"]["play"] and data["labels"]["repeat_all"]
    for hook in ("data-player", "data-prev", "data-toggle", "data-next", "data-seek",
                 "data-shuffle", "data-repeat", "data-play-album"):  # fmt: skip
        assert hook in html
    assert "calc(3.5rem + env(safe-area-inset-bottom))" in html  # just above the tabs


def test_an_album_you_do_not_have_has_no_player(user_client, world):
    html = user_client.get(f"/m/album/{world['missing'].id}").text
    assert "data-player" not in html and "player-data" not in html


def test_the_tracks_are_rows_the_player_starts(user_client, world):
    html = user_client.get(f"/m/album/{world['owned'].id}/tracks").text
    assert '@click="play(1)"' in html and '@click="play(2)"' in html
    assert "isCurrent(1)" in html  # the row of the track that plays is marked


def test_the_theme_choice_marks_the_current_theme(user_client):
    html = user_client.get("/m/preferences").text
    assert "x-data=\"{ theme: 'auto' }\"" in html and "bg-indigo-600 font-medium text-white" in html
    assert "has-[:checked]" not in html


# ------------------------------------------------- previews that must not be replaced


def test_a_section_that_refreshes_itself_waits_while_a_preview_plays(user_client, world, db):
    from datetime import timedelta

    artist = db.get(Artist, world["artist"].id)
    artist.top_checked_at = utcnow() - timedelta(minutes=30)  # the previews must be renewed
    for position, provider in ((1, "itunes"), (2, "deezer")):
        db.add(
            ArtistTopTrack(
                artist_id=artist.id, position=position, title=f"Top {position}",
                provider=provider, preview_url=f"https://cdn.test/{position}.m4a",
                preview_provider=provider, fetched_at=utcnow() - timedelta(minutes=30),
            )
        )  # fmt: skip
    db.commit()
    top = post(user_client, f"/m/artist/{artist.id}/top", {}, page="/m/artists", headers=HX).text
    assert 'hx-trigger="every 3s [dkIdle()]"' in top and "<audio" in top
    tracks = post(
        user_client, f"/m/album/{world['missing'].id}/tracks", {}, page="/m/albums", headers=HX
    ).text
    assert 'hx-trigger="every 2s [dkIdle()]"' in tracks


def test_the_pages_define_the_idle_check_the_polling_asks(user_client, world):
    for path in ("/m/artists", "/dashboard"):
        html = user_client.get(path).text
        assert "window.dkIdle = function" in html and "!a.paused && !a.ended" in html


# ------------------------------------------------- a search that found nothing


def no_results_request(db, world):
    from app.services import downloads as svc_

    req = requests_in(db)[0] if requests_in(db) else None
    assert req is not None
    svc_.no_results(db, req.id, "Aretha Franklin Aretha Now | aretha now | franklin")
    db.commit()
    return req


def test_a_search_with_no_results_lists_what_was_tried_and_suggests_a_shorter_text(
    user_client, world, db
):
    req = mobile_request(user_client, world, db)
    no_results_request(db, world)
    for page in (f"/requests/{req.id}", f"/m/requests/{req.id}"):
        html = user_client.get(page).text
        assert "Searched for: Aretha Franklin Aretha Now · aretha now · franklin" in html, page
        assert "data-no-results-hint" in html
    assert 'name="query"' in user_client.get(f"/m/requests/{req.id}").text  # a text to try


def test_the_phone_can_search_again_with_another_text(user_client, world, db):
    req = mobile_request(user_client, world, db)
    no_results_request(db, world)
    r = post(
        user_client,
        f"/m/requests/{req.id}/retry",
        {"query": "franklin"},
        page=f"/m/requests/{req.id}",
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/m/requests/")
    assert requests_in(db)[-1].query_override == "franklin"
