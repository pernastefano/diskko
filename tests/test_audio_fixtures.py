"""The generated fixtures must be real audio for mutagen, otherwise the other tests mean nothing."""

import mutagen
import pytest

from tests.audio import make_track


@pytest.mark.parametrize("name", ["a.flac", "a.mp3", "a.ogg", "a.m4a", "a.wav"])
def test_fixture_is_recognised_by_mutagen(tmp_path, name):
    path = make_track(tmp_path / name)
    audio = mutagen.File(path)
    assert audio is not None and audio.info.length > 0


def test_flac_fixture_parameters(tmp_path):
    info = mutagen.File(make_track(tmp_path / "a.flac", _sample_rate=96000, _bit_depth=24)).info
    assert (info.sample_rate, info.bits_per_sample, round(info.length)) == (96000, 24, 3)


@pytest.mark.parametrize("bitrate", [128, 192, 256, 320])
def test_mp3_fixture_bitrate(tmp_path, bitrate):
    info = mutagen.File(make_track(tmp_path / "a.mp3", _bitrate=bitrate)).info
    assert info.bitrate == bitrate * 1000


def test_m4a_fixture_alac_variant(tmp_path):
    info = mutagen.File(make_track(tmp_path / "a.m4a", _codec=b"alac", _bit_depth=24)).info
    assert info.codec.startswith("alac")


def test_tags_survive_a_roundtrip(tmp_path):
    flac = make_track(tmp_path / "a.flac", artist="Aretha", album="Lady Soul", track="1/10")
    tags = mutagen.File(flac).tags
    assert tags["artist"] == ["Aretha"] and tags["tracknumber"] == ["1/10"]
