"""A small stateful SLSKD for the tests: answers like the real one (shapes checked live, 0.26).

Mount it on a respx mock with `fake.mount(mock)`. Behaviour is scripted through attributes:
`responses` (what a search finds), `offline` (users that cannot be downloaded from), `refuse`
(file names refused at enqueue), `script` (how a transfer evolves with every poll) and `busy`
(how many POST /searches answer 429 before one is accepted).
"""

import json
import re
import uuid
from collections.abc import Callable

import httpx

BASE = "http://slskd.test:5030/api/v0"
KEY = "slskd-test-key-123456"


def file_json(path, size=30_000_000, ext="flac", **attrs):
    """A file as SLSKD lists it: optional attributes are left out."""
    data = {"filename": path, "size": size, "extension": ext, "code": 1, "isLocked": False}
    data.update(attrs)
    return data


def flac_album(user, folder, count=10, **resp):
    files = [
        file_json(
            f"@@x\\Music\\{folder}\\{i:02d} Track {i}.flac",
            bitDepth=16,
            sampleRate=44100,
            length=200,
        )
        for i in range(1, count + 1)
    ]
    return response(user, files, **resp)


def response(user, files, free=True, queue=0, speed=2_000_000):
    return {
        "username": user,
        "files": files,
        "lockedFiles": [],
        "fileCount": len(files),
        "lockedFileCount": 0,
        "hasFreeUploadSlot": free,
        "queueLength": queue,
        "uploadSpeed": speed,
        "token": 1,
    }


class Tick:
    """What a transfer looks like on one poll: a state and the bytes received."""

    def __init__(self, state, fraction=0.0, exception=None):
        self.state, self.fraction, self.exception = state, fraction, exception


def finishes(polls_to_run=1):
    """Queued, running for a while, then done."""

    def script(transfer, tick):
        if tick < 1:
            return Tick("Queued, Remotely")
        if tick <= polls_to_run:
            return Tick("InProgress", 0.5)
        return Tick("Completed, Succeeded", 1.0)

    return script


class FakeSlskd:
    def __init__(self):
        self.responses: list[dict] = []
        self.offline: set[str] = set()
        self.refuse: set[str] = set()
        self.busy = 0
        self.connected = True
        self.search_polls_needed = 1
        self.script: Callable = finishes()
        self.searches: dict[str, dict] = {}
        self.transfers: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.deleted_transfers: list[str] = []
        self.searches_by_text: dict[str, list[dict]] = {}
        self.auth = KEY

    # ------------------------------------------------------------------- helpers

    def _record(self, request):
        self.calls.append((request.method, request.url.path.removeprefix("/api/v0")))

    def _forbidden(self, request):
        return request.headers.get("X-API-Key") != self.auth

    # ----------------------------------------------------------------- endpoints

    def application(self, request):
        self._record(request)
        if self._forbidden(request):
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "version": {"current": "0.26.2.0"},
                "server": {
                    "state": "Connected, LoggedIn" if self.connected else "Disconnected",
                    "isConnected": self.connected,
                    "isLoggedIn": self.connected,
                },
            },
        )

    def create_search(self, request):
        self._record(request)
        if self._forbidden(request):
            return httpx.Response(403)
        if self.busy:
            self.busy -= 1
            return httpx.Response(429)
        body = json.loads(request.content)
        self.searches[body["id"]] = {"body": body, "polls": 0}
        return httpx.Response(200, json={"id": body["id"], "state": "Requested, InProgress"})

    def get_search(self, request):
        self._record(request)
        sid = request.url.path.rsplit("/", 1)[-1]
        search = self.searches.get(sid)
        if search is None:
            return httpx.Response(404)
        search["polls"] += 1
        done = search["polls"] >= self.search_polls_needed
        results = self._results_for(search["body"]["searchText"])
        return httpx.Response(
            200,
            json={
                "id": sid,
                "searchText": search["body"]["searchText"],
                "state": "Completed, TimedOut" if done else "InProgress",
                "isComplete": done,
                "responseCount": len(results),
                "fileCount": sum(len(r["files"]) for r in results),
            },
        )

    def _results_for(self, text):
        for key, results in self.searches_by_text.items():
            if key.lower() == text.lower():
                return results
        return self.responses if not self.searches_by_text else []

    def get_responses(self, request):
        self._record(request)
        sid = request.url.path.split("/")[-2]
        search = self.searches.get(sid)
        if search is None:
            return httpx.Response(404)
        return httpx.Response(200, json=self._results_for(search["body"]["searchText"]))

    def delete_search(self, request):
        self._record(request)
        self.searches.pop(request.url.path.rsplit("/", 1)[-1], None)
        return httpx.Response(204)

    def enqueue(self, request):
        self._record(request)
        if self._forbidden(request):
            return httpx.Response(403)
        body = json.loads(request.content)
        user = body["username"]
        if user in self.offline:
            return httpx.Response(404, json="User offline")
        failures = []
        for f in body["files"]:
            if f["filename"] in self.refuse:
                failures.append({"filename": f["filename"], "message": "Refused"})
                continue
            tid = str(uuid.uuid4())
            self.transfers[tid] = {
                "id": tid,
                "username": user,
                "filename": f["filename"],
                "size": f["size"],
                "polls": 0,
                "destination": body["options"].get("destination"),
            }
        if failures and len(failures) == len(body["files"]):
            return httpx.Response(200, json={"failures": failures})
        if failures:
            return httpx.Response(207, json={"failures": failures})
        return httpx.Response(201, json={"batch": {"id": str(uuid.uuid4())}, "failures": []})

    def list_downloads(self, request):
        self._record(request)
        users: dict[str, dict] = {}
        for t in self.transfers.values():
            tick = self.script(t, t["polls"])
            t["polls"] += 1
            item = {
                "id": t["id"],
                "username": t["username"],
                "direction": "Download",
                "filename": t["filename"],
                "size": t["size"],
                "state": tick.state,
                "bytesTransferred": int(t["size"] * tick.fraction),
                "averageSpeed": 1000.0,
                "removed": False,
            }
            if tick.exception:
                item["exception"] = tick.exception
            folder = t["filename"].rsplit("\\", 1)[0]
            users.setdefault(t["username"], {}).setdefault(folder, []).append(item)
        return httpx.Response(
            200,
            json=[
                {
                    "username": u,
                    "directories": [
                        {"directory": d, "fileCount": len(fs), "files": fs}
                        for d, fs in dirs.items()
                    ],
                }
                for u, dirs in users.items()
            ],
        )

    def delete_transfer(self, request):
        self._record(request)
        tid = request.url.path.rsplit("/", 1)[-1]
        self.transfers.pop(tid, None)
        self.deleted_transfers.append(tid)
        return httpx.Response(204)

    # --------------------------------------------------------------------- mount

    def mount(self, mock):
        mock.get(f"{BASE}/application").mock(side_effect=self.application)
        mock.post(f"{BASE}/searches").mock(side_effect=self.create_search)
        mock.get(url__regex=re.escape(BASE) + r"/searches/[^/]+/responses$").mock(
            side_effect=self.get_responses
        )
        mock.get(url__regex=re.escape(BASE) + r"/searches/[^/]+$").mock(side_effect=self.get_search)
        mock.delete(url__regex=re.escape(BASE) + r"/searches/[^/]+$").mock(
            side_effect=self.delete_search
        )
        mock.post(f"{BASE}/transfers/downloads/batches").mock(side_effect=self.enqueue)
        mock.get(f"{BASE}/transfers/downloads").mock(side_effect=self.list_downloads)
        mock.delete(url__regex=re.escape(BASE) + r"/transfers/downloads/[^/]+/[^/]+").mock(
            side_effect=self.delete_transfer
        )
        return self
