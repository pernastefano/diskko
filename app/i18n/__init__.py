"""Minimal i18n: flat JSON catalogs per language, English is the default and fallback."""

import json
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANG = "en"
LANG_NAMES = {"en": "English", "it": "Italiano"}
SUPPORTED = tuple(LANG_NAMES)
LANG_COOKIE = "diskko_lang"

_catalogs: dict[str, dict[str, str]] = {
    lang: json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    for lang in SUPPORTED
}


def has_key(key: str) -> bool:
    return key in _catalogs[DEFAULT_LANG]


def translate(lang: str, key: str, **params: object) -> str:
    text = _catalogs.get(lang, {}).get(key) or _catalogs[DEFAULT_LANG].get(key) or key
    return text.format(**params) if params else text


def parse_accept_language(header: str) -> list[str]:
    """Primary language subtags from an Accept-Language header, best first."""
    items: list[tuple[float, int, str]] = []
    for idx, part in enumerate(header.split(",")):
        tokens = part.strip().split(";")
        tag = tokens[0].strip().lower()
        if not tag or tag == "*":
            continue
        q = 1.0
        for extra in tokens[1:]:
            extra = extra.strip()
            if extra.startswith("q="):
                try:
                    q = float(extra[2:])
                except ValueError:
                    q = 0.0
        items.append((-q, idx, tag.split("-")[0]))
    return [tag for _, _, tag in sorted(items)]


def resolve_lang(user_language: str | None, cookie: str | None, accept: str | None) -> str:
    """User preference, then cookie, then Accept-Language, then English."""
    if user_language in SUPPORTED:
        return user_language
    if cookie in SUPPORTED:
        return cookie
    for tag in parse_accept_language(accept or ""):
        if tag in SUPPORTED:
            return tag
    return DEFAULT_LANG
