import pytest
from sqlalchemy import func, select

from app.auth import users as users_service
from app.models import User, UserSession
from tests.conftest import PASSWORD, add_user, login, post

HX = {"HX-Request": "true"}


def _user(db, name):
    db.expire_all()
    return db.scalar(select(User).where(User.username == name))


def test_create_user(admin_client, db):
    r = add_user(admin_client, "Carol", role="user")
    assert r.headers["location"] == "/admin/users?flash=user_created"
    carol = _user(db, "carol")
    assert carol.role == "user" and carol.is_active
    page = admin_client.get("/admin/users?flash=user_created")
    assert "carol" in page.text and "User created." in page.text


def test_create_admin_user(admin_client, db):
    add_user(admin_client, "boss", role="admin")
    assert _user(db, "boss").is_admin


@pytest.mark.parametrize(
    ("username", "password", "role", "message"),
    [
        ("ADMIN", PASSWORD, "user", "already taken"),  # case-insensitive duplicate
        ("ok-name", "short", "user", "too short"),
        ("bad name", PASSWORD, "user", "Invalid username"),
        ("ok-name", PASSWORD, "superuser", "Invalid role"),
    ],
)
def test_create_user_validation(admin_client, db, username, password, role, message):
    r = post(
        admin_client,
        "/admin/users",
        {"username": username, "password": password, "role": role},
        page="/admin/users",
    )
    assert r.status_code == 400
    assert message in r.text
    assert db.scalar(select(func.count(User.id))) == 1


def test_change_role_htmx(admin_client, db):
    add_user(admin_client, "bob")
    bob = _user(db, "bob")
    r = post(admin_client, f"/admin/users/{bob.id}/role", {"role": "admin"}, headers=HX)
    assert r.status_code == 200 and f'id="user-{bob.id}"' in r.text
    assert _user(db, "bob").role == "admin"


def test_change_role_plain_redirects(admin_client, db):
    add_user(admin_client, "bob")
    bob = _user(db, "bob")
    r = post(admin_client, f"/admin/users/{bob.id}/role", {"role": "admin"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users?flash=user_updated"


def test_invalid_role_rejected(admin_client, db):
    add_user(admin_client, "bob")
    bob = _user(db, "bob")
    r = post(admin_client, f"/admin/users/{bob.id}/role", {"role": "root"}, headers=HX)
    assert r.status_code == 409 and "Invalid role" in r.text
    assert _user(db, "bob").role == "user"


@pytest.mark.parametrize(
    ("action", "data"),
    [("role", {"role": "user"}), ("active", {"active": "0"}), ("delete", {})],
)
def test_admin_cannot_act_on_self(admin_client, db, action, data):
    me = _user(db, "admin")
    r = post(admin_client, f"/admin/users/{me.id}/{action}", data, headers=HX)
    assert r.status_code == 409
    assert "your own account" in r.text
    me = _user(db, "admin")
    assert me is not None and me.is_admin and me.is_active


def test_disable_user_revokes_sessions_and_blocks_login(admin_client, user_client, db):
    bob = _user(db, "bob")
    assert user_client.get("/library").status_code == 200
    r = post(admin_client, f"/admin/users/{bob.id}/active", {"active": "0"}, headers=HX)
    assert r.status_code == 200
    assert not _user(db, "bob").is_active
    assert user_client.get("/library").status_code == 303  # session gone
    assert db.scalar(select(func.count(UserSession.id)).where(UserSession.user_id == bob.id)) == 0


def test_reenable_user(admin_client, db, new_client):
    add_user(admin_client, "bob")
    bob = _user(db, "bob")
    post(admin_client, f"/admin/users/{bob.id}/active", {"active": "0"}, headers=HX)
    post(admin_client, f"/admin/users/{bob.id}/active", {"active": "1"}, headers=HX)
    assert login(new_client(), "bob").status_code == 303


def test_reset_password_revokes_sessions(admin_client, user_client, db, new_client):
    bob = _user(db, "bob")
    new_password = "a-brand-new-password"
    r = post(admin_client, f"/admin/users/{bob.id}/password", {"new_password": new_password})
    assert r.status_code == 303
    assert user_client.get("/library").status_code == 303
    assert login(new_client(), "bob", PASSWORD).status_code == 400
    assert login(new_client(), "bob", new_password).status_code == 303


def test_reset_password_validates_length(admin_client, db):
    add_user(admin_client, "bob")
    bob = _user(db, "bob")
    r = post(admin_client, f"/admin/users/{bob.id}/password", {"new_password": "x"}, headers=HX)
    assert r.status_code == 409 and "too short" in r.text


def test_delete_user_removes_sessions_too(admin_client, user_client, db):
    bob_id = _user(db, "bob").id
    r = post(admin_client, f"/admin/users/{bob_id}/delete", headers=HX)
    assert r.status_code == 200 and r.text == ""
    assert _user(db, "bob") is None
    assert db.scalar(select(func.count(UserSession.id)).where(UserSession.user_id == bob_id)) == 0
    assert user_client.get("/library").status_code == 303


def test_unknown_user_is_404(admin_client):
    r = post(admin_client, "/admin/users/9999/delete", headers=HX)
    assert r.status_code == 404


def test_last_admin_cannot_be_removed(admin_client, db, settings):
    """Service-level guard, independent of the 'not yourself' check."""
    add_user(admin_client, "bob")
    admin, bob = _user(db, "admin"), _user(db, "bob")
    for call in (
        lambda: users_service.set_role(db, bob, admin, "user"),
        lambda: users_service.set_active(db, bob, admin, False),
        lambda: users_service.delete_user(db, bob, admin),
    ):
        with pytest.raises(users_service.UserError) as exc:
            call()
        assert exc.value.code == "last_admin"


def test_demoting_one_of_two_admins_is_allowed(admin_client, db):
    add_user(admin_client, "boss", role="admin")
    boss = _user(db, "boss")
    r = post(admin_client, f"/admin/users/{boss.id}/role", {"role": "user"}, headers=HX)
    assert r.status_code == 200
    assert _user(db, "boss").role == "user"


def test_users_list_shows_everyone(admin_client, user_client):
    page = admin_client.get("/admin/users").text
    assert "admin" in page and "bob" in page
