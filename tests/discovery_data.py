"""Recorded answers of Last.fm, ListenBrainz, iTunes and Deezer (real payloads, public data)."""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"

RADIOHEAD = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
THOM_YORKE = "8ed2e0b3-aa4c-4e13-bec3-dc7393ed4d6b"
NIRVANA = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
ITUNES_OKC = "1097861387"
DEEZER_OKC = "14879699"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
