import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.jobs.registry import job_handler
from app.jobs.scheduler import JobScheduler
from app.jobs.worker import JobWorker
from app.main import create_app
from app.models import STATUS_SUCCEEDED, Setting
from app.services import runtime_config as rc
from app.services.runtime_config import (
    FIELDS,
    FieldError,
    apply_runtime_config,
    effective_settings,
    fields_of,
    parse_concurrency,
    parse_value,
    read_overrides,
    save_values,
)
from tests.test_jobs_worker import enqueue, wait_for_status

# ----------------------------------------------------------------------- validation


def ok(key, raw):
    return parse_value(FIELDS[key], raw)


def bad(key, raw):
    with pytest.raises(FieldError) as exc:
        parse_value(FIELDS[key], raw)
    return exc.value.code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://plex.local:32400", "http://plex.local:32400"),
        (" https://plex.example.com/ ", "https://plex.example.com"),
        ("http://192.168.1.10:5030/", "http://192.168.1.10:5030"),
        ("https://host/base/path/", "https://host/base/path"),
        ("", ""),  # an integration can be left unconfigured
    ],
)
def test_urls_are_accepted_and_normalized(raw, expected):
    assert ok("plex_url", raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("plex.local:32400", "url_invalid"),
        ("ftp://host", "url_invalid"),
        ("file:///etc/passwd", "url_invalid"),
        ("javascript:alert(1)", "url_invalid"),
        ("http://", "url_invalid"),
        ("http://host:notaport", "url_invalid"),
        ("http://ho st", "url_invalid"),
        ("http://user:pass@host", "url_credentials"),
        ("http://user@host", "url_credentials"),
        ("http://host\nX-Evil: 1", "invalid_characters"),
        ("http://" + "a" * 300, "too_long"),
    ],
)
def test_bad_urls_are_rejected(raw, code):
    assert bad("slskd_url", raw) == code


def test_music_dir_must_be_an_absolute_path():
    assert ok("music_dir", "/music") == "/music"
    assert ok("music_dir", " /srv/media//music/../library/ ") == "/srv/media/library"
    assert bad("music_dir", "music") == "path_not_absolute"
    assert bad("music_dir", "./music") == "path_not_absolute"
    assert bad("music_dir", "") == "required"
    assert bad("music_dir", "/mu\x00sic") == "invalid_characters"


@pytest.mark.parametrize("value", ["0 2 * * *", "*/15 * * * *", "30 4 * * 1", ""])
def test_cron_accepts_crontab_and_empty(value):
    assert ok("library_scan_cron", value) == value


@pytest.mark.parametrize("value", ["not a cron", "61 * * * *", "* * *", "0 25 * * *"])
def test_cron_rejects_garbage(value):
    assert bad("library_scan_cron", value) == "cron_invalid"


def test_numbers_are_range_checked():
    assert ok("mb_cache_ttl_days", "7") == 7
    assert bad("mb_cache_ttl_days", "0") == "number_range"
    assert bad("mb_cache_ttl_days", "366") == "number_range"
    assert bad("mb_cache_ttl_days", "seven") == "number_invalid"
    assert bad("mb_cache_ttl_days", "") == "number_invalid"


def test_choices():
    assert ok("library_source", "plex") == "plex"
    assert bad("library_source", "spotify") == "choice_invalid"


def test_booleans():
    assert [ok("coverart_enabled", v) for v in ("1", "on", "true", "0", "", "off")] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]


def test_plex_section_is_required():
    assert bad("plex_music_section", "  ") == "required"
    assert ok("plex_music_section", " Music ") == "Music"


@pytest.mark.parametrize(
    "raw", ["", "/data/music=/music", "/a=/b;C:\\Music=/music", "/a=/b\n/c=/d"]
)
def test_path_maps(raw):
    assert ok("library_path_map", raw).count("=") == raw.count("=")


@pytest.mark.parametrize("raw", ["/a", "=/b", "/a=", "/a=/b=/c", "/a=/b;;/c"])
def test_bad_path_maps(raw):
    assert bad("library_path_map", raw) == "path_map_invalid"


def test_secrets():
    assert ok("plex_token", " abc123 ") == "abc123"
    assert bad("plex_token", "has space inside") == "secret_invalid"
    assert bad("plex_token", "x" * 600) == "secret_invalid"


def test_concurrency():
    assert parse_concurrency({"default": "3", "library": "1", "coverart": "4", "slskd": "2"}) == (
        "default:3,library:1,musicbrainz:1,coverart:4,slskd:2"
    )
    assert parse_concurrency({"musicbrainz": "5", "default": "2"}) == "default:2,musicbrainz:1"
    for value, code in (
        ("0", "number_range"),
        ("9", "number_range"),
        ("x", "number_invalid"),
        ("-1", "number_range"),
    ):
        with pytest.raises(FieldError) as exc:
            parse_concurrency({"default": value})
        assert exc.value.code == code


def test_every_field_has_a_section_and_exists_on_settings():
    names = set(Settings.model_fields)
    assert set(FIELDS) <= names
    assert {f.section for f in FIELDS.values()} == {"library", "integrations", "jobs", "quality"}
    for secret in (f for f in FIELDS.values() if f.kind == "secret"):
        assert secret.key in ("plex_token", "slskd_api_key", "lastfm_api_key")


def test_only_runtime_safe_settings_are_editable():
    """Bootstrap values stay in the environment: they cannot be changed from the UI."""
    for key in (
        "secret_key",
        "base_url",
        "data_dir",
        "tz",
        "jobs_enabled",
        "env",
        "session_ttl_hours",
        "downloads_dir",
        "min_password_length",
    ):
        assert key not in FIELDS


# ---------------------------------------------------------------- storage layer


def form(**kw):
    return {k: str(v) for k, v in kw.items()}


def test_saved_values_override_the_environment(jdb, settings):
    errors = save_values(
        jdb,
        fields_of("library"),
        form(music_dir="/srv/music", library_source="plex", library_scan_cron="15 3 * * *"),
    )
    jdb.commit()
    assert errors == {}
    overrides = read_overrides(jdb)
    assert overrides == {
        "music_dir": "/srv/music",
        "library_source": "plex",
        "library_scan_cron": "15 3 * * *",
    }
    effective = effective_settings(settings, overrides)
    assert effective.music_dir == Path("/srv/music") and isinstance(effective.music_dir, Path)
    assert effective.library_source == "plex" and effective.library_scan_cron == "15 3 * * *"
    assert settings.library_source == "filesystem"  # the environment settings are untouched


def test_no_overrides_means_the_environment(jdb, settings):
    assert effective_settings(settings, read_overrides(jdb)) == settings


def test_an_invalid_field_saves_nothing(jdb):
    errors = save_values(
        jdb,
        fields_of("library"),
        form(music_dir="relative/path", library_source="plex", library_scan_cron="0 1 * * *"),
    )
    jdb.commit()
    assert errors == {"music_dir": "path_not_absolute"}
    assert read_overrides(jdb) == {}  # not even the valid fields


def test_every_error_is_reported_at_once(jdb):
    errors = save_values(
        jdb, fields_of("library"), form(music_dir="x", library_source="y", library_scan_cron="z")
    )
    assert set(errors) == {"music_dir", "library_source", "library_scan_cron"}


def test_a_blank_secret_keeps_the_stored_one(jdb):
    plex = fields_of("integrations", "plex")
    save_values(
        jdb,
        plex,
        form(
            plex_url="http://plex:32400",
            plex_token="s3cret-token",
            plex_music_section="Music",
            library_path_map="",
        ),
    )
    jdb.commit()
    save_values(
        jdb,
        plex,
        form(
            plex_url="http://plex2:32400",
            plex_token="",
            plex_music_section="Music",
            library_path_map="",
        ),
    )
    jdb.commit()
    overrides = read_overrides(jdb)
    assert (
        overrides["plex_token"] == "s3cret-token" and overrides["plex_url"] == "http://plex2:32400"
    )
    save_values(
        jdb,
        plex,
        form(
            plex_url="http://plex2:32400",
            plex_token="new-token",
            plex_music_section="Music",
            library_path_map="",
        ),
    )
    jdb.commit()
    assert read_overrides(jdb)["plex_token"] == "new-token"


def test_an_unconfigured_integration_can_be_saved_empty(jdb):
    errors = save_values(jdb, fields_of("integrations", "slskd"), form(slskd_url=""))
    jdb.commit()
    assert errors == {} and read_overrides(jdb)["slskd_url"] == ""


def test_reset_goes_back_to_the_environment_default(jdb, settings):
    save_values(
        jdb,
        fields_of("library"),
        form(music_dir="/srv/music", library_source="plex", library_scan_cron=""),
    )
    jdb.commit()
    save_values(
        jdb,
        fields_of("library"),
        form(library_source="plex", library_scan_cron=""),
        reset={"music_dir"},
    )
    jdb.commit()
    overrides = read_overrides(jdb)
    assert "music_dir" not in overrides and overrides["library_source"] == "plex"
    assert effective_settings(settings, overrides).music_dir == settings.music_dir


def test_booleans_are_saved_when_unchecked(jdb):
    save_values(jdb, fields_of("integrations", "coverart"), {})  # an unchecked box sends nothing
    jdb.commit()
    assert read_overrides(jdb)["coverart_enabled"] is False
    save_values(jdb, fields_of("integrations", "coverart"), {"coverart_enabled": "on"})
    jdb.commit()
    assert read_overrides(jdb)["coverart_enabled"] is True


def test_concurrency_is_stored_as_the_environment_format(jdb, settings):
    save_values(
        jdb,
        fields_of("jobs"),
        form(
            queue_default=4, queue_library=1, queue_musicbrainz=3, queue_coverart=1, queue_slskd=2
        ),
    )
    jdb.commit()
    effective = effective_settings(settings, read_overrides(jdb))
    assert effective.queue_concurrency == {
        "default": 4,
        "library": 1,
        "musicbrainz": 1,
        "coverart": 1,
        "slskd": 2,
    }


def test_unknown_and_foreign_rows_are_ignored(jdb):
    from app.services.app_settings import set_setting

    set_setting(jdb, "config.not_a_setting", "x")
    set_setting(jdb, "config.secret_key", "hijack")  # not an editable field
    set_setting(jdb, "missing_types", ["album"])
    jdb.commit()
    assert read_overrides(jdb) == {}


def test_overrides_are_stored_under_the_config_prefix(jdb):
    save_values(
        jdb,
        fields_of("library"),
        form(music_dir="/m", library_source="filesystem", library_scan_cron=""),
    )
    jdb.commit()
    keys = {s.key for s in jdb.scalars(select(Setting))}
    assert {"config.music_dir", "config.library_source", "config.library_scan_cron"} <= keys


# ----------------------------------------------------------- applying at runtime


def test_the_effective_settings_are_used_after_startup(settings):
    app = create_app(settings)
    with TestClient(app):
        with app.state.session_factory() as db:
            save_values(
                db,
                fields_of("integrations", "musicbrainz"),
                form(mb_user_agent="Diskko/1 ( me )", mb_cache_ttl_days=3, mb_refresh_days=10),
            )
            db.commit()
        assert app.state.settings.mb_user_agent == ""  # not applied until asked
        apply_runtime_config(app)
        assert app.state.settings.mb_user_agent == "Diskko/1 ( me )"
        assert app.state.settings.mb_cache_ttl_days == 3
        assert app.state.base_settings.mb_user_agent == ""  # the environment copy is never mutated


def test_saved_values_survive_a_restart(settings):
    first = create_app(settings)
    with TestClient(first), first.state.session_factory() as db:
        save_values(
            db,
            fields_of("library"),
            form(music_dir="/srv/elsewhere", library_source="plex", library_scan_cron=""),
        )
        db.commit()
    second = create_app(settings)
    with TestClient(second):
        assert second.state.settings.music_dir == Path("/srv/elsewhere")
        assert second.state.settings.library_source == "plex"
        assert second.state.settings.library_scan_cron == ""


def test_jobs_run_with_the_new_settings(settings):
    live = settings.model_copy(update={"jobs_enabled": True, "job_poll_interval": 0.05})
    app = create_app(live)
    with TestClient(app):
        with app.state.session_factory() as db:
            save_values(db, fields_of("integrations", "slskd"), form(slskd_url="http://slskd:5030"))
            db.commit()
        apply_runtime_config(app)
        assert app.state.worker.settings.slskd_url == "http://slskd:5030"
        assert app.state.worker.settings is app.state.settings


def test_a_changed_scan_time_reschedules_without_a_restart(settings):
    live = settings.model_copy(update={"jobs_enabled": True, "job_poll_interval": 0.05})
    app = create_app(live)
    with TestClient(app):
        scheduler = app.state.scheduler
        assert "scheduled:scan_library" in scheduler.job_ids()
        with app.state.session_factory() as db:
            save_values(
                db,
                fields_of("library"),
                form(music_dir="/m", library_source="filesystem", library_scan_cron="45 5 * * *"),
            )
            db.commit()
        apply_runtime_config(app)
        job = next(j for j in scheduler._scheduler.get_jobs() if j.id == "scheduled:scan_library")
        assert "hour='5'" in str(job.trigger) and "minute='45'" in str(job.trigger)
        with app.state.session_factory() as db:
            save_values(
                db,
                fields_of("library"),
                form(music_dir="/m", library_source="filesystem", library_scan_cron=""),
            )
            db.commit()
        apply_runtime_config(app)
        assert "scheduled:scan_library" not in scheduler.job_ids()  # an empty schedule turns it off
        assert "scheduled:housekeeping" in scheduler.job_ids()


async def test_scheduler_with_explicit_schedules_is_left_alone(settings, session_factory):
    from app.jobs.scheduler import Schedule

    scheduler = JobScheduler(
        settings, session_factory, schedules=[Schedule("test.steps", "0 1 * * *")]
    )
    scheduler.start()
    try:
        scheduler.reschedule(settings)
        assert scheduler.job_ids() == ["scheduled:test.steps"]
    finally:
        scheduler.shutdown()


# ------------------------------------------------------------ worker concurrency

_gauge = {"now": 0, "max": 0}


@job_handler("test.cc", queue="default", max_attempts=1)
async def _cc(ctx):
    _gauge["now"] += 1
    _gauge["max"] = max(_gauge["max"], _gauge["now"])
    await asyncio.sleep(0.15)
    _gauge["now"] -= 1


@pytest.fixture
def cc_settings(settings):
    return settings.model_copy(
        update={"job_poll_interval": 0.02, "job_queue_concurrency": "default:1"}
    )


async def run_batch(worker, factory, n=8):
    _gauge.update(now=0, max=0)
    ids = [enqueue(factory, "test.cc") for _ in range(n)]
    worker.notify()
    for job_id in ids:
        await wait_for_status(factory, job_id, STATUS_SUCCEEDED, timeout=15)
    return _gauge["max"]


async def test_more_workers_start_immediately(cc_settings, session_factory):
    worker = JobWorker(cc_settings, session_factory)
    await worker.start()
    assert await run_batch(worker, session_factory) == 1
    worker.update_settings(cc_settings.model_copy(update={"job_queue_concurrency": "default:4"}))
    await asyncio.sleep(0.1)
    assert await run_batch(worker, session_factory) == 4
    await worker.stop()


async def test_fewer_workers_retire_without_interrupting_jobs(cc_settings, session_factory):
    worker = JobWorker(
        cc_settings.model_copy(update={"job_queue_concurrency": "default:4"}), session_factory
    )
    await worker.start()
    assert await run_batch(worker, session_factory) == 4
    running = [enqueue(session_factory, "test.cc") for _ in range(4)]
    worker.notify()
    await asyncio.sleep(0.08)  # all four are in flight
    worker.update_settings(cc_settings)  # back to one worker
    for job_id in running:  # nothing is cut short
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED, timeout=15)
    await asyncio.sleep(0.3)
    assert await run_batch(worker, session_factory, n=6) == 1
    await worker.stop()


async def test_musicbrainz_stays_at_one_whatever_is_saved(cc_settings, session_factory):
    worker = JobWorker(cc_settings, session_factory)
    await worker.start()
    worker.update_settings(
        cc_settings.model_copy(update={"job_queue_concurrency": "musicbrainz:6,default:1"})
    )
    await asyncio.sleep(0.1)
    assert worker._target["musicbrainz"] == 1 and worker._active["musicbrainz"] == 1
    await worker.stop()


async def test_update_settings_after_stop_is_harmless(cc_settings, session_factory):
    worker = JobWorker(cc_settings, session_factory)
    await worker.start()
    await worker.stop()
    worker.update_settings(cc_settings.model_copy(update={"job_queue_concurrency": "default:3"}))
    await asyncio.sleep(0.1)
    assert worker.settings.queue_concurrency["default"] == 3 and not any(
        not t.done() for t in worker._tasks
    )


def test_secrets_never_appear_in_the_job_redaction_list_when_blank(settings):
    assert (
        settings.model_copy(update={"lastfm_api_key": "abcd1234"}).secret_values.count("abcd1234")
        == 1
    )
    assert "" not in settings.secret_values


def test_default_quality_is_flac_standard(settings):
    assert settings.default_quality == "flac_standard"


def test_rc_module_exports_what_the_pages_need():
    assert rc.QUEUES == ("default", "library", "musicbrainz", "coverart", "slskd", "discovery")
    assert rc.SOURCES == ("filesystem", "plex")
