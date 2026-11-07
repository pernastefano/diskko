"""The album page: tracks, playing owned files, previews of albums not owned, streaming."""

import os
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.integrations import ratelimit
from app.integrations.cached_http import CachedJsonClient
from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    ReleaseData,
    TrackData,
    _parse_release,
)
from app.jobs.worker import JobWorker
from app.models import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    Job,
    LibraryTrack,
    PreviewTrack,
    ReleaseTrack,
    TracklistFetch,
    utcnow,
)
from app.services import albums as svc
from app.services.tracklists import (
    choose_release,
    load_tracks,
    store_tracklist,
    tracklist_state,
)
from tests.conftest import MB_UA, post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.mbdata import ARETHA
from tests.mbdata import load as mb_load
from tests.test_jobs_worker import enqueue, logs, wait_for_status

MB = "https://musicbrainz.test/ws/2"


@pytest.fixture
def settings(settings, tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    return settings.model_copy(
        update={"music_dir": music, "mb_user_agent": MB_UA, "musicbrainz_url": MB}
    )


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(CachedJsonClient, "BACKOFF_BASE", 0)
    ratelimit._LIMITERS.clear()
    yield
    ratelimit._LIMITERS.clear()


def track(title, position=1, disc=1, ms=180_000, mbid=None):
    return TrackData(disc, position, str(position), title, ms, mbid)


def release(mbid, date, titles, country="US"):
    return ReleaseData(
        mbid, date, country, "Official", [track(t, i + 1) for i, t in enumerate(titles)]
    )


def fixture_releases():
    return [_parse_release(r) for r in mb_load("releases_lady_soul.json")["releases"]]


def make_file(settings, relative, data=b"RIFFfake-audio"):
    path = settings.music_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def add_track(db, album, n, *, title=None, path=None, ext="flac", disc=None, tier="flac_standard"):
    t = LibraryTrack(
        album_id=album.id,
        source="filesystem",
        path=path or f"{album.path}/{n:02d} Song {n}.{ext}",
        title=title or f"Song {n}",
        track_no=n,
        disc_no=disc,
        codec=ext,
        duration=200.0,
        quality_tier=tier,
    )
    db.add(t)
    db.flush()
    return t


@pytest.fixture
def owned(db, settings):
    """An album in the library, matched to MusicBrainz, with three files on disk."""
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
    group = make_rg(db, artist, "Lady Soul", 1968)
    album = make_album(db, artist, "Lady Soul", 1968, mbid_rg=group.mbid)
    album.release_group_id, album.match_status, album.quality_tier = (
        group.id,
        "matched",
        "flac_standard",
    )
    album.format = "flac"
    tracks = [add_track(db, album, n) for n in (1, 2, 3)]
    db.commit()
    for t in tracks:
        make_file(settings, t.path)
    return artist, group, album, tracks


@pytest.fixture
def remote(db):
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
    group = make_rg(db, artist, "Lady Soul", 1968)
    return artist, group


def jobs_of(db, type_):
    db.expire_all()
    return db.scalars(select(Job).where(Job.type == type_).order_by(Job.id)).all()


# =========================================================== MusicBrainz track lists


async def test_browse_releases_parses_the_real_answer(settings, session_factory):
    with respx.mock() as mock:
        route = mock.get(f"{MB}/release").respond(json=mb_load("releases_lady_soul.json"))
        async with MusicBrainzClient(settings, session_factory) as client:
            releases = await client.browse_releases("05571372-5b11-39b7-89c5-1c02589a27c8")
            again = await client.browse_releases("05571372-5b11-39b7-89c5-1c02589a27c8")
    assert len(releases) == 15 and releases == again and route.call_count == 1  # cached
    params = route.calls.last.request.url.params
    assert params["release-group"] == "05571372-5b11-39b7-89c5-1c02589a27c8"
    assert params["inc"] == "recordings" and params["status"] == "official"
    first = releases[0]
    assert first.tracks[0].title and first.tracks[0].recording_mbid and first.tracks[0].length_ms


async def test_browse_releases_without_official_ones_asks_for_any(settings, session_factory):
    seen = []

    def answer(request):
        seen.append(dict(request.url.params))
        if "status" in request.url.params:
            return httpx.Response(200, json={"releases": []})
        return httpx.Response(200, json=mb_load("releases_lady_soul.json"))

    with respx.mock() as mock:
        mock.get(f"{MB}/release").mock(side_effect=answer)
        async with MusicBrainzClient(settings, session_factory) as client:
            releases = await client.browse_releases("g")
    assert len(releases) == 15 and "status" in seen[0] and "status" not in seen[1]


async def test_browse_releases_of_an_unknown_group(settings, session_factory):
    with respx.mock() as mock:
        mock.get(f"{MB}/release").respond(404)
        async with MusicBrainzClient(settings, session_factory) as client:
            assert await client.browse_releases("g") is None


def test_parsing_skips_tracks_without_a_title_and_reads_discs():
    parsed = _parse_release(
        {
            "id": "r1",
            "date": "1999",
            "media": [
                {"position": 1, "tracks": [{"position": 1, "title": "A", "length": 1000}]},
                {"position": 2, "tracks": [{"position": 1, "title": "B"}, {"position": 2}]},
            ],
        }
    )
    assert [(t.disc, t.position, t.title, t.length_ms) for t in parsed.tracks] == [
        (1, 1, "A", 1000),
        (2, 1, "B", None),
    ]
    assert _parse_release({"title": "no id"}) is None


# ------------------------------------------------------------------------ choosing


def test_the_usual_number_of_tracks_wins_over_bonus_editions():
    standard = [release(f"s{i}", f"199{i}", [f"T{n}" for n in range(10)]) for i in range(3)]
    deluxe = release("d", "1980", [f"T{n}" for n in range(14)])  # the earliest, but a reissue
    assert choose_release([deluxe, *standard]).mbid == "s0"


def test_among_equals_the_earliest_release_wins_and_partial_dates_count():
    a = release("late", "2005-03-01", ["x", "y"])
    b = release("early", "1968", ["x", "y"])
    c = release("undated", None, ["x", "y"])
    assert choose_release([a, c, b]).mbid == "early"


def test_a_tie_in_counts_prefers_the_shorter_album():
    short, long = release("s", "2000", ["a"] * 3), release("l", "1990", ["a"] * 5)
    assert choose_release([long, short]).mbid == "s"


def test_nothing_to_choose():
    assert choose_release([]) is None
    assert choose_release([ReleaseData("r", None, None, None, [])]) is None


def test_the_real_releases_give_the_us_original_with_ten_tracks():
    chosen = choose_release(fixture_releases())
    assert (chosen.date, chosen.country, len(chosen.tracks)) == ("1968-01-22", "US", 10)
    assert chosen.tracks[0].title == "Chain of Fools"


# ------------------------------------------------------------------------- storing


def test_storing_a_tracklist_replaces_the_old_one_and_sets_the_hint(db, remote):
    _, group = remote
    store_tracklist(db, group.id, release("r1", "1968", ["A", "B", "C"]))
    store_tracklist(db, group.id, release("r2", "1968", ["X", "Y"]))
    db.commit()
    assert [t.title for t in load_tracks(db, group.id)] == ["X", "Y"]
    db.refresh(group)
    assert group.track_count_hint == 2
    fetch = db.get(TracklistFetch, group.id)
    assert (fetch.release_mbid, fetch.track_count) == ("r2", 2)


def test_an_empty_answer_is_remembered_and_keeps_the_hint(db, remote):
    _, group = remote
    group.track_count_hint = 9
    db.commit()
    store_tracklist(db, group.id, None)
    db.commit()
    assert load_tracks(db, group.id) == [] and db.get(TracklistFetch, group.id).track_count == 0
    db.refresh(group)
    assert group.track_count_hint == 9


def test_duplicate_positions_are_stored_once(db, remote):
    _, group = remote
    rel = ReleaseData("r", None, None, None, [track("A", 1), track("A again", 1), track("B", 2)])
    assert store_tracklist(db, group.id, rel) == 2


def test_the_tracklist_state(db, remote):
    _, group = remote
    assert tracklist_state(db, group.id) == "unknown"
    store_tracklist(db, group.id, release("r", None, ["A"]))
    db.commit()
    assert tracklist_state(db, group.id) == "fresh"
    assert tracklist_state(db, group.id, now=lambda: utcnow() + timedelta(days=31)) == "stale"


def test_deleting_an_album_removes_its_tracks(db, remote):
    _, group = remote
    store_tracklist(db, group.id, release("r", None, ["A", "B"]))
    db.commit()
    db.delete(group)
    db.commit()
    assert (
        db.scalars(select(ReleaseTrack)).all() == []
        and db.scalars(select(TracklistFetch)).all() == []
    )


# ------------------------------------------------------------------------ the job


async def test_the_job_fetches_and_stores_the_tracks(settings, session_factory):
    with session_factory() as db:
        artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
        group = make_rg(db, artist, "Lady Soul", 1968)
        group_id = group.id
    s = settings.model_copy(update={"job_poll_interval": 0.02})
    with respx.mock() as mock:
        mock.get(f"{MB}/release").respond(json=mb_load("releases_lady_soul.json"))
        worker = JobWorker(s, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "fetch_tracklist", {"release_group_id": group_id})
        worker.notify()
        job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
        await worker.stop()
    assert job.queue == "musicbrainz"
    with session_factory() as db:
        assert len(load_tracks(db, group_id)) == 10
    assert any("10 track(s) from release" in m for m in logs(session_factory, job_id))


async def test_the_job_without_a_musicbrainz_contact_fails_for_good(settings, session_factory):
    with session_factory() as db:
        group = make_rg(db, make_artist(db, "A"), "T", 2000)
        group_id = group.id
    s = settings.model_copy(update={"mb_user_agent": "", "job_poll_interval": 0.02})
    worker = JobWorker(s, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "fetch_tracklist", {"release_group_id": group_id})
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_FAILED)
    await worker.stop()


async def test_the_job_for_an_unknown_album_fails_for_good(settings, session_factory):
    s = settings.model_copy(update={"job_poll_interval": 0.02})
    worker = JobWorker(s, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "fetch_tracklist", {"release_group_id": 9999})
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_FAILED)
    await worker.stop()


# =================================================================== the rows service


def test_owned_rows_say_what_can_be_played(db, owned):
    _, _, album, _ = owned
    add_track(db, album, 4, ext="ape")
    add_track(db, album, 5, path="plex:track:77")
    add_track(db, album, 6, ext="mp3", disc=2)
    db.commit()
    rows = svc.owned_rows(db, album)
    by_title = {r.title: r for r in rows}
    flac = by_title["Song 1"]
    assert (flac.source, flac.mime, flac.codec, flac.seconds) == ("file", "audio/flac", "flac", 200)
    assert flac.url.startswith("/stream/") and flac.quality == "flac_standard"
    assert (by_title["Song 4"].source, by_title["Song 4"].note) == ("none", "not_playable")
    assert (by_title["Song 5"].source, by_title["Song 5"].note) == ("none", "no_file")
    assert by_title["Song 6"].disc == 2 and by_title["Song 6"].mime == "audio/mpeg"
    assert [r.number for r in rows[:3]] == ["1", "2", "3"]


@pytest.mark.parametrize(
    ("ext", "playable"),
    [
        ("flac", True),
        ("MP3", True),
        ("m4a", True),
        ("ogg", True),
        ("opus", True),
        ("wav", True),
        ("aac", True),
        ("aiff", True),
        ("ape", False),
        ("wma", False),
        ("wv", False),
        ("", False),
    ],
)
def test_which_extensions_are_played(ext, playable):
    assert (svc.media_type_of(ext) is not None) is playable


def test_extension_of():
    assert svc.extension_of("a/b/01 Song.FLAC") == "flac"
    assert svc.extension_of("a.b/noextension") == "" and svc.extension_of("x.mp3") == "mp3"


def preview(db, group, n, title, disc=1):
    p = PreviewTrack(
        release_group_id=group.id, disc=disc, position=n, title=title, url=f"https://p/{n}.m4a"
    )
    db.add(p)
    db.flush()
    return p


def test_previews_are_matched_by_position_and_by_title(db, remote):
    _, group = remote
    store_tracklist(
        db,
        group.id,
        release("r", "1968", ["Chain of Fools", "Money Won’t Change You", "People Get Ready"]),
    )
    tracks = load_tracks(db, group.id)
    previews = [
        preview(db, group, 1, "Chain of Fools"),  # same position and title
        preview(db, group, 7, "People Get Ready"),  # another position, same title
        preview(db, group, 2, "Something Else Entirely"),  # same position, a different song
    ]
    matched = svc.match_previews(tracks, previews)
    assert matched[tracks[0].id].position == 1 and matched[tracks[2].id].position == 7
    assert tracks[1].id not in matched  # no preview of "Money Won’t Change You"
    assert len({p.id for p in matched.values()}) == len(matched)  # a preview is used once


def test_the_apostrophe_style_does_not_matter(db, remote):
    _, group = remote
    store_tracklist(db, group.id, release("r", None, ["Money Won’t Change You"]))
    p = preview(db, group, 9, "Money Won't Change You")
    [t] = load_tracks(db, group.id)
    assert svc.match_previews([t], [p])[t.id] is p


def test_remote_rows_use_the_musicbrainz_list_and_mark_what_has_a_preview(db, remote):
    _, group = remote
    store_tracklist(db, group.id, release("r", None, ["One", "Two"]))
    preview(db, group, 1, "One")
    rows = svc.remote_rows(load_tracks(db, group.id), list(db.scalars(select(PreviewTrack))))
    assert [(r.title, r.source, r.note) for r in rows] == [
        ("One", "preview", None),
        ("Two", "none", "no_preview"),
    ]
    assert rows[0].seconds == 180


def test_without_a_musicbrainz_list_the_previews_are_the_rows(db, remote):
    _, group = remote
    preview(db, group, 2, "Second")
    preview(db, group, 1, "First")
    rows = svc.remote_rows([], list(db.scalars(select(PreviewTrack))))
    assert [r.title for r in rows] == ["First", "Second"] and all(
        r.source == "preview" for r in rows
    )
    assert svc.remote_rows([], []) == []


def test_the_total_length():
    rows = [
        svc.TrackRow(1, "1", "a", 60),
        svc.TrackRow(1, "2", "b", None),
        svc.TrackRow(1, "3", "c", 90),
    ]
    assert svc.total_seconds(rows) == 150


# --------------------------------------------------------------------- file safety


def test_a_library_file_is_found_inside_the_library(tmp_path):
    root = tmp_path / "lib"
    (root / "A").mkdir(parents=True)
    f = root / "A" / "s.flac"
    f.write_bytes(b"x")
    assert svc.resolve_library_file(root, "A/s.flac") == f.resolve()


@pytest.mark.parametrize(
    "relative",
    [
        "../outside.flac",
        "A/../../outside.flac",
        "/etc/hosts",
        "",
        "plex:track:1",
        "A",
        "missing.flac",
    ],
)
def test_paths_that_leave_the_library_or_do_not_exist_are_refused(tmp_path, relative):
    root = tmp_path / "lib"
    (root / "A").mkdir(parents=True)
    (tmp_path / "outside.flac").write_bytes(b"secret")
    assert svc.resolve_library_file(root, relative) is None


def test_a_link_pointing_outside_the_library_is_refused(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (tmp_path / "secret.flac").write_bytes(b"secret")
    os.symlink(tmp_path / "secret.flac", root / "link.flac")
    os.symlink(tmp_path, root / "dirlink")
    assert svc.resolve_library_file(root, "link.flac") is None
    assert svc.resolve_library_file(root, "dirlink/secret.flac") is None


# ======================================================================== the pages


def album_page(client, group_id):
    r = client.get(f"/album/{group_id}")
    assert r.status_code == 200, r.status_code
    return r.text


def test_an_owned_album_shows_its_tracks_and_plays_them_from_disk(admin_client, db, owned, app):
    artist, group, album, tracks = owned
    text = album_page(admin_client, group.id)
    assert "Lady Soul" in text and 'data-ownership="owned"' in text
    assert f'href="/artist/{artist.id}"' in text and "3 tracks" in text
    assert text.count('data-source="file"') == 3 and text.count("<audio") == 3
    for t in tracks:
        assert f'src="/stream/{t.id}"' in text
    assert "FLAC Standard (16/44.1)" in text and "3:20" in text
    assert "data-album-request" not in text and "Request download" not in text
    assert jobs_of(db, "fetch_tracklist") == [] and jobs_of(db, "resolve_previews") == []


def test_an_owned_album_never_asks_anyone_for_anything(admin_client, db, owned):
    _, group, _, _ = owned
    r = post(admin_client, f"/album/{group.id}/tracks", page="/dashboard")
    assert r.status_code == 200 and r.text.count('data-source="file"') == 3
    assert db.scalars(select(Job)).all() == []


def test_owned_tracks_that_cannot_be_played_are_listed_with_the_reason(admin_client, db, owned):
    _, group, album, _ = owned
    add_track(db, album, 4, ext="ape")
    add_track(db, album, 5, path="plex:track:5")
    db.commit()
    text = album_page(admin_client, group.id)
    assert text.count('data-track="') == 5 and text.count("<audio") == 3
    assert 'data-source="none"' in text


def test_multi_disc_albums_are_grouped_by_disc(admin_client, db, owned):
    _, group, album, _ = owned
    add_track(db, album, 1, title="Second disc", disc=2, path="x/d2.flac")
    db.commit()
    text = album_page(admin_client, group.id)
    assert "Disc 1" in text and "Disc 2" in text and "2 discs" in text


def test_an_album_that_is_not_owned_shows_the_musicbrainz_tracks_and_a_request_button(
    admin_client, db, remote
):
    artist, group = remote
    store_tracklist(db, group.id, release("r", "1968", ["Chain of Fools", "People Get Ready"]))
    db.commit()
    text = album_page(admin_client, group.id)
    assert 'data-ownership="remote"' in text and "Not in your library" in text
    assert "Chain of Fools" in text and "People Get Ready" in text and "2 tracks" in text
    assert (
        f'hx-post="/artist/{artist.id}/request/{group.id}"' in text and "Request download" in text
    )
    assert text.count("<audio") == 0
    assert f"musicbrainz.org/release-group/{group.mbid}" in text


def test_the_page_itself_does_not_queue_anything_but_it_asks_for_the_rest(admin_client, db, remote):
    _, group = remote
    text = album_page(admin_client, group.id)
    assert jobs_of(db, "fetch_tracklist") == [] and jobs_of(db, "resolve_previews") == []
    assert f'hx-post="/album/{group.id}/tracks"' in text and 'hx-trigger="load"' in text


def test_opening_the_page_queues_the_tracklist_and_the_previews_once(admin_client, db, remote):
    _, group = remote
    first = post(admin_client, f"/album/{group.id}/tracks", page="/dashboard")
    post(admin_client, f"/album/{group.id}/tracks", page="/dashboard")
    assert "data-loading" in first.text and 'hx-trigger="every 2s"' in first.text
    assert len(jobs_of(db, "fetch_tracklist")) == 1 and len(jobs_of(db, "resolve_previews")) == 1
    assert jobs_of(db, "fetch_tracklist")[0].queue == "musicbrainz"
    poll = admin_client.get(f"/album/{group.id}/tracks")
    assert poll.status_code == 200 and len(jobs_of(db, "fetch_tracklist")) == 1  # polling is free


def test_without_a_musicbrainz_contact_only_the_previews_are_asked(
    app, settings, admin_client, db, remote
):
    _, group = remote
    app.state.settings = settings.model_copy(update={"mb_user_agent": ""})
    post(admin_client, f"/album/{group.id}/tracks", page="/dashboard")
    assert jobs_of(db, "fetch_tracklist") == [] and len(jobs_of(db, "resolve_previews")) == 1


def test_an_old_tracklist_is_shown_while_it_is_refreshed(admin_client, db, remote):
    _, group = remote
    store_tracklist(db, group.id, release("r", None, ["Old Song"]))
    db.get(TracklistFetch, group.id).fetched_at = utcnow() - timedelta(days=40)
    db.commit()
    text = post(admin_client, f"/album/{group.id}/tracks", page="/dashboard").text
    assert "Old Song" in text
    assert len(jobs_of(db, "fetch_tracklist")) == 1


def test_a_fresh_tracklist_is_not_asked_again(admin_client, db, remote):
    _, group = remote
    store_tracklist(db, group.id, release("r", None, ["Song"]))
    db.commit()
    post(admin_client, f"/album/{group.id}/tracks", page="/dashboard")
    assert jobs_of(db, "fetch_tracklist") == []


def test_a_failed_tracklist_job_is_reported_not_retried_in_a_loop(admin_client, db, remote):
    _, group = remote
    post(admin_client, f"/album/{group.id}/tracks", page="/dashboard")
    for job in jobs_of(db, "fetch_tracklist") + jobs_of(db, "resolve_previews"):
        job.status, job.finished_at = STATUS_FAILED, utcnow()
    db.commit()
    text = post(admin_client, f"/album/{group.id}/tracks", page="/dashboard").text
    assert "data-failed" in text and "data-loading" not in text
    assert len(jobs_of(db, "fetch_tracklist")) == 1


def test_an_album_with_no_known_tracks_says_so(admin_client, db, remote):
    _, group = remote
    store_tracklist(db, group.id, None)
    from app.services.previews import store_previews

    store_previews(db, group.id, None, None, [])
    db.commit()
    text = post(admin_client, f"/album/{group.id}/tracks", page="/dashboard").text
    assert "data-no-tracks" in text and "<audio" not in text


def test_an_unmatched_library_album_has_a_local_page(admin_client, db, settings):
    artist = make_artist(db, "Home Band")
    album = make_album(db, artist, "Home Recording", 2001)
    t = add_track(db, album, 1, ext="mp3", tier="mp3_320")
    db.commit()
    make_file(settings, t.path)
    r = admin_client.get(f"/album/local/{album.id}")
    assert r.status_code == 200
    assert "Home Recording" in r.text and 'data-ownership="owned"' in r.text
    assert "not matched to MusicBrainz" in r.text and f'src="/stream/{t.id}"' in r.text
    assert "data-album-request" not in r.text and "musicbrainz.org/release-group" not in r.text
    assert db.scalars(select(Job)).all() == []


def test_the_local_page_of_a_matched_album_goes_to_the_album_page(admin_client, owned):
    _, group, album, _ = owned
    r = admin_client.get(f"/album/local/{album.id}")
    assert r.status_code == 303 and r.headers["location"] == f"/album/{group.id}"


def test_unknown_albums_are_404(admin_client):
    assert admin_client.get("/album/9999").status_code == 404
    assert admin_client.get("/album/local/9999").status_code == 404


def test_the_album_pages_need_a_login(admin_client, new_client, remote):
    _, group = remote
    anon = new_client()
    for path in (f"/album/{group.id}", f"/album/{group.id}/tracks", "/album/local/1", "/stream/1"):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_ordinary_users_can_open_albums_and_play(user_client, owned):
    _, group, _, tracks = owned
    assert "Lady Soul" in album_page(user_client, group.id)
    assert user_client.get(f"/stream/{tracks[0].id}").status_code == 200


def test_the_album_page_follows_the_language(admin_client, owned):
    _, group, _, _ = owned
    post(admin_client, "/lang", {"lang": "it", "next": "/dashboard"})
    text = album_page(admin_client, group.id)
    assert "Nella tua libreria" in text and "Tracce" in text and "3 tracce" in text


def test_requesting_from_the_album_page_uses_the_normal_request(
    app, settings, admin_client, db, remote
):
    artist, group = remote
    app.state.settings = settings.model_copy(
        update={"slskd_url": "http://slskd.test:5030", "slskd_api_key": "k" * 20}
    )
    r = post(
        admin_client,
        f"/artist/{artist.id}/request/{group.id}",
        page=f"/album/{group.id}",
        headers={"HX-Request": "true"},
    )
    assert 'data-request-status="queued"' in r.text
    assert 'data-request-status="queued"' in album_page(admin_client, group.id)


def test_a_cover_is_shown_big_when_there_is_one(admin_client, db, remote):
    _, group = remote
    group.cover_path, group.cover_status = f"{group.mbid}.jpg", "ok"
    db.commit()
    assert f'src="/covers/{group.mbid}.jpg"' in album_page(admin_client, group.id)


# =========================================================================== streaming


def test_a_track_is_streamed_with_the_right_type(admin_client, owned):
    _, _, _, tracks = owned
    r = admin_client.get(f"/stream/{tracks[0].id}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/flac"
    assert r.content == b"RIFFfake-audio" and r.headers["cache-control"].startswith("private")
    assert r.headers.get("accept-ranges") == "bytes"


def test_seeking_works_through_range_requests(admin_client, settings, db, owned):
    _, _, album, _ = owned
    t = add_track(db, album, 9, path="Aretha/Big.flac")
    db.commit()
    make_file(settings, t.path, bytes(range(256)) * 4)
    r = admin_client.get(f"/stream/{t.id}", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == bytes(range(10, 20))
    assert r.headers["content-range"] == "bytes 10-19/1024"


def test_every_playable_format_has_its_own_type(admin_client, settings, db, owned):
    _, _, album, _ = owned
    seen = {}
    for ext, mime in (
        ("mp3", "audio/mpeg"),
        ("m4a", "audio/mp4"),
        ("ogg", "audio/ogg"),
        ("wav", "audio/wav"),
    ):
        t = add_track(db, album, 20, ext=ext, path=f"Fmt/song.{ext}")
        db.commit()
        make_file(settings, t.path)
        seen[ext] = (admin_client.get(f"/stream/{t.id}").headers["content-type"], mime)
    assert all(got == want for got, want in seen.values())


def test_a_format_the_browser_cannot_play_is_refused(admin_client, settings, db, owned):
    _, _, album, _ = owned
    t = add_track(db, album, 30, ext="ape", path="X/song.ape")
    db.commit()
    make_file(settings, t.path)
    assert admin_client.get(f"/stream/{t.id}").status_code == 415


def test_unknown_missing_and_plex_tracks_are_404(admin_client, db, owned):
    _, _, album, _ = owned
    gone = add_track(db, album, 40, path="not/there.flac")
    plex = add_track(db, album, 41, path="plex:track:41")
    db.commit()
    for track_id in (999999, gone.id, plex.id):
        assert admin_client.get(f"/stream/{track_id}").status_code == 404


def test_a_track_whose_path_points_outside_the_library_is_never_served(
    admin_client, settings, db, owned, tmp_path
):
    _, _, album, _ = owned
    secret = tmp_path / "secret.flac"
    secret.write_bytes(b"do not serve")
    bad = [
        add_track(db, album, 50, path="../secret.flac"),
        add_track(db, album, 51, path=str(secret)),
    ]
    os.symlink(secret, settings.music_dir / "sneaky.flac")
    bad.append(add_track(db, album, 52, path="sneaky.flac"))
    db.commit()
    for t in bad:
        r = admin_client.get(f"/stream/{t.id}")
        assert r.status_code == 404 and b"do not serve" not in r.content


def test_streaming_is_addressed_by_track_id_only(admin_client):
    assert admin_client.get("/stream/../../etc/passwd").status_code in (404, 422)
    assert admin_client.get("/stream/abc").status_code == 422


# ====================================================================== the cards


def artist_page(client, artist_id, extra=""):
    r = client.get(f"/artist/{artist_id}{extra}")
    assert r.status_code == 200
    return r.text


def test_cards_have_no_preview_button_and_link_to_the_album(admin_client, db, owned):
    artist, group, album, _ = owned
    other = make_rg(db, artist, "I Never Loved a Man", 1967)
    unmatched = make_album(db, artist, "Home Tape", 1999)
    db.commit()
    text = artist_page(admin_client, artist.id)
    assert "data-preview-button" not in text and "/previews/" not in text and "Preview" not in text
    assert f'href="/album/{group.id}"' in text  # the owned, matched album
    assert f'href="/album/{other.id}"' in text  # a missing album
    assert f'href="/album/local/{unmatched.id}"' in text  # an owned album without a match
    assert text.count(f'href="/album/{other.id}"') == 2  # the cover and the title
    assert text.count(f'href="/album/{group.id}"') == 2


def test_old_preview_routes_are_gone(admin_client, remote):
    _, group = remote
    assert post(admin_client, f"/previews/{group.id}", page="/dashboard").status_code in (404, 405)
    assert admin_client.get(f"/previews/{group.id}").status_code in (404, 405)
