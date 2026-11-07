import pytest

from app.services.library.quality import (
    DEFAULT_QUALITY,
    LOSSLESS_TIERS,
    QUALITY_TIERS,
    album_quality,
    classify_track,
    dominant,
    is_lossless_tier,
    tier_rank,
)


def test_tiers_are_ordered_from_worst_to_best():
    assert QUALITY_TIERS == (
        "any",
        "mp3_192",
        "mp3_256",
        "mp3_320",
        "flac_standard",
        "flac_studio",
        "flac_hires",
        "flac_master",
    )
    ranks = [tier_rank(t) for t in QUALITY_TIERS]
    assert ranks == sorted(ranks) and len(set(ranks)) == 8
    assert tier_rank("unknown") == 0


def test_the_default_is_flac_standard():
    assert DEFAULT_QUALITY == "flac_standard" and DEFAULT_QUALITY in QUALITY_TIERS
    assert all(is_lossless_tier(t) for t in LOSSLESS_TIERS)
    assert not any(is_lossless_tier(t) for t in ("any", "mp3_192", "mp3_256", "mp3_320"))


@pytest.mark.parametrize(
    ("bitrate", "expected"),
    [
        (None, "any"),
        (96, "any"),
        (128, "any"),
        (191, "any"),
        (192, "mp3_192"),
        (224, "mp3_192"),
        (256, "mp3_256"),
        (319, "mp3_256"),
        (320, "mp3_320"),
        (512, "mp3_320"),
    ],
)
def test_lossy_tiers_by_bitrate(bitrate, expected):
    assert classify_track(lossless=False, bitrate_kbps=bitrate) == expected


def test_v0_vbr_counts_as_top_lossy_tier():
    assert classify_track(lossless=False, bitrate_kbps=245, v0=True) == "mp3_320"


@pytest.mark.parametrize(
    ("bit_depth", "sample_rate", "expected"),
    [
        (16, 44100, "flac_standard"),  # CD
        (16, 32000, "flac_standard"),  # lower than CD is still lossless
        (None, None, "flac_standard"),  # unknown resolution is read as CD quality
        (16, None, "flac_standard"),
        (None, 44100, "flac_standard"),
        (16, 48000, "flac_studio"),
        (24, 44100, "flac_studio"),
        (24, 48000, "flac_studio"),
        (32, 48000, "flac_studio"),
        (16, 88200, "flac_hires"),
        (24, 88200, "flac_hires"),
        (24, 96000, "flac_hires"),
        (24, 176400, "flac_master"),
        (24, 192000, "flac_master"),
        (16, 192000, "flac_master"),
        (24, 352800, "flac_master"),
    ],
)
def test_lossless_tiers_by_resolution(bit_depth, sample_rate, expected):
    assert (
        classify_track(
            lossless=True, bitrate_kbps=900, bit_depth=bit_depth, sample_rate=sample_rate
        )
        == expected
    )


def test_lossless_ignores_bitrate():
    assert classify_track(lossless=True, bitrate_kbps=10) == "flac_standard"
    assert classify_track(lossless=True, bitrate_kbps=9999) == "flac_standard"


def test_other_lossless_codecs_use_the_same_levels():
    """ALAC, WAV... are lossless too: the level only depends on the resolution."""
    assert (
        classify_track(lossless=True, bitrate_kbps=None, bit_depth=24, sample_rate=96000)
        == "flac_hires"
    )


def test_resolution_is_ignored_for_lossy_files():
    assert (
        classify_track(lossless=False, bitrate_kbps=320, bit_depth=24, sample_rate=96000)
        == "mp3_320"
    )


def test_album_quality_is_the_weakest_track():
    assert album_quality(["flac_standard", "mp3_256", "flac_master"]) == "mp3_256"
    assert album_quality(["flac_master", "flac_standard"]) == "flac_standard"
    assert album_quality(["flac_hires", "flac_studio"]) == "flac_studio"
    assert album_quality(["mp3_320"]) == "mp3_320"
    assert album_quality([]) == "any"


def test_dominant_value():
    assert dominant(["flac", "mp3", "flac", None]) == "flac"
    assert dominant([None, ""]) is None
