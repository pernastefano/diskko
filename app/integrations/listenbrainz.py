"""ListenBrainz similar artists, the fallback of Last.fm.

The official documentation (listenbrainz.readthedocs.io) has no similar-artists endpoint. The
one used here lives on the "labs" dataset server and is checked against live answers only:
GET {labs}/similar-artists/json?artist_mbids=<mbid>&algorithm=<algorithm> returns a JSON list of
{"artist_mbid", "name", "comment", "type", "gender", "score" (integer), "reference_mbid"},
best first. The official rules (1 request per second, a User-Agent with a contact) are followed.
"""

import re
from datetime import timedelta
from typing import Any

from app.integrations.cached_http import CachedJsonClient, NotConfigured
from app.integrations.discovery_types import SimilarHit

SIMILAR_TTL = timedelta(days=14)
# One of the algorithms the labs viewer lists; changing it changes the scale of the scores.
ALGORITHM = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ListenBrainzClient(CachedJsonClient):
    service = "listenbrainz"
    min_interval = 1.1

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not self.settings.mb_user_agent.strip():  # ListenBrainz wants a contact
            raise NotConfigured("MB_USER_AGENT is not set: ListenBrainz requires a contact")

    async def similar(self, mbid: str, *, limit: int = 30, force: bool = False) -> list[SimilarHit]:
        data = await self.get_json(
            f"{self.settings.listenbrainz_url.rstrip('/')}/similar-artists/json",
            {"artist_mbids": mbid, "algorithm": ALGORITHM},
            ttl=SIMILAR_TTL,
            force=force,
        )
        return parse_similar(data, mbid)[:limit]


def parse_similar(data: Any, seed_mbid: str = "") -> list[SimilarHit]:
    """Scores are scaled so the best result is 1."""
    rows = []
    for raw in data if isinstance(data, list) else []:
        mbid = str(raw.get("artist_mbid") or "").lower()
        name = str(raw.get("name") or "").strip()
        if not name or not _UUID.match(mbid) or mbid == seed_mbid.lower():
            continue
        try:
            rows.append((name, mbid, float(raw.get("score") or 0)))
        except (TypeError, ValueError):
            continue
    top = max((score for _, _, score in rows), default=0.0)
    rows.sort(key=lambda r: -r[2])
    return [
        SimilarHit(name, mbid, round(score / top, 4) if top else 0.0) for name, mbid, score in rows
    ]
