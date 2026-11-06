"""Quality tiers, in the order of the quality profiles of PLAN section 4."""

from collections import Counter
from collections.abc import Iterable

# Lowest to highest.
QUALITY_TIERS = ("any", "mp3_192", "mp3_256", "mp3_320", "lossless", "lossless_24")

_RANK = {tier: i for i, tier in enumerate(QUALITY_TIERS)}


def tier_rank(tier: str) -> int:
    return _RANK.get(tier, 0)


def classify_track(
    *,
    lossless: bool,
    bitrate_kbps: int | None,
    bit_depth: int | None = None,
    v0: bool = False,
) -> str:
    """Map a track to a quality tier.

    Lossless files are `lossless` (`lossless_24` from 24 bit up). Lossy files are
    ranked by bitrate; a LAME V0 VBR file counts as the top lossy tier ("320/V0").
    """
    if lossless:
        return "lossless_24" if (bit_depth or 0) >= 24 else "lossless"
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
