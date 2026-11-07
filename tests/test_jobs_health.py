"""The Jobs icon: yellow while jobs are in progress, red after recent failures."""

import re
from datetime import timedelta

from app.jobs import engine, registry
from app.jobs.engine import JobsHealth, jobs_health
from app.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    utcnow,
)
from tests.conftest import post


def add(db, type_="test.steps", status=STATUS_QUEUED, finished_ago=None):
    job = engine.enqueue(db, type_, {})
    job.status = status
    job.finished_at = utcnow() - finished_ago if finished_ago is not None else None
    db.commit()
    return job


# ----------------------------------------------------------------------- the engine


def test_an_idle_system_has_nothing_to_show(jdb):
    assert jobs_health(jdb) == JobsHealth(0, 0)


def test_queued_and_running_jobs_are_in_progress(jdb):
    add(jdb, status=STATUS_QUEUED)
    add(jdb, status=STATUS_RUNNING)
    add(jdb, status=STATUS_SUCCEEDED, finished_ago=timedelta(minutes=1))
    add(jdb, status=STATUS_CANCELLED, finished_ago=timedelta(minutes=1))
    assert jobs_health(jdb) == JobsHealth(2, 0)


def test_recent_failures_count_and_old_ones_do_not(jdb):
    add(jdb, status=STATUS_FAILED, finished_ago=timedelta(hours=1))
    add(jdb, type_="housekeeping", status=STATUS_FAILED, finished_ago=timedelta(hours=23))
    add(jdb, type_="scan_library", status=STATUS_FAILED, finished_ago=timedelta(hours=25))
    assert jobs_health(jdb).failed == 2
    assert jobs_health(jdb, window=timedelta(minutes=30)).failed == 0


def test_a_later_success_of_the_same_job_type_clears_the_failure(jdb):
    add(jdb, status=STATUS_FAILED, finished_ago=timedelta(hours=2))
    assert jobs_health(jdb).failed == 1
    add(jdb, status=STATUS_SUCCEEDED, finished_ago=timedelta(hours=1))
    assert jobs_health(jdb).failed == 0  # it was fixed afterwards


def test_other_types_and_earlier_successes_do_not_clear_it(jdb):
    add(jdb, status=STATUS_SUCCEEDED, finished_ago=timedelta(hours=3))
    add(jdb, status=STATUS_FAILED, finished_ago=timedelta(hours=2))  # the success came before
    add(jdb, type_="housekeeping", status=STATUS_SUCCEEDED, finished_ago=timedelta(hours=1))
    assert jobs_health(jdb).failed == 1


def test_each_failure_is_judged_on_its_own_job_type(jdb):
    add(jdb, type_="scan_library", status=STATUS_FAILED, finished_ago=timedelta(hours=2))
    add(jdb, type_="housekeeping", status=STATUS_FAILED, finished_ago=timedelta(hours=2))
    add(jdb, type_="scan_library", status=STATUS_SUCCEEDED, finished_ago=timedelta(hours=1))
    assert jobs_health(jdb).failed == 1  # only housekeeping is still broken


# ------------------------------------------------------------------------ the icon


def jobs_link(html):
    return re.search(r'<a href="/admin/jobs"[^>]*>', html).group(0)


def test_the_jobs_icon_is_neutral_when_nothing_is_going_on(admin_client):
    link = jobs_link(admin_client.get("/library").text)
    assert "data-jobs" not in link and "text-slate-500" in link


def test_the_jobs_icon_is_yellow_while_jobs_are_in_progress(admin_client, db):
    add(db, status=STATUS_RUNNING)
    link = jobs_link(admin_client.get("/library").text)
    assert 'data-jobs="active"' in link and "bg-amber-100" in link
    assert 'title="Jobs: 1 in progress"' in link


def test_the_jobs_icon_is_red_after_a_recent_failure(admin_client, db):
    add(db, status=STATUS_FAILED, finished_ago=timedelta(minutes=5))
    link = jobs_link(admin_client.get("/library").text)
    assert 'data-jobs="failed"' in link and "bg-red-100" in link
    assert 'title="Jobs: 1 failed recently"' in link


def test_red_wins_over_yellow_and_the_tooltip_says_both(admin_client, db):
    add(db, status=STATUS_RUNNING)
    add(db, status=STATUS_QUEUED)
    add(db, type_="housekeeping", status=STATUS_FAILED, finished_ago=timedelta(minutes=5))
    link = jobs_link(admin_client.get("/library").text)
    assert "bg-red-100" in link and "bg-amber-100" not in link
    assert "Jobs: 1 failed recently · 2 in progress" in link


def test_an_old_failure_does_not_keep_the_icon_red(admin_client, db):
    add(db, status=STATUS_FAILED, finished_ago=timedelta(hours=30))
    assert "data-jobs" not in jobs_link(admin_client.get("/library").text)


def test_the_icon_follows_the_language(admin_client, db):
    add(db, status=STATUS_RUNNING)
    post(admin_client, "/lang", {"lang": "it", "next": "/library"})
    assert 'title="Job: 1 in corso"' in jobs_link(admin_client.get("/library").text)


def test_ordinary_users_do_not_get_the_icons(user_client, db):
    add(db, status=STATUS_FAILED, finished_ago=timedelta(minutes=5))
    assert "nav-icons" not in user_client.get("/library").text


# ------------------------------------------------------------- the refreshing partial


def test_the_top_bar_refreshes_the_icons_by_itself(admin_client):
    text = admin_client.get("/library").text
    assert 'id="nav-icons"' in text and 'hx-get="/nav/icons"' in text
    assert 'hx-trigger="every 15s"' in text


def test_the_partial_returns_only_the_icons_with_the_current_highlight(admin_client, db):
    add(db, status=STATUS_RUNNING)
    r = admin_client.get("/nav/icons", headers={"HX-Current-URL": "http://testserver/admin/jobs"})
    assert r.status_code == 200 and "<html" not in r.text and 'id="nav-icons"' in r.text
    link = jobs_link(r.text)
    assert 'aria-current="page"' in link and "bg-amber-100" in link
    assert r.text.count('aria-current="page"') == 1


def test_the_partial_is_for_administrators_only(user_client, new_client, admin_client):
    assert user_client.get("/nav/icons").status_code == 403
    anon = new_client().get("/nav/icons")
    assert anon.status_code == 303 and anon.headers["location"].startswith("/login")


# ----------------------------------------------------------------- no demo job any more


def test_the_demo_job_is_gone(admin_client):
    assert registry.get_spec("demo") is None
    page = admin_client.get("/admin/jobs").text
    assert "/admin/jobs/demo" not in page and "demo" not in page.lower()
    assert post(admin_client, "/admin/jobs/demo", page="/admin/jobs").status_code in (404, 405, 422)
