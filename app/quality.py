"""Quality tiers, in the order of the quality profiles of PLAN section 4.

Lossy files are ranked by bitrate. Lossless files (FLAC, ALAC, WAV...) are ranked by their
resolution, named after the FLAC levels users know:

    FLAC Standard   16-bit / 44.1 kHz (CD quality)
    FLAC Studio     24-bit at 44.1/48 kHz, or 16-bit at 48 kHz
    FLAC Hi-Res     88.2 / 96 kHz (normally 24-bit)
    FLAC Master     176.4 / 192 kHz (normally 24-bit)
"""

from collections import Counter
from collections.abc import Iterable

# Lowest to highest.
QUALITY_TIERS = (
    "any",
    "mp3_192",
    "mp3_256",
    "mp3_320",
    "flac_standard",
    "flac_studio",
    "flac_hires",
    "flac_master",
)
LOSSLESS_TIERS = ("flac_standard", "flac_studio", "flac_hires", "flac_master")
DEFAULT_QUALITY = "flac_standard"

_RANK = {tier: i for i, tier in enumerate(QUALITY_TIERS)}

# Sample-rate boundaries (Hz) between the lossless tiers.
_STUDIO_RATE = 48_000
_HIRES_RATE = 88_200
_MASTER_RATE = 176_400


def tier_rank(tier: str) -> int:
    return _RANK.get(tier, 0)


def is_lossless_tier(tier: str) -> bool:
    return tier in LOSSLESS_TIERS


def classify_track(
    *,
    lossless: bool,
    bitrate_kbps: int | None,
    bit_depth: int | None = None,
    sample_rate: int | None = None,
    v0: bool = False,
) -> str:
    """Map a track to a quality tier.

    Unknown resolution is read as CD quality (16-bit / 44.1 kHz). A LAME V0 VBR file counts
    as the top lossy tier ("320/V0").
    """
    if lossless:
        rate = sample_rate or 44_100
        bits = bit_depth or 16
        if rate >= _MASTER_RATE:
            return "flac_master"
        if rate >= _HIRES_RATE:
            return "flac_hires"
        if rate >= _STUDIO_RATE or bits >= 24:
            return "flac_studio"
        return "flac_standard"
    if v0:
        return "mp3_320"
    kbps = bitrate_kbps or 0
    if kbps >= 320:
        return "mp3_320"
    if kbps >= 256:
        return "mp3_256"
    if kbps >= 192:
        return "mp3_192"
    return "any"


def album_quality(tiers: Iterable[str]) -> str:
    """An album is as good as its weakest track."""
    ranked = [t for t in tiers if t]
    return min(ranked, key=tier_rank) if ranked else "any"


def dominant(values: Iterable[str | None]) -> str | None:
    """Most common non-empty value (first seen wins ties)."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None
