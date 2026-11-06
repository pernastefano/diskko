from sqlalchemy import select

from app.models import User, UserSession
from tests.conftest import PASSWORD, login, post

NEW = "another-long-password"


def _change(client, current=PASSWORD, new=NEW, confirm=None):
    return post(
        client,
        "/account/password",
        {
            "current_password": current,
            "new_password": new,
            "new_password_confirm": new if confirm is None else confirm,
        },
        page="/account/password",
    )


def test_change_password_success(admin_client, new_client):
    r = _change(admin_client)
    assert r.status_code == 303
    assert "Password changed." in admin_client.get(r.headers["location"]).text
    assert login(new_client(), "admin", PASSWORD).status_code == 400
    assert login(new_client(), "admin", NEW).status_code == 303


def test_change_password_keeps_current_session_drops_others(admin_client, new_client, db):
    other = new_client()
    login(other, "admin")
    assert len(db.scalars(select(UserSession)).all()) == 2
    _change(admin_client)
    assert admin_client.get("/").status_code == 200
    assert other.get("/").status_code == 303
    assert len(db.scalars(select(UserSession)).all()) == 1


def test_wrong_current_password(admin_client):
    r = _change(admin_client, current="not-it-at-all")
    assert r.status_code == 400 and "current password is wrong" in r.text


def test_mismatched_confirmation(admin_client):
    r = _change(admin_client, confirm="different-one-here")
    assert r.status_code == 400 and "do not match" in r.text


def test_new_password_too_short(admin_client):
    r = _change(admin_client, new="short")
    assert r.status_code == 400 and "too short" in r.text


def test_hash_changes_and_is_argon2(admin_client, db):
    old = db.scalar(select(User)).password_hash
    _change(admin_client)
    db.expire_all()
    new = db.scalar(select(User)).password_hash
    assert new != old and new.startswith("$argon2")


def test_page_requires_login(client, admin_client, new_client):
    assert new_client().get("/account/password").status_code == 303
