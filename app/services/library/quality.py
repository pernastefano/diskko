"""Quality profiles live in app.quality (a leaf module, so the configuration can use them)."""

from app.quality import (
    DEFAULT_PROFILES,
    DEFAULT_QUALITY,
    LOSSLESS_CODECS,
    Profile,
    ProfileSet,
    dominant,
)

__all__ = [
    "DEFAULT_PROFILES",
    "DEFAULT_QUALITY",
    "LOSSLESS_CODECS",
    "Profile",
    "ProfileSet",
    "dominant",
]
