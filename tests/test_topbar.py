import re

import pytest

from tests.conftest import post

ADMIN_PAGES = [
    "/library",
    "/admin/artists",
    "/admin/library",
    "/admin/jobs",
    "/admin/status",
    "/settings/library",
    "/settings/integrations",
    "/settings/jobs",
    "/settings/albums",
    "/settings/users",
    "/account/preferences",
    "/account/password",
]


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def nav_icons(text):
    return re.findall(
        r'<a href="([^"]+)" title="([^"]*)" aria-label="([^"]*)" data-nav="([a-z]+)"', text
    )


def test_admins_get_three_icons_in_a_fixed_order(admin_client):
    icons = nav_icons(page(admin_client, "/library"))
    assert [(i[0], i[3]) for i in icons] == [
        ("/admin/jobs", "jobs"),
        ("/admin/status", "status"),
        ("/settings", "settings"),
    ]


def test_icons_come_before_the_user_menu_on_the_right(admin_client):
    text = page(admin_client, "/library")
    positions = [text.index(f'data-nav="{n}"') for n in ("jobs", "status", "settings")]
    assert positions == sorted(positions) and positions[-1] < text.index('aria-haspopup="menu"')
    assert text.index('href="/library"') < positions[0]  # the text links stay on the left


def test_icons_have_labels_for_mouse_and_screen_readers(admin_client):
    text = page(admin_client, "/library")
    for _, title, label, _ in nav_icons(text):
        assert title == label and title
    assert {i[1] for i in nav_icons(text)} == {"Jobs", "Status", "Settings"}
    assert text.count('<span class="sr-only">Jobs</span>') == 1 and "<svg" in text


def test_icons_are_translated(admin_client):
    post(admin_client, "/lang", {"lang": "it", "next": "/library"})
    assert {i[1] for i in nav_icons(page(admin_client, "/library"))} == {
        "Job",
        "Stato",
        "Impostazioni",
    }


def test_regular_users_have_no_icons(user_client):
    text = page(user_client, "/library")
    assert nav_icons(text) == [] and "data-nav=" not in text
    assert 'href="/library"' in text and 'aria-haspopup="menu"' in text


@pytest.mark.parametrize(
    ("path", "nav"),
    [
        ("/admin/jobs", "jobs"),
        ("/admin/jobs/1", "jobs"),
        ("/admin/status", "status"),
        ("/settings/jobs", "settings"),
        ("/settings/albums", "settings"),
    ],
)
def test_the_icon_of_the_current_section_is_highlighted(admin_client, app, path, nav):
    if path.endswith("/1"):
        from app.jobs import engine

        with app.state.session_factory() as db:
            engine.enqueue(db, "demo", {})
    text = page(admin_client, path)
    current = re.findall(r'<a href="[^"]+"[^>]*data-nav="([a-z]+)"[^>]*aria-current="page"', text)
    assert current == [nav]
    assert text.count('aria-current="page"') >= 1


def test_no_icon_is_highlighted_elsewhere(admin_client):
    assert not re.search(r'data-nav="[a-z]+"[^>]*aria-current', page(admin_client, "/library"))


def test_the_user_menu(admin_client):
    text = page(admin_client, "/library")
    menu = text.split('role="menu"', 1)[1]
    for target in ("/account/preferences", "/account/password", "/logout"):
        assert target in menu
    assert "admin" in text and "(Administrator)" in text


def test_the_top_bar_wraps_instead_of_overflowing(admin_client):
    nav = re.search(r"<nav class=\"([^\"]+)\"", page(admin_client, "/library")).group(1)
    assert "flex-wrap" in nav and "max-w-5xl" in nav


# ----------------------------------------------------- one width for every page


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_every_page_has_the_same_width(admin_client, path):
    text = page(admin_client, path)
    assert re.search(r'<main class="mx-auto max-w-5xl px-4 py-8">', text)
    assert "max-w-5xl" in re.search(r'<nav class="([^"]+)"', text).group(1)
    assert "max-w-7xl" not in text


def test_the_artist_page_has_the_same_width_too(app, admin_client):
    from tests.dbhelpers import make_artist

    with app.state.session_factory() as db:
        artist_id = make_artist(db, "Someone").id
    text = page(admin_client, f"/artist/{artist_id}")
    assert '<main class="mx-auto max-w-5xl px-4 py-8">' in text and "max-w-7xl" not in text


def test_no_template_sets_its_own_page_width():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"
    for template in root.rglob("*.html"):
        assert "main_width" not in template.read_text(), template
