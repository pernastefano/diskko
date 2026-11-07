"""A job made of timed steps: progress, logs, cancellation, resume and simulated failures.

It exists only for the tests of the job engine, the worker, the scheduler and the pages (the
application itself has no such job). Importing this module registers it; conftest does.
"""

import asyncio

from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler


@job_handler("test.steps", queue="default", max_attempts=3, backoff_base=5)
async def steps_job(ctx: JobContext) -> None:
    steps = int(ctx.payload.get("steps", 30))
    step_seconds = float(ctx.payload.get("step_seconds", 1.0))
    fail_times = int(ctx.payload.get("fail_times", 0))  # fail the first N attempts
    fail_at = int(ctx.payload.get("fail_at", steps // 2))

    if ctx.payload.get("permanent_fail"):
        raise PermanentJobError("permanent failure requested")

    state = await ctx.get_state()
    done = int(state.get("done", 0))
    if done:
        await ctx.log(f"Resuming from step {done}/{steps}")
    else:
        await ctx.log(f"Starting ({steps} steps)")

    for i in range(done, steps):
        await ctx.checkpoint()
        await asyncio.sleep(step_seconds)
        if ctx.attempt <= fail_times and i + 1 == fail_at:
            raise RuntimeError(f"simulated failure at step {i + 1}")
        done = i + 1
        await ctx.save_state({"done": done})
        await ctx.progress(done * 100 / steps, f"Step {done}/{steps}")
        if done % 5 == 0 or done == steps:
            await ctx.log(f"Reached step {done}/{steps}")
