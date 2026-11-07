import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.auth.deps import UserAuth
from app.jobs.handlers.fetch_covers import covers_dir

router = APIRouter()

# <release-group MBID>.jpg|png (or deezer-<album id>.jpg|png for albums that MusicBrainz lacks):
# nothing else is ever served from the covers folder.
_NAME = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|deezer-\d{1,20})\.(jpg|png)$"
)
_TYPES = {"jpg": "image/jpeg", "png": "image/png"}


@router.get("/covers/{filename}")
def cover(request: Request, auth: UserAuth, filename: str):
    if not _NAME.match(filename):
        raise HTTPException(status_code=404, detail="not_found")
    directory = covers_dir(request.app.state.settings.data_dir).resolve()
    path = (directory / filename).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(status_code=404, detail="not_found")
    return FileResponse(
        Path(path),
        media_type=_TYPES[filename.rsplit(".", 1)[1]],
        headers={"Cache-Control": "private, max-age=86400"},
    )
