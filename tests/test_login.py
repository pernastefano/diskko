from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import User, UserSession, utcnow
from app.web.routers.auth import safe_next
from tests.conftest import PASSWORD, add_user, do_setup, login, post


def test_login_success_sets_hardened_cookie(admin_client, new_client):
    c = new_client()
    r = login(c, "admin")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "diskko_session=" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie  # plain-http base URL
    assert c.get("/library").status_code == 200


def test_session_cookie_is_secure_on_https(tmp_path):
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        base_url="https://diskko.example",
        config_dir=tmp_path / "config",
    )
    app = create_app(settings)
    with TestClient(app, base_url="https://diskko.example", follow_redirects=False) as c:
        r = do_setup(c)
        assert "secure" in r.headers["set-cookie"].lower()


def test_username_is_case_insensitive(admin_client, new_client):
    c = new_client()
    assert login(c, "ADMIN").status_code == 303


def test_password_is_stored_as_argon2(admin_client, db):
    user = db.scalar(select(User))
    assert user.password_hash.startswith("$argon2")
    assert PASSWORD not in user.password_hash


def test_wrong_password_and_unknown_user_look_identical(admin_client, new_client):
    c = new_client()
    wrong = login(c, "admin", "not-the-password")
    unknown = login(c, "nobody", "not-the-password")
    assert wrong.status_code == unknown.status_code == 400
    assert "Invalid username or password" in wrong.text
    assert "Invalid username or password" in unknown.text
    assert "diskko_session" not in wrong.headers.get("set-cookie", "")
    assert "diskko_session" not in unknown.headers.get("set-cookie", "")


def test_disabled_user_cannot_login(admin_client, new_client, db):
    add_user(admin_client, "bob")
    bob = db.scalar(select(User).where(User.username == "bob"))
    bob.is_active = False
    db.commit()
    r = login(new_client(), "bob")
    assert r.status_code == 400 and "disabled" in r.text


def test_logout_revokes_session(admin_client, db):
    assert db.scalar(select(func.count(UserSession.id))) == 1
    r = post(admin_client, "/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert db.scalar(select(func.count(UserSession.id))) == 0
    assert admin_client.get("/library").status_code == 303


def test_login_next_redirects_to_local_path(admin_client, new_client):
    c = new_client()
    r = login(c, "admin", next_="/admin/users")
    assert r.headers["location"] == "/admin/users"


def test_login_next_rejects_external_targets(admin_client, new_client):
    evil_targets = (
        "//evil.example",
        "https://evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
    )
    for evil in evil_targets:
        r = login(new_client(), "admin", next_=evil)
        assert r.headers["location"] == "/", evil


def test_safe_next_unit():
    assert safe_next("/admin/users?x=1") == "/admin/users?x=1"
    assert safe_next("") == "/"
    assert safe_next(None, "/home") == "/home"
    assert safe_next("//evil") == "/"
    assert safe_next("http://evil") == "/"


def test_protected_page_redirects_with_next(admin_client, new_client):
    c = new_client()
    r = c.get("/admin/users?page=2")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fadmin%2Fusers%3Fpage%3D2"


def test_login_page_when_already_logged_in_redirects(admin_client):
    assert admin_client.get("/login").status_code == 303


def test_tampered_cookie_is_anonymous(admin_client, new_client):
    c = new_client()
    c.cookies.set("diskko_session", "garbage")
    assert c.get("/library").status_code == 303


def test_expired_session_is_rejected(admin_client, db):
    row = db.scalar(select(UserSession))
    row.expires_at = utcnow().replace(year=2000)
    db.commit()
    assert admin_client.get("/library").status_code == 303
    assert db.scalar(select(func.count(UserSession.id))) == 0


def test_session_slides_when_used(admin_client, db):
    from datetime import timedelta

    row = db.scalar(select(UserSession))
    old_expiry = row.expires_at - timedelta(days=5)
    row.last_seen = utcnow() - timedelta(hours=1)
    row.expires_at = old_expiry
    db.commit()
    r = admin_client.get("/library")
    assert r.status_code == 200
    assert "diskko_session" in r.headers.get("set-cookie", "")
    db.expire_all()
    assert db.scalar(select(UserSession)).expires_at > old_expiry


def test_login_replaces_previous_session_of_same_browser(admin_client, db):
    login(admin_client, "admin", page="/library")
    assert db.scalar(select(func.count(UserSession.id))) == 1
