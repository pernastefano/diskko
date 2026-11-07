from sqlalchemy import select

from app.models import User
from tests.conftest import PASSWORD, add_user, csrf_of, login, post


def test_login_without_token_rejected(admin_client, new_client):
    c = new_client()
    r = c.post("/login", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 403


def test_login_with_wrong_token_rejected(admin_client, new_client):
    c = new_client()
    csrf_of(c, "/login")  # obtain the anonymous cookie
    r = c.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": "nope"})
    assert r.status_code == 403


def test_token_without_matching_cookie_rejected(admin_client, new_client):
    token = csrf_of(new_client(), "/login")
    other = new_client()  # no csrf cookie at all
    r = other.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token})
    assert r.status_code == 403


def test_header_token_is_accepted(admin_client, new_client):
    c = new_client()
    token = csrf_of(c, "/login")
    r = c.post(
        "/login",
        data={"username": "admin", "password": PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 303


def test_logged_in_post_without_token_rejected(admin_client):
    assert admin_client.post("/logout").status_code == 403
    assert admin_client.get("/library").status_code == 200  # still logged in


def test_admin_actions_without_token_rejected(admin_client, db):
    add_user(admin_client, "bob")
    bob = db.scalar(select(User).where(User.username == "bob"))
    assert admin_client.post(f"/admin/users/{bob.id}/delete").status_code == 403
    assert admin_client.post("/admin/users", data={"username": "x"}).status_code == 403


def test_token_is_bound_to_session(admin_client, user_client):
    admin_token = csrf_of(admin_client, "/library")
    bob_token = csrf_of(user_client, "/library")
    assert admin_token != bob_token
    r = user_client.post("/logout", data={"csrf_token": admin_token})
    assert r.status_code == 403


def test_token_rotates_on_login(admin_client):
    before = csrf_of(admin_client, "/library")
    login(admin_client, "admin", page="/library")
    assert csrf_of(admin_client, "/library") != before


def test_html_exposes_token_for_htmx(admin_client):
    token = csrf_of(admin_client, "/library")
    assert token in admin_client.get("/library").text
    assert "X-CSRF-Token" in admin_client.get("/library").text


def test_csrf_error_page_message(admin_client):
    r = admin_client.post("/logout", headers={"accept": "text/html"})
    assert r.status_code == 403 and "expired or is invalid" in r.text


def test_safe_methods_need_no_token(admin_client):
    assert admin_client.get("/settings/users").status_code == 200
    assert post(admin_client, "/logout").status_code == 303
