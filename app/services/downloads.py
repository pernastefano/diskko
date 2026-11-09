"""Download requests: creating them, their state changes, and what the monitor needs."""

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.slskd_client import Transfer, is_failed, is_succeeded
from app.models import (
    MODE_AUTO,
    MODES,
    REQ_ACTIVE,
    REQ_AWAITING_CHOICE,
    REQ_CANCELLED,
    REQ_CANDIDATE_FOUND,
    REQ_DOWNLOADED,
    REQ_DOWNLOADING,
    REQ_FAILED,
    REQ_FINISHED,
    REQ_NO_RESULTS,
    REQ_QUEUED,
    REQ_SEARCHING,
    Artist,
    DownloadFile,
    DownloadRequest,
    Job,
    ReleaseGroup,
    utcnow,
)
from app.services.app_settings import get_setting
from app.services.candidate_selection import Candidate, Wanted, group_by_subdir
from app.services.notifications import notify_request
from app.services.quality_profiles import default_key, load_profiles

DEST_ROOT = "diskko"  # under the downloads folder: <downloads>/diskko/<request id>/...
_UNSAFE = re.compile(r"[^\w .()\[\]\-]")


class RequestError(ValueError):
    """A refused change; `code` is the suffix of an `error.request_*` i18n key."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def slskd_configured(settings) -> bool:
    return bool(settings.slskd_url.strip() and settings.slskd_api_key.strip())


def search_dedupe_key(request_id: int) -> str:
    return f"slskd_search:{request_id}"


def monitor_dedupe_key(request_id: int) -> str:
    return f"download_monitor:{request_id}"


def postprocess_dedupe_key(request_id: int) -> str:
    return f"postprocess:{request_id}"


# ----------------------------------------------------------------------- creating


USER_QUALITY_KEY = "default_quality"  # a user's own setting: the quality of their requests


def user_quality(db: Session, user_id: int) -> str:
    """The quality a user's new requests aim for: their own choice if it is still a profile,
    else the one of the system."""
    chosen = get_setting(db, USER_QUALITY_KEY, "", user_id)
    return chosen if chosen and load_profiles(db).get(chosen) else default_key(db)


def create_request(
    db: Session,
    user_id: int,
    release_group_id: int,
    *,
    default_mode: str = MODE_AUTO,
    mode: str | None = None,
    quality_key: str | None = None,
    query: str | None = None,
    upgrade_from: str | None = None,
) -> tuple[DownloadRequest, bool]:
    """A new request, or the one already running for that album. The caller commits.

    Returns (request, created). One album has at most one active request, whoever made it.
    """
    group = db.get(ReleaseGroup, release_group_id)
    if group is None:
        raise RequestError("request_not_found")
    active = db.scalar(
        select(DownloadRequest).where(
            DownloadRequest.release_group_id == release_group_id,
            DownloadRequest.status.in_(REQ_ACTIVE),
        )
    )
    if active is not None:
        return active, False
    profiles = load_profiles(db)
    wanted_quality = quality_key if profiles.get(quality_key) else user_quality(db, user_id)
    if not profiles.get(wanted_quality):
        wanted_quality = profiles.lowest.key
    chosen_mode = mode if mode in MODES else (default_mode if default_mode in MODES else MODE_AUTO)
    request = DownloadRequest(
        user_id=user_id,
        release_group_id=release_group_id,
        status=REQ_QUEUED,
        quality_key=wanted_quality,
        mode=chosen_mode,
        query_override=" ".join(query.split())[:255] if query and query.strip() else None,
        upgrade_from=upgrade_from,
    )
    db.add(request)
    db.flush()
    request.dest_dir = f"{DEST_ROOT}/{request.id}"
    return request, True


@dataclass(frozen=True)
class RequestInfo:
    id: int
    status: str
    progress: float


def request_infos(db: Session, release_group_ids: list[int]) -> dict[int, RequestInfo]:
    """{release_group_id: its request}: the running one, else the latest failure.

    The library is shared, so a request made by anyone shows for everyone.
    """
    if not release_group_ids:
        return {}
    infos: dict[int, RequestInfo] = {}
    rows = db.execute(
        select(
            DownloadRequest.release_group_id,
            DownloadRequest.id,
            DownloadRequest.status,
            DownloadRequest.progress,
        )
        .where(DownloadRequest.release_group_id.in_(release_group_ids))
        .order_by(DownloadRequest.id)
    )
    for group_id, request_id, status, progress in rows:  # later requests overwrite earlier ones
        infos[group_id] = RequestInfo(request_id, status, progress or 0.0)
    return {
        g: i
        for g, i in infos.items()
        if i.status in REQ_ACTIVE or i.status in (REQ_FAILED, REQ_NO_RESULTS)
    }


def minimum_rank(profiles, minimum_key: str) -> int:
    """Rank of the minimum quality; an unknown key (a deleted profile) asks for nothing."""
    minimum = profiles.get(minimum_key)
    return minimum.rank if minimum else profiles.lowest.rank


def upgrade_targets(profiles, current_key: str, minimum_key: str = "") -> list:
    """What an upgrade can aim at, best first: better than the current quality and at least the
    minimum quality (an upgrade that still missed the minimum would be pointless)."""
    floor = max(profiles.rank(current_key) + 1, minimum_rank(profiles, minimum_key))
    return [p for p in reversed(profiles.profiles) if p.rank >= floor]


def below_minimum(profiles, current_key: str, minimum_key: str) -> bool:
    """Does an album of this quality fail the minimum quality set in the configuration?"""
    return profiles.rank(current_key) < minimum_rank(profiles, minimum_key)


def request_statuses(db: Session, release_group_ids: list[int]) -> dict[int, str]:
    return {g: i.status for g, i in request_infos(db, release_group_ids).items()}


# ----------------------------------------------------------------------- reading


def wanted_for(db: Session, request: DownloadRequest) -> tuple[Wanted, str, str]:
    """(what to look for, artist, album) of a request."""
    group = db.get(ReleaseGroup, request.release_group_id)
    artist = db.get(Artist, group.artist_id)
    return (
        Wanted(
            artist=artist.name,
            album=group.title,
            year=group.year,
            track_count=group.track_count_hint,
            min_quality=request.quality_key,
            various=artist.name.lower() == "various artists",
        ),
        artist.name,
        group.title,
    )


def candidates_of(request: DownloadRequest) -> list[Candidate]:
    return [Candidate.from_dict(d) for d in json.loads(request.candidates_json or "[]")]


def release_options_of(request: DownloadRequest) -> list[dict]:
    """The releases the finished download may be, when no one fits on its own (else empty)."""
    try:
        return list(json.loads(request.release_options_json or "[]"))
    except ValueError:
        return []


RELEASE_NONE = "none"  # import it with the tags the files have
RELEASE_REJECT = "reject"  # do not import it: to the quarantine


def store_release_options(db: Session, request_id: int, options: list[dict]) -> None:
    """The download waits for the user: which release is it (or none)?"""
    request = db.get(DownloadRequest, request_id)
    if request is None or request.status == REQ_CANCELLED:
        return
    request.release_options_json = json.dumps(options)
    request.status = REQ_AWAITING_CHOICE
    request.error = None
    db.flush()


def choose_release_option(db: Session, request: DownloadRequest, choice: str) -> str:
    """The user's answer: a release offered, `none` or `reject`. The request goes back to the
    import, which is queued by the caller. Returns the choice."""
    options = release_options_of(request)
    if request.status != REQ_AWAITING_CHOICE or not options:
        raise RequestError("request_not_choosing")
    if choice not in (RELEASE_NONE, RELEASE_REJECT) and choice not in {o["mbid"] for o in options}:
        raise RequestError("request_bad_release")
    request.release_options_json = None
    request.status = REQ_DOWNLOADED
    request.error = None
    db.flush()
    return choice


def current_candidate(request: DownloadRequest) -> Candidate | None:
    cands = candidates_of(request)
    return cands[request.candidate_index] if 0 <= request.candidate_index < len(cands) else None


# ----------------------------------------------------------------- state changes


def set_status(
    db: Session, request_id: int, status: str, *, error: str | None = None, **fields
) -> DownloadRequest | None:
    request = db.get(DownloadRequest, request_id)
    if request is None:
        return None
    if request.status == REQ_CANCELLED and status != REQ_CANCELLED:
        return request  # the user cancelled while a job was working: the job must not revive it
    request.status = status
    request.error = error
    for key, value in fields.items():
        setattr(request, key, value)
    if status in REQ_FINISHED:
        request.finished_at = utcnow()
    db.flush()
    notify_request(db, request, status)  # the owner hears about completed, failed...
    return request


def store_candidates(db: Session, request_id: int, candidates: list[Candidate], query: str) -> None:
    request = db.get(DownloadRequest, request_id)
    request.candidates_json = json.dumps([c.to_dict() for c in candidates])
    request.query_used = query
    request.candidate_index = -1
    db.flush()


def choose_candidate(db: Session, request: DownloadRequest, index: int) -> None:
    """Pick one of the stored candidates (manual choice, or the best one in auto mode)."""
    if request.status == REQ_CANCELLED:
        raise RequestError("request_not_active")
    if not 0 <= index < len(candidates_of(request)):
        raise RequestError("request_bad_candidate")
    request.candidate_index = index
    request.status = REQ_CANDIDATE_FOUND
    request.error = None
    db.flush()


def cancel_request(db: Session, request: DownloadRequest) -> list[int]:
    """Mark a request cancelled; returns the ids of its jobs so the caller can stop them."""
    if request.status not in REQ_ACTIVE:
        raise RequestError("request_not_active")
    request.status = REQ_CANCELLED
    request.finished_at = utcnow()
    db.flush()
    keys = [
        search_dedupe_key(request.id),
        monitor_dedupe_key(request.id),
        postprocess_dedupe_key(request.id),
    ]
    return list(
        db.scalars(
            select(Job.id).where(Job.dedupe_key.in_(keys), Job.status.in_(("queued", "running")))
        )
    )


def can_delete(request: DownloadRequest) -> bool:
    """A request that is over (completed, failed, cancelled, nothing found) may be removed from
    the list; one that is still running must be cancelled first."""
    return request.status in REQ_FINISHED


def can_retry(request: DownloadRequest) -> bool:
    return request.status in (REQ_FAILED, REQ_NO_RESULTS, REQ_CANCELLED)


# -------------------------------------------------------------------- the files


def safe_subdir(name: str) -> str:
    return _UNSAFE.sub("_", name).strip(" .")[:80]


def destination(dest_dir: str | None, request_id: int, subdir: str = "") -> str:
    """Where SLSKD saves the files, relative to its downloads folder."""
    base = dest_dir or f"{DEST_ROOT}/{request_id}"
    return f"{base}/{safe_subdir(subdir)}" if subdir else base


def create_files(db: Session, request: DownloadRequest, candidate: Candidate) -> None:
    """One row per file of the chosen candidate (replacing the rows of an earlier one)."""
    for row in db.scalars(select(DownloadFile).where(DownloadFile.request_id == request.id)):
        db.delete(row)
    db.flush()
    for subdir, files in group_by_subdir(candidate).items():
        for f in files:
            db.add(
                DownloadFile(
                    request_id=request.id,
                    username=candidate.username,
                    filename=f["filename"],
                    subdir=subdir,
                    size=int(f["size"] or 0),
                )
            )
    db.flush()


@dataclass
class FileSnapshot:
    id: int
    username: str
    filename: str
    subdir: str
    size: int
    transfer_id: str | None
    state: str
    bytes_transferred: int
    attempts: int


def snapshot(db: Session, request_id: int) -> list[FileSnapshot]:
    rows = db.scalars(
        select(DownloadFile).where(DownloadFile.request_id == request_id).order_by(DownloadFile.id)
    )
    return [
        FileSnapshot(
            r.id,
            r.username,
            r.filename,
            r.subdir,
            r.size,
            r.transfer_id,
            r.state,
            r.bytes_transferred,
            r.attempts,
        )
        for r in rows
    ]


def attach_transfers(db: Session, request_id: int, transfers: list[Transfer]) -> int:
    """Give the rows without a transfer the id SLSKD made for them (user + file name)."""
    rows = list(db.scalars(select(DownloadFile).where(DownloadFile.request_id == request_id)))
    used = {r.transfer_id for r in rows if r.transfer_id}
    attached = 0
    for row in rows:
        if row.transfer_id:
            continue
        match = next(
            (
                t
                for t in transfers
                if t.id not in used and t.username == row.username and t.filename == row.filename
            ),
            None,
        )
        if match:
            row.transfer_id = match.id
            row.state = match.state
            used.add(match.id)
            attached += 1
    db.flush()
    return attached


def update_files(db: Session, request_id: int, transfers: list[Transfer]) -> float:
    """Copy what SLSKD reports into the rows; returns the overall progress (0-100)."""
    by_id = {t.id: t for t in transfers}
    rows = list(db.scalars(select(DownloadFile).where(DownloadFile.request_id == request_id)))
    done = total = 0
    for row in rows:
        t = by_id.get(row.transfer_id or "")
        if t is not None:
            row.state = t.state
            row.bytes_transferred = t.bytes_transferred
            row.speed = t.average_speed
            row.error = t.exception if is_failed(t.state) else None
            if is_succeeded(t.state):
                row.bytes_transferred = max(row.bytes_transferred, row.size)
        done += min(row.bytes_transferred, row.size) if row.size else 0
        total += row.size
        row.updated_at = utcnow()
    progress = round(100 * done / total, 1) if total else 0.0
    request = db.get(DownloadRequest, request_id)
    if request is not None:
        request.progress = progress
    db.flush()
    return progress


def reset_file(db: Session, file_id: int, *, error: str | None = None) -> None:
    row = db.get(DownloadFile, file_id)
    row.transfer_id, row.state, row.bytes_transferred = None, "Requested", 0
    row.attempts += 1
    row.error = error
    db.flush()


def next_candidate(db: Session, request_id: int, reason: str) -> bool:
    """The chosen candidate failed: forget its files and move on. False when none is left."""
    request = db.get(DownloadRequest, request_id)
    if request.status == REQ_CANCELLED:
        return False  # cancelled meanwhile: nothing to move on to, and nothing to announce
    for row in db.scalars(select(DownloadFile).where(DownloadFile.request_id == request_id)):
        db.delete(row)
    request.candidate_index += 1
    request.progress = 0.0
    if request.candidate_index < len(candidates_of(request)):
        request.status = REQ_CANDIDATE_FOUND
        request.error = None
        db.flush()
        return True
    request.status = REQ_FAILED
    request.error = reason[:500]
    request.finished_at = utcnow()
    db.flush()
    notify_request(db, request, REQ_FAILED)
    return False


def mark_downloaded(db: Session, request_id: int) -> None:
    set_status(db, request_id, REQ_DOWNLOADED, progress=100.0)


def downloading(db: Session, request_id: int) -> None:
    set_status(db, request_id, REQ_DOWNLOADING)


def searching(db: Session, request_id: int) -> None:
    set_status(db, request_id, REQ_SEARCHING)


def awaiting_choice(db: Session, request_id: int) -> None:
    set_status(db, request_id, REQ_AWAITING_CHOICE)


def no_results(db: Session, request_id: int, query: str) -> None:
    set_status(db, request_id, REQ_NO_RESULTS, query_used=query)
