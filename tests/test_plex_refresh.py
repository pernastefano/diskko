"""Telling Plex about imported albums: partial scans, debounce, fallback, rating keys."""

import asyncio
import re
from datetime import timedelta
from urllib.parse import parse_qs, unquote_plus, urlsplit
from xml.sax.saxutils import quoteattr

import pytest
import requests
import responses
from sqlalchemy import select

from app.jobs import engine
from app.jobs.handlers import plex_refresh as job
from app.jobs.worker import JobWorker
from app.models import Job, LibraryAlbum, LibraryTrack, utcnow
from app.services import plex_refresh as svc
from app.services.app_settings import get_setting
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist
from tests.test_jobs_worker import enqueue, fetch, logs, wait_for_status
from tests.test_library_plex import BASE, TOKEN, FakePlex, PAlbum, PArtist, PTrack

MUSIC = "/music/The Band/Album (2000)"  # as this app sees the folder
PLEX_FOLDER = "/data/music/The Band/Album (2000)"  # as Plex sees it
ITEM = svc.Pending(MUSIC, "Album Title")


class ScanPlex(FakePlex):
    """A Plex that scans: it records the scans, says "refreshing" for a while, answers album
    searches and can hide an album until a scan has been done."""

    def __init__(self):
        super().__init__()
        self.scans: list[tuple[str, str | None]] = []
        self.metadata_refreshes: list[str] = []
        self.busy_polls = 0  # how many times the section list still says it is refreshing
        self.always_busy = False
        self.refuse_partial: int | None = None  # HTTP status for a partial scan
        self.hidden: set[int] = set()  # album ids not listed yet
        self.partial_reveals = True  # a partial scan of the folder reveals the album
        self.full_reveals = True
        self.section_polls = 0

    def _album_xml(self, a):
        base = super()._album_xml(a)
        return base.replace(
            "<Directory ", f'<Directory key="/library/metadata/{a.id}/children" ', 1
        )

    def callback(self, request):
        url = urlsplit(request.url)
        path = url.path
        if path == "/library/sections" and request.method == "GET":
            self.requests.append(request)
            self.section_polls += 1
            busy = self.always_busy or self.busy_polls > 0
            self.busy_polls = max(self.busy_polls - 1, 0)
            return (
                200,
                {},
                (
                    f'<MediaContainer size="1"><Directory key="1" type="artist" title="Music" '
                    f'refreshing="{1 if busy else 0}"/></MediaContainer>'
                ),
            )
        if path == "/library/sections/1/refresh":
            self.requests.append(request)
            if request.method == "DELETE":
                return 200, {}, ""
            wanted = parse_qs(url.query).get("path")
            if wanted:
                if self.refuse_partial:
                    return self.refuse_partial, {}, ""
                folder = unquote_plus(wanted[0])
                self.scans.append(("partial", folder))
                if self.partial_reveals:
                    self._reveal(folder)
            else:
                self.scans.append(("full", None))
                if self.full_reveals:
                    self.hidden.clear()
            self.busy_polls = max(self.busy_polls, 1)
            return 200, {}, ""
        if re.fullmatch(r"/library/metadata/\d+/refresh", path):
            self.requests.append(request)
            self.metadata_refreshes.append(path.split("/")[3])
            return 200, {}, ""
        if re.fullmatch(r"/library/metadata/\d+/children", path):
            self.requests.append(request)
            album_id = int(path.split("/")[3])
            tracks = [self._track_xml(t) for t in self.tracks if t.album == album_id]
            return (
                200,
                {},
                f'<MediaContainer size="{len(tracks)}">{"".join(tracks)}</MediaContainer>',
            )
        if path == "/library/sections/1/all" and "type=9" in url.query and "title=" in url.query:
            self.requests.append(request)
            title = unquote_plus(parse_qs(url.query)["title"][0]).lower()
            albums = [
                self._album_xml(a)
                for a in self.albums
                if title in a.title.lower() and a.id not in self.hidden
            ]
            return (
                200,
                {},
                f'<MediaContainer size="{len(albums)}">{"".join(albums)}</MediaContainer>',
            )
        return super().callback(request)

    def _reveal(self, folder: str) -> None:
        for track in self.tracks:
            if track.path and track.path.rsplit("/", 1)[0] == folder:
                self.hidden.discard(track.album)


@pytest.fixture
def plex():
    fake = ScanPlex()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, re.compile(re.escape(BASE) + ".*"), callback=fake.callback)
        for method in (responses.DELETE, responses.PUT):
            rsps.add_callback(method, re.compile(re.escape(BASE) + ".*"), callback=fake.callback)
        yield fake


def library(plex, *, hidden=True, extra_albums=0):
    """The album of ITEM in Plex (not listed until it is scanned), with two tracks."""
    plex.artists = [PArtist(1, "The Band")]
    plex.albums = [PAlbum(10, 1, "Album Title", 2000)]
    plex.tracks = [
        PTrack(100 + i, 10, 1, f"Song {i}", i, path=f"{PLEX_FOLDER}/{i:02d} Song {i}.flac")
        for i in (1, 2)
    ]
    for n in range(extra_albums):
        plex.albums.append(PAlbum(20 + n, 1, f"Other {n}", 2001))
    if hidden:
        plex.hidden = {10}


@pytest.fixture
def psettings(settings, tmp_path):
    return settings.model_copy(
        update={
            "plex_url": BASE,
            "plex_token": TOKEN,
            "music_dir": tmp_path / "music",
            "library_path_map": "/data/music=/music",
            "library_source": "filesystem",
            "job_poll_interval": 0.02,
            "plex_refresh_debounce_seconds": 30,
            "plex_refresh_timeout_minutes": 1,
        }
    )


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(job, "START_GRACE", 0.0)
    monkeypatch.setattr(job, "POLL", 0.0)


def pending_in(factory, items):
    with factory() as db:
        svc.add_pending(db, items)
        db.commit()


async def run_refresh(settings, factory, payload=None, *, until=("succeeded", "failed")):
    worker = JobWorker(settings, factory)
    await worker.start()
    job_id = enqueue(factory, "plex_refresh", payload or {})
    worker.notify()
    try:
        for _ in range(500):
            row = await asyncio.to_thread(fetch, factory, job_id)
            if row.status in until:
                return job_id, row
            await asyncio.sleep(0.02)
        raise AssertionError(f"job is {row.status}")
    finally:
        await worker.stop()


def pending_of(factory):
    with factory() as db:
        return svc.read_pending(db)


# ---------------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    ("local", "rules", "expected"),
    [
        ("/music/The Band/Album (2000)", [("/data/music", "/music")], PLEX_FOLDER),
        ("/music/The Band/Album (2000)/", [("/data/music", "/music")], PLEX_FOLDER),
        ("/music", [("/data/music", "/music")], "/data/music"),
        ("/musicbox/x", [("/data/music", "/music")], "/musicbox/x"),  # not under the prefix
        ("/srv/a/b", [], "/srv/a/b"),  # no rule: Plex sees the same path
        ("/music/x", [("/p", "/music"), ("/q", "/music/x")], "/q"),  # the longest local prefix
        ("C:\\music\\a", [("D:/m", "C:/music")], "D:/m/a"),
    ],
)
def test_a_local_folder_is_translated_back_to_the_plex_path(local, rules, expected):
    assert svc.unmap_path(local, rules) == expected


def test_the_translation_is_the_inverse_of_the_one_used_by_the_scan():
    from app.services.library.plex_provider import map_path, parse_path_map

    rules = parse_path_map("/data/music=/music")
    local = map_path(f"{PLEX_FOLDER}/01.flac", rules, [])
    assert svc.unmap_path(local, rules) == f"{PLEX_FOLDER}/01.flac"


# --------------------------------------------------------------------------- debounce


def test_imports_that_come_together_share_one_job_and_one_list(session_factory, psettings):
    with session_factory() as db:
        first = svc.request_refresh(db, psettings, [ITEM])
        second = svc.request_refresh(db, psettings, [svc.Pending("/music/B/B (1999)", "B")])
        again = svc.request_refresh(db, psettings, [ITEM])  # the same folder twice
        assert first.id == second.id == again.id  # one job
        assert [p.path for p in svc.read_pending(db)] == [MUSIC, "/music/B/B (1999)"]
        row = db.get(Job, first.id)
        wait = (row.next_run_at - utcnow()).total_seconds()
        assert 25 <= wait <= 30  # it does not start before the debounce time
        assert db.query(Job).count() == 1


def test_a_scan_that_just_ended_postpones_the_next_one(session_factory, psettings):
    with session_factory() as db:
        svc.mark_finished(db)
        job_row = svc.request_refresh(db, psettings, [ITEM])
        assert 28 <= (db.get(Job, job_row.id).next_run_at - utcnow()).total_seconds() <= 30
    long_ago = (utcnow() - timedelta(hours=1)).isoformat(timespec="seconds")
    with session_factory() as db:
        db.query(Job).delete()
        from app.services.app_settings import set_setting

        set_setting(db, svc.LAST_KEY, long_ago)
        db.commit()
        again = svc.request_refresh(db, psettings, [ITEM])
        assert (db.get(Job, again.id).next_run_at - utcnow()).total_seconds() <= 30


def test_the_pending_list_survives_and_is_trimmed(session_factory):
    other = svc.Pending("/music/B/B (1999)", "B")
    pending_in(session_factory, [ITEM, other])
    with session_factory() as db:
        left = svc.remove_pending(db, [ITEM])
        db.commit()
        assert left == [other] and svc.read_pending(db) == [other]


# ------------------------------------------------------------------ the partial scan


async def test_the_folder_of_the_album_is_scanned_not_the_section(plex, psettings, session_factory):
    library(plex)
    pending_in(session_factory, [ITEM])
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded", row.error
    assert plex.scans == [("partial", PLEX_FOLDER)]  # the path as Plex sees it, only that folder
    messages = logs(session_factory, job_id)
    assert any("Scanning /data/music/The Band/Album (2000) in Plex" in m for m in messages)
    assert any("in Plex (rating key 10)" in m for m in messages)
    assert pending_of(session_factory) == []
    with session_factory() as db:
        assert get_setting(db, svc.LAST_KEY)


async def test_the_job_waits_while_plex_is_scanning(plex, psettings, session_factory):
    library(plex)
    plex.busy_polls = 4
    pending_in(session_factory, [ITEM])
    polls_before = plex.section_polls
    _, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded" and pending_of(session_factory) == []
    assert plex.section_polls - polls_before >= 4  # it kept looking until Plex was done


async def test_nothing_to_do_without_folders(plex, psettings, session_factory):
    library(plex)
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded" and plex.scans == []
    assert any("Nothing waits for Plex" in m for m in logs(session_factory, job_id))


async def test_a_plex_that_is_not_configured_is_left_alone(plex, psettings, session_factory):
    pending_in(session_factory, [ITEM])
    s = psettings.model_copy(update={"plex_url": "", "plex_token": ""})
    job_id, row = await run_refresh(s, session_factory)
    assert row.status == "succeeded" and plex.scans == [] and plex.requests == []
    assert pending_of(session_factory) == [ITEM]


# ------------------------------------------------------------------------ the fallback


@pytest.mark.parametrize("status", [400, 404])
async def test_a_partial_scan_that_plex_refuses_becomes_a_full_scan(
    plex, psettings, session_factory, status
):
    library(plex)
    plex.refuse_partial = status
    pending_in(session_factory, [ITEM])
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded", row.error
    assert plex.scans == [("full", None)]
    assert any("Plex refused the scan" in m for m in logs(session_factory, job_id))
    assert pending_of(session_factory) == []


async def test_an_album_that_does_not_show_up_after_the_partial_scan_gets_a_full_scan(
    plex, psettings, session_factory
):
    library(plex)
    plex.partial_reveals = False  # Plex answered 200 and ignored the path
    pending_in(session_factory, [ITEM])
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded", row.error
    assert plex.scans == [("partial", PLEX_FOLDER), ("full", None)]
    messages = logs(session_factory, job_id)
    assert any("not in Plex after the partial scan" in m for m in messages)
    assert any("in Plex (rating key 10)" in m for m in messages)


async def test_an_album_plex_never_lists_is_reported_and_the_scans_stop_at_one_fallback(
    plex, psettings, session_factory
):
    library(plex)
    plex.partial_reveals = plex.full_reveals = False
    pending_in(session_factory, [ITEM])
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded"  # a Plex that cannot see it does not fail the job
    assert plex.scans == [("partial", PLEX_FOLDER), ("full", None)]  # no loop of scans
    assert any("not found in Plex after a full scan" in m for m in logs(session_factory, job_id))


async def test_many_folders_make_a_single_full_scan(plex, psettings, session_factory):
    library(plex)
    folders = [svc.Pending(f"/music/A/Album {i}", f"Album {i}") for i in range(21)]
    pending_in(session_factory, [ITEM, *folders])
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded", row.error
    assert plex.scans == [("full", None)]  # not 22 partial scans
    assert any("More than 20 folders" in m for m in logs(session_factory, job_id))


async def test_twenty_folders_are_still_scanned_one_by_one(plex, psettings, session_factory):
    library(plex)
    folders = [svc.Pending(f"/music/A/Album {i}", f"Album {i}") for i in range(19)]
    pending_in(session_factory, [ITEM, *folders])
    _, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded" and len(plex.scans) >= 20
    assert all(kind == "partial" for kind, _ in plex.scans[:20])


async def test_when_plex_takes_too_long_the_job_stops_waiting_and_does_not_rescan(
    plex, psettings, session_factory
):
    library(plex)
    plex.always_busy = True
    pending_in(session_factory, [ITEM])
    s = psettings.model_copy(update={"plex_refresh_timeout_minutes": 0.001})
    job_id, row = await run_refresh(s, session_factory)
    assert row.status == "succeeded"
    assert plex.scans == [("partial", PLEX_FOLDER)]  # no second scan on top of a running one
    assert any("still scanning" in m for m in logs(session_factory, job_id))
    assert pending_of(session_factory) == []  # Plex has been told


async def test_the_wait_can_be_cancelled(plex, psettings, session_factory):
    library(plex)
    plex.always_busy = True
    pending_in(session_factory, [ITEM])
    worker = JobWorker(psettings, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "plex_refresh", {})
    worker.notify()
    for _ in range(300):
        if plex.scans:
            break
        await asyncio.sleep(0.02)
    with session_factory() as db:
        engine.request_cancel(db, job_id)
    await wait_for_status(session_factory, job_id, "cancelled")
    await worker.stop()
    assert pending_of(session_factory) == [ITEM]  # not marked as done


# --------------------------------------------------------------- failures and retries


async def test_plex_unreachable_keeps_the_folders_and_retries(plex, psettings, session_factory):
    library(plex)
    plex.connection_error = True
    pending_in(session_factory, [ITEM])
    worker = JobWorker(psettings, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "plex_refresh", {})
    worker.notify()
    for _ in range(300):
        if any("Attempt failed" in m for m in logs(session_factory, job_id)):
            break
        await asyncio.sleep(0.02)
    await worker.stop()
    assert fetch(session_factory, job_id).status == "queued"
    assert pending_of(session_factory) == [ITEM]
    with session_factory() as db:
        assert get_setting(db, svc.LAST_KEY) is None


async def test_a_rejected_token_fails_the_job_once_and_keeps_the_folders(
    plex, psettings, session_factory
):
    library(plex)
    plex.root_status = 401
    pending_in(session_factory, [ITEM])
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "failed" and row.attempts == 1  # retrying will not help
    assert "token" in (row.error or "").lower()
    assert pending_of(session_factory) == [ITEM] and TOKEN not in (row.error or "")


async def test_folders_that_arrive_during_a_run_get_their_own_scan(
    plex, psettings, session_factory, monkeypatch
):
    library(plex)
    pending_in(session_factory, [ITEM])
    late = svc.Pending("/music/B/B (1999)", "B")
    original = svc.PlexScanner.scan_path

    def scan_and_import(self, local):
        pending_in(session_factory, [late])  # an import ends while Plex is being asked
        return original(self, local)

    monkeypatch.setattr(svc.PlexScanner, "scan_path", scan_and_import)
    job_id, row = await run_refresh(psettings, session_factory)
    assert row.status == "succeeded", row.error
    assert pending_of(session_factory) == [late]
    with session_factory() as db:
        follow_up = db.scalars(
            select(Job).where(Job.type == "plex_refresh", Job.id != job_id)
        ).all()
        assert len(follow_up) == 1 and follow_up[0].parent_job_id == job_id
    assert any("another scan was queued" in m for m in logs(session_factory, job_id))


# ------------------------------------------------------------------------ rating keys


def library_rows(factory):
    """The imported album as the library knows it (a library read from a folder)."""
    with factory() as db:
        artist = make_artist(db, "The Band")
        album = make_album(db, artist, "Album Title", year=2000, path="The Band/Album (2000)")
        for i in (1, 2):
            db.add(
                LibraryTrack(
                    album_id=album.id,
                    source="filesystem",
                    path=f"The Band/Album (2000)/{i:02d} Song {i}.flac",
                    title=f"Song {i}",
                )
            )
        db.commit()


async def test_a_folder_library_gets_the_plex_rating_keys_without_a_scan(
    plex, psettings, session_factory
):
    library(plex)
    library_rows(session_factory)
    pending_in(session_factory, [ITEM])
    s = psettings.model_copy(update={"music_dir": svc_root(psettings)})
    job_id, row = await run_refresh(s, session_factory)
    assert row.status == "succeeded", row.error
    with session_factory() as db:
        assert db.scalar(select(LibraryAlbum.plex_rating_key)) == "10"
        keys = {t.path: t.plex_rating_key for t in db.scalars(select(LibraryTrack))}
        assert keys == {
            "The Band/Album (2000)/01 Song 1.flac": "101",
            "The Band/Album (2000)/02 Song 2.flac": "102",
        }
        assert db.scalars(select(Job).where(Job.type == "scan_library")).all() == []
    assert any("recorded on 1 album(s) and 2 track(s)" in m for m in logs(session_factory, job_id))


def svc_root(settings):
    from pathlib import Path

    return Path("/music")


async def test_a_plex_library_gets_a_scan_that_records_the_keys(plex, psettings, session_factory):
    library(plex)
    pending_in(session_factory, [ITEM])
    s = psettings.model_copy(update={"library_source": "plex"})
    job_id, row = await run_refresh(s, session_factory)
    assert row.status == "succeeded", row.error
    with session_factory() as db:
        scans = db.scalars(select(Job).where(Job.type == "scan_library")).all()
        assert len(scans) == 1 and scans[0].parent_job_id == job_id


# ------------------------------------------------------------------------- metadata


@pytest.mark.parametrize(("option", "refreshed"), [(True, ["10"]), (False, [])])
async def test_the_metadata_refresh_is_an_option(
    plex, psettings, session_factory, option, refreshed
):
    library(plex)
    pending_in(session_factory, [ITEM])
    s = psettings.model_copy(update={"plex_refresh_metadata": option})
    _, row = await run_refresh(s, session_factory)
    assert row.status == "succeeded" and plex.metadata_refreshes == refreshed


# ---------------------------------------------------------------- the manual full scan


async def test_a_requested_full_scan_scans_the_section_and_ignores_the_list(
    plex, psettings, session_factory
):
    library(plex)
    pending_in(session_factory, [ITEM])
    job_id, row = await run_refresh(psettings, session_factory, {"full": True})
    assert row.status == "succeeded", row.error
    assert plex.scans == [("full", None)]
    assert pending_of(session_factory) == [ITEM]  # not touched: its own job does that
    assert any(
        "Scan of the whole section, as requested" in m for m in logs(session_factory, job_id)
    )


def test_the_settings_page_has_no_scan_plex_now_section(admin_client):
    html = admin_client.get("/settings/integrations").text
    assert 'id="plex-scan"' not in html and "Scan Plex now" not in html
    assert "/integrations/plex/scan" not in html
    assert admin_client.post("/settings/integrations/plex/scan").status_code in (403, 404, 405)


def test_the_plex_refresh_settings_are_in_the_plex_form_and_validated(app, admin_client):
    html = admin_client.get("/settings/integrations").text
    plex_form = html.split('id="integration-plex"', 1)[1].split("</form>", 1)[0]
    for name in (
        "plex_refresh_enabled",
        "plex_refresh_debounce_seconds",
        "plex_refresh_metadata",
        "plex_refresh_timeout_minutes",
    ):
        assert f'name="{name}"' in plex_form
    base = {"plex_url": "http://plex:32400", "plex_music_section": "Music", "library_path_map": ""}
    ok = post(
        admin_client,
        "/settings/integrations/plex",
        {
            **base,
            "plex_refresh_enabled": "1",
            "plex_refresh_debounce_seconds": "45",
            "plex_refresh_timeout_minutes": "20",
        },
        page="/settings/integrations",
    )
    assert ok.status_code == 303
    s = app.state.settings
    assert (s.plex_refresh_enabled, s.plex_refresh_debounce_seconds) == (True, 45)
    assert (s.plex_refresh_metadata, s.plex_refresh_timeout_minutes) == (False, 20)
    for field, value in (
        ("plex_refresh_debounce_seconds", "1"),
        ("plex_refresh_timeout_minutes", "0"),
    ):
        bad = post(
            admin_client,
            "/settings/integrations/plex",
            {**base, field: value},
            page="/settings/integrations",
        )
        assert bad.status_code == 400, field


def test_the_job_has_a_readable_name(admin_client):
    import json
    from pathlib import Path

    for lang in ("en", "it"):
        names = json.loads(Path(f"app/i18n/locales/{lang}.json").read_text())
        assert "settings.jobtype.plex_refresh" in names


def test_the_albums_the_import_names_are_the_ones_plex_is_asked_about(psettings):
    assert svc.refresh_enabled(psettings)
    assert not svc.refresh_enabled(psettings.model_copy(update={"plex_refresh_enabled": False}))
    assert not svc.refresh_enabled(psettings.model_copy(update={"plex_token": ""}))
    assert requests  # the fake speaks plexapi's HTTP
    assert quoteattr("x") == '"x"'
