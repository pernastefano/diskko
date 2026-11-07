import pytest

from tests.mbdata import LADY_SOUL_RG

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
PNG = b"\x89PNG" + b"\0" * 16


@pytest.fixture
def covers(app, admin_client):
    directory = app.state.settings.data_dir / "covers"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{LADY_SOUL_RG}.jpg").write_bytes(JPEG)
    (directory / "05571372-5b11-39b7-89c5-1c02589a28c9.png").write_bytes(PNG)
    (app.state.settings.data_dir / "secret.txt").write_text("top secret")
    return directory


def test_a_cover_is_served_to_logged_in_users(admin_client, user_client, covers):
    for client in (admin_client, user_client):
        r = client.get(f"/covers/{LADY_SOUL_RG}.jpg")
        assert r.status_code == 200 and r.content == JPEG
        assert r.headers["content-type"] == "image/jpeg"
        assert "private" in r.headers["cache-control"]


def test_png_covers_have_their_own_type(admin_client, covers):
    r = admin_client.get("/covers/05571372-5b11-39b7-89c5-1c02589a28c9.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_covers_need_a_login(admin_client, new_client, covers):
    r = new_client().get(f"/covers/{LADY_SOUL_RG}.jpg")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_a_missing_cover_is_404(admin_client, covers):
    assert admin_client.get("/covers/00000000-0000-4000-8000-000000000000.jpg").status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "../secret.txt",
        "..%2Fsecret.txt",
        "%2e%2e%2fsecret.txt",
        "secret.txt",
        f"{LADY_SOUL_RG}.gif",
        f"{LADY_SOUL_RG}.jpg.txt",
        f"{LADY_SOUL_RG[:-1]}.jpg",
        f"..%5C{LADY_SOUL_RG}.jpg",
        ".tmp-abc",
        "",
    ],
)
def test_only_release_group_images_are_ever_served(admin_client, covers, name):
    r = admin_client.get(f"/covers/{name}")
    assert r.status_code == 404
    assert b"top secret" not in r.content


def test_a_symlink_out_of_the_covers_folder_is_not_followed(app, admin_client, covers, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    (covers / "11111111-1111-4111-8111-111111111111.jpg").symlink_to(outside)
    assert admin_client.get("/covers/11111111-1111-4111-8111-111111111111.jpg").status_code == 404
