"""Download front covers (250 px) of an artist's release groups into DATA_DIR/covers."""

import os
import tempfile
from pathlib import Path

from sqlalchemy import select, update

from app.integrations.coverart import CoverArtClient, CoverArtError
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.models import ReleaseGroup
from app.services.musicbrainz.missing import get_missing_types, owned_release_group_ids

MAX_CONSECUTIVE_ERRORS = 3


def covers_dir(data_dir: Path) -> Path:
    return data_dir / "covers"


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

    def wanted(db) -> list[tuple[int, str]]:
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
            select(ReleaseGroup.id, ReleaseGroup.mbid, ReleaseGroup.category).where(
                ReleaseGroup.artist_id == artist_id, ReleaseGroup.cover_status == "unknown"
            )
        )
        return [(rid, mbid) for rid, mbid, category in rows if category in types or rid in owned]

    todo = await ctx.run_db(wanted)
    if not todo:
        await ctx.log("No covers to fetch")
        await ctx.progress(100, "Done")
        return

    directory = covers_dir(ctx.settings.data_dir)
    saved = missing = errors = consecutive = 0
    async with CoverArtClient(ctx.settings) as client:
        for i, (rg_id, mbid) in enumerate(todo, start=1):
            await ctx.checkpoint()
            await ctx.progress(100 * (i - 1) / len(todo), f"Cover {i}/{len(todo)}")
            try:
                result = await client.fetch_front(mbid)
            except ValueError:  # not a valid MBID: nothing to look up
                result = None
            except CoverArtError as exc:
                errors += 1
                consecutive += 1
                await ctx.log(f"Cover {mbid}: {exc}", "warning")
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    raise  # let the job engine retry later
                continue
            consecutive = 0
            if result is None:
                missing += 1
                await ctx.run_db(_set_cover(rg_id, None, "missing"))
                continue
            data, extension = result
            name = f"{mbid}{extension}"
            write_atomic(directory, name, data)
            saved += 1
            await ctx.run_db(_set_cover(rg_id, name, "ok"))
    await ctx.log(f"Covers: {saved} saved, {missing} without cover, {errors} error(s)")
    await ctx.progress(100, "Done")


def _set_cover(rg_id: int, path: str | None, status: str):
    def run(db) -> None:
        rg = db.get(ReleaseGroup, rg_id)
        if rg is not None:
            rg.cover_path, rg.cover_status = path, status
            db.commit()

    return run
