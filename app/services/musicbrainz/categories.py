"""User-facing album types ("categories") derived from MusicBrainz primary/secondary types.

A release group has one primary type (Album, Single, EP, Broadcast, Other) and any number
of secondary types (Compilation, Soundtrack, Live, ...). Users think in single values:
"Album + Live" is a *live* album. The first matching secondary type, in the priority order
below, wins; otherwise the primary type is used.
"""

CATEGORIES = (
    "album",
    "ep",
    "single",
    "broadcast",
    "other",
    "soundtrack",
    "compilation",
    "live",
    "remix",
    "dj-mix",
    "mixtape",
    "demo",
    "spokenword",
    "interview",
    "audiobook",
    "audio-drama",
    "field-recording",
)

# MusicBrainz name (casefolded) -> category, in priority order.
_SECONDARY = {
    "soundtrack": "soundtrack",
    "compilation": "compilation",
    "live": "live",
    "remix": "remix",
    "dj-mix": "dj-mix",
    "mixtape/street": "mixtape",
    "demo": "demo",
    "spokenword": "spokenword",
    "interview": "interview",
    "audiobook": "audiobook",
    "audio drama": "audio-drama",
    "field recording": "field-recording",
}
_PRIMARY = {"album", "single", "ep", "broadcast", "other"}

# Studio releases only; everything else is opt-in.
DEFAULT_MISSING_TYPES = ("album", "ep")


def category_for(primary: str | None, secondary: list[str]) -> str:
    present = {s.casefold() for s in secondary}
    for name, category in _SECONDARY.items():
        if name in present:
            return category
    key = (primary or "").casefold()
    return key if key in _PRIMARY else "other"


def year_of(date: str | None) -> int | None:
    """'1968', '1968-03' or '1968-03-22' -> 1968."""
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None
