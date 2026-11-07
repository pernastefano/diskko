"""SLSKD web API client (async): searches, batch downloads and transfer monitoring.

Endpoints and payloads were read from the slskd sources (docs/config.md, the controllers, the
Soulseek.NET enums) and checked against a live instance (0.26): base `/api/v0`, header
`X-API-Key`, JSON in camelCase with enums as strings, null fields omitted.

* POST /searches {id, searchText, searchTimeout, responseLimit, fileLimit, ...}: 429 while
  another search is being created. `searchTimeout` is in MILLISECONDS: the source comment says
  seconds, but 30 ended a live search after 30 ms with no answers while 30000 worked.
  GET /searches/{id} (state "Completed, TimedOut" ..., isComplete),
  GET /searches/{id}/responses, DELETE /searches/{id}.
* POST /transfers/downloads/batches {username, files:[{filename,size}], options:{destination}}:
  201 all queued, 207 some failed, 200 all failed, 404 user offline, 409 id in use.
  `destination` is relative to the downloads folder and replaces slskd's own pattern.
* GET /transfers/downloads: users -> directories -> files (transfers).
  DELETE /transfers/downloads/{username}/{id}?remove=true.

Secrets never appear in errors or logs: messages carry statuses and short server messages only.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app import __version__
from app.config import Settings
from app.services.candidate_selection import RemoteFile, RemoteResponse

REQUEST_TIMEOUT = 30.0
SEARCH_POLL_SECONDS = 2.0
BUSY_WAIT_SECONDS = 3.0
BUSY_ATTEMPTS = 10


class SlskdError(Exception):
    """SLSKD could not be used (bad answer, too many failures)."""


class SlskdNotConfigured(SlskdError):
    pass


class SlskdAuthError(SlskdError):
    """The API key is missing, wrong or not allowed to do this (needs ReadWrite)."""


class SlskdUserOffline(SlskdError):
    """The user we want to download from is not on the network (HTTP 404)."""


class SlskdBusy(SlskdError):
    """SLSKD keeps answering 429: another operation is in progress."""


# ------------------------------------------------------------------ transfer states


def state_flags(state: str) -> set[str]:
    """'Completed, Succeeded' -> {'Completed', 'Succeeded'} (the states are .NET flags)."""
    return {p.strip() for p in str(state or "").split(",") if p.strip()}


def is_succeeded(state: str) -> bool:
    flags = state_flags(state)
    return "Completed" in flags and "Succeeded" in flags


def is_finished(state: str) -> bool:
    return "Completed" in state_flags(state)


def is_failed(state: str) -> bool:
    return is_finished(state) and not is_succeeded(state)


def is_queued(state: str) -> bool:
    flags = state_flags(state)
    return not is_finished(state) and bool(flags & {"Requested", "Queued"})


def is_running(state: str) -> bool:
    return not is_finished(state) and bool(state_flags(state) & {"Initializing", "InProgress"})


# ------------------------------------------------------------------------- results


@dataclass(frozen=True)
class SearchInfo:
    id: str
    state: str
    is_complete: bool
    response_count: int = 0
    file_count: int = 0


@dataclass(frozen=True)
class Transfer:
    id: str
    username: str
    filename: str
    size: int
    state: str
    bytes_transferred: int = 0
    average_speed: float = 0.0
    place_in_queue: int | None = None
    exception: str | None = None


@dataclass
class EnqueueResult:
    failures: dict[str, str] = field(default_factory=dict)  # filename -> why SLSKD refused it


def parse_file(raw: dict[str, Any]) -> RemoteFile:
    def number(key: str) -> int | None:
        value = raw.get(key)
        return (
            int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
        )

    return RemoteFile(
        filename=str(raw.get("filename") or ""),
        size=int(raw.get("size") or 0),
        extension=str(raw.get("extension") or ""),
        bit_rate=number("bitRate"),
        bit_depth=number("bitDepth"),
        sample_rate=number("sampleRate"),
        length=number("length"),
        is_vbr=bool(raw.get("isVariableBitRate")),
        locked=bool(raw.get("isLocked")),
    )


def parse_responses(data: Any) -> list[RemoteResponse]:
    """Answers of a search. Attributes are optional; locked files are left out."""
    responses = []
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict) or not raw.get("username"):
            continue
        files = tuple(
            f
            for item in raw.get("files") or []
            if isinstance(item, dict) and (f := parse_file(item)).filename
        )
        responses.append(
            RemoteResponse(
                username=str(raw["username"]),
                files=files,
                has_free_slot=bool(raw.get("hasFreeUploadSlot")),
                queue_length=int(raw.get("queueLength") or 0),
                upload_speed=int(raw.get("uploadSpeed") or 0),
            )
        )
    return responses


def parse_transfers(data: Any) -> list[Transfer]:
    """Flatten GET /transfers/downloads (users -> directories -> files)."""
    transfers: list[Transfer] = []
    for user in data if isinstance(data, list) else []:
        for directory in (user or {}).get("directories") or []:
            for raw in (directory or {}).get("files") or []:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                place = raw.get("placeInQueue")
                transfers.append(
                    Transfer(
                        id=str(raw["id"]),
                        username=str(raw.get("username") or user.get("username") or ""),
                        filename=str(raw.get("filename") or ""),
                        size=int(raw.get("size") or 0),
                        state=str(raw.get("state") or ""),
                        bytes_transferred=int(raw.get("bytesTransferred") or 0),
                        average_speed=float(raw.get("averageSpeed") or 0.0),
                        place_in_queue=int(place) if isinstance(place, int) else None,
                        exception=str(raw["exception"])[:300] if raw.get("exception") else None,
                    )
                )
    return transfers


# ---------------------------------------------------------------------------- client


class SlskdClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 2,
    ) -> None:
        if not (settings.slskd_url.strip() and settings.slskd_api_key.strip()):
            raise SlskdNotConfigured("SLSKD is not configured: set its URL and API key")
        self.settings = settings
        self._base = settings.slskd_url.strip().rstrip("/") + "/api/v0"
        self._key = settings.slskd_api_key.strip()
        self._http = http
        self._own_http = http is None
        self._sleep = sleep
        self._max_retries = max_retries

    async def __aenter__(self) -> "SlskdClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()

    # ---------------------------------------------------------------- plumbing

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None
    ) -> httpx.Response:
        assert self._http is not None, "use the client as an async context manager"
        headers = {
            "X-API-Key": self._key,
            "Accept": "application/json",
            "User-Agent": f"Diskko/{__version__}",
        }
        last = "no attempt"
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.request(
                    method, self._base + path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                last = type(exc).__name__  # never the message: it can contain the URL
                await self._sleep(1.0 * (2**attempt))
                continue
            if resp.status_code in (401, 403):
                raise SlskdAuthError(
                    "SLSKD refused the API key (wrong, or without the ReadWrite role)"
                )
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                await self._sleep(1.0 * (2**attempt))
                continue
            return resp
        raise SlskdError(f"SLSKD is not answering ({last}) after retries")

    @staticmethod
    def _message(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            body = None
        text = (
            body
            if isinstance(body, str)
            else (body or {}).get("title")
            if isinstance(body, dict)
            else ""
        )
        return str(text or resp.text or "")[:200]

    # ------------------------------------------------------------------ status

    async def server_state(self) -> dict[str, Any]:
        """{'connected': bool, 'logged_in': bool, 'state': str, 'version': str}."""
        resp = await self._request("GET", "/application")
        if resp.status_code != 200:
            raise SlskdError(f"unexpected answer from SLSKD (HTTP {resp.status_code})")
        data = resp.json()
        server = data.get("server") or {}
        version = data.get("version") or {}
        return {
            "connected": bool(server.get("isConnected")),
            "logged_in": bool(server.get("isLoggedIn")),
            "state": str(server.get("state") or ""),
            "version": str(version.get("current") or "") if isinstance(version, dict) else "",
        }

    # ---------------------------------------------------------------- searches

    async def create_search(
        self,
        text: str,
        *,
        timeout_s: int = 30,
        response_limit: int = 100,
        file_limit: int = 10000,
    ) -> str:
        """Start a search; returns its id. SLSKD allows one creation at a time (429): wait."""
        search_id = str(uuid.uuid4())
        body = {
            "id": search_id,
            "searchText": text,
            "searchTimeout": max(5, int(timeout_s)) * 1000,  # milliseconds (checked live)
            "responseLimit": response_limit,
            "fileLimit": file_limit,
            "filterResponses": True,
            "minimumResponseFileCount": 1,
        }
        for _ in range(BUSY_ATTEMPTS):
            resp = await self._request("POST", "/searches", json=body)
            if resp.status_code == 429:
                await self._sleep(BUSY_WAIT_SECONDS)
                continue
            if resp.status_code == 200:
                return search_id
            raise SlskdError(
                f"SLSKD refused the search (HTTP {resp.status_code}) {self._message(resp)}"
            )
        raise SlskdBusy("SLSKD is busy with another search")

    async def get_search(self, search_id: str) -> SearchInfo | None:
        resp = await self._request("GET", f"/searches/{search_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SlskdError(f"unexpected answer from SLSKD (HTTP {resp.status_code})")
        data = resp.json()
        return SearchInfo(
            id=str(data.get("id") or search_id),
            state=str(data.get("state") or ""),
            is_complete=bool(data.get("isComplete")),
            response_count=int(data.get("responseCount") or 0),
            file_count=int(data.get("fileCount") or 0),
        )

    async def wait_for_search(
        self, search_id: str, *, hard_timeout: float, poll: float | None = None
    ) -> SearchInfo:
        """Poll until the search is complete (or `hard_timeout` seconds passed)."""
        poll = SEARCH_POLL_SECONDS if poll is None else poll
        waited = 0.0
        while True:
            info = await self.get_search(search_id)
            if info is None:
                raise SlskdError("the search disappeared from SLSKD")
            if info.is_complete or waited >= hard_timeout:
                return info
            await self._sleep(poll)
            waited += poll

    async def get_responses(self, search_id: str) -> list[RemoteResponse]:
        resp = await self._request("GET", f"/searches/{search_id}/responses")
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise SlskdError(f"unexpected answer from SLSKD (HTTP {resp.status_code})")
        return parse_responses(resp.json())

    async def delete_search(self, search_id: str) -> None:
        """Forget a search (best effort: a leftover search is only clutter)."""
        with contextlib.suppress(SlskdError):
            await self._request("DELETE", f"/searches/{search_id}")

    # --------------------------------------------------------------- downloads

    async def enqueue(
        self,
        username: str,
        files: list[tuple[str, int]],
        destination: str,
        *,
        external_id: str | None = None,
    ) -> EnqueueResult:
        """Queue files of one user as a batch; they are saved under `destination` (relative)."""
        body: dict[str, Any] = {
            "username": username,
            "files": [{"filename": name, "size": size} for name, size in files],
            "options": {"destination": destination},
        }
        if external_id:
            body["options"]["externalId"] = external_id
        for _ in range(BUSY_ATTEMPTS):
            resp = await self._request("POST", "/transfers/downloads/batches", json=body)
            if resp.status_code == 429:
                await self._sleep(BUSY_WAIT_SECONDS)
                continue
            break
        else:
            raise SlskdBusy("SLSKD is busy queueing other downloads")
        if resp.status_code == 404:
            raise SlskdUserOffline(f"{username} is not available on the network")
        if resp.status_code not in (200, 201, 207):
            raise SlskdError(
                f"SLSKD refused the download (HTTP {resp.status_code}) {self._message(resp)}"
            )
        result = EnqueueResult()
        if resp.status_code == 200:  # every file failed
            result.failures = {name: "refused" for name, _ in files}
        data = resp.json() if resp.content else {}
        for failure in (
            (data.get("failures") or data.get("Failures") or []) if isinstance(data, dict) else []
        ):
            if isinstance(failure, dict) and failure.get("filename"):
                result.failures[str(failure["filename"])] = str(failure.get("message") or "")[:200]
        return result

    async def list_downloads(self) -> list[Transfer]:
        resp = await self._request("GET", "/transfers/downloads")
        if resp.status_code != 200:
            raise SlskdError(f"unexpected answer from SLSKD (HTTP {resp.status_code})")
        return parse_transfers(resp.json())

    async def cancel_transfer(
        self, username: str, transfer_id: str, *, remove: bool = True
    ) -> None:
        resp = await self._request(
            "DELETE",
            f"/transfers/downloads/{username}/{transfer_id}",
            params={"remove": str(remove).lower()},
        )
        if resp.status_code not in (200, 204, 404):
            raise SlskdError(f"SLSKD could not cancel the transfer (HTTP {resp.status_code})")
