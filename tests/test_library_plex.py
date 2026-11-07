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


def test_filesystem_and_plex_rows_coexist_without_interfering(
    plex, psettings, session_factory, tmp_path
):
    from app.services.library.filesystem import FilesystemProvider

    root = tmp_path / "music"
    make_track(
        root / "Aretha Franklin/Local Album/01.flac",
        artist="Aretha Franklin",
        albumartist="Aretha Franklin",
        album="Local Album",
    )
    run_scan(session_factory, FilesystemProvider(root), RecordingReporter())
    basic_library(plex)
    run_scan(session_factory, PlexProvider(psettings), RecordingReporter())
    plex.tracks, plex.albums, plex.artists = [], [], []
    run_scan(session_factory, PlexProvider(psettings), RecordingReporter(), max_removal_pct=100)
    with session_factory() as db:
        sources = {t.source for t in db.scalars(select(LibraryTrack))}
        assert sources == {"filesystem"}  # the Plex rows went away, the local ones did not
        assert len(db.scalars(select(Artist).where(Artist.name == "Aretha Franklin")).all()) == 1


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
