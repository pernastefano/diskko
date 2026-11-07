"""asyncio workers: N loops per queue, each claiming and running one job at a time."""

import asyncio
import contextlib
import json
import logging

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.jobs import engine, registry
from app.jobs.context import JobContext, redact
from app.jobs.errors import JobCancelled, PermanentJobError, RetryLater
from app.models import Job

log = logging.getLogger("diskko.jobs")


class JobWorker:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        self.settings = settings
        self._factory = session_factory
        self._tasks: list[asyncio.Task[None]] = []
        self._target: dict[str, int] = {}  # wanted loops per queue
        self._active: dict[str, int] = {}  # loops currently running per queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self.running: dict[int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> int:
        """Recover interrupted jobs and start the queue loops. Returns recovered count."""
        registry.load_handlers()
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        recovered = await asyncio.to_thread(self._recover)
        self._active = dict.fromkeys(registry.queue_names(), 0)
        self._target = self._targets()
        for queue in self._active:
            self._spawn_missing(queue)
        return recovered

    def _targets(self) -> dict[str, int]:
        concurrency = self.settings.queue_concurrency
        return {queue: concurrency.get(queue, 1) for queue in registry.queue_names()}

    def _spawn_missing(self, queue: str) -> None:
        while self._active[queue] < self._target[queue]:
            self._active[queue] += 1
            self._tasks.append(
                asyncio.create_task(
                    self._queue_loop(queue), name=f"jobs-{queue}-{self._active[queue]}"
                )
            )

    def update_settings(self, settings: Settings) -> None:
        """Use new settings (thread-safe). More workers start at once; fewer workers retire
        after finishing the job they are running, so nothing is interrupted."""
        self.settings = settings
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._reconfigure)

    def _reconfigure(self) -> None:
        if self._stopping:
            return
        self._target = self._targets()
        for queue in self._active:
            self._spawn_missing(queue)
        self._wake.set()

    def _recover(self) -> int:
        with self._factory() as db:
            return engine.recover_interrupted(db)

    async def stop(self) -> None:
        """Stop the loops; running jobs are put back in the queue (see _run_job)."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def notify(self) -> None:
        """Wake the loops up (thread-safe). Call after enqueuing a job."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    # ------------------------------------------------------------------ loops

    async def _queue_loop(self, queue: str) -> None:
        interval = self.settings.job_poll_interval
        while not self._stopping:
            if self._active[queue] > self._target[queue]:  # fewer workers wanted: retire
                self._active[queue] -= 1
                return
            job = await asyncio.to_thread(self._claim, queue)
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=interval)
                self._wake.clear()
                continue
            await self._run_job(job)

    def _claim(self, queue: str) -> Job | None:
        with self._factory() as db:
            return engine.claim_next(db, queue)

    async def _run_job(self, job: Job) -> None:
        secrets = self.settings.secret_values
        spec = registry.get_spec(job.type)
        if spec is None:
            await asyncio.to_thread(self._fail, job.id, f"Unknown job type: {job.type}")
            return
        ctx = JobContext(
            job.id,
            job.type,
            json.loads(job.payload_json or "{}"),
            job.attempts,
            self.settings,
            self._factory,
        )
        try:
            await spec.handler(ctx)
        except JobCancelled:
            await asyncio.to_thread(self._call, engine.finish_cancelled, job.id)
        except asyncio.CancelledError:
            # Shutting down: leave the job resumable instead of lost.
            await asyncio.to_thread(
                self._call, engine.requeue_interrupted, job.id, "Interrupted by shutdown"
            )
            raise
        except RetryLater as exc:
            message = redact(str(exc) or "service unavailable", secrets)
            await asyncio.to_thread(self._call, engine.requeue_later, job.id, message, exc.delay)
        except PermanentJobError as exc:
            await asyncio.to_thread(self._fail, job.id, redact(str(exc) or "failed", secrets))
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}", secrets)
            log.warning("job %s (%s) failed: %s", job.id, job.type, message)
            retry_in = None
            if job.attempts < job.max_attempts:
                retry_in = engine.backoff_delay(job.attempts, spec.backoff_base)
            await asyncio.to_thread(self._fail, job.id, message, retry_in)
        else:
            await asyncio.to_thread(self._succeed, job.id)

    # ---------------------------------------------------------- DB helpers

    def _call(self, func, *args) -> None:
        with self._factory() as db:
            func(db, *args)

    def _fail(self, job_id: int, message: str, retry_in: float | None = None) -> None:
        with self._factory() as db:
            engine.finish_failure(
                db, job_id, message, retry_in=retry_in, max_lines=self.settings.job_log_max_lines
            )

    def _succeed(self, job_id: int) -> None:
        with self._factory() as db:
            engine.finish_success(db, job_id, max_lines=self.settings.job_log_max_lines)
