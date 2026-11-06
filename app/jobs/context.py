"""The object a job handler receives: progress, logging, cancellation and checkpoints."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.jobs import engine
from app.jobs.errors import JobCancelled

CANCEL_CHECK_INTERVAL = 0.2  # seconds between DB checks in checkpoint()
TRIM_EVERY = 500  # log lines between trims


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


class JobContext:
    def __init__(
        self,
        job_id: int,
        job_type: str,
        payload: dict[str, Any],
        attempt: int,
        settings: Settings,
        session_factory: sessionmaker[Session],
    ) -> None:
        self.job_id = job_id
        self.type = job_type
        self.payload = payload
        self.attempt = attempt
        self.settings = settings
        self._factory = session_factory
        self._last_progress: tuple[int, str | None] | None = None
        self._last_cancel_check = 0.0
        self._log_count = 0

    async def run_db(self, func: Callable[[Session], Any]) -> Any:
        """Run a short DB operation in a worker thread (never block the event loop)."""

        def call() -> Any:
            with self._factory() as db:
                return func(db)

        return await asyncio.to_thread(call)

    async def progress(self, pct: float, step: str | None = None) -> None:
        key = (int(pct), step)
        if key == self._last_progress:
            return
        self._last_progress = key
        await self.run_db(lambda db: engine.update_progress(db, self.job_id, pct, step))

    async def log(self, message: str, level: str = "info") -> None:
        text = redact(str(message), self.settings.secret_values)
        self._log_count += 1
        max_lines = self.settings.job_log_max_lines

        def write(db: Session) -> None:
            engine.add_log(db, self.job_id, level, text)
            if self._log_count % TRIM_EVERY == 0:
                engine.trim_logs(db, self.job_id, max_lines)

        await self.run_db(write)

    async def checkpoint(self, *, force: bool = False) -> None:
        """Raise JobCancelled if the job was cancelled. Call it regularly in long loops."""
        now = time.monotonic()
        if not force and now - self._last_cancel_check < CANCEL_CHECK_INTERVAL:
            return
        self._last_cancel_check = now
        if await self.run_db(lambda db: engine.is_cancel_requested(db, self.job_id)):
            raise JobCancelled

    async def get_state(self) -> dict[str, Any]:
        return await self.run_db(lambda db: engine.get_state(db, self.job_id))

    async def save_state(self, state: dict[str, Any]) -> None:
        json.dumps(state)  # fail early if it is not serializable
        await self.run_db(lambda db: engine.save_state(db, self.job_id, state))
