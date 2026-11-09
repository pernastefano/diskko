"""Replacing the old copy of an album: what is removed, what is overwritten, what is never touched,
and the album that is in the library more than once."""

import os

import pytest
from sqlalchemy import select

from app.models import LibraryAlbum, LibraryTrack
from app.services import album_copies
from app.services.importer import library_import as lib
from app.services.importer.integrity import ImportRejected
from tests.audio import make_track
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg


def touch(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# -------------------------------------------------------------------- local paths


def test_a_stored_path_must_lead_inside_the_library(tmp_path):
    music = tmp_path / "music"
    (music / "A").mkdir(parents=True)
    (tmp_path / "outside.flac").write_bytes(b"s")
    os.symlink(tmp_path / "outside.flac", music / "link.flac")
    assert lib.local_path(music, "A") == (music / "A").resolve()
    assert lib.local_path(music, str(music / "A")) == (music / "A").resolve()  # absolute, inside
    for bad in ("", "plex:track:1", "../outside.flac", "/etc/passwd", "link.flac", "."):
        assert lib.local_path(music, bad) is None, bad


# ---------------------------------------------------------------------- retiring


def old_copy(music, name="Old", files=("01.mp3", "02.mp3"), extras=("cover.jpg", "rip.log")):
    folder = music / "Artist" / name
    paths = [touch(folder / f) for f in files]
    for e in extras:
        touch(folder / e)
    return lib.OldAlbum(1, folder.resolve(), [p.resolve() for p in paths])


def test_the_old_files_cover_and_empty_folders_go(tmp_path):
    music = tmp_path / "music"
    old = old_copy(music)
    new_folder = music / "Artist" / "New"
    touch(new_folder / "01.flac")
    left = lib.retire_album(old, music, {(new_folder / "01.flac").resolve()}, new_folder.resolve())
    assert left == [] and not old.folder.exists()
    assert (music / "Artist").is_dir() and (new_folder / "01.flac").is_file()  # the artist stays
    assert music.is_dir()


def test_what_is_not_a_sidecar_is_left_and_reported(tmp_path):
    music = tmp_path / "music"
    old = old_copy(music, extras=("cover.jpg", "booklet.pdf"))
    new = (music / "Artist" / "New").resolve()
    left = lib.retire_album(old, music, set(), new)
    assert left == ["booklet.pdf"] and (old.folder / "booklet.pdf").is_file()
    assert not (old.folder / "cover.jpg").exists() and not list(old.folder.glob("*.mp3"))


def test_a_folder_with_another_album_keeps_its_files(tmp_path):
    music = tmp_path / "music"
    old = old_copy(music)
    touch(old.folder / "99 other album.flac")
    new = (music / "Artist" / "New").resolve()
    lib.retire_album(old, music, set(), new)
    assert (old.folder / "99 other album.flac").is_file()  # not ours
    assert (old.folder / "cover.jpg").is_file()  # the folder is shared: its extras stay too


def test_in_the_same_folder_only_the_old_tracks_go(tmp_path):
    music = tmp_path / "music"
    old = old_copy(music)
    touch(old.folder / "01.flac")  # the new copy, same folder
    keep = {(old.folder / "01.flac").resolve()}
    lib.retire_album(old, music, keep, old.folder)
    assert sorted(p.name for p in old.folder.iterdir()) == ["01.flac", "cover.jpg", "rip.log"]


def test_the_old_files_can_be_moved_aside_instead(tmp_path):
    music = tmp_path / "music"
    old = old_copy(music)
    moved = []
    lib.retire_album(old, music, set(), (music / "Artist" / "New").resolve(), aside=moved.append)
    assert [p.name for p in moved] == ["01.mp3", "02.mp3"]  # handed to the caller, not deleted
    assert all(p.is_file() for p in moved)


# ------------------------------------------------------------------------ merging


def test_the_staged_files_overwrite_the_ones_with_the_same_name(tmp_path):
    staging, final = tmp_path / "stage", tmp_path / "final"
    touch(staging / "01.flac", b"new")
    touch(staging / ".diskko-import-1", b"m")
    touch(final / "01.flac", b"old")
    touch(final / "notes.txt", b"mine")
    assert lib.merge_into(staging, final, set()) == 1
    assert (final / "01.flac").read_bytes() == b"new" and (final / "notes.txt").is_file()
    assert (final / ".diskko-import-1").is_file() and not staging.exists()


def test_a_file_of_another_album_stops_the_merge_before_anything_is_written(tmp_path):
    staging, final = tmp_path / "stage", tmp_path / "final"
    touch(staging / "01.flac", b"new")
    touch(staging / "02.flac", b"new")
    touch(final / "02.flac", b"theirs")
    with pytest.raises(ImportRejected, match="belongs to another album"):
        lib.merge_into(staging, final, {(final / "02.flac").resolve()})
    assert (final / "02.flac").read_bytes() == b"theirs" and not (final / "01.flac").exists()


# -------------------------------------------------------- the same album twice


@pytest.fixture
def settings(settings, tmp_path):
    return settings.model_copy(update={"music_dir": tmp_path / "music"})


@pytest.fixture
def twice(db, settings):
    """One album in the library twice: an MP3 copy and a FLAC copy in other folders."""
    artist = make_artist(db, "The Band")
    group = make_rg(db, artist, "Record", 2001)
    copies = {}
    for tier, ext, name in (
        ("mp3_320", "mp3", "Record (mp3)"),
        ("flac_standard", "flac", "Record"),
    ):
        album = make_album(db, artist, "Record", year=2001, path=f"The Band/{name}")
        album.release_group_id, album.match_status, album.quality_tier = group.id, "matched", tier
        make_track(
            settings.music_dir / f"The Band/{name}/01.{ext}",
            title="One",
            artist="The Band",
            album="Record",
            track=1,
        )
        touch(settings.music_dir / f"The Band/{name}/cover.jpg")
        db.add(
            LibraryTrack(
                album_id=album.id,
                source="filesystem",
                path=f"The Band/{name}/01.{ext}",
                title="One",
                track_no=1,
                quality_tier=tier,
                file_size=2_000_000,
            )
        )
        copies[ext] = album
    db.commit()
    return {
        "group": group,
        "mp3": copies["mp3"],
        "flac": copies["flac"],
        "music": settings.music_dir,
    }


def test_the_admin_sees_the_copies_with_their_quality(admin_client, twice):
    html = admin_client.get(f"/album/{twice['group'].id}").text
    assert "data-copies" in html and "in your library 2 times" in html
    assert html.count("data-keep-copy") == 2
    assert f'data-copy="{twice["mp3"].id}"' in html and "The Band/Record (mp3)" in html


def test_a_user_and_a_single_copy_show_no_such_box(admin_client, user_client, twice, db):
    assert "data-copies" not in user_client.get(f"/album/{twice['group'].id}").text
    db.delete(db.get(LibraryAlbum, twice["mp3"].id))
    db.commit()
    assert "data-copies" not in admin_client.get(f"/album/{twice['group'].id}").text


def test_keeping_one_copy_deletes_the_other_from_the_disk_and_the_library(admin_client, twice, db):
    group, music = twice["group"], twice["music"]
    r = post(
        admin_client,
        f"/album/{group.id}/copies/keep",
        {"album_id": str(twice["flac"].id)},
        page=f"/album/{group.id}",
    )
    assert (
        r.status_code == 303 and r.headers["location"] == f"/album/{group.id}?flash=copies_removed"
    )
    assert not (music / "The Band/Record (mp3)").exists()  # files, cover and folder
    assert (music / "The Band/Record/01.flac").is_file() and (
        music / "The Band/Record/cover.jpg"
    ).is_file()
    db.expire_all()
    assert [a.id for a in db.scalars(select(LibraryAlbum))] == [twice["flac"].id]
    assert db.query(LibraryTrack).count() == 1
    assert "deleted" in admin_client.get(r.headers["location"]).text


def test_only_an_admin_can_delete_copies_and_a_wrong_id_deletes_nothing(
    admin_client, user_client, twice, db
):
    group, music = twice["group"], twice["music"]
    url = f"/album/{group.id}/copies/keep"
    assert (
        post(user_client, url, {"album_id": str(twice["flac"].id)}, page="/dashboard").status_code
        == 403
    )
    post(admin_client, url, {"album_id": "99999"}, page=f"/album/{group.id}")
    post(admin_client, url, {"album_id": "x"}, page=f"/album/{group.id}")
    assert (music / "The Band/Record (mp3)/01.mp3").is_file() and db.query(
        LibraryAlbum
    ).count() == 2
    assert admin_client.post(url, data={"album_id": "1"}).status_code in (400, 403)  # no CSRF token
    assert album_copies.copies_of(db, music, group.id)[0].album.id == twice["mp3"].id
