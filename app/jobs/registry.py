"""Registry of job handlers: each job type maps to a coroutine and a queue."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.jobs.context import JobContext

Handler = Callable[["JobContext"], Awaitable[None]]


@dataclass(frozen=True)
class JobSpec:
    type: str
    handler: Handler
    queue: str = "default"
    max_attempts: int = 3
    backoff_base: float = 30.0  # seconds; doubles at every attempt


_REGISTRY: dict[str, JobSpec] = {}


def job_handler(
    type: str, *, queue: str = "default", max_attempts: int = 3, backoff_base: float = 30.0
) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        _REGISTRY[type] = JobSpec(type, func, queue, max_attempts, backoff_base)
        return func

    return decorator


def load_handlers() -> None:
    """Import the handler modules so that their decorators run (idempotent)."""
    from app.jobs import handlers  # noqa: F401


def get_spec(type: str) -> JobSpec | None:
    load_handlers()
    return _REGISTRY.get(type)


def job_types() -> list[str]:
    load_handlers()
    return sorted(_REGISTRY)


def queue_names() -> list[str]:
    load_handlers()
    return sorted({spec.queue for spec in _REGISTRY.values()} | {"default"})
