"""Login throttling: too many failed sign-ins from one address or for one account pause it.

In memory and per process (DISKKO runs as one process), so a restart forgets it, which is fine for
a brake against guessing. Both the address and the account are counted: a spread-out attack on one
account and one address trying many accounts are both stopped. A success clears the account.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app.config import Settings


class LoginThrottle:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _keys(ip: str, username: str) -> tuple[str, str]:
        return f"ip:{ip}", f"user:{username.strip().lower()[:64]}"

    def _recent(self, key: str, window: float) -> deque[float]:
        hits = self._failures[key]
        cutoff = self._clock() - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._failures.pop(key, None)
            return deque()
        return hits

    def _sweep(self, window: float) -> None:
        """Forget the keys whose last failure left the window: attempts under made-up names
        would otherwise pile up for ever (keys are only looked at when they are asked for)."""
        cutoff = self._clock() - window
        for key in [k for k, hits in self._failures.items() if not hits or hits[-1] <= cutoff]:
            del self._failures[key]

    def retry_after(self, settings: Settings, ip: str, username: str) -> int:
        """Seconds to wait before another attempt, 0 when it is allowed."""
        window = float(settings.login_window_seconds)
        waits = []
        with self._lock:
            if len(self._failures) > SWEEP_ABOVE:
                self._sweep(window)
            for key, limit in zip(
                self._keys(ip, username),
                (settings.login_max_attempts_ip, settings.login_max_attempts),
                strict=True,
            ):
                hits = self._recent(key, window)
                if limit > 0 and len(hits) >= limit:
                    waits.append(hits[0] + window - self._clock())
        return max(1, int(max(waits) + 0.999)) if waits else 0

    def failed(self, ip: str, username: str) -> None:
        with self._lock:
            for key in self._keys(ip, username):
                self._failures[key].append(self._clock())

    def succeeded(self, username: str) -> None:
        with self._lock:
            self._failures.pop(self._keys("", username)[1], None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()


SWEEP_ABOVE = 2000  # keys kept before stale ones are dropped
THROTTLE = LoginThrottle()


def client_ip(request: Request, settings: Settings) -> str:
    """The address of the browser. X-Forwarded-For is read only when the reverse proxy in front
    is trusted (TRUST_PROXY=true): otherwise anybody could pick the address they are counted as."""
    if settings.trust_proxy:
        hops = [
            h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()
        ]
        n = max(1, settings.trust_proxy_hops)
        if len(hops) >= n:  # the entry our own proxy added; what is left of it is client-written
            return hops[-n][:64]
    return request.client.host if request.client else "unknown"
