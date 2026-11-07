"""Which service answers what: a default provider and ordered fallbacks per capability.

The order is a comma separated list in the settings (the first is the default, the others are
tried in turn when it fails or has nothing). Unknown names are ignored; an empty or invalid
list falls back to the built-in order.

Metadata (MusicBrainz) has a single provider: it only gets the circuit breaker and the re-queue.
"""

from app.config import Settings

# capability -> (setting name, providers it may use, in the built-in order)
CAPABILITIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "metadata": ("providers_metadata", ("musicbrainz", "deezer")),
    "covers": ("providers_covers", ("coverartarchive", "deezer")),
    "previews": ("providers_previews", ("itunes", "deezer")),
    "similar": ("providers_similar", ("lastfm", "listenbrainz")),
    "toptracks": ("providers_toptracks", ("deezer", "lastfm")),
    "bio": ("providers_bio", ("wikipedia", "lastfm")),
}


# Capabilities whose first provider cannot change: MusicBrainz is the reference for metadata,
# the others only fill what it does not have.
FIXED_FIRST: dict[str, str] = {"providers_metadata": "musicbrainz"}


def metadata_fallback_enabled(settings: Settings) -> bool:
    """May artists and albums missing from MusicBrainz be filled in from Deezer?"""
    return "deezer" in provider_order(settings, "metadata")


def provider_order(settings: Settings, capability: str) -> list[str]:
    key, known = CAPABILITIES[capability]
    order: list[str] = []
    for name in str(getattr(settings, key, "")).split(","):
        name = name.strip().lower()
        if name in known and name not in order:
            order.append(name)
    first = FIXED_FIRST.get(key)
    if first:
        order = [first, *[n for n in order if n != first]]
    return order or list(known)
