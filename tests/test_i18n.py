import json
import re
import string
from pathlib import Path

import pytest
from sqlalchemy import select

from app.i18n import (
    DEFAULT_LANG,
    LOCALES_DIR,
    SUPPORTED,
    parse_accept_language,
    resolve_lang,
    translate,
)
from app.integrations.status import INTEGRATION_NAMES
from app.models import JOB_STATUSES, MATCH_STATUSES, MB_STATUSES, User
from app.services.library.quality import QUALITY_TIERS
from app.services.musicbrainz.categories import CATEGORIES
from tests.conftest import login, post

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _catalog(lang):
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_default_language_is_english():
    assert DEFAULT_LANG == "en"
    assert set(SUPPORTED) == {"en", "it"}


def test_catalogs_have_same_keys():
    en, it = _catalog("en"), _catalog("it")
    assert set(en) == set(it)
    assert all(v.strip() for v in en.values()) and all(v.strip() for v in it.values())


def test_catalogs_have_same_placeholders():
    def fields(text):
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    en, it = _catalog("en"), _catalog("it")
    for key in en:
        assert fields(en[key]) == fields(it[key]), key


def test_all_used_keys_exist():
    """Every t('...') in templates/code and every error code has a catalog entry."""
    en = _catalog("en")
    used = set()
    for path in list(APP_DIR.rglob("*.html")) + list(APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"\bt\(\s*'([\w.]+)'", text))
        used |= {f"error.{c}" for c in re.findall(r'UserError\(\s*"(\w+)"', text)}
        used |= {f"error.{c}" for c in re.findall(r'detail\s*=\s*"(\w+)"', text)}
        used |= {f"flash.{c}" for c in re.findall(r"flash=(\w+)", text)}
        used |= {f"role.{r}" for r in ("admin", "user")}
        used |= {f"job.status.{s}" for s in JOB_STATUSES}
        used |= {f"quality.{t}" for t in QUALITY_TIERS}
        used |= {f"category.{c}" for c in CATEGORIES}
        used |= {f"mb.status.{s}" for s in MB_STATUSES}
        used |= {f"match.status.{s}" for s in MATCH_STATUSES}
        used |= {
            f"review.reason.{r}" for r in ("ambiguous", "low_score", "no_match", "invalid_mbid")
        }
        used |= {f"artists.source.{s}" for s in ("tag", "auto", "manual")}
        used |= {
            f"request.status.{s}"
            for s in (
                "queued",
                "searching",
                "candidate_found",
                "downloading",
                "postprocessing",
                "importing",
                "completed",
                "failed",
                "cancelled",
                "no_results",
            )
        }
        used |= {"library.source.filesystem", "library.source.plex"}
        used |= {f"integration.name.{n}" for n in INTEGRATION_NAMES}
        used |= {
            f"integration.result.{c}"
            for c in ("ok", "not_configured", "unreachable", "auth_failed", "http_error")
        } | {"integration.result.rate_limited", "integration.result.invalid_response"}
    missing = sorted(k for k in used if not k.endswith(".") and k not in en)
    assert not missing, missing


def test_translate_fallbacks():
    assert translate("it", "action.save") == "Salva"
    assert translate("fr", "action.save") == "Save"  # unknown language -> English
    assert translate("it", "does.not.exist") == "does.not.exist"
    assert translate("en", "jobs.detail_title", id=7) == "Job #7"


def test_parse_accept_language():
    assert parse_accept_language("it-IT,it;q=0.9,en;q=0.8") == ["it", "it", "en"]
    assert parse_accept_language("en;q=0.2, it;q=0.9") == ["it", "en"]
    assert parse_accept_language("*") == []
    assert parse_accept_language("") == []


def test_resolve_priority():
    assert resolve_lang("it", "en", "en") == "it"
    assert resolve_lang(None, "it", "en") == "it"
    assert resolve_lang(None, None, "it-IT,en;q=0.5") == "it"
    assert resolve_lang(None, None, "fr, de") == "en"
    assert resolve_lang("xx", "yy", None) == "en"


def test_pages_default_to_english(admin_client, new_client):
    r = new_client().get("/login")
    assert "Sign in" in r.text and 'lang="en"' in r.text


def test_accept_language_italian(admin_client, new_client):
    r = new_client().get("/login", headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.5"})
    assert "Accedi" in r.text and 'lang="it"' in r.text


def test_unsupported_accept_language_falls_back(admin_client, new_client):
    r = new_client().get("/login", headers={"Accept-Language": "fr-FR"})
    assert "Sign in" in r.text


def test_language_cookie_beats_accept_language(admin_client, new_client):
    c = new_client()
    token = re.search(r'csrf-token" content="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/lang", data={"lang": "it", "next": "/login", "csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = c.get("/login", headers={"Accept-Language": "en"})
    assert "Accedi" in page.text


def test_invalid_language_ignored(admin_client, new_client):
    c = new_client()
    token = re.search(r'csrf-token" content="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/lang", data={"lang": "xx", "next": "//evil", "csrf_token": token})
    assert r.headers["location"] == "/"
    assert "diskko_lang" not in r.headers.get("set-cookie", "")


def test_user_preference_is_saved_and_follows_login(admin_client, new_client, db):
    post(admin_client, "/lang", {"lang": "it", "next": "/"})
    db.expire_all()
    assert db.scalar(select(User)).language == "it"
    fresh = new_client()  # no cookie, English browser
    login(fresh, "admin")
    assert "Esci" in fresh.get("/library", headers={"Accept-Language": "en"}).text


def test_setup_stores_detected_language(client, db):
    from tests.conftest import csrf_of

    token = csrf_of(client, "/setup")
    client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
            "csrf_token": token,
        },
        headers={"Accept-Language": "it"},
    )
    assert db.scalar(select(User)).language == "it"


def test_admin_pages_translated(admin_client):
    post(admin_client, "/lang", {"lang": "it", "next": "/"})
    page = admin_client.get("/admin/users").text
    assert "Utenti" in page and "Crea utente" in page


@pytest.mark.parametrize("lang", SUPPORTED)
def test_error_messages_translated(admin_client, lang):
    post(admin_client, "/lang", {"lang": lang, "next": "/"})
    r = admin_client.post("/logout", headers={"accept": "text/html"})
    expected = translate(lang, "error.csrf")
    assert expected in r.text.replace("&#39;", "'")
