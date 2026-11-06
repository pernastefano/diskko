from fastapi.testclient import TestClient

from app import __version__
from app.config import Settings
from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_index_is_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "DISKKO" in r.text


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "abc")
    s = Settings(_env_file=None)
    assert s.music_dir == tmp_path
    assert s.secret_key == "abc"


def test_settings_defaults(monkeypatch):
    for var in ("MUSIC_DIR", "SECRET_KEY", "BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.base_url == "http://localhost:8080"
