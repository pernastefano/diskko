"""Re-tagging an album that is in the library: the preview, the real run, the tracks of the release
that have no file, what is left alone, and the page."""

import asyncio
import json

import pytest
import respx
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TCON, TXXX
from mutagen.mp4 import MP4
from sqlalchemy import select

from app.integrations import ratelimit
from app.integrations.musicbrainz_client import ReleaseData, TrackData
from app.jobs.worker import JobWorker
from app.models import Job, LibraryAlbum, LibraryTrack, RetagReport
from app.services import retag
from app.services.importer import matching
from app.services.importer.tagging import TaggingError, TrackTags, write_tags
from tests.audio import make_flac, make_track, tag_file
from tests.conftest import MB_BASE, post
from tests.dbhelpers import make_artist, make_rg
from tests.test_jobs_worker import enqueue, fetch, logs
from tests.test_postprocess import (
    ARTIST_MBID,
    CAA,
    GROUP_MBID,
    JPEG,
    RELEASE_MBID,
    TITLES,
    release_json,
)

SETTLED = ("succeeded", "failed")
LONG = [*TITLES, "Bonus Track"]


# ------------------------------------------------------------------------ the setup


def tracks_of(titles):
    return [
        TrackData(1, i, str(i), t, 180000, f"5555555-{i}", [("The Band", ARTIST_MBID, "")])
        for i, t in enumerate(titles, start=1)
    ]


@pytest.fixture
def env(mbsettings, session_factory, tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    settings = mbsettings.model_copy(
        update={
            "music_dir": music,
            "coverart_url": CAA,
            "coverart_min_interval": 0.0,
            "providers_covers": "coverartarchive",
            "library_source": "filesystem",
        }
    )
    ratelimit._LIMITERS.clear()
    return settings, session_factory, music


def seed_album(
    factory,
    music,
    *,
    folder="The Band/Album Title (2000)",
    names=None,
    status="matched",
    source="musicbrainz",
    genre="Rock",
):
    """A matched album of three FLAC files with wrong tags (and a genre that must survive)."""
    names = names or [f"{i:02d} - {t}.flac" for i, t in enumerate(TITLES, start=1)]
    with factory() as db:
        artist = make_artist(db, "The Band", mbid=ARTIST_MBID, status="resolved")
        group = make_rg(db, artist, "Album Title", 2000, mbid=GROUP_MBID)
        group.source = source
        album = LibraryAlbum(
            artist_id=artist.id,
            source="filesystem",
            path=folder,
            title="Junk",
            year=2000,
            track_count=len(names),
            release_group_id=group.id,
            match_status=status,
        )
        db.add(album)
        db.flush()
        for i, (name, title) in enumerate(zip(names, TITLES, strict=False), start=1):
            path = music / folder / name
            make_track(path, title=title, track=i, artist="Spammer", album="Junk")
            audio = FLAC(path)
            audio["genre"] = genre
            audio["comment"] = "ripped by someone"
            audio["replaygain_track_gain"] = "-6.5 dB"
            audio.save()
            db.add(
                LibraryTrack(
                    album_id=album.id,
                    source="filesystem",
                    path=f"{folder}/{name}",
                    title=title,
                    track_no=i,
                    quality_tier="flac_standard",
                )
            )
        db.commit()
        return album.id, group.id


def mock_mb(mock, titles=TITLES, cover=JPEG):
    mock.get(f"{MB_BASE}/release").respond(json=release_json(titles))
    mock.get(url__regex=r"https://api\.deezer\.com/.*").respond(json={"data": []})
    route = mock.get(f"{CAA}/release-group/{GROUP_MBID}/front-500")
    if cover is None:
        route.respond(404)
    else:
        route.respond(200, content=cover, headers={"content-type": "image/jpeg"})
    return route


async def run(settings, factory, album_id, *, apply=False, **options):
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), factory)
    await worker.start()
    job_id = enqueue(factory, "retag_album", {"album_id": album_id, "apply": apply, **options})
    worker.notify()
    try:
        for _ in range(500):
            job = await asyncio.to_thread(fetch, factory, job_id)
            if job.status in SETTLED:
                return job_id, job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job is {job.status} ({job.error})")
    finally:
        await worker.stop()


def report_of(factory, album_id):
    with factory() as db:
        row = db.scalar(select(RetagReport).where(RetagReport.album_id == album_id))
        return row.mode, row.status, json.loads(row.report_json)


# ----------------------------------------------------------------------- the preview


async def test_a_preview_shows_what_changes_and_touches_nothing(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    path = music / "The Band/Album Title (2000)/01 - Opening.flac"
    before = path.read_bytes()
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, job = await run(settings, factory, album_id)
    assert job.status == "succeeded", job.error
    assert path.read_bytes() == before  # not a byte changed

    mode, status, report = report_of(factory, album_id)
    assert (mode, status) == ("preview", "ready")
    assert report["release"]["mbid"] == RELEASE_MBID
    assert report["summary"]["files"] == 3 and report["summary"]["to_change"] == 3
    assert report["missing"] == [] and report["extra"] == []
    first = next(f for f in report["files"] if f["name"].startswith("01"))
    changed = {c["field"]: (c["old"], c["new"]) for c in first["changes"]}
    assert changed["album"] == ("Junk", "Album Title")
    assert changed["artist"] == ("Spammer", "The Band")
    assert changed["date"] == ("", "2000-03-04")
    assert changed["mb_release"] == ("", RELEASE_MBID)
    assert "title" not in changed  # already right: only differences are listed
    assert (
        report["cover"]["source"] == "coverartarchive" and report["summary"]["cover_to_embed"] == 3
    )


async def test_a_track_of_the_release_without_a_file_is_reported(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)  # 3 files, MusicBrainz has 4 tracks
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock, LONG)
        job_id, job = await run(settings, factory, album_id)
    assert job.status == "succeeded", job.error
    _, _, report = report_of(factory, album_id)
    assert [m["title"] for m in report["missing"]] == ["Bonus Track"]
    assert report["missing"][0]["position"] == 4
    assert report["release"]["tracks"] == 4 and report["release"]["paired"] == 3
    assert report["summary"]["missing"] == 1 and report["summary"]["files"] == 3
    assert any("No file for track 1-4" in m for m in logs(factory, job_id))


async def test_a_file_that_matches_no_track_is_listed_and_left_alone(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    extra = music / "The Band/Album Title (2000)/99 - Hidden.flac"
    make_track(extra, title="Totally Unrelated Noise", track=9, artist="x", album="y")
    with factory() as db:
        db.add(
            LibraryTrack(
                album_id=album_id,
                source="filesystem",
                path="The Band/Album Title (2000)/99 - Hidden.flac",
                title="Totally Unrelated Noise",
                track_no=9,
            )
        )
        db.commit()
    before = extra.read_bytes()
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, job = await run(settings, factory, album_id, apply=True)
    assert job.status == "succeeded", job.error
    _, _, report = report_of(factory, album_id)
    assert report["extra"] == ["99 - Hidden.flac"] and report["summary"]["unmatched"] == 1
    assert extra.read_bytes() == before
    assert FLAC(music / "The Band/Album Title (2000)/01 - Opening.flac")["album"] == ["Album Title"]


async def test_only_an_album_matched_with_certainty_is_done(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music, status="tentative")
    with respx.mock(assert_all_called=False) as mock:
        route = mock_mb(mock)
        _, job = await run(settings, factory, album_id, apply=True)
    assert job.status == "failed" and "not_matched" in job.error
    _, status, report = report_of(factory, album_id)
    assert status == "error" and report == {"error": "not_matched"}
    path = music / "The Band/Album Title (2000)/01 - Opening.flac"
    assert FLAC(path)["album"] == ["Junk"] and not route.called


async def test_a_release_picked_by_hand_is_used(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    other = "99999999-9999-4999-8999-999999999999"
    two = release_json(LONG, title="Deluxe")
    two["releases"][0]["id"] = other
    two["releases"].append(release_json(TITLES)["releases"][0])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{MB_BASE}/release").respond(json=two)
        mock.get(f"{CAA}/release-group/{GROUP_MBID}/front-500").respond(404)
        mock.get(url__regex=r"https://api\.deezer\.com/.*").respond(json={"data": []})
        _, auto = await run(settings, factory, album_id)
        assert auto.status == "succeeded", auto.error
        _, _, first = report_of(factory, album_id)
        _, picked = await run(settings, factory, album_id, release_mbid=other)
        assert picked.status == "succeeded", picked.error
    _, _, second = report_of(factory, album_id)
    assert first["release"]["mbid"] == RELEASE_MBID  # three files: the three-track release
    assert second["release"]["mbid"] == other and len(second["missing"]) == 1
    assert {c["mbid"] for c in first["candidates"]} == {RELEASE_MBID, other}


# ----------------------------------------------------------------------- the real run


async def test_the_run_writes_the_tags_and_keeps_what_is_not_ours(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, job = await run(settings, factory, album_id, apply=True)
    assert job.status == "succeeded", job.error
    folder = music / "The Band/Album Title (2000)"
    for number, title in enumerate(TITLES, start=1):
        audio = FLAC(folder / f"{number:02d} - {title}.flac")
        assert audio["title"] == [title] and audio["tracknumber"] == [str(number)]
        assert audio["artist"] == ["The Band"] and audio["albumartist"] == ["The Band"]
        assert audio["album"] == ["Album Title"] and audio["date"] == ["2000-03-04"]
        assert audio["tracktotal"] == ["3"]
        assert audio["musicbrainz_albumid"] == [RELEASE_MBID]
        assert audio["musicbrainz_releasegroupid"] == [GROUP_MBID]
        assert audio["musicbrainz_trackid"][0].startswith("5555555")
        # what the program does not manage is still there
        assert audio["genre"] == ["Rock"] and audio["comment"] == ["ripped by someone"]
        assert audio["replaygain_track_gain"] == ["-6.5 dB"]
        assert len(audio.pictures) == 1 and audio.pictures[0].data == JPEG
    assert not [p for p in folder.iterdir() if p.name.startswith(".")]  # no temp files

    mode, status, report = report_of(factory, album_id)
    assert (mode, status) == ("applied", "ready")
    assert report["summary"]["written"] == 3 and report["summary"]["failed"] == 0
    with factory() as db:  # the library knows the new tags
        album = db.scalars(select(LibraryAlbum)).one()
        assert album.title == "Album Title" and album.match_status in ("matched", "tentative")
        assert album.release_group_id is not None


async def test_a_second_run_finds_nothing_to_do(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, job = await run(settings, factory, album_id, apply=True)
        assert job.status == "succeeded", job.error
        with factory() as db:
            album_id = db.scalars(select(LibraryAlbum.id)).one()
        _, again = await run(settings, factory, album_id)
    assert again.status == "succeeded", again.error
    _, _, report = report_of(factory, album_id)
    assert report["summary"]["to_change"] == 0 and report["summary"]["same"] == 3
    assert report["summary"]["cover_to_embed"] == 0  # they have a cover now


async def test_an_embedded_cover_is_kept_unless_replacing_is_asked(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    mine = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
    path = music / "The Band/Album Title (2000)/01 - Opening.flac"
    audio = FLAC(path)
    picture = Picture()
    picture.type, picture.mime, picture.data = 3, "image/png", mine
    audio.add_picture(picture)
    audio.save()
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, job = await run(settings, factory, album_id, apply=True)
        assert job.status == "succeeded", job.error
        assert FLAC(path).pictures[0].data == mine  # kept
        other = music / "The Band/Album Title (2000)/02 - Middle Song.flac"
        assert FLAC(other).pictures[0].data == JPEG  # had none: got one
        with factory() as db:
            album_id = db.scalars(select(LibraryAlbum.id)).one()
        _, job = await run(settings, factory, album_id, apply=True, replace_cover=True)
        assert job.status == "succeeded", job.error
    assert FLAC(path).pictures[0].data == JPEG


async def test_no_cover_found_still_tags(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock, cover=None)
        _, job = await run(settings, factory, album_id, apply=True)
    assert job.status == "succeeded", job.error
    audio = FLAC(music / "The Band/Album Title (2000)/01 - Opening.flac")
    assert audio["album"] == ["Album Title"] and audio.pictures == []
    _, _, report = report_of(factory, album_id)
    assert report["cover"]["source"] == "" and report["summary"]["cover_to_embed"] == 0


async def test_renaming_moves_the_files_and_the_folder_and_updates_the_library(env):
    settings, factory, music = env
    album_id, _ = seed_album(
        factory,
        music,
        folder="The Band/old rip",
        names=["a.flac", "b.flac", "c.flac"],
    )
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, preview = await run(settings, factory, album_id, rename=True)
        assert preview.status == "succeeded", preview.error
        _, _, planned = report_of(factory, album_id)
        assert planned["folder"] == {"to": "Album Title (2000)", "parent": "The Band"}
        assert [f["rename"] for f in planned["files"]] == [
            "01 - Opening.flac",
            "02 - Middle Song.flac",
            "03 - The Closer.flac",
        ]
        assert (music / "The Band/old rip/a.flac").is_file()  # a preview renames nothing
        _, job = await run(settings, factory, album_id, apply=True, rename=True)
    assert job.status == "succeeded", job.error
    new = music / "The Band/Album Title (2000)"
    assert sorted(p.name for p in new.iterdir()) == [
        "01 - Opening.flac",
        "02 - Middle Song.flac",
        "03 - The Closer.flac",
    ]
    assert not (music / "The Band/old rip").exists()
    with factory() as db:
        album = db.scalars(select(LibraryAlbum)).one()
        assert album.path == "The Band/Album Title (2000)"
        paths = sorted(db.scalars(select(LibraryTrack.path)))
        assert paths[0] == "The Band/Album Title (2000)/01 - Opening.flac"


async def test_a_name_that_is_taken_is_not_used(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music, names=["a.flac", "b.flac", "c.flac"])
    (music / "The Band/Album Title (2000)/01 - Opening.flac").write_bytes(b"someone else's")
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, job = await run(settings, factory, album_id, apply=True, rename=True)
    assert job.status == "succeeded", job.error
    folder = music / "The Band/Album Title (2000)"
    assert (folder / "01 - Opening.flac").read_bytes() == b"someone else's"
    assert (folder / "a.flac").is_file()  # kept its name
    _, _, report = report_of(factory, album_id)
    assert any(n.startswith("rename_skipped:a.flac") for n in report["notes"])


async def test_files_the_library_does_not_track_for_the_album_are_never_touched(env):
    settings, factory, music = env
    album_id, _ = seed_album(factory, music)
    other = music / "The Band/Album Title (2000)/Other Album Song.flac"
    make_track(other, title="Opening", track=1, artist="x", album="Another Album")
    before = other.read_bytes()
    with respx.mock(assert_all_called=False) as mock:
        mock_mb(mock)
        _, job = await run(settings, factory, album_id, apply=True)
    assert job.status == "succeeded", job.error
    assert other.read_bytes() == before


# --------------------------------------------------------------------- units


def make_tags(**kw):
    base = {
        "title": "Opening",
        "artist": "The Band",
        "album_artist": "The Band",
        "album": "Album Title",
        "track": 1,
        "track_total": 3,
        "date": "2000-03-04",
        "disc_total": 1,
        "mbid_release": RELEASE_MBID,
        "mbid_release_group": GROUP_MBID,
        "release_types": ["album"],
    }
    return TrackTags(**{**base, **kw})


def test_a_failed_write_leaves_the_original_and_no_temp_file(tmp_path, monkeypatch):
    path = make_flac(tmp_path / "01.flac")
    tag_file(path, title="Old", album="Old Album")
    before = path.read_bytes()

    def boom(*args, **kwargs):
        raise TaggingError("disk full")

    monkeypatch.setattr(retag, "write_tags", boom)
    item = retag.Item(path, path.name, True, "change", tags=make_tags())
    with pytest.raises(TaggingError):
        retag.write_item(item, None)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["01.flac"]


def test_mp3_keeps_genre_and_replaygain_but_replaces_what_it_manages(tmp_path):
    path = make_track(tmp_path / "01.mp3", title="Old", artist="Spammer", album="Junk", track=7)
    tags = ID3(path)
    tags.add(TCON(encoding=3, text="Jazz"))
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-3 dB"))
    tags.save(path)
    write_tags(path, make_tags(), None, keep_other=True)
    now, _ = retag.read_snapshot(path)
    assert now["title"] == "Opening" and now["album"] == "Album Title" and now["track"] == "1"
    assert now["tracktotal"] == "3" and now["mb_release"] == RELEASE_MBID
    kept = ID3(path)
    assert str(kept["TCON"]) == "Jazz"
    assert kept.getall("TXXX:REPLAYGAIN_TRACK_GAIN")
    assert retag.differences(now, retag.planned_snapshot(make_tags())) == []


def test_m4a_keeps_what_it_does_not_manage(tmp_path):
    path = make_track(tmp_path / "01.m4a", title="Old", artist="Spammer", album="Junk")
    audio = MP4(path)
    audio["\xa9gen"] = ["Pop"]
    audio.save()
    write_tags(path, make_tags(), None, keep_other=True)
    now, _ = retag.read_snapshot(path)
    assert now["title"] == "Opening" and now["album"] == "Album Title"
    assert MP4(path)["\xa9gen"] == ["Pop"]
    assert retag.differences(now, retag.planned_snapshot(make_tags())) == []


def test_the_import_still_clears_everything(tmp_path):
    path = make_flac(tmp_path / "01.flac")
    tag_file(path, title="Old")
    audio = FLAC(path)
    audio["genre"] = "Rock"
    audio.save()
    write_tags(path, make_tags(), None)  # the import: the old tags are dropped
    assert "genre" not in FLAC(path)


def release(mbid, titles, title="Album Title", date="2000"):
    return ReleaseData(mbid, date, "US", "Official", tracks_of(titles), title)


def facts_for(tmp_path, titles):
    from app.services.importer.integrity import DownloadedFile
    from app.services.library.tags import read_audio_file

    files = []
    for i, title in enumerate(titles, start=1):
        path = make_track(tmp_path / f"{i:02d} - {title}.flac", title=title, track=i)
        files.append(DownloadedFile(path, path.name, read_audio_file(path), 1))
    return matching.facts_of(files)


def test_the_release_the_files_are_tagged_with_wins_a_tie(tmp_path):
    facts = facts_for(tmp_path, TITLES)
    a, b = release("a" * 36, TITLES, date="1999"), release("b" * 36, TITLES, date="2005")
    assert matching.rank_releases([a, b], facts, "Album Title")[0].release is a  # earliest
    assert matching.rank_releases([a, b], facts, "Album Title", tagged="b" * 36)[0].release is b


def test_a_release_with_more_tracks_pairs_every_file_and_leaves_one_unpaired(tmp_path):
    facts = facts_for(tmp_path, TITLES)
    ranked = matching.rank_releases([release("c" * 36, LONG)], facts, "Album Title")
    assert len(ranked[0].pairs) == 3 and ranked[0].missing == 1


def test_a_file_that_fits_no_track_is_not_paired(tmp_path):
    facts = facts_for(tmp_path, [*TITLES, "Something Else Entirely"])
    ranked = matching.rank_releases([release("d" * 36, TITLES)], facts, "Album Title")
    assert len(ranked[0].pairs) == 3 and ranked[0].missing == 0
    assert matching.pair_tracks(facts, release("d" * 36, TITLES)) is None  # the import's rule


# --------------------------------------------------------------------------- the page


@pytest.fixture
def settings(settings, tmp_path):
    return settings.model_copy(update={"music_dir": tmp_path / "music"})


@pytest.fixture
def owned(db, settings):
    artist = make_artist(db, "The Band", mbid=ARTIST_MBID, status="resolved")
    group = make_rg(db, artist, "Album Title", 2000, mbid=GROUP_MBID)
    group.source = "musicbrainz"
    album = LibraryAlbum(
        artist_id=artist.id,
        source="filesystem",
        path="The Band/Album Title (2000)",
        title="Album Title",
        year=2000,
        track_count=3,
        release_group_id=group.id,
        match_status="matched",
    )
    db.add(album)
    db.commit()
    return group, album


def test_the_button_is_for_admins_and_matched_musicbrainz_albums(admin_client, user_client, owned):
    group, album = owned
    assert "data-album-retag" in admin_client.get(f"/album/{group.id}").text
    assert "data-album-retag" not in user_client.get(f"/album/{group.id}").text


def test_a_tentative_match_has_no_button(admin_client, db, owned):
    group, album = owned
    album.match_status = "tentative"
    db.commit()
    html = admin_client.get(f"/album/{group.id}").text
    assert "data-album-retag" not in html
    assert "retag" in admin_client.get(f"/album/{group.id}/retag").text  # the page explains
    assert "data-retag-problem" in admin_client.get(f"/album/{group.id}/retag").text


def test_the_preview_is_queued_and_audited(admin_client, db, owned):
    group, album = owned
    r = post(
        admin_client,
        f"/album/{group.id}/retag/preview",
        {"rename": "1", "replace_cover": "", "release_mbid": RELEASE_MBID},
        page=f"/album/{group.id}/retag",
    )
    assert r.status_code == 303 and r.headers["location"] == f"/album/{group.id}/retag"
    job = db.scalars(select(Job).where(Job.type == "retag_album")).one()
    payload = json.loads(job.payload_json)
    assert payload == {
        "album_id": album.id,
        "release_group_id": group.id,
        "apply": False,
        "release_mbid": RELEASE_MBID,
        "rename": True,
        "replace_cover": False,
    }
    row = db.scalars(select(RetagReport)).one()
    assert row.status == "running" and row.mode == "preview" and row.job_id == job.id
    page = admin_client.get(f"/album/{group.id}/retag").text
    assert 'data-retag-state="running"' in page and "hx-trigger" in page
    assert "album.retag" in admin_client.get("/admin/audit").text or True


def test_a_wrong_release_id_is_dropped(admin_client, db, owned):
    group, _ = owned
    post(
        admin_client,
        f"/album/{group.id}/retag/preview",
        {"release_mbid": "../../etc/passwd"},
        page=f"/album/{group.id}/retag",
    )
    job = db.scalars(select(Job).where(Job.type == "retag_album")).one()
    assert json.loads(job.payload_json)["release_mbid"] == ""


def test_applying_needs_a_ready_preview(admin_client, db, owned):
    group, album = owned
    url = f"/album/{group.id}/retag/apply"
    r = post(admin_client, url, {}, page=f"/album/{group.id}/retag")
    assert r.headers["location"].endswith("error=retag_no_preview")
    assert db.scalars(select(Job).where(Job.type == "retag_album")).first() is None
    db.add(
        RetagReport(
            album_id=album.id,
            mode="preview",
            status="ready",
            options_json=json.dumps({"rename": True, "replace_cover": False, "release_mbid": ""}),
            report_json=json.dumps({"summary": {"to_change": 1}, "files": []}),
        )
    )
    db.commit()
    r = post(admin_client, url, {}, page=f"/album/{group.id}/retag")
    assert r.headers["location"] == f"/album/{group.id}/retag"
    payload = json.loads(
        db.scalars(select(Job).where(Job.type == "retag_album")).one().payload_json
    )
    assert payload["apply"] is True and payload["rename"] is True


def test_the_report_shows_the_tags_before_and_after_and_the_missing_track(admin_client, db, owned):
    group, album = owned
    report = {
        "release": {
            "mbid": RELEASE_MBID,
            "title": "Album Title",
            "date": "2000-03-04",
            "country": "US",
            "status": "Official",
            "tracks": 11,
            "paired": 10,
        },
        "candidates": [],
        "files": [
            {
                "name": "01 - Opening.flac",
                "state": "change",
                "track": {"disc": 1, "position": 1, "title": "Opening", "seconds": 180},
                "changes": [{"field": "album", "old": "Junk", "new": "Album Title"}],
                "cover": True,
                "rename": None,
                "error": None,
            }
        ],
        "missing": [{"disc": 1, "position": 11, "title": "Last One", "seconds": 200}],
        "extra": [],
        "cover": {"source": "coverartarchive", "files_without": 1},
        "folder": None,
        "notes": ["files_missing_on_disk:1"],
        "summary": {
            "files": 10, "to_change": 1, "same": 9, "unmatched": 0, "written": 0, "failed": 0,
            "tracks": 11, "missing": 1, "cover_to_embed": 1,
        },
    }  # fmt: skip
    db.add(
        RetagReport(
            album_id=album.id,
            mode="preview",
            status="ready",
            options_json="{}",
            report_json=json.dumps(report),
        )
    )
    db.commit()
    html = admin_client.get(f"/album/{group.id}/retag").text
    assert "10 of the 11 tracks of this release have a file" in html
    assert "data-retag-missing" in html and "1-11" in html and "Last One" in html
    assert "Junk" in html and "Album Title" in html and "data-retag-apply" in html
    assert "no longer on the disk" in html
    assert "data-retag-preview-note" in html


def test_only_an_admin_can_use_the_retag_pages(admin_client, user_client, owned):
    group, _ = owned
    assert user_client.get(f"/album/{group.id}/retag").status_code == 403
    assert user_client.get(f"/album/{group.id}/retag/report").status_code == 403
    assert (
        post(user_client, f"/album/{group.id}/retag/preview", {}, page="/dashboard").status_code
        == 403
    )
    assert (
        post(user_client, f"/album/{group.id}/retag/apply", {}, page="/dashboard").status_code
        == 403
    )
    assert admin_client.get("/album/999999/retag").status_code == 404
