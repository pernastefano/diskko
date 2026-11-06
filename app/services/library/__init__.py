"""Library scanning: providers (filesystem, Plex), grouping, quality and persistence."""

from app.config import Settings
from app.models import SOURCE_FILESYSTEM, SOURCE_PLEX
from app.services.library.filesystem import FilesystemProvider
from app.services.library.plex_provider import PlexProvider
from app.services.library.provider import LibraryError, LibraryProvider


def build_provider(settings: Settings) -> LibraryProvider:
    """The provider selected by LIBRARY_SOURCE."""
    source = settings.library_source.strip().lower()
    if source == SOURCE_FILESYSTEM:
        return FilesystemProvider(settings.music_dir)
    if source == SOURCE_PLEX:
        return PlexProvider(settings)
    raise LibraryError(f"Unknown library source: {settings.library_source!r}")
