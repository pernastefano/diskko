"""Importing this package registers every job handler."""

from app.jobs.handlers import (  # noqa: F401
    demo,
    fetch_covers,
    housekeeping,
    integration_check,
    reclassify_quality,
    scan_library,
    sync_artist,
    sync_pending,
)
