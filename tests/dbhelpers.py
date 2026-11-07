"""Builders for MusicBrainz-related rows."""

import json
import uuid

from app.models import Artist, LibraryAlbum, ReleaseGroup
from app.services.library.grouping import name_key
from app.services.musicbrainz.categories import category_for, year_of
from app.services.musicbrainz.titles import album_key


def make_artist(db, name="Aretha Franklin", mbid=None, status="pending", in_library=True):
    artist = Artist(
        name=name,
        sort_name=name,
        name_key=name_key(name),
        mbid=mbid,
        mb_status=status,
        in_library=in_library,
    )
    db.add(artist)
    db.commit()
    return artist


def make_album(db, artist, title, year=None, compilation=False, mbid_rg=None, path=None):
    album = LibraryAlbum(
        artist_id=artist.id,
        source="filesystem",
        path=path or f"{artist.name}/{title}",
        title=title,
        year=year,
        is_compilation=compilation,
        mbid_release_group=mbid_rg,
        track_count=10,
    )
    db.add(album)
    db.commit()
    return album


def make_rg(db, artist, title, year=None, primary="Album", secondary=(), mbid=None):
    date = str(year) if year else None
    rg = ReleaseGroup(
        mbid=mbid
        or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{artist.id}|{title}|{year}|{primary}|{secondary}")),
        artist_id=artist.id,
        title=title,
        primary_type=primary,
        secondary_types_json=json.dumps(list(secondary)),
        first_release_date=date,
        year=year_of(date),
        category=category_for(primary, list(secondary)),
    )
    db.add(rg)
    db.commit()
    return rg


def load_discography(db, artist, *fixture_pages):
    """Store release groups from recorded browse pages as the artist's discography."""
    from app.integrations.musicbrainz_client import _parse_release_group
    from app.services.musicbrainz.sync import store_release_groups
    from tests.mbdata import load

    groups = [
        g
        for name in fixture_pages
        for raw in load(name)["release-groups"]
        if (g := _parse_release_group(raw))
    ]
    store_release_groups(db, artist.id, groups)
    db.commit()
    return groups


__all__ = ["album_key", "load_discography", "make_album", "make_artist", "make_rg"]


def bulk_library(db, artists=100, rgs_per_artist=6, albums_per_artist=3, seed=7, prefix="Artist"):
    """A large synthetic library: artists with release groups (mixed types) and some owned albums."""
    import random

    rnd = random.Random(seed)
    kinds = [
        ("Album", ()),
        ("Album", ()),
        ("EP", ()),
        ("Single", ()),
        ("Album", ("Live",)),
        ("Album", ("Compilation",)),
    ]
    made = []
    for i in range(artists):
        name = f"{prefix} {i:04}"
        artist = Artist(
            name=name,
            sort_name=name,
            name_key=name_key(name),
            mb_status="resolved",
            mbid=str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
        )
        db.add(artist)
        db.flush()
        groups = []
        for j in range(rgs_per_artist):
            primary, secondary = kinds[j % len(kinds)]
            year = 1980 + rnd.randrange(40)
            rg = ReleaseGroup(
                mbid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{name}-{j}")),
                artist_id=artist.id,
                title=f"{name} Record {j}",
                primary_type=primary,
                secondary_types_json=json.dumps(list(secondary)),
                first_release_date=str(year),
                year=year,
                category=category_for(primary, list(secondary)),
                cover_status="ok" if rnd.random() < 0.5 else "unknown",
            )
            rg.cover_path = f"{rg.mbid}.jpg" if rg.cover_status == "ok" else None
            groups.append(rg)
        db.add_all(groups)
        db.flush()
        for rg in rnd.sample(groups, min(albums_per_artist, len(groups))):
            status = rnd.choice(["matched", "matched", "tentative", "unmatched", "manual", "none"])
            db.add(
                LibraryAlbum(
                    artist_id=artist.id,
                    source="filesystem",
                    path=f"{name}/{rg.title}",
                    title=rg.title,
                    year=rg.year,
                    track_count=10,
                    release_group_id=None if status in ("unmatched", "none") else rg.id,
                    match_status=status,
                    quality_tier=rnd.choice(
                        [
                            "any",
                            "mp3_320",
                            "flac_standard",
                            "flac_studio",
                            "flac_hires",
                            "flac_master",
                        ]
                    ),
                    format="flac",
                )
            )
        made.append(artist)
    db.commit()
    return made
