from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.auth.csrf import csrf_protect
from app.auth.sessions import set_session_cookie
from app.config import DEFAULT_SECRET_KEY, Settings, get_settings
from app.db import make_engine, make_session_factory, run_migrations
from app.jobs.scheduler import JobScheduler
from app.jobs.worker import JobWorker
from app.web.errors import register_error_handlers
from app.web.routers import (
    artists,
    auth,
    covers,
    jobs,
    library,
    library_admin,
    pages,
    preferences,
    setup,
    status,
    users,
)

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.is_production and settings.secret_key == DEFAULT_SECRET_KEY:
        raise RuntimeError("SECRET_KEY must be set to a random value in production")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        run_migrations(settings.database_url)
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.worker = None
        app.state.scheduler = None
        if settings.jobs_enabled:
            worker = JobWorker(settings, app.state.session_factory)
            await worker.start()
            scheduler = JobScheduler(settings, app.state.session_factory, notify=worker.notify)
            scheduler.start()
            app.state.worker, app.state.scheduler = worker, scheduler
        yield
        if app.state.scheduler:
            app.state.scheduler.shutdown()
        if app.state.worker:
            await app.state.worker.stop()
        engine.dispose()

    app = FastAPI(
        title="DISKKO",
        version=__version__,
        lifespan=lifespan,
        # Interactive docs/schema are public by default; keep them off until gated by admin.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if "text/html" in response.headers.get("content-type", ""):
            response.headers.setdefault("Cache-Control", "no-store")
        cookie = getattr(request.state, "refresh_session_cookie", None)
        if cookie:  # sliding session: extend the browser cookie as well
            set_session_cookie(response, cookie, settings)
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    register_error_handlers(app)
    for router in (
        setup.router,
        auth.router,
        users.router,
        jobs.router,
        library_admin.router,
        library.router,
        artists.router,
        preferences.router,
        covers.router,
        status.router,
        pages.router,
    ):
        app.include_router(router)
    return app


app = create_app()
