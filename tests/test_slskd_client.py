"""The SLSKD client against a mocked API (shapes checked against a live 0.26 instance)."""

import json

import httpx
import pytest
import respx

from app.integrations import slskd_client as sc
from app.integrations.slskd_client import (
    SlskdAuthError,
    SlskdBusy,
    SlskdClient,
    SlskdError,
    SlskdNotConfigured,
    SlskdUserOffline,
    is_failed,
    is_finished,
    is_queued,
    is_running,
    is_succeeded,
    parse_responses,
    parse_transfers,
    state_flags,
)
from tests.fake_slskd import BASE, KEY, FakeSlskd, file_json, flac_album, response

NO_SLEEP_CALLS: list[float] = []


async def no_sleep(seconds):
    NO_SLEEP_CALLS.append(seconds)


@pytest.fixture
def ssettings(settings):
    return settings.model_copy(
        update={"slskd_url": "http://slskd.test:5030/", "slskd_api_key": KEY}
    )


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sc, "BUSY_WAIT_SECONDS", 0)
    monkeypatch.setattr(sc, "SEARCH_POLL_SECONDS", 0)
    NO_SLEEP_CALLS.clear()


def client(settings, **kw):
    return SlskdClient(settings, sleep=no_sleep, **kw)


# ---------------------------------------------------------------------- the states


@pytest.mark.parametrize(
    ("state", "queued", "running", "ok", "failed", "finished"),
    [
        ("Requested", True, False, False, False, False),
        ("Queued, Remotely", True, False, False, False, False),
        ("Queued, Locally", True, False, False, False, False),
        ("Initializing", False, True, False, False, False),
        ("InProgress", False, True, False, False, False),
        ("Completed, Succeeded", False, False, True, False, True),
        ("Completed, Cancelled", False, False, False, True, True),
        ("Completed, TimedOut", False, False, False, True, True),
        ("Completed, Errored", False, False, False, True, True),
        ("Completed, Rejected", False, False, False, True, True),
        ("Completed, Aborted", False, False, False, True, True),
        ("", False, False, False, False, False),
    ],
)
def test_transfer_states(state, queued, running, ok, failed, finished):
    assert is_queued(state) is queued and is_running(state) is running
    assert is_succeeded(state) is ok and is_failed(state) is failed
    assert is_finished(state) is finished


def test_state_flags():
    assert state_flags("Completed, Succeeded") == {"Completed", "Succeeded"}
    assert state_flags("") == set()


# --------------------------------------------------------------------------- parsing


def test_responses_are_parsed_with_optional_attributes():
    data = [
        response(
            "alice",
            [
                file_json("a\\b\\01.flac", bitDepth=24, sampleRate=96000, length=200),
                file_json("a\\b\\02.mp3", ext="mp3", bitRate=320, length=180),
                file_json("a\\b\\03.mp3", ext="mp3", bitRate=245, isVariableBitRate=True),
                file_json("a\\b\\04.ogg", ext="ogg"),  # no attributes at all
            ],
            free=False,
            queue=7,
            speed=123,
        ),
        {"files": []},  # no user name: skipped
        "garbage",
    ]
    [r] = parse_responses(data)
    assert (r.username, r.has_free_slot, r.queue_length, r.upload_speed) == ("alice", False, 7, 123)
    flac, mp3, vbr, bare = r.files
    assert (flac.bit_depth, flac.sample_rate, flac.bit_rate, flac.is_lossless) == (
        24,
        96000,
        None,
        True,
    )
    assert (mp3.bit_rate, mp3.bit_depth, mp3.is_vbr) == (320, None, False)
    assert vbr.is_vbr and vbr.is_v0 and not mp3.is_v0
    assert bare.bit_rate is None and bare.length is None and bare.ext == "ogg"
    assert parse_responses(None) == [] and parse_responses({"error": 1}) == []


def test_transfers_are_flattened_from_users_and_folders():
    data = [
        {
            "username": "alice",
            "directories": [
                {
                    "directory": "a\\b",
                    "fileCount": 2,
                    "files": [
                        {
                            "id": "11111111-1111-1111-1111-111111111111",
                            "username": "alice",
                            "filename": "a\\b\\01.flac",
                            "size": 100,
                            "state": "Completed, Succeeded",
                            "bytesTransferred": 100,
                            "averageSpeed": 55.5,
                        },
                        {
                            "id": "22222222-2222-2222-2222-222222222222",
                            "filename": "a\\b\\02.flac",
                            "size": 100,
                            "state": "Completed, Errored",
                            "exception": "boom",
                            "placeInQueue": 3,
                        },
                        {"filename": "no id"},
                    ],
                }
            ],
        }
    ]
    a, b = parse_transfers(data)
    assert (
        a.state == "Completed, Succeeded" and a.bytes_transferred == 100 and a.average_speed == 55.5
    )
    assert b.username == "alice" and b.exception == "boom" and b.place_in_queue == 3
    assert parse_transfers(None) == [] and parse_transfers([{"username": "x"}]) == []


# ---------------------------------------------------------------------------- setup


def test_the_client_needs_a_url_and_a_key(settings):
    with pytest.raises(SlskdNotConfigured):
        SlskdClient(settings)
    with pytest.raises(SlskdNotConfigured):
        SlskdClient(settings.model_copy(update={"slskd_url": "http://x"}))


async def test_requests_carry_the_key_and_a_user_agent(ssettings):
    fake = FakeSlskd()
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            state = await c.server_state()
        request = mock.calls.last.request
    assert state == {
        "connected": True,
        "logged_in": True,
        "state": "Connected, LoggedIn",
        "version": "0.26.2.0",
    }
    assert request.headers["X-API-Key"] == KEY and request.headers["User-Agent"].startswith(
        "Diskko/"
    )
    assert str(request.url) == f"{BASE}/application"  # the trailing slash of the setting is gone


async def test_a_refused_key_is_an_auth_error_that_never_shows_the_key(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/application").respond(403)
        async with client(ssettings) as c:
            with pytest.raises(SlskdAuthError) as exc:
                await c.server_state()
    assert KEY not in str(exc.value) and "slskd.test" not in str(exc.value)


async def test_server_errors_and_network_errors_are_retried_then_reported(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{BASE}/application").respond(503)
        async with client(ssettings, max_retries=2) as c:
            with pytest.raises(SlskdError) as exc:
                await c.server_state()
    assert route.call_count == 3 and "503" in str(exc.value) and len(NO_SLEEP_CALLS) == 3
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/application").mock(side_effect=httpx.ConnectError("boom"))
        async with client(ssettings, max_retries=1) as c:
            with pytest.raises(SlskdError) as exc:
                await c.server_state()
    assert "ConnectError" in str(exc.value) and "slskd.test" not in str(exc.value)


async def test_a_transient_error_then_success(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/application").mock(
            side_effect=[
                httpx.Response(502),
                httpx.Response(200, json={"server": {"isConnected": True}}),
            ]
        )
        async with client(ssettings) as c:
            assert (await c.server_state())["connected"] is True


# --------------------------------------------------------------------------- searches


async def test_create_search_sends_the_documented_body(ssettings):
    fake = FakeSlskd()
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            sid = await c.create_search("Aretha Franklin Lady Soul", timeout_s=3)
        post = next(call for call in mock.calls if call.request.method == "POST")
        body = json.loads(post.request.content)
    assert body["id"] == sid and body["searchText"] == "Aretha Franklin Lady Soul"
    # SLSKD takes MILLISECONDS here (a live search with 30 ended at once, with 30000 it worked)
    assert body["searchTimeout"] == 5000  # and 5 seconds is the least it accepts
    assert body["filterResponses"] is True and body["minimumResponseFileCount"] == 1
    assert body["responseLimit"] == 100 and body["fileLimit"] == 10000


async def test_create_search_waits_while_slskd_is_busy(ssettings):
    fake = FakeSlskd()
    fake.busy = 3
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            assert await c.create_search("x y")
    assert fake.busy == 0 and len(NO_SLEEP_CALLS) == 3


async def test_create_search_gives_up_when_slskd_stays_busy(ssettings, monkeypatch):
    monkeypatch.setattr(sc, "BUSY_ATTEMPTS", 3)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/searches").respond(429)
        async with client(ssettings) as c:
            with pytest.raises(SlskdBusy):
                await c.create_search("x y")
    assert route.call_count == 3


async def test_create_search_refusals_are_errors(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/searches").respond(400, json="bad")
        async with client(ssettings) as c:
            with pytest.raises(SlskdError, match="HTTP 400"):
                await c.create_search("x y")


async def test_a_search_is_polled_until_it_completes(ssettings):
    fake = FakeSlskd()
    fake.search_polls_needed = 3
    fake.responses = [flac_album("alice", "Band\\Album")]
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            sid = await c.create_search("band album")
            info = await c.wait_for_search(sid, hard_timeout=60)
            responses = await c.get_responses(sid)
            await c.delete_search(sid)
            assert await c.get_search(sid) is None
    assert info.is_complete and info.state == "Completed, TimedOut" and info.response_count == 1
    assert sum(1 for m, p in fake.calls if m == "GET" and p.endswith(sid)) >= 3
    assert responses[0].username == "alice" and len(responses[0].files) == 10


async def test_waiting_for_a_search_stops_at_the_hard_limit(ssettings):
    fake = FakeSlskd()
    fake.search_polls_needed = 10**6
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            sid = await c.create_search("x y")
            info = await c.wait_for_search(sid, hard_timeout=3, poll=1)
    assert not info.is_complete


async def test_a_search_that_vanished_is_an_error(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/searches/.*").respond(404)
        async with client(ssettings) as c:
            with pytest.raises(SlskdError, match="disappeared"):
                await c.wait_for_search("00000000-0000-0000-0000-000000000000", hard_timeout=1)
            assert await c.get_responses("00000000-0000-0000-0000-000000000000") == []


async def test_deleting_a_search_never_raises(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        mock.delete(url__regex=r".*/searches/.*").respond(503)
        async with client(ssettings, max_retries=0) as c:
            await c.delete_search("x")


# -------------------------------------------------------------------------- downloads


async def test_enqueue_sends_a_batch_with_a_destination(ssettings):
    fake = FakeSlskd()
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            result = await c.enqueue(
                "alice",
                [("a\\b\\01.flac", 10), ("a\\b\\02.flac", 20)],
                "diskko/7",
                external_id="diskko-7",
            )
        request = next(r for r in mock.calls if r.request.url.path.endswith("/batches")).request
    body = json.loads(request.content)
    assert body == {
        "username": "alice",
        "files": [
            {"filename": "a\\b\\01.flac", "size": 10},
            {"filename": "a\\b\\02.flac", "size": 20},
        ],
        "options": {"destination": "diskko/7", "externalId": "diskko-7"},
    }
    assert result.failures == {} and len(fake.transfers) == 2
    assert {t["destination"] for t in fake.transfers.values()} == {"diskko/7"}


async def test_enqueue_reports_refused_files(ssettings):
    fake = FakeSlskd()
    fake.refuse = {"a\\b\\02.flac"}
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            partial = await c.enqueue("alice", [("a\\b\\01.flac", 1), ("a\\b\\02.flac", 1)], "d")
            everything = await c.enqueue("alice", [("a\\b\\02.flac", 1)], "d")
    assert partial.failures == {"a\\b\\02.flac": "Refused"}  # HTTP 207
    assert list(everything.failures) == ["a\\b\\02.flac"]  # HTTP 200: all failed


async def test_enqueue_for_an_offline_user(ssettings):
    fake = FakeSlskd()
    fake.offline = {"ghost"}
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            with pytest.raises(SlskdUserOffline):
                await c.enqueue("ghost", [("a\\b\\01.flac", 1)], "d")


async def test_enqueue_waits_when_slskd_is_busy_and_reports_other_refusals(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/transfers/downloads/batches").mock(
            side_effect=[httpx.Response(429), httpx.Response(201, json={})]
        )
        async with client(ssettings) as c:
            assert (await c.enqueue("alice", [("f", 1)], "d")).failures == {}
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/transfers/downloads/batches").respond(409, json="duplicate")
        async with client(ssettings) as c:
            with pytest.raises(SlskdError, match="HTTP 409"):
                await c.enqueue("alice", [("f", 1)], "d")


async def test_listing_and_cancelling_transfers(ssettings):
    fake = FakeSlskd()
    with respx.mock(assert_all_called=False) as mock:
        fake.mount(mock)
        async with client(ssettings) as c:
            await c.enqueue("alice", [("a\\b\\01.flac", 100)], "d")
            [transfer] = await c.list_downloads()
            assert transfer.username == "alice" and transfer.size == 100
            await c.cancel_transfer("alice", transfer.id)
            assert await c.list_downloads() == []
            await c.cancel_transfer("alice", "gone")  # a missing transfer is fine
        delete = next(r for r in mock.calls if r.request.method == "DELETE").request
    assert delete.url.params["remove"] == "true"


async def test_cancel_errors_are_reported(ssettings):
    with respx.mock(assert_all_called=False) as mock:
        mock.delete(url__regex=r".*/transfers/downloads/.*").respond(400)
        async with client(ssettings) as c:
            with pytest.raises(SlskdError, match="HTTP 400"):
                await c.cancel_transfer("alice", "x")


async def test_a_peer_name_cannot_add_path_segments_to_the_cancel_request(ssettings):
    """The username comes from a Soulseek peer: it is one path segment, whatever it contains."""
    with respx.mock(assert_all_called=False) as mock:
        mock.delete(url__regex=r".*/transfers/downloads/.*").respond(204)
        async with client(ssettings) as c:
            await c.cancel_transfer("../../application#x?y", "id/with/slashes")
        sent = next(r for r in mock.calls if r.request.method == "DELETE").request
    path = sent.url.raw_path.decode().split("?")[0]
    assert path.startswith("/api/v0/transfers/downloads/")
    assert path.count("/") == 6  # /api/v0/transfers/downloads/<user>/<id>: two segments only
    assert "%2F" in path and "#" not in path and "application" not in path.split("/")
