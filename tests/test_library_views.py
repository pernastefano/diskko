import random

import pytest
from sqlalchemy import event, select

from app.models import LibraryAlbum, ReleaseGroup, User
from app.services.library_views import (
    PAGE_SIZE,
    UNMATCHED,
    available_letters,
    build_artist_sections,
    describe_format,
    list_artists,
    representative_covers,
    request_statuses,
)
from app.services.musicbrainz.categories import CATEGORIES
from app.services.musicbrainz.matching import match_artist_albums
from app.services.musicbrainz.missing import missing_counts, missing_release_groups
from tests.dbhelpers import bulk_library, make_album, make_artist, make_rg

ALBUMS = ["album"]


def names(rows):
    return [r.artist.name for r in rows]


@pytest.fixture
def small(jdb):
    """Three artists: owns/misses different things."""
    a = make_artist(jdb, "Alpha")
    b = make_artist(jdb, "Beta")
    c = make_artist(jdb, "The Gamma")
    for artist, count in ((a, 3), (b, 2), (c, 1)):
        for i in range(count):
            make_rg(jdb, artist, f"{artist.name} {i}", 2000 + i)
    make_album(jdb, a, "Alpha 0", year=2000)
    match_artist_albums(jdb, a.id)
    jdb.commit()
    return a, b, c


# --------------------------------------------------------------------- listing


def test_counts_albums_and_missing_per_artist(jdb, small):
    rows, total = list_artists(jdb, types=ALBUMS)
    assert total == 3
    by = {r.artist.name: (r.albums, r.missing) for r in rows}
    assert by == {"Alpha": (1, 2), "Beta": (0, 2), "The Gamma": (0, 1)}


def test_missing_follows_the_selected_types(jdb, small):
    a, *_ = small
    make_rg(jdb, a, "Alpha Live", 2005, "Album", ("Live",))
    assert {r.artist.name: r.missing for r in list_artists(jdb, types=ALBUMS)[0]}["Alpha"] == 2
    assert {r.artist.name: r.missing for r in list_artists(jdb, types=["live"])[0]}["Alpha"] == 1
    assert {r.artist.name: r.missing for r in list_artists(jdb, types=[])[0]}["Alpha"] == 0
    assert {r.artist.name: r.missing for r in list_artists(jdb, types=CATEGORIES)[0]}["Alpha"] == 3


def test_artists_outside_the_library_are_hidden(jdb, small):
    make_artist(jdb, "Gone", in_library=False)
    assert "Gone" not in names(list_artists(jdb, types=ALBUMS)[0])


def test_name_sorting_ignores_case_and_uses_the_sort_name(jdb, small):
    _, _, gamma = small
    gamma.sort_name = "Gamma, The"
    make_artist(jdb, "alpha two")
    jdb.commit()
    assert names(list_artists(jdb, types=ALBUMS)[0]) == ["Alpha", "alpha two", "Beta", "The Gamma"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("alp", ["Alpha"]),
        ("ALPHA", ["Alpha"]),
        ("amma", ["The Gamma"]),
        ("zzz", []),
        ("  beta ", ["Beta"]),
    ],
)
def test_search_is_case_insensitive_and_partial(jdb, small, query, expected):
    assert names(list_artists(jdb, types=ALBUMS, q=query)[0]) == expected


def test_search_ignores_accents_and_punctuation(jdb):
    make_artist(jdb, "Sigur Rós")
    make_artist(jdb, "AC/DC")
    assert names(list_artists(jdb, types=ALBUMS, q="sigur ros")[0]) == ["Sigur Rós"]
    assert names(list_artists(jdb, types=ALBUMS, q="ac dc")[0]) == ["AC/DC"]


@pytest.mark.parametrize("wild", ["%", "_", "\\", "%%", "_%"])
def test_search_treats_wildcards_literally(jdb, small, wild):
    assert list_artists(jdb, types=ALBUMS, q=wild)[1] == 0


def test_punctuation_in_a_search_is_ignored_like_everywhere_else(jdb, small):
    assert names(list_artists(jdb, types=ALBUMS, q="alp%")[0]) == ["Alpha"]


def test_letter_filter_and_the_hash_bucket(jdb, small):
    make_artist(jdb, "!!!")
    make_artist(jdb, "2Pac")
    make_artist(jdb, "Édith Piaf")
    make_artist(jdb, "Борис Гребенщиков")
    assert names(list_artists(jdb, types=ALBUMS, letter="a")[0]) == ["Alpha"]
    assert names(list_artists(jdb, types=ALBUMS, letter="E")[0]) == ["Édith Piaf"]  # accents fold
    assert sorted(names(list_artists(jdb, types=ALBUMS, letter="#")[0])) == [
        "!!!",
        "2Pac",
        "Борис Гребенщиков",
    ]
    assert list_artists(jdb, types=ALBUMS, letter="Z")[1] == 0
    assert list_artists(jdb, types=ALBUMS, letter="ß")[1] == 7  # not a letter we filter on: ignored


def test_available_letters(jdb, small):
    make_artist(jdb, "2Pac")
    assert available_letters(jdb) == {"A", "B", "T", "#"}


def test_only_missing_filter(jdb, small):
    a, b, c = small
    for rg in jdb.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == c.id)):
        make_album(jdb, c, rg.title, year=rg.year)
    match_artist_albums(jdb, c.id)
    jdb.commit()
    assert names(list_artists(jdb, types=ALBUMS, only_missing=True)[0]) == ["Alpha", "Beta"]
    assert list_artists(jdb, types=[], only_missing=True)[1] == 0


def test_sort_by_missing_puts_the_most_missing_first(jdb, small):
    assert names(list_artists(jdb, types=ALBUMS, sort="missing")[0]) == [
        "Alpha",
        "Beta",
        "The Gamma",
    ]
    a, b, c = small
    make_rg(jdb, c, "Another", 2010)
    make_rg(jdb, c, "Yet Another", 2011)
    assert names(list_artists(jdb, types=ALBUMS, sort="missing")[0])[0] == "The Gamma"


def test_sort_by_recent(jdb, small):
    newest = make_artist(jdb, "Newest")
    assert names(list_artists(jdb, types=ALBUMS, sort="recent")[0])[0] == "Newest"
    assert newest.id == max(r.artist.id for r in list_artists(jdb, types=ALBUMS)[0])


def test_unknown_sort_falls_back_to_name(jdb, small):
    assert names(list_artists(jdb, types=ALBUMS, sort="bogus")[0]) == ["Alpha", "Beta", "The Gamma"]


def test_pagination_is_stable_and_complete(jdb):
    bulk_library(jdb, artists=130, rgs_per_artist=3, albums_per_artist=1)
    seen = []
    for page in (1, 2, 3, 4):
        rows, total = list_artists(jdb, types=ALBUMS, page=page)
        assert total == 130 and len(rows) == (PAGE_SIZE if page < 3 else 34 if page == 3 else 0)
        seen += names(rows)
    assert len(seen) == len(set(seen)) == 130 and seen == sorted(seen)
    assert (
        list_artists(jdb, types=ALBUMS, page=0)[0][0].artist.name == "Artist 0000"
    )  # page < 1 = first


def test_pagination_with_ties_does_not_repeat_or_skip(jdb):
    bulk_library(jdb, artists=100, rgs_per_artist=2, albums_per_artist=0)
    for sort in ("missing", "recent"):
        ids = []
        for page in (1, 2, 3):
            ids += [r.artist.id for r in list_artists(jdb, types=ALBUMS, sort=sort, page=page)[0]]
        assert len(ids) == len(set(ids)) == 100, sort


def test_representative_cover_is_the_earliest_owned_cover(jdb):
    artist = make_artist(jdb, "Covered")
    late = make_rg(jdb, artist, "Late", 2010)
    early = make_rg(jdb, artist, "Early", 1990)
    nocover = make_rg(jdb, artist, "Bare", 1980)
    for rg, path in ((late, "late.jpg"), (early, "early.jpg")):
        rg.cover_status, rg.cover_path = "ok", path
    for rg in (late, early, nocover):
        make_album(jdb, artist, rg.title, year=rg.year)
    match_artist_albums(jdb, artist.id)
    jdb.commit()
    assert representative_covers(jdb, [artist.id]) == {artist.id: "early.jpg"}
    assert representative_covers(jdb, []) == {}
    assert list_artists(jdb, types=ALBUMS)[0][0].cover == "early.jpg"


def test_only_owned_albums_provide_a_tile_cover(jdb):
    artist = make_artist(jdb, "Unowned")
    rg = make_rg(jdb, artist, "Not Mine", 2000)
    rg.cover_status, rg.cover_path = "ok", "x.jpg"
    jdb.commit()
    assert representative_covers(jdb, [artist.id]) == {}


# ------------------------------------- SQL aggregation must agree with the Python rules


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sql_missing_counts_match_the_python_rules(jdb, seed):
    bulk_library(jdb, artists=60, rgs_per_artist=8, albums_per_artist=4, seed=seed)
    rnd = random.Random(seed)
    for types in (
        ["album"],
        ["album", "ep"],
        ["live", "compilation"],
        list(CATEGORIES),
        [],
        ["single"],
    ):
        rows, _ = list_artists(jdb, types=types, page_size=1000)
        ids = [r.artist.id for r in rows]
        python = missing_counts(jdb, ids, types)
        for row in rows:
            assert (
                row.missing
                == python[row.artist.id]
                == len(missing_release_groups(jdb, row.artist.id, types))
            ), (seed, types, row.artist.name)
    rnd.random()


def test_two_users_with_different_types_see_different_numbers(jdb, small):
    a, *_ = small
    make_rg(jdb, a, "Alpha Live", 2005, "Album", ("Live",))
    one = {r.artist.name: r.missing for r in list_artists(jdb, types=["album"])[0]}
    two = {r.artist.name: r.missing for r in list_artists(jdb, types=["live"])[0]}
    assert one["Alpha"] == 2 and two["Alpha"] == 1 and two["Beta"] == 0


# ------------------------------------------------------------- the query budget


class QueryCounter:
    def __init__(self, engine):
        self.count = 0
        self._engine = engine
        event.listen(engine, "before_cursor_execute", self._hit)

    def _hit(self, *args):
        self.count += 1

    def close(self):
        event.remove(self._engine, "before_cursor_execute", self._hit)


def count_queries(db, func):
    counter = QueryCounter(db.get_bind())
    try:
        func()
    finally:
        counter.close()
    return counter.count


def test_listing_uses_a_constant_number_of_queries(jdb):
    """No N+1: a page costs the same however many artists the library has."""
    bulk_library(jdb, artists=20, rgs_per_artist=6)
    small = count_queries(jdb, lambda: list_artists(jdb, types=ALBUMS))
    bulk_library(jdb, artists=400, rgs_per_artist=6, prefix="More")
    large = count_queries(jdb, lambda: list_artists(jdb, types=ALBUMS))
    deep = count_queries(jdb, lambda: list_artists(jdb, types=ALBUMS, sort="missing", page=5))
    assert small == large == deep and large <= 4


def test_artist_page_uses_a_constant_number_of_queries(jdb):
    artist = bulk_library(jdb, artists=2, rgs_per_artist=6)[0]
    base = count_queries(jdb, lambda: build_artist_sections(jdb, artist.id, ALBUMS))
    for i in range(80):
        make_rg(jdb, artist, f"More {i}", 1990 + i % 20)
    many = count_queries(jdb, lambda: build_artist_sections(jdb, artist.id, ALBUMS))
    assert base == many and many <= 3


def test_a_thousand_artists_page_quickly(jdb):
    import time

    bulk_library(jdb, artists=1500, rgs_per_artist=6, albums_per_artist=3)
    start = time.perf_counter()
    for sort in ("name", "missing", "recent"):
        rows, total = list_artists(jdb, types=ALBUMS, sort=sort, page=10)
        assert total == 1500 and len(rows) == PAGE_SIZE
    list_artists(jdb, types=ALBUMS, q="artist 07")
    list_artists(jdb, types=ALBUMS, only_missing=True, sort="missing")
    assert time.perf_counter() - start < 2.0  # five queries over 1500 artists


# ---------------------------------------------------------------- artist sections


def test_sections_group_owned_and_missing_by_type(jdb):
    artist = make_artist(
        jdb, "Band", mbid="a" * 8 + "-0000-4000-8000-000000000001", status="resolved"
    )
    make_rg(jdb, artist, "Studio One", 1990)
    make_rg(jdb, artist, "Studio Two", 1992)
    make_rg(jdb, artist, "The EP", 1993, "EP")
    make_rg(jdb, artist, "The Single", 1994, "Single")
    make_rg(jdb, artist, "Live Show", 1995, "Album", ("Live",))
    make_rg(jdb, artist, "Best Of", 1999, "Album", ("Compilation",))
    make_album(jdb, artist, "Studio One", year=1990)
    make_album(jdb, artist, "Live Show", year=1995)
    make_album(jdb, artist, "Home Recording", year=1991, path="x/home")
    match_artist_albums(jdb, artist.id)
    jdb.commit()

    sections = build_artist_sections(jdb, artist.id, ["album", "ep"])
    assert [(c, [i.album.title for i in items]) for c, items in sections.owned] == [
        ("album", ["Studio One"]),
        ("live", ["Live Show"]),
        (UNMATCHED, ["Home Recording"]),
    ]
    assert [(c, [g.title for g in items]) for c, items in sections.missing] == [
        ("album", ["Studio Two"]),
        ("ep", ["The EP"]),
    ]
    assert (sections.owned_total, sections.missing_total, sections.hidden_missing) == (3, 2, 2)


def test_show_all_lists_every_category(jdb):
    artist = make_artist(jdb, "Band")
    make_rg(jdb, artist, "Studio", 1990)
    make_rg(jdb, artist, "Single", 1991, "Single")
    make_rg(jdb, artist, "Soundtrack", 1992, "Album", ("Soundtrack",))
    wide = build_artist_sections(jdb, artist.id, ["album"], show_all=True)
    assert [c for c, _ in wide.missing] == [
        "album",
        "single",
        "soundtrack",
    ] and wide.hidden_missing == 0
    narrow = build_artist_sections(jdb, artist.id, ["album"])
    assert [c for c, _ in narrow.missing] == ["album"] and narrow.hidden_missing == 2


def test_missing_albums_are_sorted_by_year_then_title(jdb):
    artist = make_artist(jdb, "Band")
    for title, year in (("b", 2000), ("A", 2000), ("z", 1990), ("Undated", None)):
        make_rg(jdb, artist, title, year)
    [(_, groups)] = build_artist_sections(jdb, artist.id, ["album"]).missing
    assert [g.title for g in groups] == ["z", "A", "b", "Undated"]


def test_tentative_matches_are_shown_as_owned_and_none_as_unmatched(jdb):
    artist = make_artist(jdb, "Band")
    make_rg(jdb, artist, "Same", 1990)
    make_rg(jdb, artist, "Same", 1991, mbid="b" * 8 + "-0000-4000-8000-000000000000")
    make_album(jdb, artist, "Same")  # no year: tentative
    declined = make_album(jdb, artist, "Declined", path="x/d")
    match_artist_albums(jdb, artist.id)
    declined.match_status, declined.release_group_id = "none", None
    jdb.commit()
    sections = build_artist_sections(jdb, artist.id, ["album"])
    owned = dict(sections.owned)
    assert (
        owned["album"][0].album.match_status == "tentative" and owned["album"][0].group is not None
    )
    assert [i.album.title for i in owned[UNMATCHED]] == ["Declined"]
    assert sections.missing_total == 1  # the other "Same"


def test_covers_pending_only_counts_what_is_visible(jdb):
    artist = make_artist(jdb, "Band")
    shown = make_rg(jdb, artist, "Shown", 1990)
    make_rg(jdb, artist, "Hidden Single", 1991, "Single")  # type not selected
    shown.cover_status = "ok"
    jdb.commit()
    assert build_artist_sections(jdb, artist.id, ["album"]).covers_pending is False
    assert build_artist_sections(jdb, artist.id, ["album", "single"]).covers_pending is True


def test_an_artist_without_anything(jdb):
    artist = make_artist(jdb, "Empty")
    sections = build_artist_sections(jdb, artist.id, ["album"])
    assert sections.owned == [] and sections.missing == [] and not sections.covers_pending


@pytest.mark.parametrize(
    ("codec", "tier", "bitrate", "expected"),
    [
        ("flac", "flac_standard", 900, "FLAC"),
        ("flac", "flac_master", 2000, "FLAC"),
        ("mp3", "mp3_320", 320, "MP3 · 320 kbps"),
        ("aac", "any", 128, "AAC · 128 kbps"),
        ("mp3", "any", None, "MP3"),
        (None, "any", None, ""),
    ],
)
def test_describe_format(codec, tier, bitrate, expected):
    album = LibraryAlbum(format=codec, quality_tier=tier, bitrate=bitrate)
    assert describe_format(album) == expected


def test_nobody_has_requested_anything_yet(jdb):
    user = User(username="ann", password_hash="x", role="user")
    jdb.add(user)
    jdb.commit()
    assert request_statuses(jdb, user.id, [1, 2, 3]) == {}
