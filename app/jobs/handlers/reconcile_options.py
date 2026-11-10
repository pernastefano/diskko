"""The releases of another album, for a download that waits for the user.

The user said the files are not the album that was requested but another one of MusicBrainz: this
reads that album's releases and works out how the files fit each, then the request asks again.
"""

from app.integrations.musicbrainz_client import MusicBrainzError
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.handlers.postprocess import load_releases
from app.jobs.registry import job_handler
from app.models import REQ_AWAITING_CHOICE, DownloadRequest, ReleaseGroup
from app.services import downloads as downloads_svc
from app.services.importer.reconcile import build_state


def reconcile_dedupe_key(request_id: int) -> str:
    return f"reconcile_options:{request_id}"


@job_handler("reconcile_options", queue="musicbrainz", max_attempts=2, backoff_base=30)
async def reconcile_options(ctx: JobContext) -> None:
    request_id = int(ctx.payload["request_id"])

    def load(db):
        request = db.get(DownloadRequest, request_id)
        if request is None or request.status != REQ_AWAITING_CHOICE:
            return None
        group = db.get(ReleaseGroup, request.release_group_id)
        state = downloads_svc.release_state_of(request)
        return {
            "files": state["files"],
            "group": {
                "id": group.id,
                "mbid": group.mbid,
                "title": group.title,
                "source": group.source,
            },
        }

    info = await ctx.run_db(load)
    if info is None:
        await ctx.log("Skipped: the request is no longer waiting")
        return
    group, files = info["group"], info["files"]
    await ctx.log(f"Reading the releases of {group['title']}")
    try:
        releases = await load_releases(ctx, group, force=bool(ctx.payload.get("force")))
    except MusicBrainzError:
        raise  # the engine tries again later
    if not releases:
        await ctx.run_db(lambda db: _fail(db, request_id, group))
        raise PermanentJobError("MusicBrainz lists no release for this album")
    state = build_state(group, files, releases, group["title"])

    def save(db):
        downloads_svc.store_release_options(db, request_id, state)
        db.commit()

    await ctx.run_db(save)
    best = state["options"][0] if state["options"] else None
    await ctx.log(
        f"{len(state['options'])} release(s)"
        + (f"; the best fits {best['paired']} of {len(files)} file(s)" if best else "")
    )
    await ctx.progress(100, "Done")


def _fail(db, request_id: int, group: dict) -> None:
    """No release: keep asking with an empty list, so the user can look for another album."""
    request = db.get(DownloadRequest, request_id)
    if request is not None:
        state = downloads_svc.release_state_of(request)
        state.update(options=[], loading=False)
        downloads_svc.store_release_options(db, request_id, state)
        db.commit()
