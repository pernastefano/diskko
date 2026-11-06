import pytest
from sqlalchemy import func, select

from app.models import User
from tests.conftest import PASSWORD, csrf_of, do_setup, post


@pytest.mark.parametrize("path", ["/", "/login", "/admin/users", "/account/password"])
def test_everything_redirects_to_setup_before_first_user(client, path):
    r = client.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_setup_page_renders(client):
    r = client.get("/setup")
    assert r.status_code == 200
    assert "administrator" in r.text.lower()


def test_setup_creates_admin_and_logs_in(client, db):
    r = do_setup(client, "Admin")
    assert r.status_code == 303 and r.headers["location"] == "/"
    user = db.scalar(select(User))
    assert user.username == "admin"  # normalized to lowercase
    assert user.role == "admin" and user.is_active
    home = client.get("/")
    assert home.status_code == 200 and "admin" in home.text


def test_setup_requires_csrf(client, db):
    r = client.post("/setup", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 403
    assert db.scalar(select(func.count(User.id))) == 0


def test_setup_closed_after_first_user(admin_client, db, new_client):
    other = new_client()
    r = other.get("/setup")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    token = csrf_of(other, "/login")
    r = other.post(
        "/setup",
        data={
            "username": "evil",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "csrf_token": token,
        },
    )
    assert r.status_code == 404
    assert db.scalar(select(func.count(User.id))) == 1


@pytest.mark.parametrize(
    ("username", "password", "confirm", "message"),
    [
        ("admin", PASSWORD, "different-password", "do not match"),
        ("admin", "short", "short", "too short"),
        ("a b", PASSWORD, PASSWORD, "Invalid username"),
        ("ab", PASSWORD, PASSWORD, "Invalid username"),
    ],
)
def test_setup_validation(client, db, username, password, confirm, message):
    r = post(
        client,
        "/setup",
        {"username": username, "password": password, "password_confirm": confirm},
        page="/setup",
    )
    assert r.status_code == 400
    assert message in r.text
    assert db.scalar(select(func.count(User.id))) == 0
