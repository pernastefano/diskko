from fastapi.testclient import TestClient

from app import __version__
from app.config import Settings


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_security_headers(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_html_not_cached(client):
    assert client.get("/setup").headers["cache-control"] == "no-store"


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "abc")
    s = Settings(_env_file=None)
    assert s.music_dir == tmp_path
    assert s.secret_key == "abc"


def test_settings_defaults(monkeypatch):
    for var in ("MUSIC_DIR", "SECRET_KEY", "BASE_URL", "ENV"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.base_url == "http://localhost:8080"
    assert not s.is_production
    assert not s.cookie_secure


def test_static_mount_exists(app):
    with TestClient(app) as c:
        assert c.get("/static/missing.js").status_code == 404
