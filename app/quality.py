"""Quality profiles: an ordered ladder that tells how good a file or an album is.

Profiles are data (see the `quality_profiles` table, editable in the settings). Each one has
a kind that decides its criteria:

* lossless: at least `min_bit_depth` bits and `min_sample_rate` Hz (unknown values are read as
  16-bit / 44.1 kHz);
* lossy: at least `min_bitrate` kbps, or a LAME V0 file when `accept_v0` is on (a lossless file
  always satisfies a lossy profile);
* any: no criteria.

A track gets the highest profile whose criteria it meets, or the lowest one when it meets none.
An album is as good as its weakest track.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

KIND_LOSSLESS = "lossless"
KIND_LOSSY = "lossy"
KIND_ANY = "any"
KINDS = (KIND_LOSSLESS, KIND_LOSSY, KIND_ANY)

# Badge colours: a fixed palette, so the dark theme has a rule for every one of them.
COLORS = ("slate", "indigo", "green", "red", "amber", "blue", "sky", "purple", "teal", "fuchsia")

# The values a profile can ask for: the usual standards, offered as choices in the settings.
BIT_DEPTHS = (8, 16, 20, 24, 32)
SAMPLE_RATES_HZ = (
    22_050,
    32_000,
    44_100,
    48_000,
    88_200,
    96_000,
    176_400,
    192_000,
    352_800,
    384_000,
)
BITRATES_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)

LOSSLESS_CODECS = frozenset({"flac", "alac", "wav", "aiff", "ape", "wavpack", "tta"})

DEFAULT_QUALITY = "flac_standard"

# Reading unknown resolutions as CD quality.
ASSUMED_BIT_DEPTH = 16
ASSUMED_SAMPLE_RATE = 44_100


@dataclass(frozen=True)
class Profile:
    key: str  # stable identifier stored on albums and tracks; never changes after creation
    name: str
    kind: str
    rank: int  # higher is better
    min_bit_depth: int | None = None
    min_sample_rate: int | None = None  # Hz
    min_bitrate: int | None = None  # kbps
    accept_v0: bool = False
    color: str = "slate"

    def matches(
        self,
        *,
        lossless: bool,
        bitrate_kbps: int | None,
        bit_depth: int | None,
        sample_rate: int | None,
        v0: bool,
    ) -> bool:
        if self.kind == KIND_ANY:
            return True
        if self.kind == KIND_LOSSLESS:
            return (
                lossless
                and (bit_depth or ASSUMED_BIT_DEPTH) >= (self.min_bit_depth or 0)
                and (sample_rate or ASSUMED_SAMPLE_RATE) >= (self.min_sample_rate or 0)
            )
        return lossless or (self.accept_v0 and v0) or (bitrate_kbps or 0) >= (self.min_bitrate or 0)


# The profiles a new installation starts with. Rates are the lower bounds of the families:
# 88.2 kHz counts as Hi-Res and 176.4 kHz as Master, like 96 and 192.
SEED_PROFILES: tuple[Profile, ...] = (
    Profile("any", "Qualsiasi / bassa", KIND_ANY, 1, color="red"),
    Profile("mp3_192", "MP3 ≥ 192", KIND_LOSSY, 2, min_bitrate=192, color="amber"),
    Profile("mp3_256", "MP3 ≥ 256", KIND_LOSSY, 3, min_bitrate=256, color="sky"),
    Profile(
        "mp3_320", "MP3 320 / V0", KIND_LOSSY, 4, min_bitrate=320, accept_v0=True, color="blue"
    ),
    Profile(
        "flac_standard",
        "FLAC Standard (16/44.1)",
        KIND_LOSSLESS,
        5,
        min_bit_depth=16,
        min_sample_rate=44_100,
        color="green",
    ),
    Profile(
        "flac_hires",
        "FLAC Hi-Res (24/96)",
        KIND_LOSSLESS,
        6,
        min_bit_depth=24,
        min_sample_rate=88_200,
        color="purple",
    ),
    Profile(
        "flac_master",
        "FLAC Master (24/192)",
        KIND_LOSSLESS,
        7,
        min_bit_depth=24,
        min_sample_rate=176_400,
        color="fuchsia",
    ),
)


class ProfileSet:
    """The ladder, ready to classify files and compare qualities."""

    def __init__(self, profiles: Iterable[Profile]) -> None:
        self.profiles: tuple[Profile, ...] = tuple(sorted(profiles, key=lambda p: p.rank))
        if not self.profiles:
            raise ValueError("at least one quality profile is required")
        self._by_key = {p.key: p for p in self.profiles}
        if len(self._by_key) != len(self.profiles):
            raise ValueError("quality profile keys must be unique")
        self._best_first = tuple(reversed(self.profiles))

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(p.key for p in self.profiles)

    def get(self, key: str | None) -> Profile | None:
        return self._by_key.get(key or "")

    def name(self, key: str) -> str:
        profile = self.get(key)
        return profile.name if profile else key

    def rank(self, key: str) -> int:
        profile = self.get(key)
        return profile.rank if profile else -1  # an unknown key sorts below everything

    @property
    def lowest(self) -> Profile:
        return self.profiles[0]

    def classify(
        self,
        *,
        lossless: bool,
        bitrate_kbps: int | None,
        bit_depth: int | None = None,
        sample_rate: int | None = None,
        v0: bool = False,
    ) -> str:
        for profile in self._best_first:
            if profile.matches(
                lossless=lossless,
                bitrate_kbps=bitrate_kbps,
                bit_depth=bit_depth,
                sample_rate=sample_rate,
                v0=v0,
            ):
                return profile.key
        return self.lowest.key

    def album_quality(self, keys: Iterable[str]) -> str:
        """An album is as good as its weakest track."""
        present = [k for k in keys if k]
        return min(present, key=self.rank) if present else self.lowest.key


DEFAULT_PROFILES = ProfileSet(SEED_PROFILES)


def dominant(values: Iterable[str | None]) -> str | None:
    """Most common non-empty value (first seen wins ties)."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def slugify(name: str, taken: Iterable[str] = ()) -> str:
    """A stable key from a profile name, unique among `taken`."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower().encode("ascii", "ignore").decode()).strip("_")
    base = (base or "profile")[:40]
    used = set(taken)
    key, n = base, 2
    while key in used:
        key = f"{base}_{n}"
        n += 1
    return key
