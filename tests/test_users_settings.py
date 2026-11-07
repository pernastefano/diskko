import re

import pytest
from sqlalchemy import select

from app.models import User, UserSession
from tests.conftest import PASSWORD, add_user, login, post

HX = {"HX-Request": "true"}


def page(client, path="/settings/users"):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def scope(text):
    """The Alpine state of the users section as a dict of its first flags."""
    return {
        "createOpen": re.search(r"createOpen: (true|false)", text).group(1) == "true",
        "resetOpen": re.search(r"resetOpen: (true|false)", text).group(1) == "true",
        "resetId": re.search(r"resetId: ([^,\n]+)", text).group(1).strip(),
    }


def modal_html(text, name):
    return re.search(
        rf'<div x-show="{name}Open".*?data-modal="{name}".*?</form>', text, re.S
    ).group(0)


def me(db, name):
    db.expire_all()
    return db.scalar(select(User).where(User.username == name))


# ------------------------------------------------------------------- the section


def test_users_are_a_section_of_the_settings(admin_client):
    text = page(admin_client)
    assert 'href="/settings/users"' in text and 'aria-current="page"' in text
    assert "Create user" in text and "admin" in text


def test_the_old_users_page_redirects(admin_client):
    r = admin_client.get("/admin/users")
    assert r.status_code == 303 and r.headers["location"] == "/settings/users"


def test_the_menu_has_no_separate_users_link(admin_client):
    text = page(admin_client, "/library")
    assert 'href="/admin/users"' not in text and 'href="/settings"' in text


def test_only_admins_see_the_users(user_client, new_client, admin_client):
    assert user_client.get("/settings/users").status_code == 403
    assert (
        post(user_client, "/admin/users", {"username": "x", "password": PASSWORD}).status_code
        == 403
    )
    assert new_client().get("/settings/users").status_code == 303


def test_both_dialogs_exist_and_start_closed(admin_client):
    text = page(admin_client)
    assert scope(text) == {"createOpen": False, "resetOpen": False, "resetId": "null"}
    for name in ("create", "reset"):
        dialog = re.search(rf'<div x-show="{name}Open"[^>]*>', text).group(0)
        assert 'role="dialog"' in dialog and 'aria-modal="true"' in dialog and "x-cloak" in dialog
        assert f'aria-labelledby="{name}-title"' in dialog and f'id="{name}-title"' in text
    assert "data-modal-error" not in text


def test_the_create_form_lives_in_the_dialog_not_on_the_page(admin_client):
    text = page(admin_client)
    create = modal_html(text, "create")
    assert (
        'action="/admin/users"' in create
        and 'name="username"' in create
        and 'name="role"' in create
    )
    assert text.count('action="/admin/users"') == 1  # no second, always visible, form
    assert 'data-open="create"' in text and '@click="createOpen = true"' in text


def test_every_row_opens_the_shared_reset_dialog(admin_client, user_client):
    text = page(admin_client)
    assert re.findall(r'data-reset="(\d+)"', text) == ["1", "2"]
    assert "openReset(2, &#34;bob&#34;)" in text and "openReset(1, &#34;admin&#34;)" in text
    assert text.count('name="new_password"') == 1  # one dialog for everyone, not one form per row
    assert 'hx-post="/admin/users/2/password"' not in text  # the old inline form is gone


def test_the_dialog_can_be_closed_three_ways(admin_client):
    text = page(admin_client)
    assert '@keydown.escape.window="closeAll()"' in text
    assert '@click.self="closeAll()"' in text
    assert text.count('@click="closeAll()"') == 2  # the Cancel buttons


def test_the_dialog_form_posts_to_the_chosen_user(admin_client):
    reset = modal_html(page(admin_client), "reset")
    assert ":action=\"'/admin/users/' + resetId + '/password'\"" in reset


# ---------------------------------------------------------------- creating users


def test_creating_a_user_from_the_dialog(admin_client, db):
    r = post(
        admin_client,
        "/admin/users",
        {"username": "Carol", "password": PASSWORD, "role": "user"},
        page="/settings/users",
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings/users?flash=user_created"
    assert me(db, "carol").role == "user"
    text = page(admin_client, r.headers["location"])
    assert "User created." in text and "carol" in text
    assert scope(text)["createOpen"] is False


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"username": "admin", "password": PASSWORD, "role": "user"}, "already taken"),
        ({"username": "valid-name", "password": "tiny1", "role": "user"}, "too short"),
        ({"username": "bad name!", "password": PASSWORD, "role": "user"}, "Invalid username"),
        ({"username": "valid-name", "password": PASSWORD, "role": "root"}, "Invalid role"),
    ],
)
def test_a_failed_creation_reopens_the_dialog_with_the_error_inside(
    admin_client, db, data, message
):
    r = post(admin_client, "/admin/users", data, page="/settings/users")
    assert r.status_code == 400
    assert scope(r.text)["createOpen"] is True and scope(r.text)["resetOpen"] is False
    create = modal_html(r.text, "create")
    assert "data-modal-error" in create and message in create
    assert message not in r.text.replace(
        create, ""
    )  # not duplicated as a banner outside the dialog
    assert f'value="{data["username"]}"' in create  # what was typed is kept
    assert PASSWORD not in r.text and data["password"] not in create  # never echoed back
    assert db.scalar(select(__import__("sqlalchemy").func.count(User.id))) == 1


def test_the_chosen_role_is_kept_after_an_error(admin_client):
    r = post(
        admin_client,
        "/admin/users",
        {"username": "x", "password": "short", "role": "admin"},
        page="/settings/users",
    )
    assert re.search(r'<option value="admin" selected>', modal_html(r.text, "create"))


def test_creation_needs_csrf(admin_client):
    assert (
        admin_client.post("/admin/users", data={"username": "x", "password": PASSWORD}).status_code
        == 403
    )


# -------------------------------------------------------------- resetting passwords


def test_resetting_a_password_from_the_dialog(admin_client, user_client, new_client, db):
    bob = me(db, "bob")
    assert user_client.get("/library").status_code == 200
    r = post(
        admin_client,
        f"/admin/users/{bob.id}/password",
        {"new_password": "a-brand-new-password"},
        page="/settings/users",
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings/users?flash=user_updated"
    assert user_client.get("/library").status_code == 303  # signed out everywhere
    assert (
        db.scalar(
            select(__import__("sqlalchemy").func.count(UserSession.id)).where(
                UserSession.user_id == bob.id
            )
        )
        == 0
    )
    assert login(new_client(), "bob", "a-brand-new-password").status_code == 303
    assert login(new_client(), "bob", PASSWORD).status_code == 400


def test_a_failed_reset_reopens_its_dialog(admin_client, db):
    bob = me(db, "bob") or None
    add_user(admin_client, "bob2")
    target = me(db, "bob2")
    r = post(
        admin_client,
        f"/admin/users/{target.id}/password",
        {"new_password": "x"},
        page="/settings/users",
    )
    assert r.status_code == 303
    assert (
        r.headers["location"] == f"/settings/users?modal=reset&uid={target.id}&error=password_short"
    )
    text = page(admin_client, r.headers["location"])
    assert scope(text) == {"createOpen": False, "resetOpen": True, "resetId": str(target.id)}
    reset = modal_html(text, "reset")
    assert "data-modal-error" in reset and "too short" in reset and "bob2" in reset
    assert f'action="/admin/users/{target.id}/password"' in reset  # works even without JavaScript
    assert bob is None  # (bob only exists with the user_client fixture)


def test_the_error_is_inside_the_dialog_not_a_banner(admin_client):
    add_user(admin_client, "dave")
    uid = re.search(r"data-reset=\"(\d+)\"", page(admin_client)).group(1)
    text = page(admin_client, f"/settings/users?modal=reset&uid={uid}&error=password_short")
    assert text.count("too short") == 1 and 'role="alert" class="mb-4' not in text


@pytest.mark.parametrize(
    "query",
    [
        "modal=reset",
        "modal=reset&uid=999",
        "modal=reset&uid=abc",
        "modal=nonsense",
        "modal=reset&uid=-1",
    ],
)
def test_nonsense_dialog_requests_are_ignored(admin_client, query):
    text = page(admin_client, f"/settings/users?{query}")
    assert scope(text)["resetOpen"] is False and scope(text)["createOpen"] is False


def test_the_create_dialog_can_be_requested_by_address(admin_client):
    assert scope(page(admin_client, "/settings/users?modal=create"))["createOpen"] is True


def test_unknown_error_codes_are_not_shown(admin_client):
    assert "bogus" not in page(admin_client, "/settings/users?error=bogus")


def test_row_errors_stay_banners_outside_the_dialogs(admin_client, db):
    admin = me(db, "admin")
    r = post(admin_client, f"/admin/users/{admin.id}/delete", page="/settings/users")
    assert r.headers["location"] == "/settings/users?error=self_action"
    text = page(admin_client, r.headers["location"])
    assert "your own account" in text and "data-modal-error" not in text


# ------------------------------------------------------- row actions still work


def test_row_actions_still_update_the_row_in_place(admin_client, user_client, db):
    bob = me(db, "bob")
    r = post(
        admin_client,
        f"/admin/users/{bob.id}/role",
        {"role": "admin"},
        headers=HX,
        page="/settings/users",
    )
    assert r.status_code == 200 and f'id="user-{bob.id}"' in r.text and "<html" not in r.text
    assert "openReset(" in r.text  # the swapped row can open the shared dialog too
    assert me(db, "bob").role == "admin"
    r = post(
        admin_client,
        f"/admin/users/{bob.id}/active",
        {"active": "0"},
        headers=HX,
        page="/settings/users",
    )
    assert "Disabled" in r.text
    r = post(admin_client, f"/admin/users/{bob.id}/delete", headers=HX, page="/settings/users")
    assert r.status_code == 200 and r.text == "" and me(db, "bob") is None


def test_the_row_has_no_inline_reset_form_any_more(admin_client, user_client, db):
    bob = me(db, "bob")
    r = post(
        admin_client,
        f"/admin/users/{bob.id}/role",
        {"role": "user"},
        headers=HX,
        page="/settings/users",
    )
    assert 'name="new_password"' not in r.text and "x-data" not in r.text


# ----------------------------------------------------------------------- i18n


def test_the_section_in_italian(admin_client, db):
    post(admin_client, "/lang", {"lang": "it", "next": "/library"})
    text = page(admin_client)
    for expected in (
        "Utenti",
        "Crea utente",
        "Reimposta password",
        "Nuova password per",
        "Annulla",
    ):
        assert expected in text, expected
    assert "Reimposta password" in modal_html(text, "reset")
