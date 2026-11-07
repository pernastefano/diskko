import re

import pytest
from sqlalchemy import select

from app.models import User
from tests.conftest import add_user, login, post


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def html_tag(text):
    return re.search(r"<html[^>]*>", text).group(0)


def me(db, username):
    db.expire_all()
    return db.scalar(select(User).where(User.username == username))


def test_the_page_has_language_and_theme_and_no_album_types(user_client):
    text = page(user_client, "/account/preferences")
    assert 'name="language"' in text and 'name="theme"' in text
    assert (
        'name="types"' not in text and 'name="mode"' not in text
    )  # album types are a system setting
    assert text.count('name="theme" value=') == 3
    for label in ("Automatic", "Light", "Dark"):
        assert label in text


def test_defaults_are_the_browser_language_and_automatic_theme(user_client):
    text = page(user_client, "/account/preferences")
    assert '<option value="en" selected>' in text and 'value="auto" checked' in text


def test_saving_language_and_theme(user_client, db):
    r = post(
        user_client,
        "/account/preferences",
        {"language": "it", "theme": "dark"},
        page="/account/preferences",
    )
    assert (
        r.status_code == 303
        and r.headers["location"] == "/account/preferences?flash=preferences_saved"
    )
    bob = me(db, "bob")
    assert (bob.language, bob.theme) == ("it", "dark")
    text = page(user_client, r.headers["location"])
    assert (
        "Preferenze salvate." in text
        and '<option value="it" selected>' in text
        and 'value="dark" checked' in text
    )


def test_the_language_cookie_follows_for_the_login_page(user_client, new_client):
    r = post(
        user_client,
        "/account/preferences",
        {"language": "it", "theme": "auto"},
        page="/account/preferences",
    )
    assert "diskko_lang=it" in r.headers["set-cookie"]


@pytest.mark.parametrize(
    "data",
    [
        {"language": "fr", "theme": "dark"},
        {"language": "it", "theme": "neon"},
        {"language": "", "theme": ""},
        {},
    ],
)
def test_invalid_choices_are_refused_and_nothing_changes(user_client, db, data):
    r = post(user_client, "/account/preferences", data, page="/account/preferences")
    assert r.status_code == 400 and "Choose a language and a theme" in r.text
    bob = me(db, "bob")
    assert bob.language is None and bob.theme == "auto"


def test_each_user_has_their_own_preferences(admin_client, user_client, db):
    post(
        user_client,
        "/account/preferences",
        {"language": "it", "theme": "dark"},
        page="/account/preferences",
    )
    post(
        admin_client,
        "/account/preferences",
        {"language": "en", "theme": "light"},
        page="/account/preferences",
    )
    assert (me(db, "bob").language, me(db, "bob").theme) == ("it", "dark")
    assert (me(db, "admin").language, me(db, "admin").theme) == ("en", "light")
    assert "Ciao" not in page(admin_client, "/dashboard") and "Esci" in page(
        user_client, "/dashboard"
    )


def test_preferences_survive_a_new_login(admin_client, new_client, db):
    add_user(admin_client, "carol")
    c = new_client()
    login(c, "carol")
    post(
        c, "/account/preferences", {"language": "it", "theme": "dark"}, page="/account/preferences"
    )
    fresh = new_client()
    login(fresh, "carol")
    text = page(fresh, "/dashboard")
    assert 'lang="it"' in text and ' dark"' in html_tag(text) or "dark" in html_tag(text)


def test_the_theme_is_applied_to_the_page(user_client):
    assert (
        "dark"
        not in html_tag(page(user_client, "/dashboard")).split('class="')[1].split('"')[0].split()
    )
    assert 'data-theme="auto"' in html_tag(page(user_client, "/dashboard"))
    post(
        user_client,
        "/account/preferences",
        {"language": "en", "theme": "dark"},
        page="/account/preferences",
    )
    tag = html_tag(page(user_client, "/dashboard"))
    assert 'data-theme="dark"' in tag and "dark" in tag.split('class="')[1].split('"')[0].split()
    post(
        user_client,
        "/account/preferences",
        {"language": "en", "theme": "light"},
        page="/account/preferences",
    )
    tag = html_tag(page(user_client, "/dashboard"))
    assert (
        'data-theme="light"' in tag and "dark" not in tag.split('class="')[1].split('"')[0].split()
    )


def test_automatic_follows_the_system_with_a_script_before_the_first_paint(user_client):
    text = page(user_client, "/dashboard")
    script = re.search(r"<script>\s*\(function \(\) \{.*?</script>", text, re.S).group(0)
    assert "prefers-color-scheme: dark" in script and "addEventListener('change'" in script
    assert text.index(script) < text.index("cdn.tailwindcss.com")  # runs before the styles load
    assert '<link rel="stylesheet" href="/static/theme.css">' in text


def test_pages_before_login_use_the_automatic_theme(new_client, admin_client):
    assert 'data-theme="auto"' in html_tag(new_client().get("/login").text)


def test_the_language_selector_is_only_shown_before_logging_in(admin_client, new_client):
    assert 'id="lang-select"' in new_client().get("/login").text
    assert 'id="lang-select"' not in page(admin_client, "/dashboard")
    assert 'id="lang-select"' not in page(admin_client, "/account/preferences")


def test_the_login_language_switch_still_works(admin_client, new_client):
    c = new_client()
    token = re.search(r'csrf-token" content="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/lang", data={"lang": "it", "next": "/login", "csrf_token": token})
    assert r.status_code == 303
    assert "Accedi" in c.get("/login").text


def test_the_preferences_are_in_italian(user_client):
    post(
        user_client,
        "/account/preferences",
        {"language": "it", "theme": "auto"},
        page="/account/preferences",
    )
    text = page(user_client, "/account/preferences")
    for expected in ("Lingua", "Tema", "Automatico", "Chiaro", "Scuro"):
        assert expected in text


def test_the_old_per_user_album_types_form_is_gone(user_client):
    r = post(
        user_client,
        "/account/preferences",
        {"mode": "custom", "types": ["live"]},
        page="/account/preferences",
    )
    assert r.status_code == 400  # no language/theme in that request


def test_preferences_need_a_login(new_client, admin_client):
    r = new_client().get("/account/preferences")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
