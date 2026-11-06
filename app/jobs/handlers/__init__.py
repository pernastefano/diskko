"""Importing this package registers every job handler."""

from app.jobs.handlers import (  # noqa: F401
    demo,
    housekeeping,
    integration_check,
    scan_library,
)
