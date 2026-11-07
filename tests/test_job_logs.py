"""Job labels, the search in the logs, the routine filter and the retention of old jobs."""

import re
from datetime import timedelta

import pytest
from markupsafe import Markup
from sqlalchemy import func, select

from app.config import Settings
from app.jobs import engine
from app.jobs.engine import RetentionPolicy, purge_jobs
from app.jobs.handlers.housekeeping import retention_policy
from app.jobs.labels import job_label
from app.jobs.worker import JobWorker
from app.models import STATUS_FAILED, STATUS_SUCCEEDED, Job, JobLog, utcnow
from app.services.runtime_config import FIELDS, FieldError, parse_value, retention_job_types
from app.web.templating import mark
from tests.conftest import post
from tests.dbhelpers import make_artist, make_rg
from tests.test_jobs_worker import enqueue, logs, wait_for_status


def add(db, type_="test.steps", status=STATUS_SUCCEEDED, age_days=0.0, label=None, lines=()):
    job = engine.enqueue(db, type_, {}, label=label)
    job.status = status
    job.finished_at = utcnow() - timedelta(days=age_days)
    for line in lines:
        engine.add_log(db, job.id, "info", line)
    db.commit()
    return job


# ----------------------------------------------------------------------------- labels


def test_a_job_is_labelled_with_what_it_works_on(db):
    artist = make_artist(db, "Aretha Franklin")
    group = make_rg(db, artist, "Lady Soul", 1968)
    db.commit()
    assert job_label(db, "fetch_covers", {"artist_id": artist.id}) == "Aretha Franklin"
    assert job_label(db, "sync_artist_metadata", {"artist_id": artist.id}) == "Aretha Franklin"
    assert (
        job_label(db, "fetch_tracklist", {"release_group_id": group.id})
        == "Aretha Franklin – Lady Soul"
    )
    assert job_label(db, "search_artists", {"query": "aretha"}) == "“aretha”"
    assert job_label(db, "integration_check", {"name": "plex"}) == "plex"
    assert job_label(db, "scan_library", {"force": True}) is None
    assert job_label(db, "fetch_covers", {"artist_id": 9999}) is None
    assert job_label(db, "fetch_covers", {"artist_id": "not a number"}) is None


def test_a_request_job_is_labelled_with_the_album_and_marks_upgrades(db):
    from app.models import User
    from app.services import downloads as svc

    user = User(username="u", password_hash="x", role="user")
    db.add(user)
    group = make_rg(db, make_artist(db, "Aretha Franklin"), "Lady Soul", 1968)
    request, _ = svc.create_request(db, user.id, group.id)
    db.commit()
    assert (
        job_label(db, "slskd_search", {"request_id": request.id}) == "Aretha Franklin – Lady Soul"
    )
    request.upgrade_from = "mp3_320"
    assert job_label(db, "slskd_search", {"request_id": request.id}).endswith("(upgrade)")


def test_queued_jobs_get_their_label_and_an_explicit_one_wins(db):
    artist = make_artist(db, "Aretha Franklin")
    db.commit()
    assert engine.enqueue(db, "fetch_covers", {"artist_id": artist.id}).label == "Aretha Franklin"
    assert (
        engine.enqueue(db, "fetch_covers", {"artist_id": artist.id}, label="Mine").label == "Mine"
    )
    assert engine.enqueue(db, "scan_library", {}).label is None


def test_the_label_is_shown_in_the_list_and_the_detail(admin_client, db):
    job = add(db, label="Aretha Franklin")
    assert "data-job-label" in admin_client.get("/admin/jobs").text
    assert "Aretha Franklin" in admin_client.get("/admin/jobs").text
    assert admin_client.get(f"/admin/jobs/{job.id}").status_code == 200


# ----------------------------------------------------------------------------- search


def test_the_search_finds_log_lines_labels_and_types(db):
    a = add(db, lines=["Cover of Lady Soul saved"])
    b = add(db, label="Aretha Franklin")
    c = add(db, type_="fetch_covers")
    add(db, lines=["nothing relevant"])
    found = lambda text: {j.id for j in engine.list_jobs(db, search=text)[0]}  # noqa: E731
    assert found("lady soul") == {a.id}  # a log line, any case
    assert found("ARETHA") == {b.id}  # the label
    assert found("fetch_cov") == {c.id}  # the type
    assert found("zzzz") == set()
    assert engine.list_jobs(db, search="lady soul")[1] == 1


def test_short_searches_are_ignored(db):
    add(db, lines=["ab"])
    add(db)
    assert engine.list_jobs(db, search="ab")[1] == 2 and engine.list_jobs(db, search=" ")[1] == 2


def test_wildcards_in_the_search_are_plain_characters(db):
    add(db, lines=["100% done"])
    add(db, lines=["under_score"])
    add(db, lines=["something else"])
    assert engine.list_jobs(db, search="100%")[1] == 1
    assert engine.list_jobs(db, search="u_d")[1] == 0  # as a wildcard it would match "und"
    assert engine.list_jobs(db, search="er_sc")[1] == 1


def test_the_search_combines_with_the_other_filters(db):
    add(db, type_="fetch_covers", lines=["Cover of Lady Soul"])
    add(db, type_="fetch_tracklist", lines=["Tracks of Lady Soul"], status=STATUS_FAILED)
    assert engine.list_jobs(db, search="lady soul", type="fetch_covers")[1] == 1
    assert engine.list_jobs(db, search="lady soul", status=STATUS_FAILED)[1] == 1
    assert engine.list_jobs(db, search="lady soul")[1] == 2


def test_matching_lines_are_returned_per_job(db):
    j = add(
        db,
        lines=[
            "first match here",
            "other",
            "second match here",
            "third match here",
            "fourth match here",
        ],
    )
    found = engine.matching_lines(db, [j.id], "match here", per_job=3)
    assert found == {j.id: ["first match here", "second match here", "third match here"]}
    assert (
        engine.matching_lines(db, [j.id], "ab") == {}
        and engine.matching_lines(db, [], "match") == {}
    )


def test_the_page_shows_the_matches_highlighted(admin_client, db):
    add(db, lines=["Cover of <b>Lady Soul</b> saved"])
    text = admin_client.get("/admin/jobs?q=lady+soul").text
    assert "<mark>Lady Soul</mark>" in text and "<b>" not in text  # escaped, then highlighted
    assert "data-job-match" in text and "data-search-info" in text
    assert "1 job(s) with “lady soul”" in text


def test_the_live_rows_keep_the_search(admin_client, db):
    add(db, lines=["needle in a haystack"])
    add(db, lines=["hay only"])
    fragment = admin_client.get("/admin/jobs/rows?q=needle").text
    assert (
        "<html" not in fragment and fragment.count("<tr") == 1 and "<mark>needle</mark>" in fragment
    )
    assert "/admin/jobs/rows?q=needle" in admin_client.get("/admin/jobs?q=needle").text


def test_a_too_short_search_says_so(admin_client, db):
    add(db)
    text = admin_client.get("/admin/jobs?q=ab").text
    assert "data-search-short" in text and "at least 3 characters" in text


def test_the_search_box_is_on_the_page(admin_client):
    text = admin_client.get("/admin/jobs").text
    assert 'name="q"' in text and "Search the logs" in text


def test_the_highlighter_escapes_everything():
    out = mark("a <script>x</script> b", "script")
    assert isinstance(out, Markup) and "<script>" not in out and "<mark>script</mark>" in out
    assert str(mark("plain & text", "zz")) == "plain &amp; text"
    assert str(mark("keep", "")) == "keep"
    assert str(mark("Abc abc", "abc")) == "<mark>Abc</mark> <mark>abc</mark>"


# ---------------------------------------------------------------------- routine jobs


def test_routine_jobs_are_hidden_unless_asked_for(db, admin_client):
    add(db, type_="check_integrations")
    add(db, type_="housekeeping")
    keep = add(db, type_="fetch_covers")
    assert [j.id for j in engine.list_jobs(db, routine=False)[0]] == [keep.id]
    assert engine.list_jobs(db, routine=True)[1] == 3
    assert (
        engine.list_jobs(db, routine=False, type="check_integrations")[1] == 1
    )  # asked for by type
    assert 'name="routine"' in admin_client.get("/admin/jobs").text
    shown = admin_client.get("/admin/jobs?routine=1").text
    assert shown.count("check_integrations") >= 1 and shown.count("housekeeping") >= 1
    hidden = admin_client.get("/admin/jobs").text
    assert "check_integrations</div>" not in hidden


# ---------------------------------------------------------------------------- retention


POLICY = RetentionPolicy(routine_days=1, success_days=14, failed_days=60)


def kept(db):
    db.expire_all()
    return {j.id for j in db.scalars(select(Job))}


def test_each_kind_of_job_is_kept_as_long_as_the_policy_says(db):
    routine_old = add(db, type_="check_integrations", age_days=2)
    routine_new = add(db, type_="check_integrations", age_days=0.5)
    failed_routine_old = add(
        db, type_="housekeeping", status=STATUS_FAILED, age_days=2
    )  # routine wins
    ok_old, ok_new = add(db, age_days=15), add(db, age_days=13)
    bad_old, bad_new = (
        add(db, status=STATUS_FAILED, age_days=61),
        add(db, status=STATUS_FAILED, age_days=59),
    )
    cancelled_old = add(db, status="cancelled", age_days=15)
    removed = purge_jobs(db, POLICY)
    assert removed == 5
    assert kept(db) == {routine_new.id, ok_new.id, bad_new.id}
    assert all(
        j not in kept(db)
        for j in (routine_old.id, failed_routine_old.id, ok_old.id, bad_old.id, cancelled_old.id)
    )


def test_the_logs_go_with_their_jobs(db):
    old = add(db, age_days=40, lines=["a", "b"])
    new = add(db, age_days=1, lines=["c"])
    purge_jobs(db, POLICY)
    assert db.scalar(select(func.count(JobLog.id)).where(JobLog.job_id == old.id)) == 0
    assert (
        db.scalar(select(func.count(JobLog.id)).where(JobLog.job_id == new.id)) == 2
    )  # c + Queued


def test_jobs_still_running_or_waiting_are_never_purged(db):
    running = add(db, status="running", age_days=90)
    queued = add(db, status="queued", age_days=90)
    unfinished = add(db, age_days=90)
    unfinished.finished_at = None
    db.commit()
    assert purge_jobs(db, POLICY) == 0
    assert kept(db) == {running.id, queued.id, unfinished.id}


def test_a_per_type_value_wins_over_the_categories(db):
    scans = add(db, type_="scan_library", age_days=40)
    scans_failed = add(db, type_="scan_library", status=STATUS_FAILED, age_days=40)
    covers = add(db, type_="fetch_covers", age_days=8)
    other = add(db, type_="fetch_tracklist", age_days=8)
    policy = RetentionPolicy(1, 14, 60, {"scan_library": 90, "fetch_covers": 7})
    purge_jobs(db, policy)
    assert scans.id in kept(db) and scans_failed.id in kept(db)  # kept 90 days, even the failed one
    assert covers.id not in kept(db)  # 8 days old, but this type is kept 7
    assert other.id in kept(db)  # the general rule: 14 days
    longer = add(db, type_="scan_library", age_days=100)
    purge_jobs(db, policy)
    assert longer.id not in kept(db)


def test_a_per_type_value_can_also_shorten_a_routine_type(db):
    j = add(db, type_="check_integrations", age_days=3)
    purge_jobs(db, RetentionPolicy(1, 14, 60, {"check_integrations": 5}))
    assert j.id in kept(db)  # the override (5 days) replaces the routine value (1 day)


def test_at_least_one_day_whatever_the_value(db):
    fresh = add(db, age_days=0.2)
    purge_jobs(db, RetentionPolicy(0, 0, 0))
    assert fresh.id in kept(db)


def test_the_defaults():
    s = Settings(_env_file=None)
    assert (
        s.job_retention_routine_days,
        s.job_retention_success_days,
        s.job_retention_failed_days,
    ) == (1, 14, 60)
    assert s.retention_overrides == {}
    policy = retention_policy(s)
    assert (policy.routine_days, policy.success_days, policy.failed_days) == (1, 14, 60)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("scan_library:90,fetch_covers:7", {"scan_library": 90, "fetch_covers": 7}),
        (" scan_library : 5 , bad, x:0, y:-1, z:abc ", {"scan_library": 5}),
        ("a:1,b:2,junk,c:0", {"a": 1, "b": 2}),
        ("", {}),
    ],
)
def test_overrides_are_parsed_leniently(raw, expected):
    assert Settings(_env_file=None, job_retention_overrides=raw).retention_overrides == expected


def test_the_settings_validate_what_is_typed():
    field = FIELDS["job_retention_overrides"]
    assert parse_value(field, "Scan_Library:90,fetch_covers:7") == "scan_library:90,fetch_covers:7"
    assert parse_value(field, "a:1\nb:2") == "a:1,b:2"
    assert parse_value(field, "") == ""
    for bad in (
        "scan_library",
        "scan_library:0",
        "scan_library:abc",
        "x:99999",
        "UPPER CASE:3",
        "a:1;b:2",
    ):
        with pytest.raises(FieldError):
            parse_value(field, bad)
    days = FIELDS["job_retention_failed_days"]
    assert parse_value(days, "30") == 30
    for bad in ("0", "-5", "abc", "99999"):
        with pytest.raises(FieldError):
            parse_value(days, bad)


def test_the_retention_is_configured_in_the_job_settings(app, admin_client, db):
    text = admin_client.get("/settings/jobs").text
    for name in (
        "job_retention_routine_days",
        "job_retention_success_days",
        "job_retention_failed_days",
    ):
        assert f'name="{name}"' in text
    assert 'name="job_retention_overrides"' not in text  # no free text: a number per job type
    for job_type in retention_job_types():
        assert f'name="retention_{job_type}"' in text, job_type
    assert 'name="retention_test.steps"' not in text
    assert "How long jobs and their logs are kept" in text
    r = post(
        admin_client,
        "/settings/jobs",
        {
            "queue_default": "2",
            "job_retention_routine_days": "2",
            "job_retention_success_days": "7",
            "job_retention_failed_days": "30",
            "retention_scan_library": "90",
            "retention_fetch_covers": "",
        },
        page="/settings/jobs",
    )
    assert r.status_code == 303
    s = app.state.settings
    assert (
        s.job_retention_routine_days,
        s.job_retention_success_days,
        s.job_retention_failed_days,
    ) == (2, 7, 30)
    assert s.retention_overrides == {"scan_library": 90}


def test_the_job_settings_refuse_less_than_a_day(admin_client, app):
    for bad in ("0", "-1", "abc"):
        r = post(
            admin_client,
            "/settings/jobs",
            {"queue_default": "2", "job_retention_failed_days": bad},
            page="/settings/jobs",
        )
        assert r.status_code == 400, bad
    assert app.state.settings.job_retention_failed_days == 60
    for typed in (
        {"retention_scan_library": "0"},
        {"retention_scan_library": "abc"},
        {"retention_scan_library": "99999"},
        {"retention_no_such_job": "3"},
    ):
        bad = post(admin_client, "/settings/jobs", typed, page="/settings/jobs")
        assert bad.status_code == 400 and 'data-error="job_retention_overrides"' in bad.text, typed
    assert app.state.settings.retention_overrides == {}


async def test_the_housekeeping_job_applies_the_policy_and_says_what_it_did(
    settings, session_factory
):
    s = settings.model_copy(
        update={"job_poll_interval": 0.02, "job_retention_overrides": "fetch_covers:30"}
    )
    with session_factory() as db:
        old_ok = add(db, age_days=20)
        old_covers = add(db, type_="fetch_covers", age_days=20)
        old_routine = add(db, type_="check_integrations", age_days=3)
        fresh = add(db, age_days=1)
        ids = (old_ok.id, old_covers.id, old_routine.id, fresh.id)
    worker = JobWorker(s, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "housekeeping")
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    with session_factory() as db:
        left = {j.id for j in db.scalars(select(Job))}
    assert ids[1] in left and ids[3] in left and ids[0] not in left and ids[2] not in left
    messages = logs(session_factory, job_id)
    assert any(m.startswith("Removed 2 finished job(s)") and "30" in m for m in messages)


def test_the_database_can_be_compacted(session_factory):
    engine.compact_database(session_factory)  # does not raise; the file keeps working
    with session_factory() as db:
        assert db.scalar(select(func.count(Job.id))) == 0


def test_a_big_cleanup_gives_the_space_back(settings, session_factory, monkeypatch):
    import asyncio

    from app.jobs.handlers import housekeeping as hk

    called = []
    monkeypatch.setattr(hk, "COMPACT_AFTER", 2)
    monkeypatch.setattr(hk.engine, "compact_database", lambda factory: called.append(1))

    async def go():
        s = settings.model_copy(update={"job_poll_interval": 0.02})
        with session_factory() as db:
            for _ in range(3):
                add(db, age_days=40)
        worker = JobWorker(s, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "housekeeping")
        worker.notify()
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
        return logs(session_factory, job_id)

    messages = asyncio.run(go())
    assert called == [1] and "The database file was compacted" in messages


def test_the_list_shows_what_is_saved_and_what_applies_otherwise(app, admin_client):
    post(admin_client, "/settings/jobs", {"retention_scan_library": "90"}, page="/settings/jobs")
    text = admin_client.get("/settings/jobs").text
    box = re.search(r'<input id="retention-scan_library"[^>]*>', text).group(0)
    assert 'value="90"' in box and 'type="number"' in box and 'min="1"' in box
    empty = re.search(r'<input id="retention-fetch_covers"[^>]*>', text).group(0)
    assert 'value=""' in empty
    assert "empty = 14 days (succeeded / failed)" not in text  # the numbers are the real ones
    assert "empty = 14 / 60 days (succeeded / failed)" in text
    assert "empty = 1 day(s)" in text  # routine jobs have a single value
    # a refused form keeps what was typed
    bad = post(
        admin_client,
        "/settings/jobs",
        {"retention_scan_library": "0", "retention_fetch_covers": "7"},
        page="/settings/jobs",
    )
    assert 'id="retention-fetch_covers"' in bad.text and 'value="7"' in bad.text


def test_every_job_type_has_a_name_in_both_languages():
    import json
    from pathlib import Path

    locales = Path("app/i18n/locales")
    for lang in ("en", "it"):
        names = json.loads((locales / f"{lang}.json").read_text())
        missing = [t for t in retention_job_types() if f"settings.jobtype.{t}" not in names]
        assert not missing, (lang, missing)


def test_numbers_sit_at_the_left(admin_client):
    text = admin_client.get("/settings/jobs").text
    boxes = re.findall(r'<input id="(?:queue|retention|f)-[^"]*"[^>]*type="number"[^>]*>', text)
    assert len(boxes) > 15
    assert all("text-right" not in b and "text-left" in b for b in boxes)
