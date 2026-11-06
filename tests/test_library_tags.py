import pytest

from app.services.library.tags import (
    AUDIO_EXTENSIONS,
    UnreadableAudio,
    clean_mbid,
    parse_int,
    parse_year,
    read_audio_file,
)
from tests.audio import make_track

REL = "11111111-1111-4111-8111-111111111111"
RG = "22222222-2222-4222-8222-222222222222"
ART = "33333333-3333-4333-8333-333333333333"
AART = "44444444-4444-4444-8444-444444444444"
REC = "55555555-5555-4555-8555-555555555555"

FULL = dict(
    title="Respect",
    artist="Aretha Franklin",
    albumartist="Aretha Franklin",
    album="I Never Loved a Man",
    track="3/11",
    disc="1/2",
    date="1967-03-10",
    artist_sort="Franklin, Aretha",
    mbid_release=REL,
    mbid_release_group=RG,
    mbid_artist=ART,
    mbid_album_artist=AART,
    mbid_recording=REC,
)


@pytest.mark.parametrize("name", ["t.flac", "t.mp3", "t.ogg", "t.m4a"])
def test_full_tags_are_read_from_every_format(tmp_path, name):
    info = read_audio_file(make_track(tmp_path / name, **FULL))
    assert (info.title, info.artist, info.album_artist) == (
        "Respect",
        "Aretha Franklin",
        "Aretha Franklin",
    )
    assert info.album == "I Never Loved a Man"
    assert (info.track_no, info.disc_no, info.year) == (3, 1, 1967)
    assert info.artist_sort == "Franklin, Aretha"
    assert (info.mbid_release, info.mbid_release_group) == (REL, RG)
    assert (info.mbid_artist, info.mbid_album_artist) == (ART, AART)
    assert info.mbid_recording == REC


def test_recording_id_follows_picard_conventions(tmp_path):
    """Vorbis MUSICBRAINZ_TRACKID, ID3 UFID and MP4 'MusicBrainz Track Id' all mean *recording*."""
    for name in ("r.flac", "r.mp3", "r.m4a"):
        info = read_audio_file(make_track(tmp_path / name, mbid_recording=REC))
        assert info.mbid_recording == REC, name


def test_technical_info_flac(tmp_path):
    info = read_audio_file(make_track(tmp_path / "a.flac", _sample_rate=96000, _bit_depth=24))
    assert (info.codec, info.lossless, info.bit_depth, info.sample_rate) == (
        "flac",
        True,
        24,
        96000,
    )
    assert info.duration == pytest.approx(3.0, abs=0.1)


@pytest.mark.parametrize("bitrate", [128, 192, 256, 320])
def test_technical_info_mp3(tmp_path, bitrate):
    info = read_audio_file(make_track(tmp_path / "a.mp3", _bitrate=bitrate))
    assert (info.codec, info.lossless, info.bitrate) == ("mp3", False, bitrate)
    assert info.v0 is False and info.sample_rate == 44100


def test_technical_info_m4a_aac_and_alac(tmp_path):
    aac = read_audio_file(make_track(tmp_path / "a.m4a", _bitrate=256000))
    assert (aac.codec, aac.lossless, aac.bitrate) == ("aac", False, 256)
    alac = read_audio_file(make_track(tmp_path / "b.m4a", _codec=b"alac", _bit_depth=24))
    assert (alac.codec, alac.lossless, alac.bit_depth) == ("alac", True, 24)


def test_technical_info_ogg_and_wav(tmp_path):
    ogg = read_audio_file(make_track(tmp_path / "a.ogg", _nominal=160000))
    assert (ogg.codec, ogg.lossless, ogg.bitrate) == ("vorbis", False, 160)
    wav = read_audio_file(make_track(tmp_path / "a.wav"))
    assert (wav.codec, wav.lossless, wav.bit_depth) == ("wav", True, 16)


def test_untagged_file_has_no_tags(tmp_path):
    info = read_audio_file(make_track(tmp_path / "a.wav"))
    assert info.title is None and info.artist is None and info.album is None
    assert info.track_no is None and info.mbid_release is None


@pytest.mark.parametrize("name", ["t.flac", "t.mp3", "t.m4a"])
def test_compilation_flag(tmp_path, name):
    on = read_audio_file(make_track(tmp_path / name, compilation=True))
    assert on.compilation is True
    off = read_audio_file(make_track(tmp_path / f"off_{name}", title="x", compilation=False))
    assert off.compilation is False


def test_unicode_tags(tmp_path):
    info = read_audio_file(
        make_track(tmp_path / "a.flac", artist="Björk", album="Homogénic", title="Jóga 日本語")
    )
    assert (info.artist, info.album, info.title) == ("Björk", "Homogénic", "Jóga 日本語")


def test_invalid_mbid_is_ignored(tmp_path):
    info = read_audio_file(make_track(tmp_path / "a.flac", mbid_release="not-a-uuid"))
    assert info.mbid_release is None


def test_corrupt_and_non_audio_files_raise(tmp_path):
    junk = tmp_path / "junk.flac"
    junk.write_bytes(b"this is not audio at all")
    with pytest.raises(UnreadableAudio):
        read_audio_file(junk)
    with pytest.raises(UnreadableAudio):
        read_audio_file(tmp_path / "missing.flac")
    truncated = tmp_path / "t.mp3"
    truncated.write_bytes(b"\xff\xfb")
    with pytest.raises(UnreadableAudio):
        read_audio_file(truncated)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3", 3), ("3/12", 3), (" 07 ", 7), ("A3", None), ("", None), (None, None), ("x/9", None)],
)
def test_parse_int(raw, expected):
    assert parse_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1968", 1968), ("1968-03-22", 1968), ("2001-00-00", 2001), ("n/a", None), (None, None)],
)
def test_parse_year(raw, expected):
    assert parse_year(raw) == expected


def test_clean_mbid():
    assert clean_mbid(REL.upper()) == REL
    assert clean_mbid(f" {REL} ") == REL
    assert clean_mbid("1234") is None and clean_mbid(None) is None


def test_supported_extensions():
    for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"):
        assert ext in AUDIO_EXTENSIONS
    for ext in (".jpg", ".m3u", ".cue", ".txt", ".nfo"):
        assert ext not in AUDIO_EXTENSIONS
