"""Importing this package registers every job handler."""

from app.jobs.handlers import demo, housekeeping, integration_check  # noqa: F401
