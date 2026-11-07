"""The postprocess job from a finished download to an album in the library."""

import asyncio
import os

import pytest
import respx
from mutagen.flac import FLAC
from sqlalchemy import select

from app.integrations import ratelimit
from app.jobs.worker import JobWorker
from app.models import (
    REQ_COMPLETED,
    REQ_DOWNLOADED,
    REQ_FAILED,
    Artist,
    DownloadRequest,
    Job,
    LibraryAlbum,
    LibraryTrack,
    ReleaseGroup,
    User,
    utcnow,
)
from app.services.importer import library_import as lib
from app.services.musicbrainz.missing import missing_release_groups
from app.services.quality_profiles import load_profiles
from tests.audio import make_flac, make_track
from tests.conftest import MB_BASE
from tests.dbhelpers import make_artist, make_rg
from tests.test_jobs_worker import enqueue, fetch, logs

CAA = "https://coverartarchive.test"
ARTIST_MBID = "33333333-3333-4333-8333-333333333333"
GROUP_MBID = "22222222-2222-4222-8222-222222222222"
RELEASE_MBID = "11111111-1111-4111-8111-111111111111"
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64
TITLES = ["Opening", "Middle Song", "The Closer"]
SETTLED = ("succeeded", "failed")


@pytest.fixture
def env(mbsettings, session_factory, tmp_path):
    music, downloads = tmp_path / "music", tmp_path / "downloads"
    music.mkdir()
    downloads.mkdir()
    settings = mbsettings.model_copy(
        update={
            "music_dir": music,
            "downloads_dir": downloads,
            "coverart_url": CAA,
            "coverart_min_interval": 0.0,
            "providers_covers": "coverartarchive",
            "library_source": "filesystem",
        }
    )
    ratelimit._LIMITERS.clear()
    return settings, session_factory, music, downloads


def release_json(titles=TITLES, *, discs=1, title="Album Title", date="2000-03-04"):
    per_disc = len(titles) // discs
    media = []
    for d in range(discs):
        tracks = []
        for i, name in enumerate(titles[d * per_disc : (d + 1) * per_disc], start=1):
            tracks.append(
                {
                    "position": i,
                    "number": str(i),
                    "title": name,
                    "length": 180000,
                    "recording": {"id": f"5555555{d}-5555-4555-8555-55555555555{i}"},
                    "artist-credit": [
                        {
                            "name": "The Band",
                            "joinphrase": "",
                            "artist": {"id": ARTIST_MBID, "name": "The Band"},
                        }
                    ],
                }
            )
        media.append({"position": d + 1, "tracks": tracks})
    return {
        "releases": [
            {
                "id": RELEASE_MBID,
                "title": title,
                "date": date,
                "country": "US",
                "status": "Official",
                "media": media,
                "artist-credit": [
                    {
                        "name": "The Band",
                        "joinphrase": "",
                        "artist": {"id": ARTIST_MBID, "name": "The Band"},
                    }
                ],
            }
        ]
    }


def seed(
    factory,
    *,
    quality="flac_standard",
    upgrade_from=None,
    source="musicbrainz",
    mbid=None,
    title="Album Title",
    year=2000,
):
    with factory() as db:
        user = User(username="alice", password_hash="x", role="user")
        db.add(user)
        artist = make_artist(db, "The Band", mbid=ARTIST_MBID, status="resolved")
        group = make_rg(db, artist, title, year, mbid=mbid or GROUP_MBID)
        group.source = source
        db.flush()
        request = DownloadRequest(
            user_id=user.id,
            release_group_id=group.id,
            status=REQ_DOWNLOADED,
            quality_key=quality,
            upgrade_from=upgrade_from,
            dest_dir="diskko/1",
        )
        db.add(request)
        db.commit()
        return request.id, group.id, artist.id


def put_download(downloads, files):
    """files: [(relative path, make_track kwargs)] in <downloads>/diskko/1."""
    root = downloads / "diskko" / "1"
    for relative, kwargs in files:
        make_track(root / relative, **kwargs)
    return root


def good_files(ext="flac", **build):
    return [
        (
            f"{i}.{ext}",
            {
                "title": t,
                "track": i + 1,
                "artist": "Spammer",
                "album": "Junk",
                **{f"_{k}": v for k, v in build.items()},
            },
        )
        for i, t in enumerate(TITLES)
    ]


def mock_music(mock, release=None, cover=JPEG):
    mock.get(f"{MB_BASE}/release").respond(json=release or release_json())
    route = mock.get(f"{CAA}/release-group/{GROUP_MBID}/front-500")
    if cover is None:
        route.respond(404)
    else:
        route.respond(200, content=cover, headers={"content-type": "image/jpeg"})
    return route


async def run_job(settings, factory, request_id=1, *, wait=SETTLED):
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), factory)
    await worker.start()
    job_id = enqueue(factory, "postprocess", {"request_id": request_id})
    worker.notify()
    try:
        for _ in range(500):
            job = await asyncio.to_thread(fetch, factory, job_id)
            if job.status in wait:
                return job_id, job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job is {job.status} ({job.error})")
    finally:
        await worker.stop()


def request_of(factory, request_id=1):
    with factory() as db:
        r = db.get(DownloadRequest, request_id)
        db.expunge(r)
        return r


def album_folder(music):
    return music / "The Band" / "Album Title (2000)"


# ------------------------------------------------------------------- the happy path


async def test_a_download_becomes_a_tagged_album_in_the_right_place(env):
    settings, factory, music, downloads = env
    request_id, group_id, artist_id = seed(factory)
    source = put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        job_id, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    folder = album_folder(music)
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Opening.flac",
        "02 - Middle Song.flac",
        "03 - The Closer.flac",
        "cover.jpg",
    ]
    assert (folder / "cover.jpg").read_bytes() == JPEG

    # the tags, read back from the finished files with mutagen
    for number, title in enumerate(TITLES, start=1):
        audio = FLAC(folder / f"{number:02d} - {title}.flac")
        assert audio["title"] == [title] and audio["tracknumber"] == [str(number)]
        assert audio["artist"] == ["The Band"] and audio["albumartist"] == ["The Band"]
        assert audio["album"] == ["Album Title"] and audio["date"] == ["2000-03-04"]
        assert audio["tracktotal"] == ["3"] and audio["discnumber"] == ["1"]
        assert audio["musicbrainz_albumid"] == [RELEASE_MBID]
        assert audio["musicbrainz_releasegroupid"] == [GROUP_MBID]
        assert audio["musicbrainz_artistid"] == [ARTIST_MBID]
        assert audio["musicbrainz_albumartistid"] == [ARTIST_MBID]
        assert audio["musicbrainz_trackid"][0].startswith("5555555")
        assert audio["releasetype"] == ["album"]
        assert "comment" not in audio and "Junk" not in audio["album"]
        (picture,) = audio.pictures
        assert picture.data == JPEG and picture.type == 3

    # the database, the request, the leftovers
    request = request_of(factory)
    assert request.status == REQ_COMPLETED and request.progress == 100.0 and request.finished_at
    assert not source.exists() and not (music / lib.STAGING_DIR).exists()
    with factory() as db:
        album = db.scalar(select(LibraryAlbum))
        assert album.path == "The Band/Album Title (2000)" and album.track_count == 3
        assert (album.release_group_id, album.match_status) == (group_id, "matched")
        assert album.quality_tier == "flac_standard" and album.artist_id == artist_id
        assert db.query(LibraryTrack).count() == 3
        assert db.query(Artist).count() == 1  # the artist of the tags is the one we had
        assert missing_release_groups(db, artist_id, ["album"]) == []  # gone from "missing"
    assert any("Imported into" in m for m in logs(factory, job_id))


async def test_multi_disc_albums_get_the_disc_in_the_file_names(env):
    settings, factory, music, downloads = env
    seed(factory)
    files = [
        (f"CD{1 + i // 2}/{i}.flac", {"title": t, "track": 1 + i % 2, "artist": "x"})
        for i, t in enumerate(["One", "Two", "Three", "Four"])
    ]
    put_download(downloads, files)
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, release_json(["One", "Two", "Three", "Four"], discs=2))
        _, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    names = sorted(p.name for p in album_folder(music).glob("*.flac"))
    assert names == ["1-01 - One.flac", "1-02 - Two.flac", "2-01 - Three.flac", "2-02 - Four.flac"]
    audio = FLAC(album_folder(music) / "2-01 - Three.flac")
    assert (audio["discnumber"], audio["disctotal"], audio["tracktotal"]) == (["2"], ["2"], ["2"])


async def test_the_templates_in_the_settings_decide_the_names(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    s = settings.model_copy(
        update={
            "import_folder_template": "{album} [{year}]",
            "import_file_template": "{track} {title}",
        }
    )
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        _, job = await run_job(s, factory)
    assert job.status == "succeeded", job.error
    assert (music / "Album Title [2000]" / "1 Opening.flac").is_file()


# --------------------------------------------------------------------------- covers


async def test_without_a_cover_archive_cover_the_image_of_the_download_is_used(env):
    settings, factory, music, downloads = env
    seed(factory)
    root = put_download(downloads, good_files())
    (root / "folder.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 40)
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, cover=None)
        job_id, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    folder = album_folder(music)
    assert (folder / "cover.png").is_file()
    (picture,) = FLAC(folder / "01 - Opening.flac").pictures
    assert picture.mime == "image/png"
    assert any("Cover from the download" in m for m in logs(factory, job_id))


async def test_with_no_cover_anywhere_the_album_is_imported_without_one(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, cover=None)
        mock.get(url__regex=r"https://api\.deezer\.com/.*").respond(json={"data": []})
        job_id, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    folder = album_folder(music)
    assert not list(folder.glob("cover.*")) and not FLAC(folder / "01 - Opening.flac").pictures
    assert any("No cover found" in m for m in logs(factory, job_id))


async def test_a_cover_that_is_not_an_image_is_ignored(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, cover=b"<html>not an image</html>")
        mock.get(url__regex=r"https://api\.deezer\.com/.*").respond(json={"data": []})
        _, job = await run_job(settings, factory)
    assert job.status == "succeeded"
    assert not list(album_folder(music).glob("cover.*"))


# ------------------------------------------------------------ rejections: quarantine


def quarantined(downloads, request_id=1):
    return downloads / "diskko" / "quarantine" / str(request_id)


async def assert_rejected(env, expected, *, files, release=None, request=None):
    settings, factory, music, downloads = env
    seed(factory, **(request or {}))
    put_download(downloads, files)
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, release)
        job_id, job = await run_job(settings, factory)
    assert job.status == "failed", logs(factory, job_id)
    row = request_of(factory)
    assert row.status == REQ_FAILED and expected in row.error
    assert "kept in" in row.error and str(quarantined(downloads)) in row.error
    q = quarantined(downloads)
    assert expected in (q / "REASON.txt").read_text()
    assert {p.name for p in q.glob("*.*") if p.name != "REASON.txt"}  # the files are all there
    assert not (downloads / "diskko" / "1").exists()
    assert not any(music.rglob("*.flac")) and not (music / lib.STAGING_DIR).exists()
    with factory() as db:
        assert db.query(LibraryAlbum).count() == 0  # the library was not touched
    return job_id


async def test_a_damaged_file_sends_the_download_to_the_quarantine(env):
    files = good_files()
    files[1] = ("1.flac", {})  # replaced below by garbage
    settings, factory, music, downloads = env
    seed(factory)
    root = put_download(downloads, good_files())
    (root / "1.flac").write_bytes(b"corrupt" * 100)
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        _, job = await run_job(settings, factory)
    assert job.status == "failed"
    row = request_of(factory)
    assert (
        row.status == REQ_FAILED and "damaged or unreadable" in row.error and "1.flac" in row.error
    )
    assert (quarantined(downloads) / "REASON.txt").is_file()


async def test_a_quality_below_the_minimum_is_refused(env):
    await assert_rejected(
        env,
        "below the minimum",
        files=[
            (f"{i}.mp3", {"title": t, "track": i + 1, "_bitrate": 128})
            for i, t in enumerate(TITLES)
        ],
    )


async def test_the_quality_is_judged_on_the_worst_file(env):
    files = good_files()
    files[2] = ("2.mp3", {"title": TITLES[2], "track": 3, "_bitrate": 128})
    await assert_rejected(env, "below the minimum", files=files)


async def test_a_download_that_matches_no_release_is_refused(env):
    await assert_rejected(
        env,
        "no MusicBrainz release matches these 3 files",
        files=good_files(),
        release=release_json([*TITLES, "Bonus Track"]),  # four tracks, three files
    )


async def test_files_that_are_other_tracks_are_refused(env):
    files = [
        (f"{i}.flac", {"title": t, "track": i + 1})
        for i, t in enumerate(["Opening", "Wrong Song", "The Closer"])
    ]
    await assert_rejected(env, "no MusicBrainz release matches", files=files)


async def test_an_existing_folder_is_never_overwritten(env):
    settings, factory, music, downloads = env
    seed(factory)
    existing = album_folder(music)
    existing.mkdir(parents=True)
    (existing / "mine.txt").write_text("keep me")
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        _, job = await run_job(settings, factory)
    assert job.status == "failed"
    row = request_of(factory)
    assert "already exists in the library" in row.error and row.status == REQ_FAILED
    assert [p.name for p in existing.iterdir()] == ["mine.txt"]
    assert (quarantined(downloads) / "0.flac").exists() or list(
        quarantined(downloads).glob("*.flac")
    )
    assert not (music / lib.STAGING_DIR).exists()


async def test_a_download_with_too_few_tracks_for_a_release_less_album_is_refused(env):
    settings, factory, music, downloads = env
    _, group_id, _ = seed(factory, source="deezer", mbid="deezer:77")
    with factory() as db:
        db.get(ReleaseGroup, group_id).track_count_hint = 5
        db.commit()
    put_download(downloads, good_files())
    _, job = await run_job(settings, factory)
    assert job.status == "failed" and "expected at least 5" in request_of(factory).error


async def test_files_that_link_outside_the_download_are_refused(env):
    settings, factory, music, downloads = env
    seed(factory)
    root = put_download(downloads, good_files())
    outside = downloads.parent / "secret.flac"
    make_flac(outside)
    os.symlink(outside, root / "evil.flac")
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        _, job = await run_job(settings, factory)
    assert job.status == "failed" and "links outside" in request_of(factory).error
    assert outside.exists()  # what the link pointed to was not touched


async def test_a_request_whose_folder_is_outside_the_downloads_is_refused(env):
    settings, factory, music, downloads = env
    seed(factory)
    with factory() as db:
        db.get(DownloadRequest, 1).dest_dir = "../.."
        db.commit()
    _, job = await run_job(settings, factory)
    assert job.status == "failed" and "outside the downloads area" in request_of(factory).error


async def test_the_quarantine_folder_can_be_chosen(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, [("a.mp3", {"title": "x", "_bitrate": 128})])
    custom = downloads.parent / "my-quarantine"
    s = settings.model_copy(update={"quarantine_dir": str(custom)})
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        _, job = await run_job(s, factory)
    assert job.status == "failed" and (custom / "1" / "REASON.txt").is_file()


# --------------------------------------------------------------------------- upgrades


def existing_mp3_album(env, quality="mp3_320"):
    settings, factory, music, downloads = env
    folder = album_folder(music)
    for i, t in enumerate(TITLES):
        make_track(
            folder / f"{i + 1:02d} - {t}.mp3",
            title=t,
            track=i + 1,
            artist="The Band",
            album="Album Title",
            albumartist="The Band",
            _bitrate=320,
        )
    profiles = {}
    with factory() as db:
        profiles = load_profiles(db)
    lib.register(factory, music, folder, profiles, match_artist_ids=[1])
    return folder


async def test_an_upgrade_replaces_the_album_and_keeps_the_old_copy_aside(env):
    settings, factory, music, downloads = env
    seed(factory, quality="flac_standard", upgrade_from="mp3_320")
    existing_mp3_album(env)
    with factory() as db:
        assert db.scalar(select(LibraryAlbum.match_status)) == "matched"
        assert db.scalar(select(LibraryAlbum.quality_tier)) == "mp3_320"
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        job_id, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    assert sorted(p.suffix for p in album_folder(music).glob("*.*")) == [".flac"] * 3 + [".jpg"]
    aside = list((music / lib.REPLACED_DIR).glob("1-*/*"))
    assert len(aside) == 1 and sorted(p.suffix for p in aside[0].iterdir()) == [".mp3"] * 3
    with factory() as db:
        albums = db.scalars(select(LibraryAlbum)).all()
        assert [(a.quality_tier, a.match_status) for a in albums] == [("flac_standard", "matched")]
        assert {t.codec for t in db.scalars(select(LibraryTrack))} == {"flac"}  # old tracks gone
    assert any("set aside" in m for m in logs(factory, job_id))
    assert request_of(factory).status == REQ_COMPLETED


async def test_an_upgrade_that_is_not_better_is_refused_and_the_library_is_kept(env):
    settings, factory, music, downloads = env
    seed(factory, quality="mp3_320", upgrade_from="mp3_320")
    old = existing_mp3_album(env)
    put_download(
        downloads,
        [(f"{i}.mp3", {"title": t, "track": i + 1, "_bitrate": 320}) for i, t in enumerate(TITLES)],
    )
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        _, job = await run_job(settings, factory)
    assert job.status == "failed" and "not an upgrade" in request_of(factory).error
    assert len(list(old.glob("*.mp3"))) == 3  # the album we have stays exactly as it was
    assert not (music / lib.REPLACED_DIR).exists()
    with factory() as db:
        assert db.scalar(select(LibraryAlbum.quality_tier)) == "mp3_320"


# --------------------------------------------------- albums without a MusicBrainz release


async def test_an_album_from_deezer_is_tagged_from_the_album_data(env):
    settings, factory, music, downloads = env
    seed(factory, source="deezer", mbid="deezer:77")
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        musicbrainz = mock.get(f"{MB_BASE}/release").respond(json=release_json())
        mock.get(url__regex=r"https://api\.deezer\.com/.*").respond(json={"data": []})
        job_id, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    assert musicbrainz.call_count == 0  # no MusicBrainz release to ask about
    folder = album_folder(music)
    audio = FLAC(folder / "01 - Opening.flac")
    assert audio["album"] == ["Album Title"] and audio["albumartist"] == ["The Band"]
    assert audio["date"] == ["2000"] and audio["tracknumber"] == ["1"]
    assert not [k for k in audio if k.startswith("musicbrainz")]
    with factory() as db:
        album = db.scalar(select(LibraryAlbum))
        assert album.match_status == "matched"  # matched by its title


async def test_a_musicbrainz_group_without_releases_keeps_its_group_id(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, release={"releases": []})
        _, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    audio = FLAC(album_folder(music) / "01 - Opening.flac")
    assert audio["musicbrainz_releasegroupid"] == [GROUP_MBID]
    assert "musicbrainz_albumid" not in audio


# ----------------------------------------------------------- resuming and other modes


async def test_an_interrupted_import_is_finished_without_a_conflict(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        await run_job(settings, factory)
    # the album is in place, but pretend the run died before the database and the cleanup
    with factory() as db:
        for album in db.scalars(select(LibraryAlbum)).all():
            db.delete(album)
        db.get(DownloadRequest, 1).status = REQ_DOWNLOADED
        db.commit()
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        job_id, job = await run_job(settings, factory)
    assert job.status == "succeeded", job.error
    assert any("already in place" in m for m in logs(factory, job_id))
    assert request_of(factory).status == REQ_COMPLETED
    with factory() as db:
        assert db.query(LibraryAlbum).count() == 1 and db.query(LibraryTrack).count() == 3
    assert len(list(album_folder(music).glob("*.flac"))) == 3  # not copied twice


async def test_a_request_that_is_not_waiting_for_import_is_left_alone(env):
    settings, factory, music, downloads = env
    seed(factory)
    with factory() as db:
        db.get(DownloadRequest, 1).status = "cancelled"
        db.commit()
    job_id, job = await run_job(settings, factory)
    assert job.status == "succeeded" and any("Skipped" in m for m in logs(factory, job_id))
    assert request_of(factory).status == "cancelled"


async def test_with_a_plex_library_the_files_are_imported_and_a_scan_is_queued(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    s = settings.model_copy(update={"library_source": "plex"})
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        job_id, job = await run_job(s, factory)
    assert job.status == "succeeded", job.error
    assert len(list(album_folder(music).glob("*.flac"))) == 3
    with factory() as db:
        assert db.query(LibraryAlbum).count() == 0  # the Plex scan will record it
        scans = db.scalars(select(Job).where(Job.type == "scan_library")).all()
        assert len(scans) == 1 and scans[0].parent_job_id == job_id
    assert request_of(factory).status == REQ_COMPLETED


async def test_when_musicbrainz_does_not_answer_the_job_waits_and_nothing_changes(env):
    settings, factory, music, downloads = env
    seed(factory)
    root = put_download(downloads, good_files())
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{MB_BASE}/release").respond(503)
        await worker.start()
        job_id = enqueue(factory, "postprocess", {"request_id": 1})
        worker.notify()
        for _ in range(500):
            if any("Attempt failed" in m for m in logs(factory, job_id)):
                break
            await asyncio.sleep(0.02)
        await worker.stop()
    assert fetch(factory, job_id).status == "queued"  # to be retried
    assert root.exists() and not (music / "The Band").exists()  # nothing moved, nothing lost
    assert request_of(factory).status != REQ_FAILED


# ----------------------------------------------------------------- telling Plex


def with_plex(settings, **update):
    return settings.model_copy(
        update={"plex_url": "http://plex.test:32400", "plex_token": "tok", **update}
    )


async def import_album(env, settings):
    _, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock)
        return await run_job(settings, factory)


async def test_an_import_asks_for_a_plex_scan_of_its_folder_after_the_debounce_time(env):
    settings, factory, music, downloads = env
    job_id, job = await import_album(env, with_plex(settings))
    assert job.status == "succeeded", job.error
    from app.services import plex_refresh as plex_svc

    with factory() as db:
        assert plex_svc.read_pending(db) == [
            plex_svc.Pending(str(album_folder(music)), "Album Title")
        ]
        refresh = db.scalar(select(Job).where(Job.type == "plex_refresh"))
        assert refresh.parent_job_id == job_id and refresh.next_run_at is not None
        assert (refresh.next_run_at - utcnow()).total_seconds() > 20  # after the debounce time
        assert db.scalars(select(Job).where(Job.type == "scan_library")).all() == []
    assert any("Plex will be told" in m for m in logs(factory, job_id))
    assert request_of(factory).status == REQ_COMPLETED


async def test_with_a_plex_library_the_refresh_job_asks_for_the_scan_not_the_import(env):
    settings, factory, music, downloads = env
    _, job = await import_album(env, with_plex(settings, library_source="plex"))
    assert job.status == "succeeded", job.error
    with factory() as db:
        assert db.query(Job).filter(Job.type == "plex_refresh").count() == 1
        assert (
            db.query(Job).filter(Job.type == "scan_library").count() == 0
        )  # the refresh queues it


async def test_with_the_plex_refresh_off_a_plex_library_gets_a_plain_scan(env):
    settings, factory, music, downloads = env
    s = with_plex(settings, library_source="plex", plex_refresh_enabled=False)
    _, job = await import_album(env, s)
    assert job.status == "succeeded"
    with factory() as db:
        assert db.query(Job).filter(Job.type == "plex_refresh").count() == 0
        assert db.query(Job).filter(Job.type == "scan_library").count() == 1


async def test_without_plex_nothing_is_queued_for_it(env):
    settings, factory, music, downloads = env
    _, job = await import_album(env, settings)
    assert job.status == "succeeded"
    with factory() as db:
        assert db.query(Job).filter(Job.type == "plex_refresh").count() == 0


async def test_a_problem_telling_plex_never_fails_the_import(env, monkeypatch):
    from app.services import plex_refresh as plex_svc

    def boom(*args, **kwargs):
        raise RuntimeError("the database said no")

    monkeypatch.setattr(plex_svc, "request_refresh", boom)
    settings, factory, music, downloads = env
    job_id, job = await import_album(env, with_plex(settings))
    assert job.status == "succeeded", job.error
    assert request_of(factory).status == REQ_COMPLETED
    assert len(list(album_folder(music).glob("*.flac"))) == 3
    assert any("Plex could not be notified" in m for m in logs(factory, job_id))


# ------------------------------------------------------------------------- settings


def test_the_import_settings_are_on_the_library_page(app, admin_client):
    from tests.conftest import post

    html = admin_client.get("/settings/library").text
    assert 'id="library-import"' in html
    for name in ("import_folder_template", "import_file_template", "quarantine_dir"):
        assert f'name="{name}"' in html
    assert 'value="{artist}/{album} ({year})"' in html.replace("&#34;", '"').replace("&amp;", "&")
    base = {"music_dir": "/srv/music", "library_source": "filesystem", "library_scan_cron": ""}
    ok = post(
        admin_client,
        "/settings/library",
        {
            **base,
            "import_folder_template": "{artist} - {album}",
            "import_file_template": "{disc}.{track:02d} {title}",
            "quarantine_dir": "/srv/bad",
        },
        page="/settings/library",
    )
    assert ok.status_code == 303
    s = app.state.settings
    assert (s.import_folder_template, s.import_file_template, s.quarantine_dir) == (
        "{artist} - {album}",
        "{disc}.{track:02d} {title}",
        "/srv/bad",
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("import_folder_template", "{artist}/{label}", "template_invalid"),
        ("import_folder_template", "../{album}", "template_invalid"),
        ("import_folder_template", "{artist}", "template_missing"),
        ("import_file_template", "{track:02d}", "template_missing"),
        ("import_file_template", "{title!r}", "template_invalid"),
        ("quarantine_dir", "relative/path", "path_not_absolute"),
    ],
)
def test_the_import_settings_are_validated(app, admin_client, field, value, error):
    from tests.conftest import post

    base = {"music_dir": "/srv/music", "library_source": "filesystem", "library_scan_cron": ""}
    r = post(admin_client, "/settings/library", {**base, field: value}, page="/settings/library")
    assert r.status_code == 400 and f'data-error="{field}"' in r.text
    assert app.state.settings.import_folder_template == "{artist}/{album} ({year})"


def test_an_empty_quarantine_folder_means_the_default_place(app, admin_client):
    from tests.conftest import post

    base = {"music_dir": "/srv/music", "library_source": "filesystem", "library_scan_cron": ""}
    r = post(
        admin_client, "/settings/library", {**base, "quarantine_dir": ""}, page="/settings/library"
    )
    assert r.status_code == 303 and app.state.settings.quarantine_dir == ""


def test_the_musicbrainz_parser_keeps_the_title_and_artist_of_the_release():
    from app.integrations.musicbrainz_client import _parse_release

    release = _parse_release(release_json(title="The Title")["releases"][0])
    assert release.title == "The Title" and release.date == "2000-03-04"
    assert release.credits == [("The Band", ARTIST_MBID, "")]
    assert len(release.tracks) == 3 and release.tracks[0].credits[0][0] == "The Band"
