"""After a download: check it, tag it from MusicBrainz, rename it and move it into the library.

Every step that can say no raises ImportRejected: the download is then moved to the quarantine
with the reason, the request fails with a clear message and nothing in the library is touched.
Errors that may pass (MusicBrainz or the network) make the job retry, and only the last attempt
fails the request.
"""

import asyncio
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from app.integrations.breaker import ServiceBreakerOpen
from app.integrations.cached_http import ServiceError
from app.integrations.cover_sources import DeezerCoverSource
from app.integrations.coverart import CoverArtClient, CoverArtError
from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzNotConfigured,
    ReleaseData,
    TrackData,
)
from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.errors import JobCancelled, PermanentJobError, RetryLater
from app.jobs.registry import job_handler
from app.models import (
    REQ_COMPLETED,
    REQ_DOWNLOADED,
    REQ_FAILED,
    REQ_IMPORTING,
    REQ_POSTPROCESSING,
    SOURCE_MUSICBRAINZ,
    Artist,
    DownloadRequest,
    ReleaseGroup,
)
from app.services import downloads as downloads_svc
from app.services import plex_refresh as plex_svc
from app.services.importer import library_import as lib
from app.services.importer.integrity import (
    ImportRejected,
    check_files,
    find_files,
    pick_cover_file,
)
from app.services.importer.matching import FileFacts, choose_release, facts_of
from app.services.importer.naming import NameValues
from app.services.importer.tagging import Cover, TaggingError, TrackTags, can_tag
from app.services.quality_profiles import load_profiles

MAX_ATTEMPTS = 3
_UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
RESUMABLE = (REQ_DOWNLOADED, REQ_POSTPROCESSING, REQ_IMPORTING)


def postprocess_dedupe_key(request_id: int) -> str:
    return downloads_svc.postprocess_dedupe_key(request_id)


def _db(func):
    def run(db):
        result = func(db)
        db.commit()
        return result

    return run


def sniff_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def join_credits(credits: list[tuple[str, str | None, str]], fallback: str) -> str:
    """ "A feat. B" from MusicBrainz credits (the join text carries its own spaces)."""
    if not credits:
        return fallback
    return "".join(f"{name}{join}" for name, _, join in credits).strip() or fallback


def mbids_of(credits: list[tuple[str, str | None, str]]) -> list[str]:
    return [m for _, m, _ in credits if m and re.match(_UUID, m)]


def release_types_of(primary: str | None, secondary_json: str) -> list[str]:
    try:
        secondary = [str(s) for s in json.loads(secondary_json or "[]")]
    except ValueError:
        secondary = []
    return [t.lower() for t in ([primary] if primary else []) + secondary]


def build_plan(
    facts: list[FileFacts],
    release: ReleaseData | None,
    pairs: dict[int, TrackData] | None,
    group: dict,
    artist_name: str,
) -> tuple[list[lib.PlannedFile], NameValues]:
    """What every file becomes: its tags and the values for its name. Also the values for the
    album folder."""
    types = release_types_of(group["primary_type"], group["secondary_json"])
    compilation = "compilation" in types
    year = group["year"]
    if release is not None:
        album = release.title or group["title"]
        album_artist = join_credits(release.credits, artist_name)
        date = release.date or (str(year) if year else None)
        year = int(date[:4]) if date and date[:4].isdigit() else year
        per_disc: dict[int, int] = {}
        for t in release.tracks:
            per_disc[t.disc] = per_disc.get(t.disc, 0) + 1
        disc_total = len(per_disc)
        mbid_release, mbid_group = release.mbid, group["mbid"]
        album_artist_ids = mbids_of(release.credits)
    else:
        album, album_artist = group["title"], artist_name
        date = str(year) if year else None
        per_disc = {}
        for fact in facts:
            per_disc[fact.disc_no] = per_disc.get(fact.disc_no, 0) + 1
        disc_total = len(per_disc)
        mbid_release = None
        mbid_group = group["mbid"] if group["source"] == SOURCE_MUSICBRAINZ else None
        album_artist_ids = []

    plan: list[lib.PlannedFile] = []
    for i, fact in enumerate(facts):
        track = pairs[i] if pairs is not None else None
        if track is not None:
            title, number, disc = track.title, track.position, track.disc
            artist = join_credits(track.credits, album_artist)
            artist_ids = mbids_of(track.credits) or album_artist_ids
            recording = track.recording_mbid
        else:
            title = fact.title
            number = fact.track_no or (i + 1)
            disc = fact.disc_no
            artist = fact.file.info.artist or album_artist
            artist_ids, recording = album_artist_ids, None
        tags = TrackTags(
            title=title,
            artist=artist,
            album_artist=album_artist,
            album=album,
            track=number,
            disc=disc,
            date=date,
            track_total=per_disc.get(disc),
            disc_total=disc_total,
            compilation=compilation,
            release_types=types,
            mbid_release=mbid_release,
            mbid_release_group=mbid_group,
            mbid_artists=artist_ids,
            mbid_album_artists=album_artist_ids,
            mbid_recording=recording,
        )
        values = NameValues(artist, album, year, number, disc, title)
        plan.append(lib.PlannedFile(fact.file, tags, values, can_tag(fact.file.path)))
    # the folder: album artist, album, year
    return plan, NameValues(album_artist, album, year)


async def _find_cover(ctx: JobContext, group: dict, artist_name: str, images: list[Path]):
    """The front cover to embed: Cover Art Archive (500 px), else an image from the download,
    else Deezer. None when there is none."""
    if group["source"] == SOURCE_MUSICBRAINZ and ctx.settings.coverart_enabled:
        try:
            async with CoverArtClient(ctx.settings) as client:
                found = await client.fetch_front(group["mbid"], size=500)
            if found is not None and (mime := sniff_image(found[0])):
                await ctx.log("Cover from the Cover Art Archive")
                return Cover(found[0], mime)
        except (CoverArtError, ServiceBreakerOpen, ValueError) as exc:
            await ctx.log(f"Cover Art Archive: {exc}", "warning")
    local = pick_cover_file(images)
    if local is not None:
        data = await asyncio.to_thread(local.read_bytes)
        if mime := sniff_image(data):
            await ctx.log(f"Cover from the download ({local.name})")
            return Cover(data, mime)
    try:
        async with DeezerCoverSource(ctx.settings, ctx.session_factory) as source:
            found = await source.fetch(group["mbid"], artist_name, group["title"], None)
        if found is not None and (mime := sniff_image(found[0])):
            await ctx.log("Cover from Deezer")
            return Cover(found[0], mime)
    except (ServiceError, ServiceBreakerOpen) as exc:
        await ctx.log(f"Deezer cover: {exc}", "warning")
    await ctx.log("No cover found: the files get none", "warning")
    return None


async def _releases(ctx: JobContext, group: dict) -> list[ReleaseData]:
    if group["source"] != SOURCE_MUSICBRAINZ:
        return []
    try:
        client = MusicBrainzClient(ctx.settings, ctx.session_factory)
    except MusicBrainzNotConfigured:
        await ctx.log("MusicBrainz is not configured: tagging without a release", "warning")
        return []
    async with client:
        return await client.browse_releases(group["mbid"]) or []


def _already_imported(final: Path, group: dict, count: int, request_id: int, upgrade: bool) -> bool:
    """Is the folder the album of an earlier, interrupted run of this very import?

    The marker the run leaves in its staged folder says so exactly. Without it (an older run) the
    tags are compared, but never for an upgrade: there the folder holds the copy being replaced,
    whose tags look just the same."""
    from app.services.library.tags import UnreadableAudio, read_audio_file

    if lib.is_marked(final, request_id):
        return True
    if upgrade or group["source"] != SOURCE_MUSICBRAINZ or not final.is_dir():
        return False
    found = 0
    for path in final.iterdir():
        if path.suffix.lower() in (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".oga"):
            try:
                if read_audio_file(path).mbid_release_group != group["mbid"]:
                    return False
            except UnreadableAudio:
                return False
            found += 1
    return found == count


@job_handler("postprocess", queue="library", max_attempts=MAX_ATTEMPTS, backoff_base=60)
async def postprocess(ctx: JobContext) -> None:
    request_id = int(ctx.payload["request_id"])

    def load(db):
        request = db.get(DownloadRequest, request_id)
        if request is None:
            return None
        rg = db.get(ReleaseGroup, request.release_group_id)
        artist = db.get(Artist, rg.artist_id)
        return {
            "status": request.status,
            "dest_dir": request.dest_dir,
            "quality_key": request.quality_key,
            "upgrade_from": request.upgrade_from,
            "group": {
                "id": rg.id,
                "mbid": rg.mbid,
                "title": rg.title,
                "year": rg.year,
                "primary_type": rg.primary_type,
                "secondary_json": rg.secondary_types_json,
                "source": rg.source,
                "artist_id": rg.artist_id,
                "hint": rg.track_count_hint,
            },
            "artist": artist.name,
            "profiles": load_profiles(db),
        }

    info = await ctx.run_db(load)
    if info is None:
        raise PermanentJobError(f"download request {request_id} not found")
    if info["status"] not in RESUMABLE:
        await ctx.log(f"Skipped: the request is {info['status']}")
        if info["status"] == REQ_FAILED:  # a retry of this job cannot revive it
            raise PermanentJobError("The request failed: search again from the Requests page")
        return
    settings = ctx.settings
    group, artist_name, profiles = info["group"], info["artist"], info["profiles"]
    staging: Path | None = None
    source: Path | None = None

    def fail(message: str):
        return _db(lambda db: downloads_svc.set_status(db, request_id, REQ_FAILED, error=message))

    try:
        await ctx.run_db(
            _db(lambda db: downloads_svc.set_status(db, request_id, REQ_POSTPROCESSING))
        )
        source = lib.request_dir(settings, info["dest_dir"], request_id)
        await ctx.log(f"Checking the download in {source.name}")
        audio, images = await asyncio.to_thread(find_files, source)
        files = await asyncio.to_thread(check_files, source, audio)
        await ctx.log(f"{len(files)} audio file(s) open correctly")
        await ctx.progress(15, "Checked")
        await ctx.checkpoint()

        # ---- the effective quality, from what the files really are
        tiers = [
            profiles.classify(
                lossless=f.info.lossless,
                bitrate_kbps=f.info.bitrate,
                bit_depth=f.info.bit_depth,
                sample_rate=f.info.sample_rate,
                v0=f.info.v0,
            )
            for f in files
        ]
        album_tier = profiles.album_quality(tiers)
        await ctx.log(f"Quality of the files: {profiles.name(album_tier)}")
        if profiles.rank(album_tier) < profiles.rank(info["quality_key"]):
            raise ImportRejected(
                f"the quality is {profiles.name(album_tier)}, below the minimum "
                f"({profiles.name(info['quality_key'])})"
            )
        if info["upgrade_from"] and profiles.rank(album_tier) <= profiles.rank(
            info["upgrade_from"]
        ):
            raise ImportRejected(
                f"not an upgrade: {profiles.name(album_tier)} is not better than "
                f"{profiles.name(info['upgrade_from'])}"
            )

        # ---- the MusicBrainz release this download is, and which file is which track
        facts = facts_of(files)
        releases = await _releases(ctx, group)
        release = pairs = None
        if releases:
            chosen = choose_release(releases, facts, group["title"])
            if chosen is None:
                counts = sorted({len(r.tracks) for r in releases})
                raise ImportRejected(
                    f"no MusicBrainz release matches these {len(files)} files "
                    f"(releases have {', '.join(map(str, counts))} tracks, or the titles differ)"
                )
            release, pairs = chosen
            await ctx.log(f"MusicBrainz release: {release.title} ({release.date or 'no date'})")
        else:
            await ctx.log("No MusicBrainz release for this album: tagging from the album data")
            if group["hint"] and len(files) < group["hint"]:
                raise ImportRejected(f"{len(files)} tracks, expected at least {group['hint']}")
        await ctx.progress(30, "Release chosen")
        plan, folder_values = build_plan(facts, release, pairs, group, artist_name)
        cover = await _find_cover(ctx, group, folder_values.artist, images)
        await ctx.progress(40, "Cover")

        # ---- copy, tag and rename in a hidden folder inside the library
        await ctx.run_db(_db(lambda db: downloads_svc.set_status(db, request_id, REQ_IMPORTING)))
        final = lib.final_folder(settings, settings.import_folder_template, folder_values)
        music = Path(settings.music_dir)
        staging = music / lib.STAGING_DIR / str(request_id) / final.name
        multi_disc = len({p.tags.disc for p in plan}) > 1

        paths = await asyncio.to_thread(
            lib.stage,
            plan,
            staging,
            file_template=settings.import_file_template,
            multi_disc=multi_disc,
            cover=cover,
        )
        await asyncio.to_thread(lib.verify_staged, paths)
        await asyncio.to_thread(lib.mark_staged, staging, request_id)
        await ctx.log(f"{len(paths)} file(s) tagged and renamed")
        await ctx.progress(80, "Tagged")
        await ctx.checkpoint(force=True)

        # ---- into the library: what is there is overwritten, what is another album's is not
        old = await ctx.run_db(
            lambda db: lib.owned_albums(db, music, group["id"]) if info["upgrade_from"] else []
        )
        old_ids = [o.id for o in old]
        # the files of the new copy, by the names they get in the folder: what an old copy in the
        # same folder must not lose when it is removed
        keep = {(final / p.name).resolve() for p in staging.iterdir() if p.is_file()}
        if final.exists() and not await asyncio.to_thread(
            _already_imported, final, group, len(paths), request_id, bool(info["upgrade_from"])
        ):
            # the folder is there already: its files with the same names are overwritten, the
            # files of other albums of the folder are not (the import is refused then)
            protected = await ctx.run_db(
                lambda db: lib.other_albums_files(db, music, final, old_ids)
            )
            overwritten = await asyncio.to_thread(lib.merge_into, staging, final, protected)
            await ctx.log(
                f"The folder {final.relative_to(music)} exists: its files were overwritten "
                f"({overwritten} file(s) replaced)"
            )
        elif final.exists():
            await ctx.log("The album is already in place (an earlier run): not copying again")
        else:
            await asyncio.to_thread(lib.commit, staging, final)
        await asyncio.to_thread(lib.clean_staging, music, request_id)
        await ctx.log(f"Imported into {final.relative_to(music)}")
        await ctx.progress(88, "In place")

        # ---- the old copies go: the files of the new one are in place, so nothing is lost
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        aside = None
        if settings.keep_replaced:
            replaced_root = music / lib.REPLACED_DIR

            def aside(path: Path) -> None:
                lib.set_aside_files([path], path.parent, replaced_root, request_id, stamp)

        for copy in old:
            left = await asyncio.to_thread(lib.retire_album, copy, music, keep, final, aside=aside)
            where = f" (moved to {lib.REPLACED_DIR})" if settings.keep_replaced else ""
            await ctx.log(f"The old copy in {copy.folder.relative_to(music)} was removed{where}")
            if left:
                await ctx.log(f"Left in place there: {', '.join(left[:5])}", "warning")
        await ctx.progress(90, "Moved")

        # ---- the database
        if settings.library_source.strip().lower() == "filesystem":
            drop = old_ids  # only an upgrade has old albums
            await asyncio.to_thread(
                lib.register,
                ctx.session_factory,
                music,
                final,
                profiles,
                drop_albums=drop,
                match_artist_ids=[group["artist_id"]],
            )
        else:
            if old_ids:  # the replaced copies are gone from disk: forget their rows now
                await asyncio.to_thread(lib.drop_albums, ctx.session_factory, old_ids)
            await ctx.log("Plex library: the new rows come from Plex, as soon as it has the album")

        await asyncio.to_thread(lib.unmark, final, request_id)

        # Plex is told about the new folder; whatever goes wrong there never fails the import
        try:
            if plex_svc.refresh_enabled(settings):
                items = [plex_svc.Pending(str(final), folder_values.album)]
                for copy in old:  # Plex must also look where an old copy was, to drop it
                    gone = copy.folder.parent if copy.folder != final else None
                    if gone is not None and gone.is_dir() and gone != music:
                        items.append(plex_svc.Pending(str(gone), "", check=False))
                await ctx.run_db(
                    lambda db: plex_svc.request_refresh(
                        db, settings, items, parent_job_id=ctx.job_id
                    )
                )
                await ctx.log(
                    "Plex will be told about the album in "
                    f"{settings.plex_refresh_debounce_seconds}s"
                )
            elif settings.library_source.strip().lower() == "plex":
                await ctx.run_db(
                    lambda db: engine.enqueue(
                        db, "scan_library", {}, dedupe_key="scan_library", parent_job_id=ctx.job_id
                    )
                )
                await ctx.log("Plex refresh is off: a library scan was queued instead")
        except Exception as exc:  # noqa: BLE001 - the album is imported, Plex is a courtesy
            await ctx.log(f"Plex could not be notified ({type(exc).__name__})", "warning")

        # ---- the download is no longer needed
        await asyncio.to_thread(shutil.rmtree, source, True)
        await ctx.run_db(
            _db(lambda db: downloads_svc.set_status(db, request_id, REQ_COMPLETED, progress=100.0))
        )
        await ctx.log("Done")
        await ctx.progress(100, "Done")
    except (ImportRejected, TaggingError) as exc:
        reason = str(exc)
        await ctx.log(f"Rejected: {reason}", "error")
        if staging is not None:
            await asyncio.to_thread(lib.clean_staging, Path(settings.music_dir), request_id)
        where = ""
        if source is not None and source.exists():
            moved = await asyncio.to_thread(
                lib.quarantine, source, lib.quarantine_root(settings), request_id, reason
            )
            where = f" (kept in {moved})"
            await ctx.log(f"The download was moved to the quarantine: {moved}", "warning")
        message = f"{reason}{where}"[:500]
        await ctx.run_db(fail(message))
        raise PermanentJobError(message) from exc
    except JobCancelled:
        # cancelled by hand: what was being prepared goes, what was already put in place stays
        if staging is not None:
            await asyncio.to_thread(lib.clean_staging, Path(settings.music_dir), request_id)
        raise
    except RetryLater:
        # a service is paused: waiting is the answer, for a day at most
        if await ctx.run_db(lambda db: engine.waited_too_long(db, ctx.job_id)):
            message = "Import gave up: a service stayed unavailable for a day"
            await ctx.run_db(fail(message))
            raise PermanentJobError(message) from None
        raise
    except Exception as exc:
        # Whatever it is (a service, the disk, a bug): after the last attempt the request must
        # not stay "importing" for ever, with no way to ask again.
        if ctx.attempt >= MAX_ATTEMPTS:
            await ctx.run_db(
                fail(
                    f"Import failed after several attempts ({type(exc).__name__}): see the job log"
                )
            )
        raise
