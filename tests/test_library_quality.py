import pytest

from app.services.library.quality import (
    QUALITY_TIERS,
    album_quality,
    classify_track,
    dominant,
    tier_rank,
)


def test_tiers_are_ordered_like_the_quality_profiles():
    assert QUALITY_TIERS == ("any", "mp3_192", "mp3_256", "mp3_320", "lossless", "lossless_24")
    ranks = [tier_rank(t) for t in QUALITY_TIERS]
    assert ranks == sorted(ranks) and len(set(ranks)) == 6


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
    ("bit_depth", "expected"),
    [
        (None, "lossless"),
        (16, "lossless"),
        (20, "lossless"),
        (24, "lossless_24"),
        (32, "lossless_24"),
    ],
)
def test_lossless_tiers_by_bit_depth(bit_depth, expected):
    assert classify_track(lossless=True, bitrate_kbps=900, bit_depth=bit_depth) == expected


def test_lossless_ignores_bitrate():
    assert classify_track(lossless=True, bitrate_kbps=10) == "lossless"


def test_album_quality_is_the_weakest_track():
    assert album_quality(["lossless", "mp3_256", "lossless_24"]) == "mp3_256"
    assert album_quality(["lossless_24", "lossless"]) == "lossless"
    assert album_quality(["mp3_320"]) == "mp3_320"
    assert album_quality([]) == "any"


def test_dominant_value():
    assert dominant(["flac", "mp3", "flac", None]) == "flac"
    assert dominant([None, ""]) is None
