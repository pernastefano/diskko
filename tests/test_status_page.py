import re

from sqlalchemy import func, select

from app.integrations.status import check_dedupe_key
from app.jobs import engine
from app.models import Job
from app.services.app_settings import set_setting
from tests.conftest import post

HX = {"HX-Request": "true"}


def store(app, name, **result):
    with app.state.session_factory() as db:
        set_setting(db, f"integration_status.{name}", result)
        db.commit()


def test_status_page_lists_integrations_and_queues(admin_client):
    page = admin_client.get("/admin/status").text
    for label in ("Plex", "SLSKD", "MusicBrainz"):
        assert label in page
    assert page.count("Not configured") == 3  # nothing configured in the test settings
    assert "Never tested." in page
    assert "default" in page and "musicbrainz" in page  # queue table
    assert "Job workers are disabled" in page  # jobs_enabled=False in tests


def test_configured_integration_shows_test_button_enabled(app, admin_client):
    app.state.settings.plex_url = "http://plex.local:32400"
    app.state.settings.plex_token = "tok-12345"
    card = admin_client.get("/admin/status/plex/card").text
    assert "Not configured" not in card and "Test connection" in card
    assert not re.search(r"<button[^>]*\bdisabled\b(?!:)", card)


def test_card_shows_stored_result(app, admin_client):
    app.state.settings.slskd_url = "http://slskd:5030"
    app.state.settings.slskd_api_key = "key-1234567890"
    store(
        app,
        "slskd",
        ok=True,
        code="ok",
        detail="0.21.4",
        latency_ms=12,
        checked_at="2026-11-06T10:00:00",
    )
    card = admin_client.get("/admin/status/slskd/card").text
    assert "Connected" in card and "0.21.4" in card and "12 ms" in card
    assert "2026-11-06T10:00:00" in card


def test_card_shows_failure_in_italian(app, admin_client):
    app.state.settings.plex_url = "http://plex.local:32400"
    app.state.settings.plex_token = "tok-12345"
    store(
        app,
        "plex",
        ok=False,
        code="auth_failed",
        detail="",
        latency_ms=5,
        checked_at="2026-11-06T10:00:00",
    )
    post(admin_client, "/lang", {"lang": "it", "next": "/"})
    card = admin_client.get("/admin/status/plex/card").text
    assert "Problema" in card and "ha rifiutato le credenziali" in card


def test_test_button_enqueues_a_deduplicated_job(app, admin_client, db):
    app.state.settings.plex_url = "http://plex.local:32400"
    app.state.settings.plex_token = "tok-12345"
    r = post(admin_client, "/admin/status/plex/test", headers=HX, page="/admin/status")
    assert r.status_code == 200
    assert "Testing…" in r.text and 'hx-trigger="every 1s"' in r.text  # polls until done
    post(admin_client, "/admin/status/plex/test", headers=HX, page="/admin/status")
    jobs = db.scalars(select(Job).where(Job.type == "integration_check")).all()
    assert len(jobs) == 1
    assert jobs[0].dedupe_key == check_dedupe_key("plex")
    assert jobs[0].payload_json == '{"name": "plex"}'
    assert jobs[0].created_by is not None


def test_polling_stops_when_check_finishes(app, admin_client):
    post(admin_client, "/admin/status/slskd/test", headers=HX, page="/admin/status")
    with app.state.session_factory() as db:
        job = db.scalar(select(Job))
        engine.claim_next(db, "default")
        engine.finish_success(db, job.id)
    card = admin_client.get("/admin/status/slskd/card").text
    assert "every 1s" not in card and "Testing…" not in card


def test_plain_form_post_redirects(admin_client):
    r = post(admin_client, "/admin/status/musicbrainz/test", page="/admin/status")
    assert r.status_code == 303 and r.headers["location"] == "/admin/status"


def test_unknown_integration_is_404(admin_client, db):
    assert admin_client.get("/admin/status/spotify/card").status_code == 404
    assert post(admin_client, "/admin/status/spotify/test", headers=HX).status_code == 404
    assert db.scalar(select(func.count(Job.id))) == 0


def test_test_button_requires_csrf_and_admin(admin_client, user_client):
    assert admin_client.post("/admin/status/plex/test").status_code == 403
    assert user_client.get("/admin/status").status_code == 403
    assert post(user_client, "/admin/status/plex/test", headers=HX).status_code == 403


def test_no_secret_ever_reaches_the_status_html(app, admin_client):
    s = app.state.settings
    s.plex_url, s.plex_token = "http://plex.local:32400", "plex-secret-token"
    s.slskd_url, s.slskd_api_key = "http://slskd:5030", "slskd-secret-key-123456"
    s.mb_user_agent = "Diskko/0.1 ( private@example.com )"
    for name in ("plex", "slskd", "musicbrainz"):
        post(admin_client, f"/admin/status/{name}/test", headers=HX, page="/admin/status")
    pages = admin_client.get("/admin/status").text + admin_client.get("/admin/jobs").text
    for secret in ("plex-secret-token", "slskd-secret-key-123456", "private@example.com"):
        assert secret not in pages
