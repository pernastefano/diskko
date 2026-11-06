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
