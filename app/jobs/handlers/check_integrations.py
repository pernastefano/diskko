"""Check the connection to every configured service (scheduled; feeds the status icon)."""

from app.integrations.base import run_check
from app.integrations.status import INTEGRATION_NAMES, build_integration, record_result
from app.jobs.context import JobContext
from app.jobs.registry import job_handler


@job_handler("check_integrations", queue="default", max_attempts=1)
async def check_integrations(ctx: JobContext) -> None:
    names = [
        n
        for n in INTEGRATION_NAMES
        if (integration := build_integration(n, ctx.settings)) and integration.is_configured()
    ]
    for done, name in enumerate(names):
        result = await run_check(build_integration(name, ctx.settings))

        def store(db, name=name, result=result):
            record_result(db, name, result)
            db.commit()

        await ctx.run_db(store)
        await ctx.log(f"{name}: {result.code}" + (f" ({result.detail})" if result.detail else ""))
        await ctx.progress(100 * (done + 1) / len(names), name)
    await ctx.log(f"Checked {len(names)} configured service(s)")
