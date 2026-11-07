import pytest

from app.quality import (
    DEFAULT_PROFILES,
    DEFAULT_QUALITY,
    KIND_ANY,
    KIND_LOSSLESS,
    KIND_LOSSY,
    Profile,
    ProfileSet,
    dominant,
    slugify,
)

P = DEFAULT_PROFILES


def test_the_initial_profiles_are_ordered_from_worst_to_best():
    assert P.keys == (
        "any",
        "mp3_192",
        "mp3_256",
        "mp3_320",
        "flac_standard",
        "flac_hires",
        "flac_master",
    )
    ranks = [P.rank(k) for k in P.keys]
    assert ranks == sorted(ranks) and len(set(ranks)) == 7
    assert P.rank("unknown") == -1 and P.get("unknown") is None
    assert P.name("unknown") == "unknown"


def test_the_default_is_flac_standard():
    assert DEFAULT_QUALITY == "flac_standard" and P.get(DEFAULT_QUALITY)


@pytest.mark.parametrize(
    ("bitrate", "expected"),
    [
        (None, "any"),
        (96, "any"),
        (191, "any"),
        (192, "mp3_192"),
        (224, "mp3_192"),
        (256, "mp3_256"),
        (319, "mp3_256"),
        (320, "mp3_320"),
        (512, "mp3_320"),
    ],
)
def test_lossy_profiles_by_bitrate(bitrate, expected):
    assert P.classify(lossless=False, bitrate_kbps=bitrate) == expected


def test_v0_counts_where_the_profile_accepts_it():
    assert P.classify(lossless=False, bitrate_kbps=245, v0=True) == "mp3_320"
    no_v0 = ProfileSet(
        [
            Profile("low", "Low", KIND_ANY, 1),
            Profile("high", "High", KIND_LOSSY, 2, min_bitrate=320, accept_v0=False),
        ]
    )
    assert no_v0.classify(lossless=False, bitrate_kbps=245, v0=True) == "low"


@pytest.mark.parametrize(
    ("bit_depth", "sample_rate", "expected"),
    [
        (16, 44100, "flac_standard"),
        (None, None, "flac_standard"),  # unknown resolution is read as CD quality
        (16, None, "flac_standard"),
        (None, 44100, "flac_standard"),
        (16, 48000, "flac_standard"),
        (24, 44100, "flac_standard"),  # 24-bit alone is not Hi-Res
        (24, 48000, "flac_standard"),
        (16, 96000, "flac_standard"),  # a high rate alone is not Hi-Res either
        (16, 192000, "flac_standard"),
        (24, 88200, "flac_hires"),
        (24, 96000, "flac_hires"),
        (24, 176400, "flac_master"),
        (24, 192000, "flac_master"),
        (32, 352800, "flac_master"),
    ],
)
def test_lossless_profiles_by_resolution(bit_depth, sample_rate, expected):
    got = P.classify(lossless=True, bitrate_kbps=900, bit_depth=bit_depth, sample_rate=sample_rate)
    assert got == expected


def test_lossless_ignores_bitrate():
    assert P.classify(lossless=True, bitrate_kbps=10) == "flac_standard"
    assert P.classify(lossless=True, bitrate_kbps=9999) == "flac_standard"


def test_lossless_below_every_lossless_profile_still_beats_lossy_ones():
    assert (
        P.classify(lossless=True, bitrate_kbps=None, bit_depth=16, sample_rate=32000) == "mp3_320"
    )


def test_resolution_is_ignored_for_lossy_files():
    assert (
        P.classify(lossless=False, bitrate_kbps=320, bit_depth=24, sample_rate=96000) == "mp3_320"
    )


def test_a_file_that_meets_nothing_gets_the_lowest_profile():
    only_flac = ProfileSet(
        [Profile("f", "F", KIND_LOSSLESS, 1, min_bit_depth=16, min_sample_rate=44100)]
    )
    assert only_flac.classify(lossless=False, bitrate_kbps=128) == "f"


def test_album_quality_is_the_weakest_track():
    assert P.album_quality(["flac_standard", "mp3_256", "flac_master"]) == "mp3_256"
    assert P.album_quality(["flac_master", "flac_standard"]) == "flac_standard"
    assert P.album_quality(["mp3_320"]) == "mp3_320"
    assert P.album_quality([]) == "any"
    assert P.album_quality(["flac_master", "gone_profile"]) == "gone_profile"  # unknown is worst


def test_profile_sets_validate_themselves():
    with pytest.raises(ValueError):
        ProfileSet([])
    with pytest.raises(ValueError):
        ProfileSet([Profile("a", "A", KIND_ANY, 1), Profile("a", "B", KIND_ANY, 2)])


def test_dominant_value():
    assert dominant(["flac", "mp3", "flac", None]) == "flac"
    assert dominant([None, ""]) is None


def test_slugs_are_stable_ascii_keys():
    assert slugify("FLAC Master (24/192)") == "flac_master_24_192"
    assert slugify("Qualità alta") == "qualit_alta"
    assert slugify("!!!") == "profile"
    assert slugify("A", ["a", "a_2"]) == "a_3"
