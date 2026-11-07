"""Which service answers what: a default provider and ordered fallbacks per capability.

The order is a comma separated list in the settings (the first is the default, the others are
tried in turn when it fails or has nothing). Unknown names are ignored; an empty or invalid
list falls back to the built-in order.

Metadata (MusicBrainz) has a single provider: it only gets the circuit breaker and the re-queue.
"""

from app.config import Settings

# capability -> (setting name, providers it may use, in the built-in order)
CAPABILITIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "covers": ("providers_covers", ("coverartarchive", "deezer")),
    "previews": ("providers_previews", ("itunes", "deezer")),
    "similar": ("providers_similar", ("lastfm", "listenbrainz")),
}


def provider_order(settings: Settings, capability: str) -> list[str]:
    key, known = CAPABILITIES[capability]
    order: list[str] = []
    for name in str(getattr(settings, key, "")).split(","):
        name = name.strip().lower()
        if name in known and name not in order:
            order.append(name)
    return order or list(known)
