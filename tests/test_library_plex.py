import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest
import requests
import responses
from sqlalchemy import func, select

from app.models import Artist, LibraryAlbum, LibraryTrack
from app.quality import DEFAULT_PROFILES
from app.services.library import build_provider
from app.services.library.plex_provider import PlexProvider, map_path, parse_path_map
from app.services.library.provider import LibraryError, RecordingReporter
from app.services.library.scan import run_scan
from tests.audio import make_track
from tests.test_jobs_worker import enqueue, logs, wait_for_status

BASE = "http://plex.test:32400"
TOKEN = "plex-token-abcdef"
MBID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MBID_R = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
MBID_T = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


@dataclass
class PArtist:
    id: int
    title: str
    sort: str | None = None
    guid: str | None = None


@dataclass
class PAlbum:
    id: int
    artist: int
    title: str
    year: int | None = None
    guid: str | None = None


@dataclass
class PTrack:
    id: int
    album: int
    artist: int
    title: str
    index: int = 1
    disc: int = 1
    path: str | None = None
    codec: str = "flac"
    bitrate: int = 900
    size: int = 1000
    bit_depth: int | None = 16
    rate: int | None = 44100
    guid: str | None = None
    updated: int = 1700000000
    album_title: str = ""
    artist_title: str = ""


class FakePlex:
    """Serves the XML a Plex server would return, so plexapi parses it for real."""

    def __init__(self):
        self.artists: list[PArtist] = []
        self.albums: list[PAlbum] = []
        self.tracks: list[PTrack] = []
        self.root_status = 200
        self.section_type = "artist"
        self.section_title = "Music"
        self.connection_error = False
        self.requests: list = []

    def _artist_xml(self, a):
        guid = f'<Guid id="{a.guid}"/>' if a.guid else ""
        sort = f" titleSort={quoteattr(a.sort)}" if a.sort else ""
        return f'<Directory type="artist" ratingKey="{a.id}" title={quoteattr(a.title)}{sort}>{guid}</Directory>'

    def _album_xml(self, a):
        guid = f'<Guid id="{a.guid}"/>' if a.guid else ""
        year = f' year="{a.year}"' if a.year else ""
        return (
            f'<Directory type="album" ratingKey="{a.id}" parentRatingKey="{a.artist}" '
            f"title={quoteattr(a.title)}{year}>{guid}</Directory>"
        )

    def _track_xml(self, t):
        guid = f'<Guid id="{t.guid}"/>' if t.guid else ""
        media = ""
        if t.path:
            stream = (
                f'<Stream streamType="2" bitDepth="{t.bit_depth}" samplingRate="{t.rate}" codec="{t.codec}"/>'
                if t.bit_depth
                else ""
            )
            media = (
                f'<Media audioCodec="{t.codec}" bitrate="{t.bitrate}" audioChannels="2">'
                f'<Part file={quoteattr(t.path)} size="{t.size}">{stream}</Part></Media>'
            )
        album_title = quoteattr(
            t.album_title or next(a.title for a in self.albums if a.id == t.album)
        )
        artist_title = quoteattr(
            t.artist_title or next(a.title for a in self.artists if a.id == t.artist)
        )
        return (
            f'<Track type="track" ratingKey="{t.id}" parentRatingKey="{t.album}" grandparentRatingKey="{t.artist}" '
            f"title={quoteattr(t.title)} parentTitle={album_title} grandparentTitle={artist_title} "
            f'index="{t.index}" parentIndex="{t.disc}" duration="180000" updatedAt="{t.updated}">{media}{guid}</Track>'
        )

    def callback(self, request):
        self.requests.append(request)
        if self.connection_error:
            raise requests.exceptions.ConnectionError("boom http://plex.test:32400")
        path = request.url.split("?")[0].replace(BASE, "") or "/"
        if path == "/":
            if self.root_status != 200:
                return self.root_status, {}, ""
            return 200, {}, '<MediaContainer size="0" machineIdentifier="m" version="1.40.0"/>'
        if path == "/library":
            return 200, {}, '<MediaContainer size="1"><Directory key="sections"/></MediaContainer>'
        if path == "/library/sections":
            return (
                200,
                {},
                (
                    f'<MediaContainer size="1"><Directory key="1" type="{self.section_type}" '
                    f'title="{self.section_title}"/></MediaContainer>'
                ),
            )
        if path == "/library/sections/1/all":
            kind = re.search(r"type=(\d+)", request.url).group(1)
            items = {
                "8": [self._artist_xml(a) for a in self.artists],
                "9": [self._album_xml(a) for a in self.albums],
                "10": [self._track_xml(t) for t in self.tracks],
            }[kind]
            return (
                200,
                {},
                f'<MediaContainer size="{len(items)}" totalSize="{len(items)}">{"".join(items)}</MediaContainer>',
            )
        return 404, {}, ""


@pytest.fixture
def plex():
    fake = FakePlex()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, re.compile(re.escape(BASE) + ".*"), callback=fake.callback)
        yield fake


@pytest.fixture
def psettings(settings):
    return settings.model_copy(
        update={
            "plex_url": BASE,
            "plex_token": TOKEN,
            "library_source": "plex",
            "music_dir": Path("/music"),
            "library_path_map": "/data/music=/music",
            "job_poll_interval": 0.02,
        }
    )


def basic_library(plex):
    plex.artists = [
        PArtist(1, "Aretha Franklin", "Franklin, Aretha", f"mbid://{MBID_A}"),
        PArtist(2, "Various Artists"),
    ]
    plex.albums = [
        PAlbum(10, 1, "Lady Soul", 1968, f"mbid://{MBID_R}"),
        PAlbum(11, 1, "Aretha Now", 1968),
        PAlbum(12, 2, "Summer Hits", 2001),
    ]
    plex.tracks = [
        PTrack(
            100,
            10,
            1,
            "Chain of Fools",
            1,
            path="/data/music/Aretha Franklin/Lady Soul/01.flac",
            bit_depth=24,
            rate=96000,
            guid=f"mbid://{MBID_T}",
        ),
        PTrack(
            101, 10, 1, "Natural Woman", 2, path="/data/music/Aretha Franklin/Lady Soul/02.flac"
        ),
        PTrack(
            110,
            11,
            1,
            "Think",
            1,
            path="/data/music/Aretha Franklin/Aretha Now/01.mp3",
            codec="mp3",
            bitrate=320,
            bit_depth=None,
            rate=None,
        ),
        PTrack(
            120,
            12,
            2,
            "Hit",
            1,
            path="/data/music/Hits/01.mp3",
            codec="mp3",
            bitrate=192,
            bit_depth=None,
        ),
    ]


def scan_plex(settings, known=None, **kw):
    return PlexProvider(settings).scan(known or {}, RecordingReporter(), **kw)


# ------------------------------------------------------------------- path mapping


def test_parse_path_map_orders_longest_prefix_first():
    rules = parse_path_map("/data=/mnt;/data/music=/music\n C:\\Music = /music2 ;bad;=x")
    assert rules == [("/data/music", "/music"), ("C:/Music", "/music2"), ("/data", "/mnt")]


@pytest.mark.parametrize(
    ("plex_path", "expected"),
    [
        ("/data/music/A/B/01.flac", "A/B/01.flac"),
        ("/data/music2/A/01.flac", "/data/music2/A/01.flac"),  # not a path-boundary match
        ("C:\\Music\\A\\01.flac", "A/01.flac"),
        ("/elsewhere/x.flac", "/elsewhere/x.flac"),  # unmapped paths are kept
    ],
)
def test_map_path(plex_path, expected):
    rules = parse_path_map("/data/music=/music;C:\\Music=/music")
    assert map_path(plex_path, rules, ["/music"]) == expected


def test_without_rules_paths_inside_the_root_become_relative():
    assert map_path("/music/A/01.flac", [], ["/music"]) == "A/01.flac"


# ---------------------------------------------------------------------- mapping


def test_listing_is_mapped_to_artists_albums_and_tracks(plex, psettings):
    basic_library(plex)
    output = scan_plex(psettings)
    aretha, various = output.artists
    assert (aretha.name, aretha.sort_name, aretha.mbid) == (
        "Aretha Franklin",
        "Franklin, Aretha",
        MBID_A,
    )
    lady, now = aretha.albums
    assert (lady.title, lady.year, lady.mbid_release, lady.plex_rating_key) == (
        "Lady Soul",
        1968,
        MBID_R,
        "10",
    )
    assert lady.path == "Aretha Franklin/Lady Soul" and lady.quality_tier == "flac_standard"
    first = lady.tracks[0]
    assert first.path == "Aretha Franklin/Lady Soul/01.flac" and first.title == "Chain of Fools"
    assert (first.codec, first.lossless, first.bit_depth, first.sample_rate) == (
        "flac",
        True,
        24,
        96000,
    )
    assert (first.quality_tier, first.duration, first.mbid_recording) == (
        "flac_hires",  # 24-bit / 96 kHz
        180.0,
        MBID_T,
    )
    assert (first.plex_rating_key, first.file_size, first.disc_no) == ("100", 1000, 1)
    assert (now.format, now.quality_tier) == ("mp3", "mp3_320")
    assert various.name == "Various Artists" and various.albums[0].is_compilation
    assert output.stats.files_seen == 4 and output.stats.errors == 0


def test_streams_missing_from_the_listing_are_read_as_cd_quality(plex, psettings):
    basic_library(plex)
    plex.tracks[0].bit_depth = None
    track = scan_plex(psettings).artists[0].albums[0].tracks[0]
    assert track.bit_depth is None and track.quality_tier == "flac_standard"


def test_track_without_media_gets_a_placeholder_path(plex, psettings):
    basic_library(plex)
    plex.tracks[3].path = None
    album = scan_plex(psettings).artists[1].albums[0]
    assert album.tracks[0].path == "plex:track:120" and album.path == "plex:album:12"
    assert album.tracks[0].quality_tier == "any"


def test_multi_disc_album_uses_the_common_folder(plex, psettings):
    basic_library(plex)
    plex.tracks = [
        PTrack(1, 10, 1, "A", 1, 1, path="/data/music/Aretha Franklin/Lady Soul/CD1/01.flac"),
        PTrack(2, 10, 1, "B", 1, 2, path="/data/music/Aretha Franklin/Lady Soul/CD2/01.flac"),
    ]
    album = scan_plex(psettings).artists[0].albums[0]
    assert album.path == "Aretha Franklin/Lady Soul" and album.disc_count == 2


def test_artists_and_albums_without_tracks_are_skipped(plex, psettings):
    basic_library(plex)
    plex.artists.append(PArtist(3, "Empty Artist"))
    plex.albums.append(PAlbum(13, 1, "Empty Album"))
    output = scan_plex(psettings)
    assert [a.name for a in output.artists] == ["Aretha Franklin", "Various Artists"]
    assert [al.title for al in output.artists[0].albums] == ["Lady Soul", "Aretha Now"]


def test_requests_use_bulk_listings_with_guids(plex, psettings):
    basic_library(plex)
    scan_plex(psettings)
    listings = [r.url for r in plex.requests if "/library/sections/1/all" in r.url]
    assert len(listings) == 3  # artists, albums, tracks: not one request per artist
    assert all("includeGuids=1" in u for u in listings)
    assert {r.headers.get("X-Plex-Token") or "" for r in plex.requests} == {TOKEN}


# ----------------------------------------------------------------- persistence


def test_scan_saves_rows_and_rescan_is_stable(plex, psettings, session_factory):
    basic_library(plex)
    provider = PlexProvider(psettings)
    summary = run_scan(session_factory, provider, RecordingReporter())
    assert (summary.tracks, summary.files_read, summary.files_skipped) == (4, 4, 0)
    again = run_scan(session_factory, provider, RecordingReporter())
    assert (again.files_read, again.files_skipped, again.tracks_added) == (0, 4, 0)
    with session_factory() as db:
        assert db.scalar(select(func.count(Artist.id))) == 2
        track = db.scalar(select(LibraryTrack).where(LibraryTrack.plex_rating_key == "100"))
        assert track.source == "plex" and track.quality_tier == "flac_hires"
        album = db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Lady Soul"))
        assert album.plex_rating_key == "10" and album.source == "plex"


def test_changes_in_plex_are_picked_up(plex, psettings, session_factory):
    basic_library(plex)
    provider = PlexProvider(psettings)
    run_scan(session_factory, provider, RecordingReporter())
    plex.tracks[0].updated += 100
    plex.tracks.pop()  # Plex dropped a track
    plex.albums[0].title = "Lady Soul (Deluxe)"
    summary = run_scan(session_factory, provider, RecordingReporter())
    assert (summary.files_read, summary.tracks_removed) == (1, 1)
    with session_factory() as db:
        assert db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Lady Soul (Deluxe)"))
        assert db.scalar(select(func.count(LibraryAlbum.id))) == 2  # the retitle did not duplicate


def test_an_empty_plex_answer_does_not_wipe_the_library(plex, psettings, session_factory):
    for i in range(25):
        plex.artists = [PArtist(1, "A")]
        plex.albums = [PAlbum(10, 1, "Album")]
        plex.tracks.append(
            PTrack(i + 1, 10, 1, f"T{i}", i + 1, path=f"/data/music/A/Album/{i:02}.flac")
        )
    provider = PlexProvider(psettings)
    run_scan(session_factory, provider, RecordingReporter())
    plex.artists, plex.albums, plex.tracks = [], [], []
    with pytest.raises(LibraryError, match="would remove 25 of 25"):
        run_scan(session_factory, provider, RecordingReporter())
    with session_factory() as db:
        assert db.scalar(select(func.count(LibraryTrack.id))) == 25


def local_album(tmp_path, name="Local Album", artist="Aretha Franklin"):
    from app.services.library.filesystem import FilesystemProvider

    root = tmp_path / "music"
    make_track(root / f"{artist}/{name}/01.flac", artist=artist, albumartist=artist, album=name)
    return FilesystemProvider(root)


def test_the_source_that_was_scanned_is_the_only_one_left(
    plex, psettings, session_factory, tmp_path
):
    """Switching from the folder to Plex: the folder's rows go, every album is listed once."""
    run_scan(session_factory, local_album(tmp_path), RecordingReporter())
    basic_library(plex)
    summary = run_scan(session_factory, PlexProvider(psettings), RecordingReporter())
    assert summary.other_source_albums_removed == 1
    with session_factory() as db:
        assert {t.source for t in db.scalars(select(LibraryTrack))} == {"plex"}
        assert {a.source for a in db.scalars(select(LibraryAlbum))} == {"plex"}
        assert db.scalar(select(func.count(LibraryAlbum.id))) == 3
        aretha = db.scalars(select(Artist).where(Artist.name == "Aretha Franklin")).all()
        assert len(aretha) == 1 and aretha[0].in_library  # the same artist, still ours
    # and back: Plex is gone from the point of view of a folder scan
    plex.tracks, plex.albums, plex.artists = [], [], []
    run_scan(session_factory, PlexProvider(psettings), RecordingReporter(), max_removal_pct=100)
    with session_factory() as db:
        assert db.scalar(select(func.count(LibraryTrack.id))) == 0


def test_an_artist_only_the_old_source_had_is_no_longer_in_the_library(
    plex, psettings, session_factory, tmp_path
):
    run_scan(session_factory, local_album(tmp_path, artist="Only Local"), RecordingReporter())
    basic_library(plex)
    run_scan(session_factory, PlexProvider(psettings), RecordingReporter())
    with session_factory() as db:
        gone = db.scalar(select(Artist).where(Artist.name == "Only Local"))
        assert gone is not None and gone.in_library is False  # kept, but not listed
        assert db.scalar(select(Artist).where(Artist.name == "Aretha Franklin")).in_library


def test_what_was_known_about_the_artist_survives_the_switch(
    plex, psettings, session_factory, tmp_path
):
    from app.models import AlbumMatchOverride, utcnow
    from tests.dbhelpers import make_rg

    run_scan(session_factory, local_album(tmp_path), RecordingReporter())
    with session_factory() as db:
        artist = db.scalar(select(Artist).where(Artist.name == "Aretha Franklin"))
        artist.mbid, artist.mb_status, artist.metadata_synced_at = MBID_A, "resolved", utcnow()
        group = make_rg(db, artist, "Lady Soul", 1968)
        db.add(
            AlbumMatchOverride(
                artist_id=artist.id, album_key="somethingelse", release_group_id=None
            )
        )
        db.commit()
        artist_id, group_id = artist.id, group.id
    basic_library(plex)
    run_scan(session_factory, PlexProvider(psettings), RecordingReporter())
    with session_factory() as db:
        artist = db.get(Artist, artist_id)
        assert (artist.mbid, artist.mb_status) == (MBID_A, "resolved")  # its identity stays
        assert db.query(AlbumMatchOverride).count() == 1  # so do the manual decisions
        album = db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Lady Soul"))
        # the Plex album is matched to the release group at once (no wait for the next sync)
        assert (album.release_group_id, album.match_status) == (group_id, "matched")


def test_a_much_smaller_scan_does_not_remove_the_other_source(
    plex, psettings, session_factory, tmp_path
):
    """A folder that is not mounted looks like a tiny library: nothing is removed for it."""
    basic_library(plex)
    run_scan(session_factory, PlexProvider(psettings), RecordingReporter())
    reporter = RecordingReporter()
    summary = run_scan(session_factory, local_album(tmp_path), reporter)
    assert summary.other_source_albums_removed == 0
    assert any("earlier rows were kept" in m for m in reporter.messages("warning"))
    with session_factory() as db:
        assert {t.source for t in db.scalars(select(LibraryTrack))} == {"plex", "filesystem"}


def test_new_albums_of_a_known_artist_are_matched_without_waiting_for_a_sync(
    session_factory, tmp_path
):
    from app.models import utcnow
    from app.services.library.filesystem import FilesystemProvider
    from tests.dbhelpers import make_rg

    provider = local_album(tmp_path, "First", "Band")
    run_scan(session_factory, provider, RecordingReporter())
    with session_factory() as db:
        artist = db.scalar(select(Artist).where(Artist.name == "Band"))
        artist.mb_status, artist.metadata_synced_at = "resolved", utcnow()
        first = make_rg(db, artist, "First", 2000)
        second = make_rg(db, artist, "Second", 2001)
        db.commit()
        ids = (first.id, second.id)
    make_track(
        tmp_path / "music/Band/Second/01.flac", artist="Band", albumartist="Band", album="Second"
    )
    run_scan(session_factory, FilesystemProvider(tmp_path / "music"), RecordingReporter())
    with session_factory() as db:
        matched = {
            a.title: (a.release_group_id, a.match_status) for a in db.scalars(select(LibraryAlbum))
        }
    assert matched["Second"] == (ids[1], "matched")
    assert matched["First"][0] == ids[0]


# ---------------------------------------------------------------------- errors


def test_not_configured(settings):
    blank = settings.model_copy(update={"plex_url": "", "plex_token": ""})
    with pytest.raises(LibraryError, match="not configured"):
        scan_plex(blank)


def test_rejected_token_does_not_leak_the_token(plex, psettings):
    plex.root_status = 401
    with pytest.raises(LibraryError, match="rejected the token") as exc:
        scan_plex(psettings)
    assert TOKEN not in str(exc.value)


def test_unreachable_server(plex, psettings):
    plex.connection_error = True
    with pytest.raises(LibraryError, match="Cannot connect to Plex") as exc:
        scan_plex(psettings)
    assert "plex.test" not in str(exc.value) and TOKEN not in str(exc.value)


def test_missing_section(plex, psettings):
    plex.section_title = "Other"
    with pytest.raises(LibraryError, match="'Music' not found"):
        scan_plex(psettings)


def test_section_must_be_a_music_library(plex, psettings):
    plex.section_type = "movie"
    with pytest.raises(LibraryError, match="not a music library"):
        scan_plex(psettings)


def test_build_provider_selects_plex(psettings):
    assert isinstance(build_provider(psettings, DEFAULT_PROFILES), PlexProvider)


async def test_scan_job_with_the_plex_source(plex, psettings, session_factory):
    from app.jobs.worker import JobWorker
    from app.models import STATUS_FAILED, STATUS_SUCCEEDED

    basic_library(plex)
    worker = JobWorker(psettings, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "scan_library", {})
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    assert "Scanning the plex library" in logs(session_factory, job_id)

    plex.root_status = 401
    failing = enqueue(session_factory, "scan_library", {})
    worker.notify()
    job = await wait_for_status(session_factory, failing, STATUS_FAILED)
    await worker.stop()
    assert job.attempts == 1 and TOKEN not in job.error and "rejected the token" in job.error
    await asyncio.sleep(0)


# --------------------------------------------- two Plex albums in one folder with one title


def split_album_library(plex, second_artist=None):
    """Plex lists "Third Round" twice: 5 tracks and 2 tracks, same folder, same title."""
    plex.artists = [PArtist(1, "Manu Katche")] + (
        [PArtist(2, second_artist)] if second_artist else []
    )
    plex.albums = [
        PAlbum(10, 1, "Third Round", 2010),
        PAlbum(11, 2 if second_artist else 1, "Third Round", 2010),
    ]
    folder = "/data/music/Manu Katche/Third Round"
    plex.tracks = [
        PTrack(100 + i, 10, 1, f"Song {i}", i, path=f"{folder}/{i:02d}.flac") for i in range(1, 6)
    ] + [
        PTrack(
            200 + i,
            11,
            2 if second_artist else 1,
            f"Extra {i}",
            5 + i,
            path=f"{folder}/{5 + i:02d}.flac",
        )
        for i in range(1, 3)
    ]


def test_albums_of_one_folder_with_one_title_are_merged(plex, psettings):
    split_album_library(plex)
    output = scan_plex(psettings)
    (artist,) = output.artists
    (album,) = artist.albums
    assert album.title == "Third Round" and len(album.tracks) == 7
    assert album.path == "Manu Katche/Third Round" and album.plex_rating_key == "10"
    assert [t.track_no for t in album.tracks] == list(range(1, 8))


def test_the_merge_also_works_across_two_spellings_of_the_artist(plex, psettings):
    split_album_library(plex, second_artist="manu katche")
    output = scan_plex(psettings)
    assert [a.name for a in output.artists] == ["Manu Katche"]  # the empty one is dropped
    (album,) = output.artists[0].albums
    assert len(album.tracks) == 7


def test_albums_with_different_titles_in_one_folder_stay_apart(plex, psettings):
    split_album_library(plex)
    plex.albums[1] = PAlbum(11, 1, "Third Round (Bonus)", 2010)
    output = scan_plex(psettings)
    assert sorted(a.title for a in output.artists[0].albums) == [
        "Third Round",
        "Third Round (Bonus)",
    ]


def test_the_scan_no_longer_fails_on_the_duplicate_and_stays_stable(
    plex, psettings, session_factory
):
    split_album_library(plex, second_artist="manu katche")
    provider = PlexProvider(psettings)
    summary = run_scan(session_factory, provider, RecordingReporter())
    assert summary.tracks == 7
    again = run_scan(session_factory, provider, RecordingReporter())
    assert (again.files_read, again.tracks_added) == (0, 0)
    with session_factory() as db:
        album = db.scalar(select(LibraryAlbum).where(LibraryAlbum.title == "Third Round"))
        assert (album.track_count, album.source) == (7, "plex")
        assert db.scalar(select(func.count(LibraryAlbum.id))) == 1


def test_a_provider_that_repeats_an_album_key_does_not_break_the_scan(session_factory):
    """Safety net in the saving step, whatever the provider: the same folder and title twice
    (even under two artists) is one album."""
    from app.services.library.provider import (
        LibraryProvider,
        ScannedAlbum,
        ScannedArtist,
        ScannedTrack,
        ScanOutput,
        ScanStats,
    )

    def album(first, count):
        tracks = [
            ScannedTrack(path=f"X/Same/{first + i:02d}.flac", title=f"t{first + i}", file_size=10)
            for i in range(count)
        ]
        return ScannedAlbum(
            "X/Same",
            "Same",
            None,
            "flac",
            "flac_standard",
            None,
            1,
            10 * count,
            False,
            None,
            None,
            None,
            tracks,
        )

    class Twice(LibraryProvider):
        source = "plex"

        def scan(self, known, reporter, *, force=False):
            return ScanOutput(
                artists=[
                    ScannedArtist("One", "One", None, [album(1, 3)]),
                    ScannedArtist("Two", "Two", None, [album(10, 2)]),
                ],
                stats=ScanStats(files_seen=5),
            )

    summary = run_scan(session_factory, Twice(), RecordingReporter())
    assert summary.tracks == 5
    with session_factory() as db:
        (row,) = db.scalars(select(LibraryAlbum)).all()
        assert (row.track_count, row.total_size) == (5, 50)


class DbLogReporter(RecordingReporter):
    """Logs like the real ScanReporter: through its own connection to the database."""

    def __init__(self, factory, job_id):
        super().__init__()
        self._factory, self._job_id = factory, job_id

    def log(self, message, level="info"):
        from app.jobs import engine

        super().log(message, level)
        with self._factory() as db:
            engine.add_log(db, self._job_id, level, message)


def test_the_purge_survives_the_job_log_being_written_during_it(
    plex, psettings, session_factory, tmp_path
):
    """The log line about the removed rows is written while the scan still holds the deletes:
    it must not wait for its own lock (that rolled the whole purge back, leaving duplicates)."""
    from app.jobs import engine

    run_scan(session_factory, local_album(tmp_path), RecordingReporter())
    basic_library(plex)
    with session_factory() as db:
        job_id = engine.enqueue(db, "scan_library").id
    reporter = DbLogReporter(session_factory, job_id)
    summary = run_scan(session_factory, PlexProvider(psettings), reporter)
    assert summary.other_source_albums_removed == 1
    with session_factory() as db:
        assert {a.source for a in db.scalars(select(LibraryAlbum))} == {"plex"}
        titles = [a.title for a in db.scalars(select(LibraryAlbum))]
        assert len(titles) == len(set(titles))
