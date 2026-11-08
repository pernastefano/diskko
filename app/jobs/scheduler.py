"""Recurring jobs (APScheduler). The scheduler only enqueues; workers do the work."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
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
    Schedule("sync_pending_artists", "30 4 * * *"),
    Schedule("fetch_artist_images", "40 4 * * *"),
    Schedule("queue_pending_covers", "50 4 * * *"),
]


def schedules_for(settings: Settings) -> list[Schedule]:
    """Fixed schedules plus the nightly library scan when LIBRARY_SCAN_CRON is set."""
    result = list(SCHEDULES)
    cron = settings.library_scan_cron.strip()
    if cron:
        result.append(Schedule("scan_library", cron))
    backup_cron = settings.backup_cron.strip()
    if backup_cron:
        result.append(Schedule("backup_db", backup_cron))
    if settings.status_check_minutes > 0:
        result.append(
            Schedule("check_integrations", status_check_cron(settings.status_check_minutes))
        )
    return result


def status_check_cron(minutes: int) -> str:
    """Crontab for "every N minutes" (an hour or more means every hour)."""
    return f"*/{minutes} * * * *" if minutes < 60 else "0 * * * *"


# These also run right away, not only at the next tick: the status icon needs a first answer.
RUN_AT_ONCE = frozenset({"check_integrations"})


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
        self._follows_settings = schedules is None
        self.schedules = schedules_for(settings) if schedules is None else schedules
        self._scheduler = AsyncIOScheduler(timezone=_timezone(settings.tz))

    def _add(self, schedule: Schedule, *, now: bool = False) -> None:
        try:
            trigger = CronTrigger.from_crontab(schedule.cron, timezone=self._scheduler.timezone)
        except ValueError:
            log.error("invalid cron %r for %s: not scheduled", schedule.cron, schedule.job_type)
            return
        self._scheduler.add_job(
            self._fire,
            trigger,
            args=[schedule],
            id=schedule.dedupe_key,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            **({"next_run_time": datetime.now(self._scheduler.timezone)} if now else {}),
        )

    def start(self) -> None:
        for schedule in self.schedules:
            self._add(schedule, now=schedule.job_type in RUN_AT_ONCE)
        self._scheduler.start()

    def reschedule(self, settings: Settings) -> None:
        """Follow changed settings (e.g. a new scan time) without a restart."""
        if not self._follows_settings:
            return
        self.schedules = schedules_for(settings)
        wanted = {s.dedupe_key for s in self.schedules}
        for job in self._scheduler.get_jobs():
            if job.id not in wanted:
                self._scheduler.remove_job(job.id)
        for schedule in self.schedules:
            # saving settings re-checks the services at once (a new key, a new URL...)
            self._add(schedule, now=schedule.job_type in RUN_AT_ONCE)

    async def _fire(self, schedule: Schedule) -> None:
        await asyncio.to_thread(enqueue_scheduled, self._factory, schedule)
        if self._notify:
            self._notify()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def job_ids(self) -> list[str]:
        return [job.id for job in self._scheduler.get_jobs()]
