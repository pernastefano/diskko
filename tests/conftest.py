import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        secret_key="test-secret",
        base_url="http://testserver",
        config_dir=tmp_path / "config",
    )


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


def post(client, path, data=None, page="/", headers=None):
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
        page="/admin/users",
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
