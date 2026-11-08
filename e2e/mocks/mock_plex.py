"""A tiny stand-in for Plex, for the end-to-end test only (never used by the app itself).

Serves one music section with one artist, one album and two tracks, in the XML/JSON a real server
returns (DISKKO reads it with plexapi), and records every request at /__requests.
Run it with the DISKKO image: `python /e2e/mocks/mock_plex.py`.
"""

import os
import re
from xml.sax.saxutils import quoteattr

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TOKEN = os.environ.get("MOCK_PLEX_TOKEN", "e2e-plex-token-0123456789")
MUSIC = os.environ.get("MOCK_MUSIC_DIR", "/music")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
seen: list[dict] = []

ARTIST = {"id": 1, "title": "E2E Artist"}
ALBUM = {"id": 2, "title": "E2E Album", "year": 2001}
TRACKS = [
    {
        "id": 10 + n,
        "title": f"Track {n}",
        "index": n,
        "path": f"{MUSIC}/E2E Artist/E2E Album/{n:02d}.flac",
    }
    for n in (1, 2)
]


def xml(body: str) -> Response:
    return Response(body, media_type="application/xml")


def track_xml(t: dict) -> str:
    return (
        f'<Track type="track" ratingKey="{t["id"]}" parentRatingKey="{ALBUM["id"]}" '
        f'grandparentRatingKey="{ARTIST["id"]}" title={quoteattr(t["title"])} '
        f"parentTitle={quoteattr(ALBUM['title'])} grandparentTitle={quoteattr(ARTIST['title'])} "
        f'index="{t["index"]}" parentIndex="1" duration="180000" updatedAt="1700000000">'
        f'<Media audioCodec="flac" bitrate="900" audioChannels="2"><Part file={quoteattr(t["path"])} size="30000000">'
        f'<Stream streamType="2" bitDepth="16" samplingRate="44100" codec="flac"/></Part></Media></Track>'
    )


@app.middleware("http")
async def record(request: Request, call_next):
    if not request.url.path.startswith("/__"):
        seen.append({"method": request.method, "path": request.url.path})
    return await call_next(request)


@app.get("/identity")
def identity():
    return JSONResponse({"MediaContainer": {"machineIdentifier": "mock", "version": "1.40.0-mock"}})


@app.get("/")
def root():
    return xml('<MediaContainer size="0" machineIdentifier="mock" version="1.40.0-mock"/>')


def authorized(request: Request) -> bool:
    return (
        request.headers.get("x-plex-token") or request.query_params.get("X-Plex-Token")
    ) == TOKEN


@app.get("/library")
def library(request: Request):
    if not authorized(request):
        return Response(status_code=401)
    return xml('<MediaContainer size="1"><Directory key="sections"/></MediaContainer>')


@app.get("/library/sections")
def sections(request: Request):
    if not authorized(request):
        return Response(status_code=401)
    return xml(
        '<MediaContainer size="1"><Directory key="1" type="artist" title="Music"/></MediaContainer>'
    )


@app.get("/library/sections/1/all")
def section_all(request: Request):
    if not authorized(request):
        return Response(status_code=401)
    kind = (
        re.search(r"type=(\d+)", str(request.url)).group(1) if "type=" in str(request.url) else "8"
    )
    items = {
        "8": [
            f'<Directory type="artist" ratingKey="{ARTIST["id"]}" title={quoteattr(ARTIST["title"])}/>'
        ],
        "9": [
            f'<Directory type="album" ratingKey="{ALBUM["id"]}" parentRatingKey="{ARTIST["id"]}" '
            f'title={quoteattr(ALBUM["title"])} year="{ALBUM["year"]}"/>'
        ],
        "10": [track_xml(t) for t in TRACKS],
    }[kind]
    return xml(
        f'<MediaContainer size="{len(items)}" totalSize="{len(items)}">{"".join(items)}</MediaContainer>'
    )


@app.get("/library/sections/1/refresh")
def refresh():
    return xml('<MediaContainer size="0"/>')


@app.get("/__requests")
def requests_seen():
    return seen


@app.get("/__health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=32400, log_level="warning")
