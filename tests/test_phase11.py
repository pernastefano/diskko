"""Phase 11: login throttling, database backups, JSON logs, health check, path validation."""

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.throttle import THROTTLE, LoginThrottle
from app.logging_config import JsonFormatter, RedactFilter
from app.services import backup as bk
from tests.conftest import PASSWORD, add_user, login, post

# ------------------------------------------------------------------ login throttling


def test_failed_logins_pause_the_account_then_it_is_allowed_again(admin_client, new_client):
    browser = new_client()
    for _ in range(5):
        assert login(browser, "admin", "wrong-password").status_code == 400
    r = login(browser, "admin", PASSWORD)  # even the right password waits
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert "Too many failed attempts" in r.text
    THROTTLE.clear()  # the window passes
    assert login(browser, "admin", PASSWORD).status_code == 303


def test_one_pause_does_not_touch_another_account(admin_client, new_client):
    add_user(admin_client, "bob")
    attacker = new_client()
    for _ in range(5):
        login(attacker, "bob", "nope-nope")
    assert login(attacker, "bob", PASSWORD).status_code == 429
    other = new_client()
    assert login(other, "admin", PASSWORD).status_code == 303


def test_a_success_clears_the_failures_of_the_account(admin_client, new_client):
    browser = new_client()
    for _ in range(4):
        login(browser, "admin", "wrong-password")
    assert login(browser, "admin", PASSWORD).status_code == 303
    for _ in range(4):  # the count started again from zero
        assert login(new_client(), "admin", "wrong-password").status_code == 400


def test_the_throttle_counts_per_address_and_the_window_slides():
    now = [1000.0]
    throttle = LoginThrottle(clock=lambda: now[0])

    class S:
        login_window_seconds = 60
        login_max_attempts = 3
        login_max_attempts_ip = 4

    for name in ("a", "b", "c", "d"):  # one address, four different accounts
        throttle.failed("1.2.3.4", name)
    assert throttle.retry_after(S, "1.2.3.4", "zzz") > 0
    assert throttle.retry_after(S, "5.6.7.8", "zzz") == 0
    now[0] += 61
    assert throttle.retry_after(S, "1.2.3.4", "zzz") == 0


def test_forwarded_addresses_count_only_when_the_proxy_is_trusted(settings, app):
    from starlette.requests import Request

    from app.auth.throttle import client_ip

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"9.9.9.9, 10.0.0.1")],
        "client": ("172.18.0.1", 1234),
    }
    request = Request(scope)
    assert client_ip(request, settings) == "172.18.0.1"
    trusted = settings.model_copy(update={"trust_proxy": True})
    assert client_ip(request, trusted) == "9.9.9.9"


def test_a_throttled_attempt_is_in_the_audit_log(admin_client, new_client):
    browser = new_client()
    for _ in range(6):
        login(browser, "admin", "wrong-password")
    html = admin_client.get("/admin/audit?action=login.throttled").text
    assert "Sign-in paused" in html


# ----------------------------------------------------------------------------- backups


def make_db(settings, rows=3):
    path = settings.data_dir / "app.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


def test_a_backup_is_a_readable_copy_of_the_database(settings):
    make_db(settings)
    made = bk.create_backup(settings)
    assert bk.NAME.match(made.name) and made.size > 0
    con = sqlite3.connect(settings.backup_dir / made.name)
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    con.close()
    assert not list(settings.backup_dir.glob("*.partial"))


def test_old_backups_are_pruned_and_two_in_one_second_do_not_clash(settings):
    make_db(settings)
    settings = settings.model_copy(update={"backup_keep": 3})
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        bk.create_backup(settings, now=start + timedelta(seconds=i))
    assert len(bk.list_backups(settings)) == 3
    same = start + timedelta(days=1)
    first, second = bk.create_backup(settings, now=same), bk.create_backup(settings, now=same)
    assert first.name != second.name
    assert bk.list_backups(settings)[0].name == second.name  # newest first


def test_a_missing_database_is_a_clear_error(settings):
    with pytest.raises(bk.BackupError):
        bk.create_backup(settings)


@pytest.mark.parametrize(
    "name", ["../app.db", "..%2fapp.db", "diskko-1.db", "app.db", "diskko-20260101-000000.db/x"]
)
def test_only_our_own_backup_names_resolve(settings, name):
    make_db(settings)
    bk.create_backup(settings)
    assert bk.backup_path(settings, name) is None
    assert bk.backup_path(settings, bk.list_backups(settings)[0].name) is not None


def test_the_backup_pages_are_for_admins(admin_client, user_client, app):
    for client in (user_client,):
        assert client.get("/admin/backups").status_code == 403
        assert client.get("/admin/backups/diskko-20260101-000000.db").status_code == 403
    page = admin_client.get("/admin/backups")
    assert page.status_code == 200 and "No backups yet." in page.text
    assert "data-backups-link" in admin_client.get("/settings/library").text


async def test_the_backup_job_makes_a_downloadable_file(settings, session_factory):
    from app.jobs.worker import JobWorker
    from tests.test_jobs_worker import enqueue, logs, wait_for_status

    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "backup_db")
    worker.notify()
    await wait_for_status(session_factory, job_id, "succeeded", timeout=15)
    await worker.stop()
    (made,) = bk.list_backups(settings)
    assert any(made.name in m for m in logs(session_factory, job_id))
    con = sqlite3.connect(settings.backup_dir / made.name)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


def test_backup_now_queues_the_job_and_the_file_downloads(admin_client, settings, app):
    r = post(admin_client, "/admin/backups", page="/admin/backups")
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/jobs/")
    made = bk.create_backup(app.state.settings)
    got = admin_client.get(f"/admin/backups/{made.name}")
    assert got.status_code == 200 and got.content.startswith(b"SQLite format 3")
    assert admin_client.get("/admin/backups/..%2fapp.db").status_code == 404
    assert made.name in admin_client.get("/admin/backups").text


def test_the_nightly_backup_is_scheduled_unless_switched_off(settings):
    from app.jobs.scheduler import schedules_for

    assert "backup_db" in {s.job_type for s in schedules_for(settings)}
    off = settings.model_copy(update={"backup_cron": ""})
    assert "backup_db" not in {s.job_type for s in schedules_for(off)}


# ------------------------------------------------------------------------------- logs


def record(msg, *args, level=logging.INFO, exc_info=None):
    return logging.LogRecord("diskko.test", level, __file__, 1, msg, args, exc_info)


def test_secrets_never_reach_a_log_line():
    f = RedactFilter(["plex-token-123456", "short"])
    rec = record("GET http://plex:32400/library?X-Plex-Token=plex-token-123456&x=1 %s", "ok")
    f.filter(rec)
    assert "plex-token-123456" not in rec.msg and "***" in rec.msg and "x=1" in rec.msg
    rec = record('payload {"api_key": "abcdef123456", "name": "short"}')
    f.filter(rec)
    assert "abcdef123456" not in rec.msg and "short" in rec.msg  # too short to be a secret
    try:
        raise RuntimeError("password=hunter2hunter2")
    except RuntimeError:
        import sys

        rec = record("failed", level=logging.ERROR, exc_info=sys.exc_info())
    f.filter(rec)
    assert "hunter2" not in JsonFormatter().format(rec)


def test_json_logs_are_one_object_per_line():
    line = JsonFormatter().format(record("hello %s", "world"))
    data = json.loads(line)
    assert data["message"] == "hello world" and data["level"] == "INFO"
    assert data["logger"] == "diskko.test" and "time" in data and "\n" not in line


def test_the_configured_secrets_are_scrubbed_by_the_app_handler(settings, capsys):
    from app.logging_config import configure_logging

    cfg = settings.model_copy(update={"plex_token": "tok-abcdef-123", "log_format": "json"})
    configure_logging(cfg)
    try:
        logging.getLogger("diskko.test").info("calling plex with tok-abcdef-123")
    finally:
        configure_logging(settings)  # back to the text format
    out = capsys.readouterr().out
    assert "tok-abcdef-123" not in out and "calling plex with ***" in out


# ----------------------------------------------------------------------------- health


def test_health_is_ok_and_reports_the_version(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["version"]


def test_health_is_503_when_the_database_cannot_be_read(app, client):
    good = app.state.session_factory

    class Locked:
        """A session whose queries fail, like a database that cannot be read."""

        def __init__(self):
            self._real = good()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._real.close()
            return False

        def execute(self, *a, **k):
            raise RuntimeError("database is locked")

        def __getattr__(self, name):
            return getattr(self._real, name)

    app.state.session_factory = Locked
    try:
        r = client.get("/health")
    finally:
        app.state.session_factory = good
    assert r.status_code == 503 and r.json()["status"] == "error"


# ----------------------------------------------------------------- path validation


def test_a_peer_cannot_steer_a_download_outside_its_folder():
    from app.services.downloads import destination, safe_subdir

    for hostile in ("../../etc", "..\\..\\x", "/abs/path", "a/../../b", "C:\\Windows"):
        sub = safe_subdir(hostile)
        assert "/" not in sub and "\\" not in sub and sub not in ("", ".", "..")
        assert ".." not in destination(None, 7, hostile).split("/")


def test_the_import_only_reads_below_the_downloads_area(settings, tmp_path):
    from app.services.importer.library_import import ImportRejected, request_dir

    downloads = tmp_path / "dl"
    (downloads / "diskko" / "5").mkdir(parents=True)
    (downloads / "elsewhere").mkdir()
    cfg = settings.model_copy(update={"downloads_dir": downloads})
    assert request_dir(cfg, None, 5) == (downloads / "diskko" / "5").resolve()
    for dest in ("../elsewhere", "diskko/../elsewhere", "/etc", "diskko", "diskko/../.."):
        with pytest.raises(ImportRejected):
            request_dir(cfg, dest, 5)
    (downloads / "diskko" / "link").symlink_to(downloads / "elsewhere")
    with pytest.raises(ImportRejected):
        request_dir(cfg, "diskko/link", 5)


def test_a_library_path_cannot_climb_out_or_follow_a_link_out(tmp_path):
    from app.services.albums import resolve_library_file

    music, secret = tmp_path / "music", tmp_path / "secret.flac"
    music.mkdir()
    (music / "ok.flac").write_bytes(b"x")
    secret.write_bytes(b"s")
    (music / "link.flac").symlink_to(secret)
    assert resolve_library_file(music, "ok.flac") is not None
    for bad in ("../secret.flac", "link.flac", "/etc/passwd", "ok.flac/../../secret.flac", ""):
        assert resolve_library_file(music, bad) is None


def test_the_covers_route_only_serves_its_own_files(admin_client):
    for name in ("..%2f..%2fapp.db", "a%2fb.jpg", "x.php", "%2e%2e%2fapp.db"):
        assert admin_client.get(f"/covers/{name}").status_code == 404
