"""Recurring jobs (APScheduler). The scheduler only enqueues; workers do the work."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.jobs import engine

log = logging.getLogger("diskko.scheduler")


@dataclass(frozen=True)
class Schedule:
    job_type: str
    cron: str  # standard 5-field crontab, e.g. "30 3 * * *"
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"scheduled:{self.job_type}"


# Later phases append here (nightly library scan, metadata refresh, ...).
SCHEDULES: list[Schedule] = [
    Schedule("housekeeping", "30 3 * * *"),
]


def schedules_for(settings: Settings) -> list[Schedule]:
    """Fixed schedules plus the nightly library scan when LIBRARY_SCAN_CRON is set."""
    result = list(SCHEDULES)
    cron = settings.library_scan_cron.strip()
    if cron:
        result.append(Schedule("scan_library", cron))
    return result


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("unknown timezone %r, using UTC", name)
        return ZoneInfo("UTC")


def enqueue_scheduled(factory: sessionmaker[Session], schedule: Schedule) -> int:
    """Enqueue one scheduled run; a run still queued/running is not duplicated."""
    with factory() as db:
        return engine.enqueue(
            db, schedule.job_type, schedule.payload, dedupe_key=schedule.dedupe_key
        ).id


class JobScheduler:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        notify=None,
        schedules: list[Schedule] | None = None,
    ) -> None:
        self._factory = factory
        self._notify = notify
        self.schedules = schedules_for(settings) if schedules is None else schedules
        self._scheduler = AsyncIOScheduler(timezone=_timezone(settings.tz))

    def start(self) -> None:
        for schedule in self.schedules:
            try:
                trigger = CronTrigger.from_crontab(schedule.cron, timezone=self._scheduler.timezone)
            except ValueError:
                log.error("invalid cron %r for %s: not scheduled", schedule.cron, schedule.job_type)
                continue
            self._scheduler.add_job(
                self._fire,
                trigger,
                args=[schedule],
                id=schedule.dedupe_key,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        self._scheduler.start()

    async def _fire(self, schedule: Schedule) -> None:
        await asyncio.to_thread(enqueue_scheduled, self._factory, schedule)
        if self._notify:
            self._notify()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def job_ids(self) -> list[str]:
        return [job.id for job in self._scheduler.get_jobs()]
