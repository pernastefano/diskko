"""Download front covers (250 px) of an artist's release groups into DATA_DIR/covers.

Providers come from the settings (Cover Art Archive first, Deezer as the fallback by default).
A cover is looked up at each provider in turn until one has it. When a provider fails the next
one is tried; a cover that could not be looked up everywhere stays "unknown" and is retried by
a later job. When no provider answers for a few covers in a row, the job goes back in the
queue after a pause (the attempt is not used) so the rest of the queue keeps moving.
"""

import os
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

from sqlalchemy import select, update

from app.integrations.breaker import ServiceBreakerOpen
from app.integrations.cached_http import ServiceError
from app.integrations.cover_sources import LABELS, SOURCES
from app.integrations.providers import provider_order
from app.jobs.context import JobContext
from app.jobs.errors import RetryLater
from app.jobs.registry import job_handler
from app.models import Artist, ReleaseGroup
from app.services.musicbrainz.missing import get_missing_types, owned_release_group_ids

MAX_CONSECUTIVE_ERRORS = 3
DEFAULT_PAUSE = 120.0  # seconds, when no service says how long it will be paused


def covers_dir(data_dir: Path) -> Path:
    return data_dir / "covers"


def cover_stem(mbid: str) -> str:
    """File name (without extension) of a cover: ids of other providers have a colon."""
    return mbid.replace(":", "-")


def write_atomic(directory: Path, name: str, data: bytes) -> None:
    """Write via a temp file and rename, so a half-written image is never served."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, directory / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@job_handler("fetch_covers", queue="coverart", max_attempts=2, backoff_base=120)
async def fetch_covers(ctx: JobContext) -> None:
    artist_id = int(ctx.payload["artist_id"])
    force = bool(ctx.payload.get("force", False))

    def wanted(db) -> list[tuple[int, str, str, int | None]]:
        if force:  # retry covers that were missing
            db.execute(
                update(ReleaseGroup)
                .where(ReleaseGroup.artist_id == artist_id, ReleaseGroup.cover_status == "missing")
                .values(cover_status="unknown")
            )
            db.commit()
        types = set(get_missing_types(db))
        owned = owned_release_group_ids(db, artist_id)
        rows = db.execute(
            select(
                ReleaseGroup.id,
                ReleaseGroup.mbid,
                ReleaseGroup.category,
                ReleaseGroup.title,
                ReleaseGroup.track_count_hint,
            ).where(ReleaseGroup.artist_id == artist_id, ReleaseGroup.cover_status == "unknown")
        )
        return [
            (rid, mbid, title, hint)
            for rid, mbid, category, title, hint in rows
            if category in types or rid in owned
        ]

    artist_name = await ctx.run_db(lambda db: db.get(Artist, artist_id).name)
    todo = await ctx.run_db(wanted)
    if not todo:
        await ctx.log(f"{artist_name}: no covers to fetch")
        await ctx.progress(100, "Done")
        return

    await ctx.log(
        f"{artist_name}: looking for the cover of {len(todo)} album(s)"
        + (" (again)" if force else "")
    )
    directory = covers_dir(ctx.settings.data_dir)
    saved = missing = errors = consecutive = 0
    paused: set[str] = set()  # providers already reported as paused in this job's log
    pause = 0.0  # the longest pause reported by a paused provider
    async with AsyncExitStack() as stack:
        sources = [
            await stack.enter_async_context(SOURCES[name](ctx.settings, ctx.session_factory))
            for name in provider_order(ctx.settings, "covers")
        ]
        for i, (rg_id, mbid, title, hint) in enumerate(todo, start=1):
            await ctx.checkpoint()
            await ctx.progress(100 * (i - 1) / len(todo), f"{title} ({i}/{len(todo)})")
            found: tuple[bytes, str, str] | None = None
            answered = failed = 0  # providers that gave a definite answer / could not answer
            for source in sources:
                label = LABELS[source.name]
                try:
                    result = await source.fetch(mbid, artist_name, title, hint)
                except ServiceBreakerOpen as exc:
                    failed += 1
                    pause = max(pause, exc.delay)
                    if source.name not in paused:
                        paused.add(source.name)
                        await ctx.log(
                            f"{label} is paused after repeated failures"
                            f" ({exc.delay:.0f}s left): using the other providers",
                            "warning",
                        )
                    continue
                except ServiceError as exc:
                    failed += 1
                    await ctx.log(f"{artist_name} – {title}: {label} failed ({exc})", "warning")
                    continue
                answered += 1
                if result is not None:
                    found = (*result, label)
                    break
            if found is not None:
                consecutive = 0
                data, extension, label = found
                name = f"{cover_stem(mbid)}{extension}"
                write_atomic(directory, name, data)
                saved += 1
                await ctx.run_db(_set_cover(rg_id, name, "ok"))
                await ctx.log(
                    f"{artist_name} – {title}: cover saved ({len(data) // 1024} KB, {label})"
                )
            elif failed:  # somebody could not answer: do not conclude "no cover"
                errors += 1
                consecutive = consecutive + 1 if not answered else 0
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    await ctx.log(
                        f"{artist_name}: no provider is answering, {saved} cover(s) saved so far",
                        "warning",
                    )
                    raise RetryLater("No cover provider is answering", pause or DEFAULT_PAUSE)
            else:
                consecutive = 0
                missing += 1
                await ctx.run_db(_set_cover(rg_id, None, "missing"))
                names = " or ".join(LABELS[s.name] for s in sources)
                await ctx.log(f"{artist_name} – {title}: no cover at {names}")
    await ctx.log(
        f"{artist_name}: {saved} cover(s) saved, {missing} without cover, {errors} error(s)"
    )
    if errors and not saved and not missing:  # nothing could be looked up: retry with backoff
        raise ServiceError(f"{artist_name}: no cover could be looked up ({errors} error(s))")
    await ctx.progress(100, "Done")


def _set_cover(rg_id: int, path: str | None, status: str):
    def run(db) -> None:
        rg = db.get(ReleaseGroup, rg_id)
        if rg is not None:
            rg.cover_path, rg.cover_status = path, status
            db.commit()

    return run
