"""End-to-end test against a running DISKKO container (see e2e/docker-compose.yml).

Walks the product the way a person does, over HTTP: first run, sign-in, the sign-in brake, the
status of Plex and SLSKD (mocks), a library scan through the mock Plex, a database backup and its
download. Needs only httpx. Usage: python e2e/run_e2e.py [base_url]
"""

import re
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PLEX = "http://localhost:32400"
SLSKD = "http://localhost:5030"
PASSWORD = "e2e-password-123"
TIMEOUT = 120


def step(msg: str) -> None:
    print(f"== {msg}", flush=True)


def check(condition: bool, msg: str) -> None:
    if not condition:
        print(f"FAIL: {msg}", flush=True)
        sys.exit(1)
    print(f"ok   {msg}", flush=True)


def csrf(client: httpx.Client, page: str) -> str:
    html = client.get(page).text
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    check(found is not None, f"{page} has a CSRF token")
    return found.group(1)


def post(client: httpx.Client, path: str, data: dict, page: str) -> httpx.Response:
    return client.post(path, data={**data, "csrf_token": csrf(client, page)})


def wait_for(fn, what: str, timeout: int = TIMEOUT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(1)
    check(False, f"timed out waiting for {what}")


def job_status(client: httpx.Client, location: str) -> str:
    html = client.get(f"{BASE}{location}/summary").text
    found = re.search(r'data-job-status="(\w+)"', html)
    return found.group(1) if found else ""


def run_job(client: httpx.Client, response: httpx.Response, what: str) -> None:
    check(
        response.status_code == 303 and response.headers["location"].startswith("/admin/jobs/"),
        f"{what}: job queued",
    )
    location = response.headers["location"]
    status = wait_for(
        lambda: (s := job_status(client, location)) in ("succeeded", "failed", "cancelled") and s,
        f"{what} to finish",
    )
    check(status == "succeeded", f"{what}: job succeeded (was {status})")


def main() -> None:
    step("health")
    wait_for(lambda: httpx.get(f"{BASE}/health", timeout=5).status_code == 200, "/health")
    body = httpx.get(f"{BASE}/health").json()
    check(body["status"] == "ok" and body["version"], f"/health is ok (v{body['version']})")

    admin = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
    step("first run and sign-in")
    check(admin.get("/").headers["location"].startswith("/setup"), "first visit goes to /setup")
    r = post(
        admin,
        "/setup",
        {"username": "admin", "password": PASSWORD, "password_confirm": PASSWORD},
        "/setup",
    )
    check(r.status_code == 303, "the administrator is created")
    check(admin.get("/dashboard").status_code == 200, "the dashboard opens signed in")

    step("login brake")
    other = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
    codes = []
    for _ in range(7):
        codes.append(
            post(
                other, "/login", {"username": "admin", "password": "nope-nope"}, "/login"
            ).status_code
        )
    check(codes[:5] == [400] * 5 and codes[-1] == 429, f"too many failures are paused {codes}")
    r = post(other, "/login", {"username": "admin", "password": PASSWORD}, "/login")
    check(r.status_code == 429 and "Retry-After" in r.headers, "even the right password waits")

    step("services")
    post(admin, "/admin/status/test-all", {}, "/admin/status")
    grid = wait_for(
        lambda: 'data-health="ok"' in (g := admin.get("/admin/status/grid").text) and g,
        "Plex and SLSKD to show as connected",
    )
    check(grid is not None, "Plex and SLSKD (mocks) are connected")
    check(
        any(x["path"] == "/api/v0/application" for x in httpx.get(f"{SLSKD}/__requests").json()),
        "SLSKD mock was asked about its state",
    )

    step("library scan through Plex")
    run_job(admin, post(admin, "/settings/library/scan", {}, "/settings/library"), "library scan")
    artists = admin.get("/artists").text
    check("E2E Artist" in artists, "the artist from Plex is in the library")
    seen = httpx.get(f"{PLEX}/__requests").json()
    check(any("/library/sections/1/all" in x["path"] for x in seen), "Plex mock listed the section")

    step("database backup")
    check("No backups yet." in admin.get("/admin/backups").text, "no backup before the first one")
    run_job(admin, post(admin, "/admin/backups", {}, "/admin/backups"), "backup")
    page = admin.get("/admin/backups").text
    name = re.search(r'href="/admin/backups/(diskko-[\d-]+\.db)"', page)
    check(name is not None, "the backup is listed")
    got = admin.get(f"/admin/backups/{name.group(1)}")
    check(got.status_code == 200 and got.content.startswith(b"SQLite format 3"), "it downloads")
    check(admin.get("/admin/backups/..%2fapp.db").status_code == 404, "no path tricks")

    step("access control")
    check(
        httpx.get(f"{BASE}/admin/backups", follow_redirects=False).status_code == 303,
        "backups need a sign-in",
    )

    print("\nAll end-to-end checks passed.")


if __name__ == "__main__":
    main()
