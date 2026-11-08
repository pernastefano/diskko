"""A tiny stand-in for SLSKD, for the end-to-end test only (never used by the app itself).

It answers the calls DISKKO makes to check the connection and records every request, so the test
can look at them at /__requests. Run it with the DISKKO image: `python /e2e/mocks/mock_slskd.py`.
"""

import os

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request

API_KEY = os.environ.get("MOCK_SLSKD_API_KEY", "e2e-slskd-key-0123456789")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
seen: list[dict] = []


@app.middleware("http")
async def record(request: Request, call_next):
    if not request.url.path.startswith("/__"):
        seen.append({"method": request.method, "path": request.url.path})
    return await call_next(request)


@app.get("/api/v0/application")
def application(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401)
    return {
        "server": {"state": "Connected, LoggedIn", "isConnected": True, "isLoggedIn": True},
        "version": {"current": "0.22.0-mock"},
    }


@app.get("/api/v0/searches")
def searches(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401)
    return []


@app.get("/__requests")
def requests_seen():
    return seen


@app.get("/__health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5030, log_level="warning")
