"""The status icon: periodic checks of the configured services and the overall state."""

from datetime import timedelta

import respx
from sqlalchemy import select

from app.integrations.base import CheckResult
from app.integrations.status import (
    HEALTH_DOWN,
    HEALTH_OK,
    HEALTH_PARTIAL,
    HEALTH_UNKNOWN,
    integration_health,
    last_result,
    record_result,
)
from app.jobs.scheduler import JobScheduler, schedules_for, status_check_cron
from app.jobs.worker import JobWorker
from app.models import STATUS_SUCCEEDED, Job, utcnow
from tests.conftest import post
from tests.discovery_data import load
from tests.test_jobs_worker import enqueue, logs, wait_for_status

OK = CheckResult(True, "ok")
BAD = CheckResult(False, "unreachable", "timeout")


def record(db, **results):
    for name, result in results.items():
        record_result(db, name, result)
    db.commit()


# With the plain test settings only iTunes and Deezer need nothing, so they are the two
# configured services; everything else becomes "configured" when its setting is given.


def health(db, settings, **kw):
    return integration_health(db, settings, **kw)


def test_nothing_checked_yet_is_unknown(db, settings):
    h = health(db, settings)
    assert (h.state, h.connected, h.configured) == (HEALTH_UNKNOWN, 0, 2)


def test_all_configured_services_connected_is_ok(db, settings):
    record(db, itunes=OK, deezer=OK)
    assert health(db, settings).state == HEALTH_OK


def test_some_connected_is_partial(db, settings):
    record(db, itunes=OK, deezer=BAD)
    h = health(db, settings)
    assert (h.state, h.connected, h.configured) == (HEALTH_PARTIAL, 1, 2)


def test_none_connected_is_down(db, settings):
    record(db, itunes=BAD, deezer=BAD)
    assert health(db, settings).state == HEALTH_DOWN


def test_services_that_are_not_configured_do_not_count(db, settings):
    record(db, itunes=OK, deezer=OK, plex=BAD, slskd=BAD, lastfm=BAD)  # none of them is set up
    assert health(db, settings).state == HEALTH_OK


def test_a_configured_service_that_was_never_checked_is_not_connected(db, settings):
    record(db, itunes=OK)
    assert health(db, settings).state == HEALTH_PARTIAL
    both = settings.model_copy(update={"lastfm_api_key": "k-123456"})
    record(db, itunes=OK, deezer=OK)
    assert health(db, both).state == HEALTH_PARTIAL  # Last.fm is configured now, still unchecked
    record(db, lastfm=OK)
    assert health(db, both).state == HEALTH_OK


def test_configuring_more_services_changes_the_total(db, settings):
    more = settings.model_copy(
        update={"mb_user_agent": "Diskko/0.1 ( test@example.com )", "lastfm_api_key": "k-123456"}
    )
    assert health(db, more).configured == 5  # + MusicBrainz, ListenBrainz, Last.fm


def test_old_results_stop_counting(db, settings):
    record(db, itunes=OK, deezer=OK)
    soon = lambda: utcnow() + timedelta(minutes=10)  # noqa: E731
    later = lambda: utcnow() + timedelta(minutes=16)  # noqa: E731
    assert health(db, settings, now=soon).state == HEALTH_OK
    assert health(db, settings, now=later).state == HEALTH_UNKNOWN  # three intervals have passed


def test_without_automatic_checks_results_never_expire(db, settings):
    record(db, itunes=OK, deezer=OK)
    off = settings.model_copy(update={"status_check_minutes": 0})
    assert health(db, off, now=lambda: utcnow() + timedelta(days=30)).state == HEALTH_OK


def test_a_broken_timestamp_is_ignored(db, settings):
    from app.services.app_settings import set_setting

    set_setting(db, "integration_status.itunes", {"ok": True, "code": "ok", "checked_at": "junk"})
    db.commit()
    assert health(db, settings).state == HEALTH_UNKNOWN


# --------------------------------------------------------------------- the schedule


def test_the_checks_run_every_five_minutes_by_default(settings):
    assert settings.status_check_minutes == 5
    cron = [s.cron for s in schedules_for(settings) if s.job_type == "check_integrations"]
    assert cron == ["*/5 * * * *"]


def test_the_interval_is_configurable_and_can_be_turned_off(settings):
    def crons(minutes):
        s = settings.model_copy(update={"status_check_minutes": minutes})
        return [x.cron for x in schedules_for(s) if x.job_type == "check_integrations"]

    assert crons(15) == ["*/15 * * * *"] and crons(0) == [] and crons(-1) == []
    assert crons(120) == ["0 * * * *"]
    assert status_check_cron(59) == "*/59 * * * *" and status_check_cron(60) == "0 * * * *"


async def test_the_scheduler_checks_at_once_and_then_on_the_interval(settings, session_factory):
    scheduler = JobScheduler(settings, session_factory)
    scheduler.start()
    try:
        job = scheduler._scheduler.get_job("scheduled:check_integrations")
        assert job is not None
        assert (
            abs(
                (
                    job.next_run_time - job.next_run_time.now(job.next_run_time.tzinfo)
                ).total_seconds()
            )
            < 5
        )
        other = scheduler._scheduler.get_job("scheduled:housekeeping")
        assert (other.next_run_time - job.next_run_time).total_seconds() > 60  # not at once
    finally:
        scheduler.shutdown()


async def test_saving_settings_checks_the_services_again_at_once(settings, session_factory):
    scheduler = JobScheduler(settings, session_factory)
    scheduler.start()
    try:
        scheduler.reschedule(settings.model_copy(update={"lastfm_api_key": "new-key-123"}))
        job = scheduler._scheduler.get_job("scheduled:check_integrations")
        assert (
            abs(
                (
                    job.next_run_time - job.next_run_time.now(job.next_run_time.tzinfo)
                ).total_seconds()
            )
            < 5
        )
    finally:
        scheduler.shutdown()


# ----------------------------------------------------------------------------- the job


async def test_the_job_checks_only_the_configured_services(settings, session_factory):
    s = settings.model_copy(update={"job_poll_interval": 0.02})
    with respx.mock(assert_all_called=False) as mock:
        itunes = mock.get("https://itunes.apple.com/search").respond(
            json=load("itunes_album_search")
        )
        mock.get("https://api.deezer.com/album/302127").respond(500)
        plex = mock.get(url__regex=r"https?://plex.*")
        lastfm = mock.get("https://ws.audioscrobbler.com/2.0/")
        worker = JobWorker(s, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "check_integrations")
        worker.notify()
        job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert itunes.call_count == 1 and plex.call_count == 0 and lastfm.call_count == 0
    assert job.progress_pct == 100.0 and job.queue == "default"
    with session_factory() as db:
        assert last_result(db, "itunes")["ok"] is True
        deezer = last_result(db, "deezer")
        assert deezer["ok"] is False and deezer["code"] == "http_error"
        assert last_result(db, "plex") is None and last_result(db, "lastfm") is None
        assert health(db, s).state == HEALTH_PARTIAL
    messages = logs(session_factory, job_id)
    assert "itunes: ok" in messages and "Checked 2 configured service(s)" in messages


# ---------------------------------------------------------------------------- the pages


def status_link(html):
    import re

    return re.search(r'<a href="/admin/status"[^>]*>', html).group(0)


def test_the_status_icon_is_neutral_until_something_is_known(admin_client):
    link = status_link(admin_client.get("/dashboard").text)
    assert "data-health" not in link and "text-slate-500" in link


def test_the_status_icon_is_green_yellow_or_red(admin_client, db):
    cases = (
        ({"itunes": OK, "deezer": OK}, "ok", "bg-green-100"),
        ({"itunes": OK, "deezer": BAD}, "partial", "bg-amber-100"),
        ({"itunes": BAD, "deezer": BAD}, "down", "bg-red-100"),
    )
    for results, state, color in cases:
        record(db, **results)
        html = admin_client.get("/dashboard").text
        link = status_link(html)
        assert f'data-health="{state}"' in link and color in link, state
        assert "configured service" in link  # the tooltip says what the colour means
    # the same colours on the status page itself
    assert 'data-health="down"' in admin_client.get("/admin/status").text


def test_the_icon_tells_everything_in_words(admin_client, db):
    record(db, itunes=OK, deezer=BAD)
    link = status_link(admin_client.get("/dashboard").text)
    words = "Status: 1 of 2 configured services connected"
    assert f'title="{words}"' in link and f'aria-label="{words}"' in link
    assert link.count("title=") == 1  # no second, shorter tooltip
    assert words in admin_client.get("/dashboard").text  # also as text for screen readers


def test_ordinary_users_do_not_get_the_status_icon(user_client, db):
    record(db, itunes=BAD, deezer=BAD)
    assert "/admin/status" not in user_client.get("/dashboard").text


def test_the_status_page_shows_the_summary_and_refreshes_itself(admin_client, db):
    record(db, itunes=OK, deezer=OK)
    text = admin_client.get("/admin/status").text
    assert "All 2 configured services connected" in text
    assert "Checked automatically every 5 minutes." in text
    assert 'hx-get="/admin/status/grid"' in text and "every 30s" in text
    assert admin_client.get("/admin/status/grid").status_code == 200


def test_check_all_now_queues_one_job_and_polls_until_it_is_done(admin_client, db):
    r = post(
        admin_client,
        "/admin/status/test-all",
        headers={"HX-Request": "true"},
        page="/admin/status",
    )
    assert r.status_code == 200 and "every 1s" in r.text and "disabled" in r.text
    post(
        admin_client, "/admin/status/test-all", headers={"HX-Request": "true"}, page="/admin/status"
    )
    jobs = db.scalars(select(Job).where(Job.type == "check_integrations")).all()
    assert len(jobs) == 1 and jobs[0].dedupe_key == "scheduled:check_integrations"
    plain = post(admin_client, "/admin/status/test-all", page="/admin/status")
    assert plain.status_code == 303 and plain.headers["location"] == "/admin/status"


def test_the_automatic_checks_can_be_off(settings, app, admin_client):
    app.state.settings = settings.model_copy(update={"status_check_minutes": 0})
    assert "Automatic checks are off." in admin_client.get("/admin/status").text


def test_the_status_grid_is_for_admins_only(user_client):
    assert user_client.get("/admin/status/grid").status_code == 403
    assert post(user_client, "/admin/status/test-all", page="/dashboard").status_code == 403
