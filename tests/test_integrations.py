import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.integrations.base import run_check
from app.integrations.musicbrainz import SAMPLE_RELEASE_GROUP, MusicBrainz
from app.integrations.plex import Plex
from app.integrations.slskd import Slskd
from app.jobs.worker import JobWorker
from app.models import STATUS_FAILED, STATUS_SUCCEEDED, Job
from app.services.app_settings import get_setting
from tests.test_jobs_worker import enqueue, fetch, logs, wait_for_status

PLEX = "http://plex.local:32400"
SLSKD = "http://slskd.local:5030"
MB = "https://musicbrainz.test/ws/2"
PLEX_TOKEN = "plex-secret-token"
SLSKD_KEY = "slskd-secret-key-123456"


@pytest.fixture
def isettings(settings):
    return settings.model_copy(
        update={
            "plex_url": PLEX + "/",
            "plex_token": PLEX_TOKEN,
            "slskd_url": SLSKD + "/",
            "slskd_api_key": SLSKD_KEY,
            "mb_user_agent": "Diskko/0.1 ( test@example.com )",
            "musicbrainz_url": MB,
            "job_poll_interval": 0.02,
        }
    )


# ------------------------------------------------------------------------- Plex


async def test_plex_ok_sends_token_header_and_reads_version(isettings):
    with respx.mock(base_url=PLEX) as mock:
        ident = mock.get("/identity").respond(json={"MediaContainer": {"version": "1.40.2"}})
        sections = mock.get("/library/sections").respond(json={"MediaContainer": {}})
        result = await run_check(Plex(isettings))
    assert (result.ok, result.code, result.detail) == (True, "ok", "1.40.2")
    assert result.latency_ms is not None
    for route in (ident, sections):
        request = route.calls.last.request
        assert request.headers["X-Plex-Token"] == PLEX_TOKEN
        assert request.headers["Accept"] == "application/json"
    assert PLEX_TOKEN not in str(result.to_dict())


async def test_plex_rejected_token(isettings):
    with respx.mock(base_url=PLEX) as mock:
        mock.get("/identity").respond(json={})
        mock.get("/library/sections").respond(401)
        result = await run_check(Plex(isettings))
    assert (result.ok, result.code) == (False, "auth_failed")


@pytest.mark.parametrize(("path", "code"), [("/identity", "500"), ("/library/sections", "502")])
async def test_plex_unexpected_status(isettings, path, code):
    with respx.mock(base_url=PLEX, assert_all_called=False) as mock:
        mock.get("/identity").respond(500 if path == "/identity" else 200, json={})
        mock.get("/library/sections").respond(502)
        result = await run_check(Plex(isettings))
    assert (result.ok, result.code, result.detail) == (False, "http_error", code)


async def test_plex_unreachable_does_not_leak_url_or_token(isettings):
    with respx.mock(base_url=PLEX) as mock:
        mock.get("/identity").mock(side_effect=httpx.ConnectError("refused to plex.local"))
        result = await run_check(Plex(isettings))
    assert (result.ok, result.code, result.detail) == (False, "unreachable", "ConnectError")
    assert "plex.local" not in str(result.to_dict())


# ------------------------------------------------------------------------ SLSKD


async def test_slskd_ok_with_api_key_header(isettings):
    with respx.mock(base_url=SLSKD) as mock:
        route = mock.get("/api/v0/application").respond(json={"version": "0.21.4"})
        result = await run_check(Slskd(isettings))
    assert (result.ok, result.code, result.detail) == (True, "ok", "0.21.4")
    assert route.calls.last.request.headers["X-API-Key"] == SLSKD_KEY
    assert SLSKD_KEY not in str(result.to_dict())


async def test_slskd_version_object_and_unparseable_payloads(isettings):
    for body, expected in (({"version": {"current": "0.22.0"}}, "0.22.0"), ("not json", "")):
        with respx.mock(base_url=SLSKD) as mock:
            mock.get("/api/v0/application").respond(200, json=body)
            result = await run_check(Slskd(isettings))
        assert result.ok and result.detail == expected


@pytest.mark.parametrize("status", [401, 403])
async def test_slskd_rejected_key(isettings, status):
    with respx.mock(base_url=SLSKD) as mock:
        mock.get("/api/v0/application").respond(status)
        result = await run_check(Slskd(isettings))
    assert (result.ok, result.code) == (False, "auth_failed")


async def test_slskd_server_error_and_timeout(isettings):
    with respx.mock(base_url=SLSKD) as mock:
        mock.get("/api/v0/application").respond(500)
        assert (await run_check(Slskd(isettings))).code == "http_error"
    with respx.mock(base_url=SLSKD) as mock:
        mock.get("/api/v0/application").mock(side_effect=httpx.ReadTimeout("slow"))
        result = await run_check(Slskd(isettings))
    assert (result.code, result.detail) == ("unreachable", "ReadTimeout")


# ------------------------------------------------------------------ MusicBrainz


async def test_musicbrainz_ok_sends_user_agent_and_json_format(isettings):
    with respx.mock(base_url=MB) as mock:
        route = mock.get(f"/release-group/{SAMPLE_RELEASE_GROUP}").respond(json={"id": "x"})
        result = await run_check(MusicBrainz(isettings))
    assert result.ok
    request = route.calls.last.request
    assert request.headers["User-Agent"] == "Diskko/0.1 ( test@example.com )"
    assert request.url.params["fmt"] == "json"


async def test_musicbrainz_throttling_and_errors(isettings):
    for status, code, detail in ((503, "rate_limited", ""), (404, "http_error", "404")):
        with respx.mock(base_url=MB) as mock:
            mock.get(f"/release-group/{SAMPLE_RELEASE_GROUP}").respond(status)
            result = await run_check(MusicBrainz(isettings))
        assert (result.ok, result.code, result.detail) == (False, code, detail)


# ------------------------------------------------------------- not configured


@pytest.mark.parametrize("cls", [Plex, Slskd, MusicBrainz])
async def test_unconfigured_integration_makes_no_request(settings, cls):
    blank = settings.model_copy(update={"plex_url": "", "slskd_url": "", "mb_user_agent": ""})
    with respx.mock(assert_all_called=False) as mock:
        result = await run_check(cls(blank))
    assert (result.ok, result.code) == (False, "not_configured")
    assert mock.calls.call_count == 0


def test_integrations_need_both_url_and_credential(settings):
    only_url = settings.model_copy(update={"plex_url": PLEX, "slskd_url": SLSKD})
    assert not Plex(only_url).is_configured() and not Slskd(only_url).is_configured()


# ------------------------------------------------------------------ job handler


async def test_check_job_stores_result_and_never_logs_secrets(isettings, session_factory):
    worker = JobWorker(isettings, session_factory)
    await worker.start()
    with respx.mock(base_url=SLSKD) as mock:
        mock.get("/api/v0/application").respond(json={"version": "0.21.4"})
        job_id = enqueue(session_factory, "integration_check", {"name": "slskd"})
        worker.notify()
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    with session_factory() as db:
        stored = get_setting(db, "integration_status.slskd")
    assert stored["ok"] is True and stored["code"] == "ok" and stored["detail"] == "0.21.4"
    assert stored["checked_at"] and stored["latency_ms"] is not None
    blob = str(stored) + " ".join(logs(session_factory, job_id))
    assert SLSKD_KEY not in blob and SLSKD not in blob


async def test_failed_check_is_a_successful_job_with_a_failed_result(isettings, session_factory):
    worker = JobWorker(isettings, session_factory)
    await worker.start()
    with respx.mock(base_url=PLEX) as mock:
        mock.get("/identity").mock(side_effect=httpx.ConnectError("down"))
        job_id = enqueue(session_factory, "integration_check", {"name": "plex"})
        worker.notify()
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    with session_factory() as db:
        stored = get_setting(db, "integration_status.plex")
    assert stored["ok"] is False and stored["code"] == "unreachable"


async def test_check_job_for_unknown_integration_fails_permanently(isettings, session_factory):
    worker = JobWorker(isettings, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "integration_check", {"name": "spotify"})
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    await worker.stop()
    assert job.attempts == 1 and "unknown integration" in job.error
    assert fetch(session_factory, job_id).status == STATUS_FAILED


def test_check_jobs_are_deduplicated(session_factory):
    from app.integrations.status import check_dedupe_key

    first = enqueue(
        session_factory, "integration_check", {"name": "plex"}, dedupe_key=check_dedupe_key("plex")
    )
    second = enqueue(
        session_factory, "integration_check", {"name": "plex"}, dedupe_key=check_dedupe_key("plex")
    )
    assert first == second
    with session_factory() as db:
        assert db.scalar(select(func.count(Job.id))) == 1
