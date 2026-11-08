"""Back up the database: a consistent copy in <data_dir>/backups, the oldest ones pruned."""

import asyncio

from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.services import audit
from app.services.backup import BackupError, create_backup


@job_handler("backup_db", queue="default", max_attempts=1)
async def backup_db(ctx: JobContext) -> None:
    await ctx.log("Backing up the database")
    try:
        backup = await asyncio.to_thread(create_backup, ctx.settings)
    except BackupError as exc:
        raise PermanentJobError(f"Backup failed: {exc}") from exc

    def record(db):
        audit.record(db, None, "backup.created", details={"file": backup.name})
        db.commit()

    await ctx.run_db(record)
    await ctx.log(f"Saved {backup.name} ({backup.size / 1024 / 1024:.1f} MB)")
