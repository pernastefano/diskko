"""Demo job: 30 one-second steps with progress, logs, cancellation and resume."""

import asyncio

from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler


@job_handler("demo", queue="default", max_attempts=3, backoff_base=5)
async def demo(ctx: JobContext) -> None:
    steps = int(ctx.payload.get("steps", 30))
    step_seconds = float(ctx.payload.get("step_seconds", 1.0))
    fail_times = int(ctx.payload.get("fail_times", 0))  # fail the first N attempts (tests)
    fail_at = int(ctx.payload.get("fail_at", steps // 2))

    if ctx.payload.get("permanent_fail"):
        raise PermanentJobError("demo: permanent failure requested")

    state = await ctx.get_state()
    done = int(state.get("done", 0))
    if done:
        await ctx.log(f"Resuming from step {done}/{steps}")
    else:
        await ctx.log(f"Starting demo job ({steps} steps)")

    for i in range(done, steps):
        await ctx.checkpoint()
        await asyncio.sleep(step_seconds)
        if ctx.attempt <= fail_times and i + 1 == fail_at:
            raise RuntimeError(f"demo: simulated failure at step {i + 1}")
        done = i + 1
        await ctx.save_state({"done": done})
        await ctx.progress(done * 100 / steps, f"Step {done}/{steps}")
        if done % 5 == 0 or done == steps:
            await ctx.log(f"Reached step {done}/{steps}")
