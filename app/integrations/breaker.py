"""Circuit breaker per external service.

After `provider_breaker_threshold` consecutive failures (the service did not answer, even after
the client's own retries) a service is paused for `provider_breaker_cooldown_minutes`. While it
is paused, requests fail at once with ServiceBreakerOpen instead of piling up against a service
that is down; jobs that use a fallback provider move on to it, the others go back in the queue
(RetryLater) and wait. After the pause one request is let through: if it fails the pause starts
again, if it succeeds the service is back. "Not found" answers are successes.

The state lives in memory: a restart gives every service a fresh start.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.jobs.errors import RetryLater


class ServiceBreakerOpen(RetryLater):
    """The service is paused after repeated failures."""

    def __init__(self, service: str, delay: float) -> None:
        super().__init__(f"{service} is paused after repeated failures", delay)
        self.service = service


@dataclass
class _State:
    failures: int = 0
    open_until: float = 0.0


class CircuitBreakers:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._states: dict[str, _State] = {}

    def _state(self, service: str) -> _State:
        return self._states.setdefault(service, _State())

    def remaining(self, service: str) -> float:
        return max(self._state(service).open_until - self._clock(), 0.0)

    def check(self, service: str, threshold: int) -> None:
        state = self._state(service)
        now = self._clock()
        if state.open_until > now:
            raise ServiceBreakerOpen(service, state.open_until - now)
        if state.open_until:  # the pause is over: one failed request closes the door again
            state.open_until = 0.0
            state.failures = max(threshold - 1, 0)

    def failure(self, service: str, threshold: int, cooldown: float) -> None:
        state = self._state(service)
        state.failures += 1
        if state.failures >= threshold:
            state.failures = 0
            state.open_until = self._clock() + cooldown

    def success(self, service: str) -> None:
        state = self._state(service)
        state.failures, state.open_until = 0, 0.0

    def reset(self) -> None:
        self._states.clear()


BREAKERS = CircuitBreakers()


def check(settings: Settings, service: str) -> None:
    BREAKERS.check(service, settings.provider_breaker_threshold)


def record_failure(settings: Settings, service: str) -> None:
    BREAKERS.failure(
        service,
        settings.provider_breaker_threshold,
        settings.provider_breaker_cooldown_minutes * 60.0,
    )


def record_success(service: str) -> None:
    BREAKERS.success(service)
