import re

import pytest
from sqlalchemy import func, select

from app.jobs import engine
from app.models import STATUS_CANCELLED, STATUS_QUEUED, Job
from tests.conftest import add_user, post

HX = {"HX-Request": "true"}


@pytest.fixture
def make_job(app, admin_client):
    def make(type="demo", payload=None, **kw):
        with app.state.session_factory() as db:
            return engine.enqueue(db, type, payload, **kw).id

    return make


def fail(app, job_id, error="boom"):
    with app.state.session_factory() as db:
        engine.claim_next(db, "default")
        engine.finish_failure(db, job_id, error)


def test_empty_list(admin_client):
    page = admin_client.get("/admin/jobs").text
    assert "No jobs yet." in page and "Run demo job" in page


def test_list_shows_jobs_and_is_live(admin_client, make_job):
    job_id = make_job()
    page = admin_client.get("/admin/jobs").text
    assert f"#{job_id}" in page and "Queued" in page
    assert 'hx-trigger="every 2s"' in page
    assert 'hx-get="/admin/jobs/rows?page=1"' in page


def test_filters_by_type_and_status_and_polling_keeps_them(admin_client, make_job, app):
    demo = make_job("demo")
    hk = make_job("housekeeping")
    fail(app, demo)
    page = admin_client.get("/admin/jobs?type=housekeeping").text
    assert f"/admin/jobs/{hk}" in page and f"/admin/jobs/{demo}" not in page
    assert "type=housekeeping" in page  # the poller keeps the filter
    failed = admin_client.get("/admin/jobs?status=failed").text
    assert f"/admin/jobs/{demo}" in failed and f"/admin/jobs/{hk}" not in failed
    assert "status=failed" in failed


def test_invalid_filters_are_ignored(admin_client, make_job):
    job_id = make_job()
    page = admin_client.get("/admin/jobs?type=nope&status=weird").text
    assert f"/admin/jobs/{job_id}" in page


def test_pagination(admin_client, make_job):
    for _ in range(26):
        make_job("housekeeping")
    first = admin_client.get("/admin/jobs").text
    assert "Page 1 of 2" in first and "Next" in first and "Previous" not in first
    second = admin_client.get("/admin/jobs?page=2").text
    assert "Page 2 of 2" in second and "Previous" in second
    assert second.count('id="job-') == 1


def test_rows_fragment_is_only_rows(admin_client, make_job):
    make_job()
    r = admin_client.get("/admin/jobs/rows")
    assert r.status_code == 200
    assert "<html" not in r.text and "<tr" in r.text


def test_demo_button_enqueues_and_redirects_to_detail(admin_client, app):
    r = post(admin_client, "/admin/jobs/demo", page="/admin/jobs")
    assert r.status_code == 303
    with app.state.session_factory() as db:
        job = db.scalar(select(Job))
        assert job.type == "demo" and job.status == STATUS_QUEUED and job.created_by is not None
    assert r.headers["location"] == f"/admin/jobs/{job.id}"


def test_demo_button_requires_csrf(admin_client):
    assert admin_client.post("/admin/jobs/demo").status_code == 403


def test_detail_page_shows_logs_and_polls_while_active(admin_client, make_job):
    job_id = make_job()
    page = admin_client.get(f"/admin/jobs/{job_id}").text
    assert "Queued" in page  # the first log line
    assert f'hx-get="/admin/jobs/{job_id}/summary"' in page
    assert f'hx-get="/admin/jobs/{job_id}/logs?after=' in page


def test_detail_page_stops_polling_when_finished(admin_client, make_job, app):
    job_id = make_job()
    fail(app, job_id)
    page = admin_client.get(f"/admin/jobs/{job_id}").text
    assert "/summary" not in page and "/logs?after=" not in page
    assert "Failed: boom" in page and "Retry" in page


def test_unknown_job_is_404(admin_client):
    for path in ("/admin/jobs/999", "/admin/jobs/999/logs", "/admin/jobs/999/summary"):
        assert admin_client.get(path).status_code == 404


def test_log_cursor_returns_only_new_lines(admin_client, make_job, app):
    job_id = make_job()
    with app.state.session_factory() as db:
        engine.add_log(db, job_id, "info", "first line")
        cursor = engine.logs_after(db, job_id)[-1].id
        engine.add_log(db, job_id, "info", "second line")
    r = admin_client.get(f"/admin/jobs/{job_id}/logs?after={cursor}")
    assert "second line" in r.text and "first line" not in r.text
    new_cursor = re.search(r"after=(\d+)", r.text).group(1)
    assert int(new_cursor) > cursor
    assert 'hx-swap-oob="true"' in r.text and "every 1s" in r.text


def test_log_poller_stops_once_job_is_over(admin_client, make_job, app):
    job_id = make_job()
    fail(app, job_id)
    with app.state.session_factory() as db:
        last = engine.logs_after(db, job_id)[-1].id
    r = admin_client.get(f"/admin/jobs/{job_id}/logs?after={last}")
    assert 'id="log-poller"' in r.text and "hx-trigger" not in r.text


def test_summary_polls_only_while_active(admin_client, make_job, app):
    job_id = make_job()
    assert "every 2s" in admin_client.get(f"/admin/jobs/{job_id}/summary").text
    fail(app, job_id)
    assert "every 2s" not in admin_client.get(f"/admin/jobs/{job_id}/summary").text


def test_cancel_queued_job_via_htmx(admin_client, make_job, app):
    job_id = make_job()
    r = post(admin_client, f"/admin/jobs/{job_id}/cancel", headers=HX, page="/admin/jobs")
    assert r.status_code == 200 and "Cancelled" in r.text and "<tr" in r.text
    with app.state.session_factory() as db:
        assert db.get(Job, job_id).status == STATUS_CANCELLED


def test_cancel_running_job_shows_cancelling(admin_client, make_job, app):
    job_id = make_job()
    with app.state.session_factory() as db:
        engine.claim_next(db, "default")
    r = post(admin_client, f"/admin/jobs/{job_id}/cancel", headers=HX, page="/admin/jobs")
    assert "cancelling" in r.text


def test_cancel_finished_job_is_conflict(admin_client, make_job, app):
    job_id = make_job()
    fail(app, job_id)
    r = post(admin_client, f"/admin/jobs/{job_id}/cancel", headers=HX, page="/admin/jobs")
    assert r.status_code == 409 and "cannot be cancelled" in r.text


def test_retry_failed_job(admin_client, make_job, app):
    job_id = make_job()
    fail(app, job_id)
    r = post(admin_client, f"/admin/jobs/{job_id}/retry", headers=HX, page="/admin/jobs")
    assert r.status_code == 200 and "Queued" in r.text
    with app.state.session_factory() as db:
        job = db.get(Job, job_id)
        assert job.status == STATUS_QUEUED and job.attempts == 0 and job.error is None


def test_retry_active_job_is_conflict(admin_client, make_job):
    job_id = make_job()
    r = post(admin_client, f"/admin/jobs/{job_id}/retry", headers=HX, page="/admin/jobs")
    assert r.status_code == 409 and "Only failed or cancelled" in r.text


def test_retry_blocked_when_identical_job_is_active(admin_client, make_job, app):
    first = make_job("housekeeping", dedupe_key="same")
    fail(app, first)
    make_job("housekeeping", dedupe_key="same")
    r = post(admin_client, f"/admin/jobs/{first}/retry", headers=HX, page="/admin/jobs")
    assert r.status_code == 409 and "already queued or running" in r.text


def test_plain_form_actions_redirect_to_detail(admin_client, make_job, app):
    job_id = make_job()
    fail(app, job_id)
    r = post(admin_client, f"/admin/jobs/{job_id}/retry", page=f"/admin/jobs/{job_id}")
    assert r.status_code == 303 and r.headers["location"] == f"/admin/jobs/{job_id}"


def test_job_error_and_logs_are_escaped(admin_client, make_job, app):
    job_id = make_job()
    fail(app, job_id, error="<script>alert(1)</script>")
    with app.state.session_factory() as db:
        engine.add_log(db, job_id, "info", "<img src=x onerror=alert(2)>")
    page = admin_client.get(f"/admin/jobs/{job_id}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page
    assert "<img src=x" not in page


def test_job_pages_follow_language(admin_client, make_job):
    make_job()
    post(admin_client, "/lang", {"lang": "it", "next": "/"})
    page = admin_client.get("/admin/jobs").text
    assert "Avvia job demo" in page and "In coda" in page


def test_regular_user_cannot_use_job_pages(admin_client, user_client, make_job):
    job_id = make_job()
    assert user_client.get("/admin/jobs").status_code == 403
    assert user_client.get(f"/admin/jobs/{job_id}/logs").status_code == 403
    r = post(user_client, f"/admin/jobs/{job_id}/cancel", headers=HX)
    assert r.status_code == 403


def test_nav_links_for_admin_only(admin_client, user_client):
    assert "/admin/jobs" in admin_client.get("/").text
    assert "/admin/status" in admin_client.get("/").text
    assert "/admin/jobs" not in user_client.get("/").text


def test_job_count_unchanged_by_listing(admin_client, make_job, db):
    make_job()
    admin_client.get("/admin/jobs")
    admin_client.get("/admin/jobs/rows")
    assert db.scalar(select(func.count(Job.id))) == 1


def test_second_admin_sees_same_jobs(admin_client, new_client, make_job):
    from tests.conftest import login

    add_user(admin_client, "boss", role="admin")
    job_id = make_job()
    other = new_client()
    login(other, "boss")
    assert f"/admin/jobs/{job_id}" in other.get("/admin/jobs").text
