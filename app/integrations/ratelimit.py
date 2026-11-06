import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """Spaces requests at least `min_interval` seconds apart, across all tasks.

    MusicBrainz answers 503 to *every* request while a client exceeds 1 request/second,
    so the interval is a hard floor. `penalize()` pushes the next slot further away (used
    after a 503). Clock and sleep are injectable so tests never really wait.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock: asyncio.Lock | None = None

    async def wait(self) -> None:
        if self._lock is None:  # created lazily: it binds to the running loop
            self._lock = asyncio.Lock()
        async with self._lock:
            now = self._clock()
            delay = self._next_slot - now
            if delay > 0:
                await self._sleep(delay)
                now = self._clock()
            self._next_slot = max(now, self._next_slot) + self.min_interval

    def penalize(self, seconds: float) -> None:
        self._next_slot = max(self._next_slot, self._clock() + seconds)


_LIMITERS: dict[str, RateLimiter] = {}


def get_limiter(name: str, min_interval: float) -> RateLimiter:
    """Process-wide limiter per service, shared by every client and job."""
    limiter = _LIMITERS.get(name)
    if limiter is None:
        limiter = _LIMITERS[name] = RateLimiter(min_interval)
    limiter.min_interval = min_interval  # follows the current configuration
    return limiter
