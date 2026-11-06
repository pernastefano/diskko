"""Thin, mockable connection checks for external services (full clients come later)."""

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

import httpx

from app.config import Settings

CHECK_TIMEOUT = 10.0


@dataclass
class CheckResult:
    ok: bool
    # Machine-readable outcome; the UI translates `integration.result.<code>`.
    code: str
    detail: str = ""
    latency_ms: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Integration(ABC):
    name: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    async def check(self, client: httpx.AsyncClient) -> CheckResult: ...


async def run_check(integration: Integration, transport: httpx.AsyncBaseTransport | None = None):
    """Run a check; never raises and never includes secrets or URLs in the result."""
    if not integration.is_configured():
        return CheckResult(False, "not_configured")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT, transport=transport) as client:
            result = await integration.check(client)
    except httpx.HTTPError as exc:
        # Only the exception class: messages can contain the request URL.
        result = CheckResult(False, "unreachable", type(exc).__name__)
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result
