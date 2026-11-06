import os
import shutil

import pytest
from sqlalchemy import func, select

from app.jobs.errors import JobCancelled
from app.models import Artist, LibraryAlbum, LibraryTrack
from app.services.app_settings import get_setting
from app.services.library.filesystem import FilesystemProvider
from app.services.library.provider import LibraryError, RecordingReporter
from app.services.library.scan import LAST_SCAN_KEY, run_scan
from app.services.library.tags import read_audio_file
from tests.audio import make_track, tag_file

ART = "33333333-3333-4333-8333-333333333333"
REL = "11111111-1111-4111-8111-111111111111"


class CountingReader:
    """Wraps read_audio_file to count how many files a scan really opens."""

    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path.name)
        return read_audio_file(path)


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    return root


def build_library(root):
    """Aretha (FLAC 24-bit, 2 tracks), Daft Punk (MP3 320 + a 128 kbps track), a compilation."""
    for n in (1, 2):
        make_track(
            root / "Aretha Franklin/Lady Soul (1968)" / f"{n:02} Song {n}.flac",
            _bit_depth=24,
            title=f"Song {n}",
            artist="Aretha Franklin",
            albumartist="Aretha Franklin",
            album="Lady Soul",
            track=f"{n}/2",
            date="1968",
            mbid_album_artist=ART,
            mbid_release=REL,
        )
    make_track(
        root / "Daft Punk/Discovery/01 One More Time.mp3",
        _bitrate=320,
        title="One More Time",
        artist="Daft Punk",
        albumartist="Daft Punk",
        album="Discovery",
        track="1",
        date="2001",
    )
    make_track(
        root / "Daft Punk/Discovery/02 Aerodynamic.mp3",
        _bitrate=128,
        title="Aerodynamic",
        artist="Daft Punk",
        albumartist="Daft Punk",
        album="Discovery",
        track="2",
        date="2001",
    )
    make_track(
        root / "Hits/01.ogg", title="Hit", artist="Someone", album="Summer Hits", compilation=True
    )


def scan(factory, root, **kw):
    reader = kw.pop("reader", None) or CountingReader()
    reporter = kw.pop("reporter", None) or RecordingReporter()
    summary = run_scan(factory, FilesystemProvider(root, reader=reader), reporter, **kw)
    return summary, reader, reporter


def counts(db):
    return tuple(db.scalar(select(func.count(m.id))) for m in (Artist, LibraryAlbum, LibraryTrack))


# --------------------------------------------------------------------- first scan


def test_first_scan_groups_albums_and_computes_quality(session_factory, music):
    build_library(music)
    summary, reader, _ = scan(session_factory, music)
    assert (summary.files_seen, summary.files_read, summary.files_skipped) == (5, 5, 0)
    with session_factory() as db:
        assert counts(db) == (3, 3, 5)
        albums = {a.title: a for a in db.scalars(select(LibraryAlbum))}
        lady, disco, hits = albums["Lady Soul"], albums["Discovery"], albums["Summer Hits"]
        assert (lady.quality_tier, lady.format, lady.track_count, lady.year) == (
            "lossless_24",
            "flac",
            2,
            1968,
        )
        assert (
            disco.quality_tier == "any" and disco.format == "mp3" and disco.bitrate == 224
        )  # weakest track
        assert hits.is_compilation and hits.artist.name == "Various Artists"
        assert lady.path == "Aretha Franklin/Lady Soul (1968)"  # relative to the library root
        assert lady.mbid_release == REL and lady.artist.mbid == ART
        track = db.scalar(select(LibraryTrack).where(LibraryTrack.title == "Song 1"))
        assert (track.track_no, track.bit_depth, track.sample_rate, track.codec) == (
            1,
            24,
            44100,
            "flac",
        )
        assert track.path == "Aretha Franklin/Lady Soul (1968)/01 Song 1.flac"
        assert (
            track.quality_tier == "lossless_24" and track.file_size > 0 and track.file_mtime_ns > 0
        )


def test_summary_is_saved_for_the_admin_page(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    with session_factory() as db:
        saved = get_setting(db, LAST_SCAN_KEY)
    assert saved["tracks"] == 5 and saved["artists"] == 3 and saved["source"] == "filesystem"
    assert saved["finished_at"]


def test_progress_is_reported_per_artist(session_factory, music):
    build_library(music)
    _, _, reporter = scan(session_factory, music)
    steps = [s for _, s in reporter.progress_calls if s]
    assert any(s.startswith("Reading tags 5/5") for s in steps)
    assert [s for s in steps if s.startswith("Artist ")] == [
        "Artist 1/3: Aretha Franklin",
        "Artist 2/3: Daft Punk",
        "Artist 3/3: Various Artists",
    ]
    pct = [p for p, _ in reporter.progress_calls]
    assert pct == sorted(pct) and pct[-1] == 100


def test_non_audio_hidden_and_system_files_are_ignored(session_factory, music):
    make_track(music / "A/B/01.flac", artist="A", album="B")
    for junk in (
        "A/B/cover.jpg",
        "A/B/playlist.m3u",
        "A/B/album.cue",
        "A/B/.hidden.flac",
        "A/B/._01.flac",
        "A/.thumbs/x.flac",
        "A/@eaDir/x.flac",
        "#recycle/x.flac",
        "$RECYCLE.BIN/x.flac",
    ):
        path = music / junk
        path.parent.mkdir(parents=True, exist_ok=True)
        make_track(
            path, artist="Junk", album="Junk"
        ) if path.suffix == ".flac" else path.write_bytes(b"x")
    summary, _, _ = scan(session_factory, music)
    assert summary.files_seen == 1 and summary.tracks == 1


def test_symlink_pointing_outside_the_library_is_ignored(session_factory, music, tmp_path):
    outside = make_track(tmp_path / "outside/secret.flac", artist="Out", album="Side")
    make_track(music / "A/B/01.flac", artist="A", album="B")
    (music / "A/B/link.flac").symlink_to(outside)
    (music / "linked_dir").symlink_to(outside.parent, target_is_directory=True)
    summary, _, reporter = scan(session_factory, music)
    assert summary.files_seen == 1
    assert any("outside the library" in m for m in reporter.messages("warning"))


def test_untagged_library_uses_the_folder_structure(session_factory, music):
    make_track(music / "Radiohead/OK Computer/03 Subterranean.wav")
    scan(session_factory, music)
    with session_factory() as db:
        album = db.scalar(select(LibraryAlbum))
        assert (album.artist.name, album.title) == ("Radiohead", "OK Computer")
        track = db.scalar(select(LibraryTrack))
        assert (track.title, track.track_no) == ("Subterranean", 3)


def test_missing_or_invalid_root_is_a_clear_error(session_factory, tmp_path):
    with pytest.raises(LibraryError, match="not found"):
        scan(session_factory, tmp_path / "nope")
    afile = tmp_path / "file.txt"
    afile.write_text("x")
    with pytest.raises(LibraryError):
        scan(session_factory, afile)


def test_empty_library_is_fine(session_factory, music):
    summary, _, _ = scan(session_factory, music)
    assert summary.tracks == 0 and summary.artists == 0


def test_corrupt_file_is_reported_and_skipped(session_factory, music):
    build_library(music)
    (music / "Daft Punk/Discovery/03 Broken.flac").write_bytes(b"not audio")
    summary, _, reporter = scan(session_factory, music)
    assert summary.errors == 1 and summary.tracks == 5
    assert any("03 Broken.flac" in m for m in reporter.messages("warning"))


# ------------------------------------------------------------------- incremental


def test_rescan_reads_nothing_when_nothing_changed(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    summary, reader, _ = scan(session_factory, music)
    assert reader.calls == []
    assert (summary.files_read, summary.files_skipped, summary.tracks_added) == (0, 5, 0)
    with session_factory() as db:
        assert counts(db) == (3, 3, 5)  # no duplicates


def test_changed_file_is_read_again(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    changed = music / "Daft Punk/Discovery/02 Aerodynamic.mp3"
    st = changed.stat()
    os.utime(changed, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    _, reader, _ = scan(session_factory, music)
    assert reader.calls == ["02 Aerodynamic.mp3"]


def test_retagging_updates_the_same_album_without_duplicates(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    with session_factory() as db:
        before = db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Discovery")).id
    for f in (music / "Daft Punk/Discovery").iterdir():
        tag_file(f, album="Discovery (Remastered)")
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    scan(session_factory, music)
    with session_factory() as db:
        assert counts(db) == (3, 3, 5)
        album = db.scalar(
            select(LibraryAlbum).where(LibraryAlbum.title == "Discovery (Remastered)")
        )
        assert album.id == before and album.track_count == 2


def test_force_reads_everything(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    summary, reader, _ = scan(session_factory, music, force=True)
    assert len(reader.calls) == 5 and summary.forced


def test_deleted_track_updates_the_album(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    (music / "Aretha Franklin/Lady Soul (1968)/02 Song 2.flac").unlink()
    summary, reader, _ = scan(session_factory, music)
    assert reader.calls == [] and summary.tracks_removed == 1
    with session_factory() as db:
        assert (
            db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Lady Soul")).track_count
            == 1
        )
        assert counts(db) == (3, 3, 4)


def test_deleted_album_removes_it_and_flags_the_artist(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    shutil.rmtree(music / "Daft Punk")
    summary, _, _ = scan(session_factory, music)
    assert (summary.tracks_removed, summary.albums_removed, summary.artists_deactivated) == (
        2,
        1,
        1,
    )
    with session_factory() as db:
        daft = db.scalar(select(Artist).where(Artist.name == "Daft Punk"))
        assert daft is not None and daft.in_library is False  # row kept for later phases
        assert counts(db)[1:] == (2, 3)


def test_artist_comes_back_when_music_returns(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    shutil.move(music / "Daft Punk", music.parent / "Daft Punk")
    scan(session_factory, music)
    shutil.move(music.parent / "Daft Punk", music / "Daft Punk")
    scan(session_factory, music)
    with session_factory() as db:
        daft = db.scalars(select(Artist).where(Artist.name == "Daft Punk")).all()
        assert len(daft) == 1 and daft[0].in_library is True


def test_new_album_of_a_known_artist_joins_the_same_artist(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    make_track(
        music / "Daft Punk/Homework/01.mp3",
        _bitrate=256,
        artist="Daft Punk",
        albumartist="Daft Punk",
        album="Homework",
        track="1",
    )
    summary, reader, _ = scan(session_factory, music)
    assert reader.calls == ["01.mp3"] and summary.tracks_added == 1
    with session_factory() as db:
        assert len(db.scalars(select(Artist).where(Artist.name == "Daft Punk")).all()) == 1
        assert counts(db) == (3, 4, 6)


def test_moved_folder_does_not_duplicate_anything(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    shutil.move(music / "Daft Punk/Discovery", music / "Daft Punk/Discovery (2001)")
    scan(session_factory, music)
    with session_factory() as db:
        assert counts(db) == (3, 3, 5)
        assert (
            db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Discovery")).path
            == "Daft Punk/Discovery (2001)"
        )


def test_unreadable_known_file_keeps_its_old_data(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    path = music / "Daft Punk/Discovery/01 One More Time.mp3"
    path.write_bytes(b"corrupted now")
    summary, _, reporter = scan(session_factory, music)
    assert summary.errors == 1 and summary.tracks_removed == 0
    with session_factory() as db:
        assert counts(db)[2] == 5
    assert reporter.messages("warning")


def test_artist_rename_moves_to_a_new_artist(session_factory, music):
    make_track(music / "x/1.flac", artist="Old Name", albumartist="Old Name", album="Alb")
    scan(session_factory, music)
    f = music / "x/1.flac"
    tag_file(f, albumartist="New Name")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    scan(session_factory, music)
    with session_factory() as db:
        states = {a.name: a.in_library for a in db.scalars(select(Artist))}
        assert states == {"Old Name": False, "New Name": True}


# --------------------------------------------------------------------- safety


def test_removing_most_of_the_library_aborts_and_changes_nothing(session_factory, music):
    for i in range(25):
        make_track(
            music / f"A/Album/{i:02}.mp3",
            artist="A",
            albumartist="A",
            album="Album",
            track=str(i + 1),
        )
    scan(session_factory, music)
    shutil.rmtree(music / "A")  # e.g. the NAS volume is not mounted
    with pytest.raises(LibraryError, match="would remove 25 of 25"):
        scan(session_factory, music)
    with session_factory() as db:
        assert counts(db) == (1, 1, 25)
        assert db.scalar(select(Artist)).in_library is True


def test_guard_can_be_relaxed_and_ignores_small_libraries(session_factory, music):
    for i in range(25):
        make_track(music / f"A/Album/{i:02}.mp3", artist="A", albumartist="A", album="Album")
    scan(session_factory, music)
    shutil.rmtree(music / "A")
    summary, _, _ = scan(session_factory, music, max_removal_pct=100)
    assert summary.tracks_removed == 25
    # fewer than 20 known tracks: wiping them is allowed
    build_library(music)
    scan(session_factory, music)
    shutil.rmtree(music / "Daft Punk")
    shutil.rmtree(music / "Hits")
    shutil.rmtree(music / "Aretha Franklin")
    assert scan(session_factory, music)[0].tracks == 0


def test_cancellation_stops_without_deleting_anything(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    (music / "Hits/01.ogg").unlink()
    with pytest.raises(JobCancelled):
        scan(session_factory, music, reporter=RecordingReporter(cancel_after=8))
    with session_factory() as db:
        assert counts(db)[2] == 5  # the removal was not applied


def test_cancellation_during_tag_reading(session_factory, music):
    build_library(music)
    with pytest.raises(JobCancelled):
        scan(session_factory, music, reporter=RecordingReporter(cancel_after=3))
    with session_factory() as db:
        assert counts(db) == (0, 0, 0)  # nothing was saved yet


def test_two_sources_do_not_interfere(session_factory, music):
    build_library(music)
    scan(session_factory, music)
    with session_factory() as db:
        assert {t.source for t in db.scalars(select(LibraryTrack))} == {"filesystem"}
        assert {a.source for a in db.scalars(select(LibraryAlbum))} == {"filesystem"}


def test_scale_rescan_only_stats_files(session_factory, music):
    for i in range(300):
        make_track(
            music / f"Artist {i // 10}/Album {i // 5}/{i:03}.mp3",
            _frames=3,
            artist=f"Artist {i // 10}",
            albumartist=f"Artist {i // 10}",
            album=f"Album {i // 5}",
        )
    first, reader1, _ = scan(session_factory, music)
    second, reader2, _ = scan(session_factory, music)
    assert len(reader1.calls) == 300 and reader2.calls == []
    assert (second.files_skipped, second.tracks) == (300, 300)
    with session_factory() as db:
        assert counts(db) == (30, 60, 300)
