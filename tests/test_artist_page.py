import json
import re

import pytest
from sqlalchemy import event, func, select

from app.models import Job, ReleaseGroup, utcnow
from app.services.musicbrainz.matching import match_artist_albums
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg

HX = {"HX-Request": "true"}


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def section(html, name):
    """The HTML of one group (data-group="owned-album" ...)."""
    match = re.search(rf'<section[^>]*data-group="{name}".*?</section>', html, re.S)
    assert match, f"no group {name}"
    return match.group(0)


def titles_in(html):
    return re.findall(r'class="[^"]*\btruncate\b[^"]*" title="([^"]*)" data-album-link', html)


@pytest.fixture
def band(app, admin_client):
    with app.state.session_factory() as db:
        artist = make_artist(
            db, "The Band", mbid="a" * 8 + "-0000-4000-8000-000000000001", status="resolved"
        )
        artist.metadata_synced_at = utcnow()
        rgs = {
            "one": make_rg(db, artist, "Studio One", 1990),
            "two": make_rg(db, artist, "Studio Two", 1992),
            "three": make_rg(db, artist, "Studio Three", 1996),
            "ep": make_rg(db, artist, "Little EP", 1993, "EP"),
            "single": make_rg(db, artist, "Hit Single", 1994, "Single"),
            "live": make_rg(db, artist, "Live Show", 1995, "Album", ("Live",)),
            "live2": make_rg(db, artist, "Live Again", 1998, "Album", ("Live",)),
            "comp": make_rg(db, artist, "Best Of", 1999, "Album", ("Compilation",)),
        }
        one = make_album(db, artist, "Studio One", year=1990)
        one.quality_tier, one.format, one.bitrate, one.track_count = "flac_hires", "flac", 1500, 11
        live = make_album(db, artist, "Live Show", year=1995)
        live.quality_tier, live.format, live.bitrate, live.track_count = "mp3_320", "mp3", 320, 14
        home = make_album(db, artist, "Home Recording", year=1991, path="x/home")
        home.quality_tier, home.format, home.bitrate = "any", "mp3", 128
        match_artist_albums(db, artist.id)
        rgs["one"].cover_status, rgs["one"].cover_path = "ok", "cover-one.jpg"
        rgs["two"].cover_status = "missing"
        for rg in rgs.values():
            if rg.cover_status == "unknown":
                rg.cover_status = "missing"  # nothing pending unless a test says so
        db.commit()
        return {"id": artist.id, "rgs": {k: v.id for k, v in rgs.items()}}


# ----------------------------------------------------------------------- access


def test_login_required(band, new_client):
    r = new_client().get(f"/artist/{band['id']}")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_open_to_every_user(admin_client, user_client, band):
    for client in (admin_client, user_client):
        assert "The Band" in page(client, f"/artist/{band['id']}")


def test_unknown_artists_are_404_and_outside_ones_have_a_page(app, admin_client):
    assert admin_client.get("/artist/9999").status_code == 404
    with app.state.session_factory() as db:
        gone = make_artist(db, "Gone", in_library=False)
        gone_id = gone.id
    external = admin_client.get(f"/artist/{gone_id}")
    assert external.status_code == 200 and "data-external" in external.text
    assert admin_client.get(f"/artist/{gone_id}/sections").status_code == 200


# ---------------------------------------------------------------------- content


def test_owned_albums_are_grouped_by_type(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    owned = text.split("In your library", 1)[1].split("Missing", 1)[0]
    assert "(3)" in owned.split("data-group", 1)[0]
    assert titles_in(section(text, "owned-album")) == ["Studio One"]
    assert titles_in(section(text, "owned-live")) == ["Live Show"]
    assert titles_in(section(text, "owned-unmatched")) == ["Home Recording"]
    assert "Not matched" in section(text, "owned-unmatched")
    order = [m for m in re.findall(r'data-group="(owned-[a-z]+)"', text)]
    assert order == ["owned-album", "owned-live", "owned-unmatched"]  # unmatched last


def test_missing_albums_are_grouped_by_type_and_sorted(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    assert titles_in(section(text, "missing-album")) == ["Studio Two", "Studio Three"]
    assert titles_in(section(text, "missing-ep")) == ["Little EP"]
    assert [m for m in re.findall(r'data-group="(missing-[a-z]+)"', text)] == [
        "missing-album",
        "missing-ep",
    ]
    assert "Missing <span" in text and "(3)" in text.split("Missing <span", 1)[1][:80]
    assert "Hit Single" not in text and "Best Of" not in text  # types the user did not select


def test_quality_badges(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    badges = dict(
        zip(titles_in(text)[:3], re.findall(r'data-quality="([a-z0-9_]+)"', text), strict=False)
    )
    assert badges == {"Studio One": "flac_hires", "Live Show": "mp3_320", "Home Recording": "any"}
    assert "FLAC Hi-Res" in text and "MP3 320 / V0" in text and "Qualsiasi" in text
    assert "bg-purple-100" in text and "bg-blue-100" in text and "bg-red-100" in text


def test_cards_show_year_and_track_count_but_not_the_file_format(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    assert "11 track(s)" in text and "14 track(s)" in text
    assert "MP3 · 320 kbps" not in text  # the format line is gone
    assert re.search(r'<span class="text-xs text-slate-500">(FLAC|MP3)', text) is None


def test_titles_and_badges_are_cut_instead_of_wrapping(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    assert "line-clamp" not in text
    titles = re.findall(r'<a [^>]*class="([^"]*)" title="[^"]*" data-album-link', text)
    assert titles and all("truncate" in c for c in titles)
    badge = re.search(r'<span class="([^"]*)" data-quality="flac_hires"', text).group(1)
    assert "truncate" in badge and "max-w-full" in badge
    assert 'title="FLAC Hi-Res" data-quality' not in text  # title comes after the name
    assert re.search(r'data-quality="flac_hires" title="FLAC Hi-Res"', text)


@pytest.mark.parametrize(
    ("tier", "label", "cls"),
    [
        ("flac_master", "FLAC Master", "fuchsia"),
        ("flac_hires", "FLAC Hi-Res", "purple"),
        ("flac_standard", "FLAC Standard", "green"),
        ("mp3_320", "MP3 320 / V0", "blue"),
        ("mp3_256", "MP3 ≥ 256", "sky"),
        ("mp3_192", "MP3 ≥ 192", "amber"),
        ("any", "Qualsiasi", "red"),
    ],
)
def test_every_quality_tier_has_its_own_badge(app, admin_client, band, tier, label, cls):
    with app.state.session_factory() as db:
        album = db.scalar(
            select(__import__("app.models", fromlist=["LibraryAlbum"]).LibraryAlbum).where(
                __import__("app.models", fromlist=["LibraryAlbum"]).LibraryAlbum.title
                == "Studio One"
            )
        )
        album.quality_tier = tier
        db.commit()
    card = section(page(admin_client, f"/artist/{band['id']}"), "owned-album")
    assert f'data-quality="{tier}"' in card and label in card and f"bg-{cls}-100" in card


def test_tentative_matches_are_marked(app, admin_client, band):
    with app.state.session_factory() as db:
        from app.models import LibraryAlbum

        db.scalar(
            select(LibraryAlbum).where(LibraryAlbum.title == "Studio One")
        ).match_status = "tentative"
        db.commit()
    card = section(page(admin_client, f"/artist/{band['id']}"), "owned-album")
    assert "Probable match, not confirmed" in card and ">~<" in card


def test_covers_and_placeholders(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    assert 'src="/covers/cover-one.jpg"' in text and 'loading="lazy"' in text
    assert 'aria-hidden="true">S</div>' in section(text, "missing-album")  # initial of "Studio Two"
    assert 'aria-hidden="true">H</div>' in section(text, "owned-unmatched")  # "Home Recording"


def test_titles_are_escaped(app, admin_client, band):
    with app.state.session_factory() as db:
        artist = db.get(__import__("app.models", fromlist=["Artist"]).Artist, band["id"])
        make_rg(db, artist, "<img src=x onerror=alert(1)>", 2001)
        make_album(db, artist, "<b>bold</b>", path="x/b")
    text = page(admin_client, f"/artist/{band['id']}")
    assert "<img src=x" not in text and "&lt;img src=x" in text and "<b>bold</b>" not in text


def test_similar_artists_load_themselves(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    assert "Similar artists" in text and "Looking for similar artists" in text
    assert f'hx-post="/artist/{band["id"]}/similar"' in text


def test_empty_artist(app, admin_client):
    with app.state.session_factory() as db:
        artist = make_artist(db, "Nothing Here")
        artist_id = artist.id
    text = page(admin_client, f"/artist/{artist_id}")
    assert "No albums in the library." in text and "Nothing missing for the selected types." in text


# ---------------------------------------------------------------------- types


def test_other_types_are_offered_not_shown(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    assert f'href="/artist/{band["id"]}?all=1"' in text and "more in other types" in text
    assert "3 more in other types" in text  # single, comp and the live ones: wait, see below


def test_show_every_type(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}?all=1")
    assert [m for m in re.findall(r'data-group="(missing-[a-z]+)"', text)] == [
        "missing-album",
        "missing-ep",
        "missing-single",
        "missing-compilation",
        "missing-live",
    ]
    assert titles_in(section(text, "missing-live")) == ["Live Again"]
    assert "Showing every type" in text
    assert "show only my types" in text  # the way back to the user's own selection
    assert 'href="/artist/' + str(band["id"]) + '"' in text


def test_every_user_sees_the_same_system_wide_types(admin_client, user_client, band):
    for client in (admin_client, user_client):
        text = page(client, f"/artist/{band['id']}")
        assert [m for m in re.findall(r'data-group="(missing-[a-z]+)"', text)] == [
            "missing-album",
            "missing-ep",
        ]
    post(
        admin_client,
        "/settings/albums/types",
        {"types": ["live", "single"]},
        page="/settings/albums",
    )
    for client in (admin_client, user_client):  # an administrator's change reaches everybody
        text = page(client, f"/artist/{band['id']}")
        assert [m for m in re.findall(r'data-group="(missing-[a-z]+)"', text)] == [
            "missing-single",
            "missing-live",
        ]


# -------------------------------------------------------------- request control


def test_every_missing_album_has_a_request_button(admin_client, band):
    text = page(admin_client, f"/artist/{band['id']}")
    forms = re.findall(r'<form method="post" action="/artist/\d+/request/(\d+)"', text)
    assert sorted(map(int, forms)) == sorted(band["rgs"][k] for k in ("two", "three", "ep"))
    assert text.count("Request download") == 3
    assert 'hx-post="/artist/' in text and 'name="csrf_token"' in text


def test_without_slskd_the_button_explains_why(app, admin_client, band):
    url = f"/artist/{band['id']}/request/{band['rgs']['two']}"
    r = post(admin_client, url, headers=HX, page=f"/artist/{band['id']}")
    assert r.status_code == 200 and "SLSKD is not set up yet" in r.text
    assert 'id="request-' in r.text and "<html" not in r.text
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Job.id))) == 0  # nothing was queued


def test_without_javascript_and_without_slskd_the_button_redirects(admin_client, band):
    r = post(
        admin_client,
        f"/artist/{band['id']}/request/{band['rgs']['two']}",
        page=f"/artist/{band['id']}",
    )
    assert (
        r.status_code == 303
        and r.headers["location"] == f"/artist/{band['id']}?flash=request_unavailable"
    )
    assert "SLSKD is not set up yet" in admin_client.get(r.headers["location"]).text


def test_the_request_button_needs_csrf_and_a_login(admin_client, band, new_client):
    url = f"/artist/{band['id']}/request/{band['rgs']['two']}"
    assert admin_client.post(url).status_code == 403
    anon = new_client()
    assert anon.post(url, data={"csrf_token": "x"}).status_code in (401, 403)


def test_any_user_may_press_it(user_client, band):
    r = post(
        user_client,
        f"/artist/{band['id']}/request/{band['rgs']['two']}",
        headers=HX,
        page=f"/artist/{band['id']}",
    )
    assert r.status_code == 200


def test_requests_for_unknown_things_are_404(app, admin_client, band):
    with app.state.session_factory() as db:
        other = make_artist(db, "Other")
        foreign = make_rg(db, other, "Not Yours", 2000)
        foreign_id = foreign.id
    for url in (
        f"/artist/9999/request/{band['rgs']['two']}",
        f"/artist/{band['id']}/request/99999",
        f"/artist/{band['id']}/request/{foreign_id}",
    ):
        assert post(admin_client, url, headers=HX, page=f"/artist/{band['id']}").status_code == 404


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "searching",
        "candidate_found",
        "downloading",
        "postprocessing",
        "importing",
        "completed",
        "failed",
        "cancelled",
        "no_results",
    ],
)
def test_the_page_renders_every_request_status(monkeypatch, admin_client, band, status):
    monkeypatch.setattr(
        "app.web.routers.library.request_statuses",
        lambda db, user_id, ids: {band["rgs"]["two"]: status},
    )
    text = page(admin_client, f"/artist/{band['id']}")
    assert f'data-request-status="{status}"' in text
    assert text.count("Request download") == 2  # that album shows its status instead of a button


# ------------------------------------------------------------------ admin bits


def test_admin_controls(app, admin_client, user_client, band):
    admin, user = (
        page(admin_client, f"/artist/{band['id']}"),
        page(user_client, f"/artist/{band['id']}"),
    )
    assert (
        f'action="/admin/artists/{band["id"]}/sync"' in admin
        and f'href="/admin/artists/{band["id"]}"' in admin
    )
    assert "Refresh metadata" in admin and "disabled" in admin  # MusicBrainz is not configured here
    assert "/admin/artists/" not in user and "Refresh metadata" not in user
    app.state.settings.mb_user_agent = "Diskko/0.1 ( test )"
    assert not re.search(
        r"<button[^>]*\bdisabled\b(?!:)[^>]*>\s*Refresh",
        page(admin_client, f"/artist/{band['id']}"),
    )


@pytest.mark.parametrize(
    ("status", "synced", "notice"),
    [
        ("pending", False, "No MusicBrainz data yet"),
        ("review", False, "will review it"),
        ("unmatched", False, "not on MusicBrainz"),
    ],
)
def test_notices_for_artists_without_data(app, admin_client, status, synced, notice):
    with app.state.session_factory() as db:
        artist = make_artist(db, "Unknown To MB", status=status)
        make_album(db, artist, "Local Record")
        artist_id = artist.id
    text = page(admin_client, f"/artist/{artist_id}")
    assert notice in text and "Local Record" in text


# ------------------------------------------------------------------ lazy covers


@pytest.fixture
def pending(app, band):
    with app.state.session_factory() as db:
        rg = db.get(ReleaseGroup, band["rgs"]["three"])
        rg.cover_status = "unknown"
        db.commit()
    return band


def jobs(app):
    with app.state.session_factory() as db:
        return list(db.scalars(select(Job).where(Job.type == "fetch_covers")))


def test_opening_the_page_queues_the_missing_covers_once(app, admin_client, pending):
    page(admin_client, f"/artist/{pending['id']}")
    page(admin_client, f"/artist/{pending['id']}")
    [job] = jobs(app)
    assert job.dedupe_key == f"fetch_covers:{pending['id']}" and job.queue == "coverart"
    payload = json.loads(job.payload_json)
    assert payload == {"artist_id": pending["id"]}


def test_no_job_when_nothing_is_pending(app, admin_client, band):
    page(admin_client, f"/artist/{band['id']}")
    assert jobs(app) == []


def test_no_job_when_covers_are_disabled(app, admin_client, pending):
    app.state.settings.coverart_enabled = False
    page(admin_client, f"/artist/{pending['id']}")
    assert jobs(app) == []


def test_no_job_before_the_artist_is_synced(app, admin_client, pending):
    with app.state.session_factory() as db:
        db.get(
            __import__("app.models", fromlist=["Artist"]).Artist, pending["id"]
        ).metadata_synced_at = None
        db.commit()
    page(admin_client, f"/artist/{pending['id']}")
    assert jobs(app) == []


def test_a_job_that_just_finished_is_not_repeated(app, admin_client, pending):
    from app.jobs import engine

    page(admin_client, f"/artist/{pending['id']}")
    with app.state.session_factory() as db:
        engine.claim_next(db, "coverart")
        engine.finish_success(db, jobs(app)[0].id)
    page(admin_client, f"/artist/{pending['id']}")
    assert len(jobs(app)) == 1  # covers that are still missing failed a moment ago: wait


def test_the_page_polls_while_covers_download_and_stops_after(app, admin_client, pending):
    from app.jobs import engine

    text = page(admin_client, f"/artist/{pending['id']}")
    assert f'hx-get="/artist/{pending["id"]}/sections"' in text and 'hx-trigger="every 3s"' in text
    with app.state.session_factory() as db:
        engine.claim_next(db, "coverart")
        engine.finish_success(db, jobs(app)[0].id)
    done = page(admin_client, f"/artist/{pending['id']}/sections")
    assert "every 3s" not in done and "Studio Two" in done and "<html" not in done


def test_the_poll_keeps_the_all_types_view(admin_client, pending):
    text = page(admin_client, f"/artist/{pending['id']}?all=1")
    assert f'hx-get="/artist/{pending["id"]}/sections?all=1"' in text


def test_no_polling_when_nothing_is_downloading(admin_client, band):
    assert "every 3s" not in page(admin_client, f"/artist/{band['id']}")


# ------------------------------------------------------------- i18n, performance


def test_italian(admin_client, band):
    post(admin_client, "/lang", {"lang": "it", "next": "/dashboard"})
    text = page(admin_client, f"/artist/{band['id']}")
    for expected in (
        "Nella tua libreria",
        "Mancanti",
        "Richiedi download",
        "Artisti simili",
        "Cerco artisti simili…",
        "FLAC Hi-Res",
        "Non abbinati",
        "Aggiorna metadati",
        "Studio Two",
    ):
        assert expected in text, expected


def test_a_big_discography_costs_a_constant_number_of_queries(app, admin_client, band):
    app.state.settings.coverart_enabled = (
        False  # queuing a cover job is not part of what we measure
    )
    queries = []

    def count(*_):
        queries.append(1)

    def measure():
        queries.clear()
        event.listen(app.state.engine, "before_cursor_execute", count)
        try:
            page(admin_client, f"/artist/{band['id']}")
        finally:
            event.remove(app.state.engine, "before_cursor_execute", count)
        return len(queries)

    small = measure()
    with app.state.session_factory() as db:
        artist = db.get(__import__("app.models", fromlist=["Artist"]).Artist, band["id"])
        for i in range(250):
            make_rg(db, artist, f"Extra {i}", 1970 + i % 40)
            if i % 5 == 0:
                make_album(db, artist, f"Owned extra {i}", path=f"x/{i}")
    import time

    start = time.perf_counter()
    large = measure()
    assert small == large  # no per-album or per-release-group queries
    assert time.perf_counter() - start < 2.0
