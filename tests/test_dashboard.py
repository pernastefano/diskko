from sqlalchemy import select

from app.models import Job
from app.services.library.filesystem import FilesystemProvider
from app.services.library.provider import RecordingReporter
from app.services.library.scan import run_scan
from tests.audio import make_track
from tests.conftest import post


def populate(app):
    root = app.state.settings.music_dir
    make_track(
        root / "Aretha/Lady Soul/01.flac",
        _bit_depth=24,
        artist="Aretha",
        albumartist="Aretha",
        album="Lady Soul",
    )
    make_track(
        root / "Daft Punk/Discovery/01.mp3",
        _bitrate=128,
        artist="Daft Punk",
        albumartist="Daft Punk",
        album="Discovery",
    )
    run_scan(app.state.session_factory, FilesystemProvider(root), RecordingReporter())


def test_empty_library_page(admin_client):
    page = admin_client.get("/dashboard").text
    assert "The library has not been scanned yet." in page
    assert "Scan library" not in page  # the buttons live in the library settings
    assert "Scheduled scan" in page and "Every day (2:00)" in page


def test_page_shows_counts_quality_and_last_scan(app, admin_client, tmp_path):
    app.state.settings.music_dir = tmp_path / "music"
    populate(app)
    page = admin_client.get("/dashboard").text
    assert "FLAC Hi-Res (24/96)" in page and "Qualsiasi / bassa" in page
    assert "Files seen" in page and "The library has not been scanned yet." not in page
    assert str(tmp_path / "music") in page


def test_scan_button_queues_a_deduplicated_job(app, admin_client, db):
    r = post(admin_client, "/settings/library/scan", page="/settings/library")
    assert r.status_code == 303
    job = db.scalar(select(Job))
    assert r.headers["location"] == f"/admin/jobs/{job.id}"
    assert (job.type, job.queue, job.payload_json) == (
        "scan_library",
        "library",
        '{"force": false}',
    )
    assert job.created_by is not None
    post(admin_client, "/settings/library/scan", page="/settings/library")
    assert len(db.scalars(select(Job)).all()) == 1  # a scan is already queued
    for path in ("/dashboard", "/settings/library"):  # both say a scan is running
        page = admin_client.get(path).text
        assert "A scan is in progress" in page and f"/admin/jobs/{job.id}" in page


def test_full_rescan_sets_the_force_flag(admin_client, db):
    post(admin_client, "/settings/library/scan", {"force": "1"}, page="/settings/library")
    assert db.scalar(select(Job)).payload_json == '{"force": true}'


def test_scan_requires_csrf_and_admin(admin_client, user_client):
    assert admin_client.post("/settings/library/scan").status_code == 403
    assert post(user_client, "/settings/library/scan").status_code == 403


def test_plex_source_shows_the_section_but_no_secrets(app, admin_client):
    s = app.state.settings
    s.library_source, s.plex_url, s.plex_token = (
        "plex",
        "http://plex.local:32400",
        "secret-token-1234",
    )
    page = admin_client.get("/dashboard").text
    assert "Plex music section" in page and s.plex_music_section in page
    assert "secret-token-1234" not in page and "plex.local" not in page


def test_page_in_italian_and_nav_link(admin_client):
    post(admin_client, "/lang", {"lang": "it", "next": "/"})
    page = admin_client.get("/dashboard").text
    assert 'href="/dashboard"' in page
    settings = admin_client.get("/settings/library").text
    assert "Scansiona libreria" in settings and "Riscansione completa" in settings


def test_the_scan_buttons_are_in_the_library_settings_not_on_the_dashboard(admin_client):
    dashboard = admin_client.get("/dashboard").text
    assert "Scan library" not in dashboard and "Full rescan" not in dashboard
    assert "/dashboard/scan" not in dashboard and "/settings/library/scan" not in dashboard
    assert admin_client.post("/dashboard/scan").status_code in (404, 405)
    settings = admin_client.get("/settings/library").text
    assert 'id="library-scan"' in settings
    assert settings.count('action="/settings/library/scan"') == 2
    assert 'name="force" value="1"' in settings
    assert "Scan library" in settings and "Full rescan" in settings
