"""The quarantine page: look at what the import refused, take it back to match by hand, delete."""

import asyncio
import json

import pytest
import respx

from app.jobs.worker import JobWorker
from app.models import DownloadRequest, Job
from app.services.importer import library_import as lib
from tests.conftest import MB_BASE, MB_UA, post
from tests.test_postprocess import (
    TITLES,
    good_files,
    mock_music,
    put_download,
    release_json,
    seed,
)


@pytest.fixture
def settings(settings, tmp_path):
    music, downloads = tmp_path / "music", tmp_path / "downloads"
    music.mkdir()
    downloads.mkdir()
    return settings.model_copy(
        update={
            "music_dir": music,
            "downloads_dir": downloads,
            "musicbrainz_url": MB_BASE,
            "mb_user_agent": MB_UA,
            "mb_min_interval": 0.0,
            "job_poll_interval": 0.02,
            "coverart_enabled": False,
            "providers_covers": "none",
            "library_source": "filesystem",
        }
    )


def quarantined(settings, session_factory, *, status="failed", reason="no release matches"):
    """A request whose download was refused and moved to the quarantine."""
    seed(session_factory)
    source = put_download(settings.downloads_dir, good_files())
    with session_factory() as db:
        req = db.get(DownloadRequest, 1)
        req.status, req.error = status, reason
        db.commit()
    return lib.quarantine(source, lib.quarantine_root(settings), 1, reason)


def test_the_page_lists_each_folder_with_its_reason_files_and_album(
    admin_client, settings, session_factory
):
    quarantined(settings, session_factory)
    html = admin_client.get("/admin/quarantine").text
    assert 'data-entry="1"' in html and "Album Title" in html
    assert "no release matches" in html and "3 track(s)" in html
    for n in range(3):
        assert f"{n}.flac" in html
    assert "data-reimport" in html and "data-delete-selected" in html


def test_an_empty_quarantine_says_so_and_only_an_admin_sees_it(admin_client, user_client):
    assert "data-empty" in admin_client.get("/admin/quarantine").text
    assert user_client.get("/admin/quarantine").status_code == 403
    r = post(user_client, "/admin/quarantine/delete", {"names": ["1"]}, page="/dashboard")
    assert r.status_code == 403


def test_the_folders_chosen_are_deleted_and_nothing_else(admin_client, settings, session_factory):
    folder = quarantined(settings, session_factory)
    keep = lib.quarantine_root(settings) / "keep-me"
    keep.mkdir()
    (keep / "a.flac").write_bytes(b"x")
    r = post(admin_client, "/admin/quarantine/delete", {"names": ["1"]}, page="/admin/quarantine")
    assert (
        r.status_code == 303
        and r.headers["location"] == "/admin/quarantine?flash=quarantine_deleted"
    )
    assert not folder.exists() and keep.is_dir()


@pytest.mark.parametrize("name", ["..", "../music", "a/b", "/etc", ".", "missing"])
def test_a_name_that_is_not_a_folder_of_the_quarantine_deletes_nothing(
    admin_client, settings, session_factory, name
):
    folder = quarantined(settings, session_factory)
    r = post(admin_client, "/admin/quarantine/delete", {"names": [name]}, page="/admin/quarantine")
    assert r.headers["location"].endswith("error=quarantine_nothing")
    assert folder.is_dir() and settings.music_dir.is_dir()


def test_a_link_out_of_the_quarantine_is_not_followed(
    admin_client, settings, session_factory, tmp_path
):
    root = lib.quarantine_root(settings)
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    (root / "99").symlink_to(outside)
    post(admin_client, "/admin/quarantine/delete", {"names": ["99"]}, page="/admin/quarantine")
    assert (outside / "precious.txt").read_text() == "keep"
    assert "data-entry" not in admin_client.get("/admin/quarantine").text


def test_a_folder_without_a_request_can_only_be_deleted(admin_client, settings):
    root = lib.quarantine_root(settings)
    (root / "42").mkdir(parents=True)
    (root / "42" / "a.flac").write_bytes(b"x")
    html = admin_client.get("/admin/quarantine").text
    assert 'data-entry="42"' in html and "data-reimport" not in html and "Only delete" in html
    r = post(admin_client, "/admin/quarantine/42/reimport", {}, page="/admin/quarantine")
    assert r.headers["location"].endswith("error=quarantine_no_request")
    assert (root / "42" / "a.flac").exists()


async def test_taking_a_folder_back_makes_its_request_ask_how_to_match_the_files(
    admin_client, app, settings, session_factory
):
    folder = quarantined(settings, session_factory)
    r = post(admin_client, "/admin/quarantine/1/reimport", {}, page="/admin/quarantine")
    assert r.status_code == 303 and r.headers["location"] == "/requests/1"
    assert not folder.exists()
    back = settings.downloads_dir / "diskko" / "1"
    assert back.is_dir() and len(list(back.glob("*.flac"))) == 3
    assert not (back / "REASON.txt").exists()  # the note stays in the quarantine's history only
    with session_factory() as db:
        req = db.get(DownloadRequest, 1)
        assert (req.status, req.error, req.dest_dir) == ("downloaded", None, "diskko/1")
        assert req.quality_key == min(app.state.quality_profiles.profiles, key=lambda p: p.rank).key
        job = db.query(Job).filter_by(type="postprocess").one()
        assert json.loads(job.payload_json) == {"request_id": 1, "release_choice": "reconcile"}

    # the import reads the files and asks, even though the release has the right size
    worker = JobWorker(settings, session_factory)
    await worker.start()
    worker.notify()
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock_music(mock, release_json(TITLES))
            for _ in range(500):
                row = await asyncio.to_thread(_status, session_factory)
                if row == "awaiting_choice":
                    break
                await asyncio.sleep(0.02)
    finally:
        await worker.stop()
    assert row == "awaiting_choice"
    assert "data-mapping-form" in admin_client.get("/requests/1/reconcile").text


def _status(factory):
    with factory() as db:
        return db.get(DownloadRequest, 1).status


def test_a_request_that_is_running_again_cannot_take_a_folder_back(
    admin_client, settings, session_factory
):
    folder = quarantined(settings, session_factory, status="queued")
    r = post(admin_client, "/admin/quarantine/1/reimport", {}, page="/admin/quarantine")
    assert r.headers["location"].endswith("error=quarantine_no_request")
    assert folder.is_dir()


def test_a_folder_is_not_taken_back_over_an_existing_one(admin_client, settings, session_factory):
    folder = quarantined(settings, session_factory)
    (settings.downloads_dir / "diskko" / "1").mkdir(parents=True)
    r = post(admin_client, "/admin/quarantine/1/reimport", {}, page="/admin/quarantine")
    assert r.headers["location"].endswith("error=quarantine_target_exists")
    assert folder.is_dir()


def test_the_requests_page_links_to_the_quarantine_for_an_admin_only(admin_client, user_client):
    assert "data-quarantine-link" in admin_client.get("/requests").text
    assert "data-quarantine-link" not in user_client.get("/requests").text
