"""Wikidata and Wikipedia: the short biography of an artist.

MusicBrainz gives the artist's Wikidata id (a "wikidata" URL relation). Wikidata's
`wbgetentities` (props=sitelinks, sitefilter=itwiki|enwiki) gives the title of the artist's page in
each language: {"entities": {"Q44190": {"sitelinks": {"itwiki": {"title": "Radiohead"}}}}}, and an
unknown id comes back as {"missing": ""}. Wikipedia's REST summary,
GET /api/rest_v1/page/summary/{title} -> {"type": "standard", "extract", "content_urls":
{"desktop": {"page"}}}, has the lead of the article as plain text ("type" is "disambiguation"
when the title is not one thing). Both are free and need no key; Wikimedia asks for an
identifying User-Agent (the MusicBrainz contact is used when set). The text is CC BY-SA: it is
shown with a link to the article.
"""

import re
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from app.integrations.cached_http import MISSING, OK, CachedJsonClient, ServiceError
from app.integrations.discovery_types import BioData

SITELINKS_TTL = timedelta(days=30)
SUMMARY_TTL = timedelta(days=30)
_LANG = re.compile(r"^[a-z]{2,3}$")
_QID = re.compile(r"^Q\d{1,12}$")


class WikidataClient(CachedJsonClient):
    service = "wikidata"
    limiter_name = "wikimedia"  # Wikimedia limits per identity across all its sites together
    min_interval = 0.5  # 120 a minute: under the 200 a minute of a client with a proper User-Agent
    BACKOFF_BASE = 5.0  # after a 429 without Retry-After they ask for at least five seconds
    RATE_LIMIT_PAUSE = 5.0

    def interpret(self, status: int, data: Any) -> str:
        if isinstance(data, dict) and data.get("error"):
            raise ServiceError("Wikidata refused the request")
        return super().interpret(status, data)

    async def sitelinks(self, qid: str, langs: tuple[str, ...], *, force=False) -> dict[str, str]:
        """{language: title of the Wikipedia page} for the languages that have one."""
        if not _QID.match(qid) or not all(_LANG.match(lang) for lang in langs):
            raise ValueError("invalid Wikidata id or language")
        data = await self.get_json(
            self.settings.wikidata_url,
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "sitelinks",
                "sitefilter": "|".join(f"{lang}wiki" for lang in langs),
                "format": "json",
            },
            ttl=SITELINKS_TTL,
            force=force,
        )
        links = (((data or {}).get("entities") or {}).get(qid) or {}).get("sitelinks") or {}
        return {
            lang: str(links[f"{lang}wiki"]["title"])
            for lang in langs
            if (links.get(f"{lang}wiki") or {}).get("title")
        }


class WikipediaClient(CachedJsonClient):
    service = "wikipedia"
    limiter_name = "wikimedia"
    min_interval = 0.5
    BACKOFF_BASE = 5.0
    RATE_LIMIT_PAUSE = 5.0

    def interpret(self, status: int, data: Any) -> str:
        if status == 404:
            return MISSING
        return OK if status == 200 else super().interpret(status, data)

    async def summary(self, lang: str, title: str, *, force=False) -> BioData | None:
        """The lead of the article in plain text, with the address of the article."""
        if not _LANG.match(lang):
            raise ValueError("invalid language")
        base = self.settings.wikipedia_url.format(lang=lang).rstrip("/")
        data = await self.get_json(
            f"{base}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}",
            {},
            ttl=SUMMARY_TTL,
            force=force,
        )
        if not isinstance(data, dict) or data.get("type") != "standard":
            return None  # missing, or a disambiguation page: not the artist
        text = " ".join(str(data.get("extract") or "").split())
        if not text:
            return None
        page = str(((data.get("content_urls") or {}).get("desktop") or {}).get("page") or "")
        host = f"{lang}.wikipedia.org"
        return BioData(text, page if page.startswith(f"https://{host}/") else None)


def wikidata_id(relations: list[dict[str, Any]]) -> str | None:
    """The Q-id in the "wikidata" URL relation of a MusicBrainz artist, if there is one."""
    for rel in relations or []:
        if rel.get("type") == "wikidata":
            tail = str((rel.get("url") or {}).get("resource") or "").rstrip("/").rsplit("/", 1)[-1]
            if _QID.match(tail):
                return tail
    return None
