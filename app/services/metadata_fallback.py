"""Metadata for what MusicBrainz does not have: artists and albums found at Deezer.

Only a very sure match is used automatically. For an artist that means the same name (accents,
case and punctuation aside) AND at least one album of the library in that artist's Deezer
discography; with several namesakes the one whose discography overlaps clearly more wins, and
a tie means nobody (the artist then stays in the review queue as before). Anything looser
risks a tribute band or a namesake, and wrong data is worse than none.
"""

from dataclasses import dataclass

from app.integrations.musicbrainz_client import ReleaseGroupData
from app.integrations.previews import DeezerClient
from app.services.library.grouping import name_key
from app.services.musicbrainz.matching import AUTO_TITLE_SIMILARITY, title_similarity

NAMESAKES_TO_COMPARE = 3


@dataclass
class DeezerArtistMatch:
    provider_id: str
    name: str
    fans: int
    overlap: int  # library albums found in the Deezer discography
    groups: list[ReleaseGroupData]


def overlap_count(titles: list[str], groups: list[ReleaseGroupData]) -> int:
    return sum(
        1
        for title in titles
        if any(title_similarity(title, g.title) >= AUTO_TITLE_SIMILARITY for g in groups)
    )


async def find_artist(
    client: DeezerClient, name: str, album_titles: list[str], *, force: bool = False
) -> DeezerArtistMatch | None:
    """The Deezer artist that is this library artist, or None when it is not certain."""
    if not album_titles:
        return None  # nothing to check the identity against
    wanted = name_key(name)
    candidates = [
        c
        for c in await client.search_artists(name, force=force)
        if c.provider_id and name_key(c.name) == wanted
    ]
    candidates.sort(key=lambda c: -c.fans)
    scored: list[DeezerArtistMatch] = []
    for cand in candidates[:NAMESAKES_TO_COMPARE]:
        groups = await client.artist_albums(cand.provider_id, force=force)
        scored.append(
            DeezerArtistMatch(
                cand.provider_id, cand.name, cand.fans, overlap_count(album_titles, groups), groups
            )
        )
    if not scored:
        return None
    ranked = sorted(scored, key=lambda m: (-m.overlap, -m.fans))
    best = ranked[0]
    second = ranked[1].overlap if len(ranked) > 1 else 0
    if best.overlap >= 1 and (second == 0 or best.overlap - second >= 2):
        return best
    return None


def albums_to_add(titles: list[str], groups: list[ReleaseGroupData]) -> list[ReleaseGroupData]:
    """The Deezer albums that are the given library albums (a near-identical title)."""
    chosen: dict[str, ReleaseGroupData] = {}
    for title in titles:
        scored = [(title_similarity(title, g.title), g) for g in groups]
        scored = [(s, g) for s, g in scored if s >= AUTO_TITLE_SIMILARITY]
        if scored:
            best = max(scored, key=lambda x: x[0])[1]
            chosen[best.mbid] = best
    return list(chosen.values())
