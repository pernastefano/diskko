"""Connection test of an integration. Runs as a job: no slow I/O inside HTTP requests."""

from app.integrations.base import run_check
from app.integrations.status import build_integration
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.models import utcnow
from app.services.app_settings import set_setting


@job_handler("integration_check", queue="default", max_attempts=1)
async def integration_check(ctx: JobContext) -> None:
    name = str(ctx.payload.get("name", ""))
    integration = build_integration(name, ctx.settings)
    if integration is None:
        raise PermanentJobError(f"unknown integration: {name}")
    await ctx.log(f"Testing connection to {name}")
    result = await run_check(integration)
    record = result.to_dict() | {"checked_at": utcnow().isoformat(timespec="seconds")}

    def store(db):
        set_setting(db, f"integration_status.{name}", record)
        db.commit()

    await ctx.run_db(store)
    await ctx.log(f"Result: {result.code}" + (f" ({result.detail})" if result.detail else ""))
