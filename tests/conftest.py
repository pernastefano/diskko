import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import make_engine, make_session_factory, run_migrations
from app.main import create_app
from tests import stepjob  # noqa: F401  (registers the test-only "test.steps" job)

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session", autouse=True)
def _cheap_password_hashing():
    """Argon2 is slow on purpose (about 30 ms a hash and the same to check one) and the tests
    create and check hundreds of passwords: use the cheapest settings it accepts."""
    from argon2 import PasswordHasher

    from app.auth import security

    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    security._hasher = cheap
    security._DUMMY_HASH = cheap.hash("diskko-dummy-password")
    yield


@pytest.fixture(autouse=True)
def _no_real_backoff(monkeypatch):
    """The HTTP clients wait seconds between retries of a failing service. A test that makes a
    service fail must not really wait: tests of the delays themselves set their own."""
    from app.integrations.cached_http import CachedJsonClient
    from app.integrations.musicbrainz_client import MusicBrainzClient

    monkeypatch.setattr(CachedJsonClient, "BACKOFF_BASE", 0.0)
    monkeypatch.setattr(MusicBrainzClient, "BACKOFF_BASE", 0.0)
    monkeypatch.setattr("app.integrations.coverart.RETRY_PAUSE", 0.0)
    monkeypatch.setattr(CachedJsonClient, "RATE_LIMIT_PAUSE", 0.0)


@pytest.fixture(scope="session")
def _migrated_db(tmp_path_factory):
    """The database after every migration, made once: a test starts from a copy of it instead of
    running 18 migrations again (the migrations have tests of their own)."""
    path = tmp_path_factory.mktemp("template") / "app.db"
    run_migrations(f"sqlite:///{path}")
    con = sqlite3.connect(path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")  # one plain file: a copy is the whole database
    con.close()
    return path


@pytest.fixture(autouse=True)
def _fast_migrations(monkeypatch, _migrated_db):
    """Anything that "migrates to head" at the start of a test (the fixtures, the app's start-up)
    gets the prepared database when there is none yet. Migrating to another revision, which the
    migration tests do, is left alone."""
    import app.main
    from app.db import run_migrations as real

    def fast(url, revision="head"):
        from sqlalchemy.engine import make_url

        target = Path(make_url(url).database or "")
        if revision != "head" or not target.name:
            return real(url, revision)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_migrated_db, target)

    monkeypatch.setattr(app.main, "run_migrations", fast)
    monkeypatch.setattr(sys.modules[__name__], "run_migrations", fast)


@pytest.fixture(autouse=True)
def _fresh_rate_limiters():
    """Limiters are shared by name; their lock binds to the event loop of the test that first
    waits on it, so every test gets new ones."""
    from app.integrations import ratelimit

    ratelimit._LIMITERS.clear()
    yield
    ratelimit._LIMITERS.clear()


@pytest.fixture(autouse=True)
def _fresh_login_throttle():
    from app.auth.throttle import THROTTLE

    THROTTLE.clear()
    yield
    THROTTLE.clear()


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers():
    from app.integrations.breaker import BREAKERS

    BREAKERS.reset()
    yield
    BREAKERS.reset()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        secret_key="test-secret",
        base_url="http://testserver",
        data_dir=tmp_path / "data",
        jobs_enabled=False,  # tests that need workers start them explicitly
        providers_metadata="musicbrainz",  # the Deezer metadata fallback has its own tests
    )


@pytest.fixture
def session_factory(settings):
    """A migrated database without the web app (for engine/worker tests)."""
    run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def jdb(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    """Anonymous client; entering the context runs the app lifespan (migrations)."""
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def new_client(app, client):
    """Factory for extra browsers (own cookie jar) sharing the running app."""

    def factory():
        return TestClient(app, follow_redirects=False)

    return factory


@pytest.fixture
def db(app, client):
    with app.state.session_factory() as session:
        yield session


def csrf_of(client, path="/login"):
    """CSRF token published by a page in <meta name="csrf-token">."""
    html = client.get(path).text
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def post(client, path, data=None, page="/dashboard", headers=None):
    """POST with a valid CSRF token taken from `page`."""
    payload = dict(data or {})
    payload["csrf_token"] = csrf_of(client, page)
    return client.post(path, data=payload, headers=headers)


def login(client, username, password=PASSWORD, next_="", page="/login"):
    return post(
        client,
        "/login",
        {"username": username, "password": password, "next": next_},
        page=page,
    )


def do_setup(client, username="admin", password=PASSWORD):
    return post(
        client,
        "/setup",
        {"username": username, "password": password, "password_confirm": password},
        page="/setup",
    )


@pytest.fixture
def admin_client(client):
    assert do_setup(client).status_code == 303
    return client


def add_user(admin, username, role="user", password=PASSWORD):
    r = post(
        admin,
        "/admin/users",
        {"username": username, "password": password, "role": role},
        page="/settings/users",
    )
    assert r.status_code == 303, r.text
    return r


@pytest.fixture
def user_client(admin_client, new_client):
    """A logged-in non-admin user called 'bob'."""
    add_user(admin_client, "bob")
    c = new_client()
    assert login(c, "bob").status_code == 303
    return c


# ----------------------------------------------------------------- MusicBrainz helpers

MB_BASE = "https://musicbrainz.test/ws/2"
MB_UA = "Diskko/0.1 ( test@example.com )"


@pytest.fixture
def ft():
    from tests.mbdata import FakeTime

    return FakeTime()


@pytest.fixture
def clock():
    """Mutable 'now' for the cache TTL."""
    from app.models import utcnow

    state = {"now": utcnow()}

    def now():
        return state["now"]

    now.state = state
    return now


@pytest.fixture
def mbsettings(settings):
    return settings.model_copy(
        update={
            "musicbrainz_url": MB_BASE,
            "mb_user_agent": MB_UA,
            "mb_min_interval": 0.0,
            "job_poll_interval": 0.02,
        }
    )


@pytest.fixture
def make_client(mbsettings, session_factory, ft, clock):
    from app.integrations.musicbrainz_client import MusicBrainzClient
    from app.integrations.ratelimit import RateLimiter

    def make(**kw):
        limiter = RateLimiter(1.1, clock=ft.clock, sleep=ft.sleep)
        return MusicBrainzClient(
            kw.pop("settings", mbsettings),
            session_factory,
            limiter=limiter,
            sleep=ft.sleep,
            now=clock,
            **kw,
        )

    return make
