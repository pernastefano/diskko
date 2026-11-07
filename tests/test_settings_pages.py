import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Setting
from app.services.library import build_provider
from app.services.musicbrainz.missing import get_missing_types
from app.services.runtime_config import read_overrides
from tests.conftest import post
from tests.dbhelpers import make_artist

TOKEN = "plex-secret-token-123"
KEY = "slskd-secret-key-456"


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def overrides(app):
    with app.state.session_factory() as db:
        return read_overrides(db)


def save(client, path, data, page_url="/settings/library", **kw):
    return post(client, path, data, page=page_url, **kw)


# ----------------------------------------------------------------------- access


def test_settings_are_for_admins_only(admin_client, user_client, new_client):
    for path in (
        "/settings",
        "/settings/library",
        "/settings/integrations",
        "/settings/jobs",
        "/settings/albums",
    ):
        assert user_client.get(path).status_code == 403, path
        anon = new_client().get(path)
        assert anon.status_code == 303 and anon.headers["location"].startswith("/login")
    for path in (
        "/settings/library",
        "/settings/jobs",
        "/settings/albums/quality",
        "/settings/albums/types",
        "/settings/integrations/plex",
        "/settings/library/check",
    ):
        assert post(user_client, path, {"music_dir": "/x"}).status_code == 403, path


def test_settings_posts_need_csrf(admin_client):
    for path in (
        "/settings/library",
        "/settings/jobs",
        "/settings/integrations/plex",
        "/settings/albums/quality",
        "/settings/albums/types",
    ):
        assert admin_client.post(path, data={"music_dir": "/x"}).status_code == 403, path


def test_the_index_goes_to_the_first_section(admin_client):
    r = admin_client.get("/settings")
    assert r.status_code == 303 and r.headers["location"] == "/settings/library"


def test_unknown_sections_and_groups_are_404(admin_client):
    assert admin_client.get("/settings/nothing").status_code == 404
    assert save(admin_client, "/settings/integrations/spotify", {}).status_code == 404


def test_every_section_has_a_menu_entry(admin_client):
    text = page(admin_client, "/settings/library")
    for section in ("library", "integrations", "jobs", "albums"):
        assert f'href="/settings/{section}"' in text
    assert 'aria-current="page"' in text


def test_admins_reach_the_settings_from_the_menu(admin_client, user_client):
    assert 'href="/settings"' in page(admin_client, "/library")
    assert 'href="/settings"' not in page(user_client, "/library")


# ---------------------------------------------------------------------- library


def test_library_page_shows_the_current_values(app, admin_client):
    text = page(admin_client, "/settings/library")
    assert f'value="{app.state.settings.music_dir}"' in text
    assert '<option value="filesystem" selected>' in text and "Default" in text


def test_saving_the_library_settings_applies_them_at_once(app, admin_client, tmp_path):
    folder = tmp_path / "newmusic"
    r = save(
        admin_client,
        "/settings/library",
        {"music_dir": str(folder), "library_source": "plex", "library_scan_cron": "30 3 * * 1"},
    )
    assert (
        r.status_code == 303 and r.headers["location"] == "/settings/library?flash=settings_saved"
    )
    assert app.state.settings.music_dir == folder and app.state.settings.library_source == "plex"
    assert app.state.settings.library_scan_cron == "30 3 * * 1"
    assert overrides(app) == {
        "music_dir": str(folder),
        "library_source": "plex",
        "library_scan_cron": "30 3 * * 1",
    }
    assert app.state.base_settings.library_source == "filesystem"  # the environment is untouched
    text = page(admin_client, r.headers["location"])
    assert "Settings saved." in text and text.count("Changed here") == 3
    assert '<option value="plex" selected>' in text


def test_a_saved_folder_is_what_the_scan_reads(app, admin_client, tmp_path):
    save(
        admin_client,
        "/settings/library",
        {"music_dir": str(tmp_path), "library_source": "filesystem", "library_scan_cron": ""},
    )
    assert build_provider(app.state.settings).root == tmp_path


def test_invalid_values_are_reported_and_nothing_is_saved(app, admin_client):
    before = app.state.settings.music_dir
    r = save(
        admin_client,
        "/settings/library",
        {"music_dir": "relative/music", "library_source": "plex", "library_scan_cron": "nonsense"},
    )
    assert r.status_code == 400
    assert "Enter an absolute path" in r.text and "Not a valid crontab expression." in r.text
    assert 'value="relative/music"' in r.text  # what was typed is kept
    assert app.state.settings.music_dir == before and overrides(app) == {}


def test_reset_to_the_default(app, admin_client, tmp_path):
    save(
        admin_client,
        "/settings/library",
        {"music_dir": str(tmp_path), "library_source": "plex", "library_scan_cron": ""},
    )
    r = save(
        admin_client,
        "/settings/library",
        {
            "music_dir": str(tmp_path),
            "library_source": "plex",
            "library_scan_cron": "",
            "reset": "music_dir",
        },
    )
    assert r.status_code == 303
    assert "music_dir" not in overrides(app)
    assert app.state.settings.music_dir == app.state.base_settings.music_dir


def test_an_empty_schedule_switches_the_scan_off(app, admin_client):
    save(
        admin_client,
        "/settings/library",
        {"music_dir": "/m", "library_source": "filesystem", "library_scan_cron": ""},
    )
    assert app.state.settings.library_scan_cron == ""


def test_the_folder_check(admin_client, tmp_path):
    tmp_path = tmp_path / "lib"  # the test directory also holds the app's own config folder
    tmp_path.mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "b.flac").write_bytes(b"x")
    ok = save(
        admin_client,
        "/settings/library/check",
        {"music_dir": str(tmp_path)},
        headers={"HX-Request": "true"},
    )
    assert 'data-check="ok"' in ok.text and "2 item(s)" in ok.text and "<html" not in ok.text
    missing = save(admin_client, "/settings/library/check", {"music_dir": str(tmp_path / "nope")})
    assert 'data-check="not_found"' in missing.text and "does not exist" in missing.text
    file_ = save(admin_client, "/settings/library/check", {"music_dir": str(tmp_path / "b.flac")})
    assert 'data-check="not_directory"' in file_.text
    relative = save(admin_client, "/settings/library/check", {"music_dir": "music"})
    assert 'data-check="invalid"' in relative.text and "absolute path" in relative.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
def test_an_unreadable_folder_is_reported(admin_client, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        r = save(admin_client, "/settings/library/check", {"music_dir": str(locked)})
        assert 'data-check="not_readable"' in r.text
    finally:
        locked.chmod(0o700)


def test_the_check_never_lists_file_names(admin_client, tmp_path):
    (tmp_path / "secret-album-name").mkdir()
    r = save(admin_client, "/settings/library/check", {"music_dir": str(tmp_path)})
    assert "secret-album-name" not in r.text


# ----------------------------------------------------------------- integrations


def test_integrations_page_has_a_form_per_integration(admin_client):
    text = page(admin_client, "/settings/integrations")
    for group in ("plex", "slskd", "musicbrainz", "coverart", "lastfm"):
        assert (
            f'id="integration-{group}"' in text
            and f'action="/settings/integrations/{group}"' in text
        )


def test_connection_tests_exist_where_they_make_sense(admin_client):
    text = page(admin_client, "/settings/integrations")
    for name in ("plex", "slskd", "musicbrainz"):
        assert f'id="check-{name}"' in text and f'hx-post="/admin/status/{name}/test"' in text
    assert 'id="check-lastfm"' not in text and 'id="check-coverart"' not in text
    assert "save the form above first" in text


def test_saving_plex_applies_it_and_never_shows_the_token(app, admin_client):
    r = save(
        admin_client,
        "/settings/integrations/plex",
        {
            "plex_url": "http://plex.local:32400/",
            "plex_token": TOKEN,
            "plex_music_section": "Hi-Fi",
            "library_path_map": "/data/music=/music",
        },
        page_url="/settings/integrations",
    )
    assert r.status_code == 303 and TOKEN not in r.text
    s = app.state.settings
    assert (s.plex_url, s.plex_token, s.plex_music_section) == (
        "http://plex.local:32400",
        TOKEN,
        "Hi-Fi",
    )
    assert s.library_path_map == "/data/music=/music"
    text = page(admin_client, "/settings/integrations")
    assert TOKEN not in text and 'data-secret-state="set"' in text and "••••••••" in text
    assert 'value="http://plex.local:32400"' in text and 'value="Hi-Fi"' in text


def test_a_blank_secret_keeps_the_saved_one(app, admin_client):
    data = {
        "plex_url": "http://plex:32400",
        "plex_token": TOKEN,
        "plex_music_section": "Music",
        "library_path_map": "",
    }
    save(admin_client, "/settings/integrations/plex", data, page_url="/settings/integrations")
    save(
        admin_client,
        "/settings/integrations/plex",
        {**data, "plex_url": "http://plex2:32400", "plex_token": ""},
        page_url="/settings/integrations",
    )
    assert (
        app.state.settings.plex_token == TOKEN
        and app.state.settings.plex_url == "http://plex2:32400"
    )
    save(
        admin_client,
        "/settings/integrations/plex",
        {**data, "plex_token": "another-token-789"},
        page_url="/settings/integrations",
    )
    assert app.state.settings.plex_token == "another-token-789"


def test_saved_secrets_are_redacted_from_job_output(app, admin_client):
    save(
        admin_client,
        "/settings/integrations/slskd",
        {"slskd_url": "http://slskd:5030", "slskd_api_key": KEY},
        page_url="/settings/integrations",
    )
    save(
        admin_client,
        "/settings/integrations/lastfm",
        {"lastfm_api_key": "lastfm-key-999"},
        page_url="/settings/integrations",
    )
    secrets = app.state.settings.secret_values
    assert KEY in secrets and "lastfm-key-999" in secrets


def test_an_error_only_affects_its_own_integration(app, admin_client):
    r = save(
        admin_client,
        "/settings/integrations/slskd",
        {"slskd_url": "slskd-without-scheme", "slskd_api_key": KEY},
        page_url="/settings/integrations",
    )
    assert r.status_code == 400 and "full address starting with http" in r.text
    assert KEY not in r.text and 'value="slskd-without-scheme"' in r.text
    assert app.state.settings.slskd_api_key == "" and overrides(app) == {}
    assert r.text.count('data-error="') == 1


@pytest.mark.parametrize("url", ["http://user:pass@plex:32400", "http://user@plex:32400"])
def test_credentials_in_addresses_are_refused(admin_client, url):
    r = save(
        admin_client,
        "/settings/integrations/plex",
        {"plex_url": url, "plex_music_section": "Music"},
        page_url="/settings/integrations",
    )
    assert (
        r.status_code == 400
        and "user name or password" in r.text
        and "pass@" not in r.text.split("<main")[1].replace('value="' + url + '"', "")
    )


def test_the_musicbrainz_user_agent_enables_the_sync(app, admin_client):
    with app.state.session_factory() as db:
        artist = make_artist(db, "Someone")
        artist_id = artist.id
    assert (
        post(admin_client, f"/admin/artists/{artist_id}/sync", page="/admin/artists").status_code
        == 409
    )
    save(
        admin_client,
        "/settings/integrations/musicbrainz",
        {
            "mb_user_agent": "Diskko/0.1 ( me@example.com )",
            "mb_cache_ttl_days": "5",
            "mb_refresh_days": "20",
        },
        page_url="/settings/integrations",
    )
    assert app.state.settings.mb_user_agent == "Diskko/0.1 ( me@example.com )"
    assert (app.state.settings.mb_cache_ttl_days, app.state.settings.mb_refresh_days) == (5, 20)
    assert (
        post(admin_client, f"/admin/artists/{artist_id}/sync", page="/admin/artists").status_code
        == 303
    )


def test_numbers_are_checked(admin_client):
    r = save(
        admin_client,
        "/settings/integrations/musicbrainz",
        {"mb_user_agent": "x", "mb_cache_ttl_days": "0", "mb_refresh_days": "abc"},
        page_url="/settings/integrations",
    )
    assert (
        r.status_code == 400 and "outside the allowed range" in r.text and "whole number" in r.text
    )


def test_hostile_text_is_escaped_when_shown_again(admin_client):
    evil = '"><script>alert(1)</script>' + "x" * 300
    r = save(
        admin_client,
        "/settings/integrations/musicbrainz",
        {"mb_user_agent": evil, "mb_cache_ttl_days": "7", "mb_refresh_days": "30"},
        page_url="/settings/integrations",
    )
    assert r.status_code == 400 and "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text


def test_cover_art_can_be_switched_off(app, admin_client):
    save(
        admin_client, "/settings/integrations/coverart", {}, page_url="/settings/integrations"
    )  # unchecked box
    assert app.state.settings.coverart_enabled is False
    assert 'name="coverart_enabled" value="1" checked' not in page(
        admin_client, "/settings/integrations"
    )
    save(
        admin_client,
        "/settings/integrations/coverart",
        {"coverart_enabled": "1"},
        page_url="/settings/integrations",
    )
    assert app.state.settings.coverart_enabled is True


def test_the_lastfm_key_is_stored_and_hidden(app, admin_client):
    save(
        admin_client,
        "/settings/integrations/lastfm",
        {"lastfm_api_key": "lastfm-key-999"},
        page_url="/settings/integrations",
    )
    assert app.state.settings.lastfm_api_key == "lastfm-key-999"
    assert "lastfm-key-999" not in page(admin_client, "/settings/integrations")


def test_the_status_card_shows_the_last_test_inside_the_settings(app, admin_client):
    from app.services.app_settings import set_setting

    with app.state.session_factory() as db:
        set_setting(
            db,
            "integration_status.plex",
            {
                "ok": True,
                "code": "ok",
                "detail": "1.40",
                "latency_ms": 9,
                "checked_at": "2026-11-06T10:00:00",
            },
        )
        db.commit()
    save(
        admin_client,
        "/settings/integrations/plex",
        {
            "plex_url": "http://plex:32400",
            "plex_token": TOKEN,
            "plex_music_section": "Music",
            "library_path_map": "",
        },
        page_url="/settings/integrations",
    )
    assert "Connected" in page(admin_client, "/settings/integrations")


# ------------------------------------------------------------------------- jobs


def test_jobs_page_shows_the_queues(admin_client):
    text = page(admin_client, "/settings/jobs")
    for queue in ("default", "library", "musicbrainz", "coverart", "slskd"):
        assert f'name="queue_{queue}"' in text
    assert re.search(r'id="queue-musicbrainz"[^>]*readonly', text)
    assert 'id="queue-default"' in text and not re.search(r'id="queue-default"[^>]*readonly', text)


def test_saving_the_workers_applies_them(app, admin_client):
    r = save(
        admin_client,
        "/settings/jobs",
        {
            "queue_default": "4",
            "queue_library": "1",
            "queue_musicbrainz": "5",
            "queue_coverart": "3",
            "queue_slskd": "2",
        },
        page_url="/settings/jobs",
    )
    assert r.status_code == 303
    assert app.state.settings.queue_concurrency == {
        "default": 4,
        "library": 1,
        "musicbrainz": 1,
        "coverart": 3,
        "slskd": 2,
    }  # MusicBrainz stays at 1
    text = page(admin_client, "/settings/jobs")
    assert re.search(r'id="queue-default"[^>]*value="4"', text) and re.search(
        r'id="queue-coverart"[^>]*value="3"', text
    )


@pytest.mark.parametrize("value", ["0", "9", "x", "-2"])
def test_bad_worker_numbers_are_refused(app, admin_client, value):
    r = save(admin_client, "/settings/jobs", {"queue_default": value}, page_url="/settings/jobs")
    assert r.status_code == 400 and "settings.error" not in r.text and overrides(app) == {}
    assert re.search(
        r'id="queue-default"[^>]*value="' + re.escape(value) + '"', r.text
    )  # typed value is kept


def test_running_workers_follow_the_saved_numbers(settings):
    live = settings.model_copy(update={"jobs_enabled": True, "job_poll_interval": 0.05})
    app = create_app(live)
    with TestClient(app, follow_redirects=False) as client:
        from tests.conftest import do_setup

        do_setup(client)
        assert app.state.worker._target["default"] == 2
        save(
            client,
            "/settings/jobs",
            {"queue_default": "5", "queue_library": "1", "queue_coverart": "2", "queue_slskd": "2"},
            page_url="/settings/jobs",
        )
        import time

        for _ in range(50):  # the change is applied on the event loop thread
            if app.state.worker._active["default"] == 5:
                break
            time.sleep(0.02)
        assert app.state.worker._target["default"] == 5 and app.state.worker._active["default"] == 5


# --------------------------------------------------------------------- albums


def test_quality_options_and_the_default(admin_client):
    text = page(admin_client, "/settings/albums")
    select = re.search(r'<select id="f-default_quality".*?</select>', text, re.S).group(0)
    options = re.findall(r'<option value="([a-z0-9_]+)"\s*(selected)?>([^<]+)</option>', select)
    assert [o[0] for o in options] == [
        "any",
        "mp3_192",
        "mp3_256",
        "mp3_320",
        "flac_standard",
        "flac_studio",
        "flac_hires",
        "flac_master",
    ]
    assert [o[0] for o in options if o[1]] == ["flac_standard"]  # FLAC Standard is the default
    labels = {o[0]: o[2] for o in options}
    assert labels["flac_standard"] == "FLAC Standard (16-bit / 44.1 kHz)"
    assert labels["flac_studio"] == "FLAC Studio (24-bit or 16-bit / 48 kHz)"
    assert labels["flac_hires"] == "FLAC Hi-Res (24-bit / 96 kHz)"
    assert labels["flac_master"] == "FLAC Master (24-bit / 192 kHz)"


def test_saving_the_default_quality(app, admin_client):
    assert app.state.settings.default_quality == "flac_standard"
    r = save(
        admin_client,
        "/settings/albums/quality",
        {"default_quality": "flac_hires"},
        page_url="/settings/albums",
    )
    assert r.status_code == 303 and app.state.settings.default_quality == "flac_hires"
    assert overrides(app) == {"default_quality": "flac_hires"}
    assert '<option value="flac_hires" selected>' in page(admin_client, "/settings/albums")


@pytest.mark.parametrize("value", ["lossless", "lossless_24", "best", ""])
def test_old_and_unknown_quality_names_are_refused(app, admin_client, value):
    r = save(
        admin_client,
        "/settings/albums/quality",
        {"default_quality": value},
        page_url="/settings/albums",
    )
    assert r.status_code == 400 and app.state.settings.default_quality == "flac_standard"


def test_the_missing_types_are_a_system_setting(app, admin_client, db):
    text = page(admin_client, "/settings/albums")
    assert (
        'value="album" checked' in text
        and 'value="ep" checked' in text
        and 'value="live" checked' not in text
    )
    r = save(
        admin_client,
        "/settings/albums/types",
        {"types": ["live", "single", "bogus"]},
        page_url="/settings/albums",
    )
    assert r.status_code == 303
    db.expire_all()
    assert get_missing_types(db) == ["single", "live"]
    assert 'value="live" checked' in page(admin_client, "/settings/albums")
    rows = db.scalars(select(Setting).where(Setting.key == "missing_types")).all()
    assert [r.scope for r in rows] == ["global"]


# ------------------------------------------------------------------ i18n, misc


def test_settings_in_italian(admin_client):
    post(admin_client, "/lang", {"lang": "it", "next": "/library"})
    assert "Cartella della libreria" in page(admin_client, "/settings/library")
    text = page(admin_client, "/settings/albums")
    assert "FLAC Standard (16-bit / 44,1 kHz)" in text and "Tipi di album mancanti" in text
    assert "Chiave API" in page(admin_client, "/settings/integrations")
    assert "Worker per coda" in page(admin_client, "/settings/jobs") or "Generale" in page(
        admin_client, "/settings/jobs"
    )


def test_the_library_admin_page_no_longer_has_the_types_form(admin_client):
    assert 'action="/admin/library/missing-types"' not in page(admin_client, "/admin/library")
    assert admin_client.post("/admin/library/missing-types").status_code in (403, 404, 405)


def test_nothing_secret_is_stored_outside_the_settings_table(app, admin_client, tmp_path):
    save(
        admin_client,
        "/settings/integrations/plex",
        {
            "plex_url": "http://plex:32400",
            "plex_token": TOKEN,
            "plex_music_section": "Music",
            "library_path_map": "",
        },
        page_url="/settings/integrations",
    )
    with app.state.session_factory() as db:
        keys = {s.key for s in db.scalars(select(Setting))}
    assert "config.plex_token" in keys and Path(app.state.settings.config_dir).exists()


def test_defaults_are_shown_with_readable_names(app, admin_client):
    albums = page(admin_client, "/settings/albums")
    assert (
        "FLAC Standard (16-bit / 44.1 kHz)" in albums.split("Default")[-1]
        and "flac_standard" not in albums.split("Default:")[-1][:60]
    )
    save(
        admin_client,
        "/settings/albums/quality",
        {"default_quality": "flac_master"},
        page_url="/settings/albums",
    )
    text = page(admin_client, "/settings/albums")
    assert (
        "Default: FLAC Standard (16-bit / 44.1 kHz)" in text
    )  # what "use the default" would restore
    save(admin_client, "/settings/integrations/coverart", {}, page_url="/settings/integrations")
    assert "Default: Yes" in page(admin_client, "/settings/integrations")
