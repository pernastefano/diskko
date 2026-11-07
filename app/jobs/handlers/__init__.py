"""Importing this package registers every job handler."""

from app.jobs.handlers import (  # noqa: F401
    check_integrations,
    download_monitor,
    fetch_covers,
    fetch_similar,
    housekeeping,
    integration_check,
    reclassify_quality,
    resolve_previews,
    scan_library,
    search_artists,
    slskd_search,
    sync_artist,
    sync_pending,
)
