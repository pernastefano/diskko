from tests.conftest import csrf_of, post


def _fill(path):
    """Replace path parameters with plausible values."""
    for param, value in (
        ("name", "plex"),
        ("user_id", "1"),
        ("job_id", "1"),
        ("artist_id", "1"),
        ("album_id", "1"),
    ):
        path = path.replace("{" + param + "}", value)
    return path


def _admin_routes(app):
    routes = []
    for path, methods in app.openapi()["paths"].items():
        if path.startswith("/admin"):
            for method in methods:
                routes.append((method.upper(), _fill(path)))
    return sorted(routes)


def _call(client, method, path):
    if method == "GET":
        return client.get(path, headers={"accept": "text/html"})
    return post(client, path, {"username": "x", "password": "y", "role": "user"})


def test_admin_routes_are_discovered(app):
    # Guards the dynamic tests below against silently testing nothing.
    assert len(_admin_routes(app)) >= 28


def test_every_admin_route_denies_regular_users(app, user_client):
    for method, path in _admin_routes(app):
        r = _call(user_client, method, path)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_every_admin_route_requires_login(app, admin_client, new_client):
    anon = new_client()
    for method, path in _admin_routes(app):
        if method == "GET":
            r = anon.get(path)
            assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
        else:
            token = csrf_of(anon, "/login")
            r = anon.post(path, data={"csrf_token": token})
            assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


def test_every_admin_route_open_to_admin(app, admin_client):
    for method, path in _admin_routes(app):
        r = _call(admin_client, method, path)
        assert r.status_code != 403 and r.status_code != 401, f"{method} {path}"


def test_regular_user_gets_html_403_page(user_client):
    r = user_client.get("/admin/users", headers={"accept": "text/html"})
    assert r.status_code == 403
    assert "do not have permission" in r.text


def test_regular_user_api_style_403_is_json(user_client):
    r = user_client.get("/admin/users", headers={"accept": "application/json"})
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden"}


def test_htmx_anonymous_gets_401_with_redirect_header(admin_client, new_client):
    r = new_client().get("/admin/users", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"


def test_nav_shows_users_link_only_to_admin(admin_client, user_client):
    assert "/admin/users" in admin_client.get("/").text
    assert "/admin/users" not in user_client.get("/").text


def test_two_users_two_roles_coexist(admin_client, user_client):
    assert admin_client.get("/admin/users").status_code == 200
    assert user_client.get("/admin/users").status_code == 403
    assert user_client.get("/").status_code == 200


def test_unknown_page_is_404(user_client):
    r = user_client.get("/nope", headers={"accept": "text/html"})
    assert r.status_code == 404
