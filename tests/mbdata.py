"""Helpers for MusicBrainz tests: recorded API answers and a fake clock."""

import json
import uuid
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"

ARETHA = "2f9ecbed-27be-40e6-abca-6de49d50299e"
NIRVANA_US = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
NIRVANA_UK = "9282c8b4-ca0b-4c6b-b7e3-4f7762dfc4d6"
LADY_SOUL_RG = "05571372-5b11-39b7-89c5-1c02589a27c8"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def synthetic_release_groups(count: int, seed: str) -> list[dict]:
    """Release groups shaped like the real ones, for pagination tests."""
    return [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{seed}-{i}")),
            "title": f"{seed} {i}",
            "primary-type": "Album",
            "primary-type-id": "f529b476-6e62-324f-b0aa-1f3e33d313fc",
            "secondary-types": [],
            "secondary-type-ids": [],
            "first-release-date": f"{1970 + i % 40}",
            "disambiguation": "",
        }
        for i in range(count)
    ]


class FakeTime:
    """Clock + sleep that never really waits."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def browse_router(discographies: dict[str, list[dict]], missing: frozenset[str] = frozenset()):
    """respx side effect serving /release-group browse pages per artist MBID."""
    import httpx

    def answer(request):
        artist = request.url.params["artist"]
        if artist in missing:
            return httpx.Response(404, json={"error": "Not Found"})
        groups = discographies.get(artist, [])
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        return httpx.Response(
            200,
            json={
                "release-group-count": len(groups),
                "release-group-offset": offset,
                "release-groups": groups[offset : offset + limit],
            },
        )

    return answer


def aretha_discography() -> list[dict]:
    return (
        load("rg_browse_aretha_p0.json")["release-groups"]
        + load("rg_browse_aretha_p1.json")["release-groups"]
    )


def nirvana_discographies() -> dict[str, list[dict]]:
    return {
        NIRVANA_US: load("rg_browse_nirvana_us.json")["release-groups"],
        NIRVANA_UK: load("rg_browse_nirvana_uk.json")["release-groups"],
    }
