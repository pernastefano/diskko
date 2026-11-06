"""Album title normalization for matching library albums with MusicBrainz release groups."""

import re

from app.services.library.grouping import name_key

# A bracketed group is "edition noise" when it contains one of these words.
_EDITION_WORDS = (
    r"deluxe|remaster(?:ed)?|expanded|anniversary|bonus|edition|reissue|re-issue|legacy|"
    r"collector|special|extended|digital|explicit|clean|mono|stereo|disc\s*\d+|cd\s*\d+|"
    r"version|mix|hd|hi-?res|24[- ]?bit|\d{4}\s+(?:remaster|mix|version)"
)
_BRACKETED = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
_EDITION_RE = re.compile(rf"\b(?:{_EDITION_WORDS})\b", re.IGNORECASE)
# "Title - Remastered 2011", "Title: Deluxe Edition"
_TRAILING = re.compile(rf"\s*[-:–—]\s*(?:[\w\s]*\b(?:{_EDITION_WORDS})\b[\w\s]*)$", re.IGNORECASE)


def strip_edition(title: str) -> str:
    """Remove edition markers: '(Deluxe Edition)', '[Remastered 2011]', ' - 2019 Remaster'."""

    def drop(match: re.Match) -> str:
        return "" if _EDITION_RE.search(match.group(1)) else match.group(0)

    cleaned = _BRACKETED.sub(drop, title)
    cleaned = _TRAILING.sub("", cleaned)
    return cleaned.strip() or title.strip()


def album_key(title: str) -> str:
    """Comparable form of an album title (case, accents, punctuation and editions ignored)."""
    core = strip_edition(title).replace("&", " and ")
    return name_key(core)
