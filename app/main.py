from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app import __version__

app = FastAPI(title="DISKKO", version=__version__)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (
        "<!doctype html><html lang='it'><head><meta charset='utf-8'>"
        "<title>DISKKO</title></head>"
        "<body><h1>DISKKO</h1><p>Ciao! L'app è in esecuzione.</p></body></html>"
    )
