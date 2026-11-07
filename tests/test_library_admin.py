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
    page = admin_client.get("/admin/library").text
    assert "The library has not been scanned yet." in page
    assert "Scan library" in page and "Full rescan" in page
    assert "Nightly scan" in page and "0 2 * * *" in page


def test_page_shows_counts_quality_and_last_scan(app, admin_client, tmp_path):
    app.state.settings.music_dir = tmp_path / "music"
    populate(app)
    page = admin_client.get("/admin/library").text
    assert "FLAC Hi-Res (24/96)" in page and "Qualsiasi / bassa" in page
    assert "Files seen" in page and "The library has not been scanned yet." not in page
    assert str(tmp_path / "music") in page


def test_scan_button_queues_a_deduplicated_job(app, admin_client, db):
    r = post(admin_client, "/admin/library/scan", page="/admin/library")
    assert r.status_code == 303
    job = db.scalar(select(Job))
    assert r.headers["location"] == f"/admin/jobs/{job.id}"
    assert (job.type, job.queue, job.payload_json) == (
        "scan_library",
        "library",
        '{"force": false}',
    )
    assert job.created_by is not None
    post(admin_client, "/admin/library/scan", page="/admin/library")
    assert len(db.scalars(select(Job)).all()) == 1  # a scan is already queued
    page = admin_client.get("/admin/library").text
    assert "A scan is in progress" in page and f"/admin/jobs/{job.id}" in page


def test_full_rescan_sets_the_force_flag(admin_client, db):
    post(admin_client, "/admin/library/scan", {"force": "1"}, page="/admin/library")
    assert db.scalar(select(Job)).payload_json == '{"force": true}'


def test_scan_requires_csrf_and_admin(admin_client, user_client):
    assert admin_client.post("/admin/library/scan").status_code == 403
    assert user_client.get("/admin/library").status_code == 403
    assert post(user_client, "/admin/library/scan").status_code == 403


def test_plex_source_shows_the_section_but_no_secrets(app, admin_client):
    s = app.state.settings
    s.library_source, s.plex_url, s.plex_token = (
        "plex",
        "http://plex.local:32400",
        "secret-token-1234",
    )
    page = admin_client.get("/admin/library").text
    assert "Plex music section" in page and s.plex_music_section in page
    assert "secret-token-1234" not in page and "plex.local" not in page


def test_page_in_italian_and_nav_link(admin_client):
    post(admin_client, "/lang", {"lang": "it", "next": "/"})
    page = admin_client.get("/admin/library").text
    assert "Scansiona libreria" in page and "Riscansione completa" in page
    assert 'href="/admin/library"' in page
