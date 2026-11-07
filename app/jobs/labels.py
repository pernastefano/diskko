"""A short description of what a job is about ("Aretha Franklin – Lady Soul"), for the lists."""

from sqlalchemy.orm import Session

from app.models import Artist, DownloadRequest, ReleaseGroup


def _album(db: Session, release_group_id: int) -> str | None:
    group = db.get(ReleaseGroup, release_group_id)
    if group is None:
        return None
    artist = db.get(Artist, group.artist_id)
    return f"{artist.name} – {group.title}" if artist else group.title


def job_label(db: Session, job_type: str, payload: dict) -> str | None:
    """What the job works on, from its payload; None when there is nothing specific to say."""
    try:
        if "request_id" in payload:
            request = db.get(DownloadRequest, int(payload["request_id"]))
            label = _album(db, request.release_group_id) if request else None
            return f"{label} (upgrade)" if label and request and request.upgrade_from else label
        if "release_group_id" in payload:
            return _album(db, int(payload["release_group_id"]))
        if "artist_id" in payload:
            artist = db.get(Artist, int(payload["artist_id"]))
            return artist.name if artist else None
        if job_type == "search_artists" and payload.get("query"):
            return f"“{str(payload['query'])[:80]}”"
        if job_type == "integration_check" and payload.get("name"):
            return str(payload["name"])
    except (TypeError, ValueError):
        return None
    return None
