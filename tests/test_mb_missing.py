import pytest
from sqlalchemy import select

from app.models import ReleaseGroup, User
from app.services.musicbrainz.categories import CATEGORIES, DEFAULT_MISSING_TYPES
from app.services.musicbrainz.matching import match_artist_albums
from app.services.musicbrainz.missing import (
    clear_user_missing_types,
    get_missing_types,
    has_user_override,
    missing_counts,
    missing_release_groups,
    owned_release_group_ids,
    set_missing_types,
)
from tests.dbhelpers import load_discography, make_album, make_artist, make_rg
from tests.mbdata import ARETHA


@pytest.fixture
def studio(jdb):
    """An artist with a small, hand-made discography."""
    artist = make_artist(jdb, "Band", mbid=ARETHA, status="resolved")
    rgs = {
        "first": make_rg(jdb, artist, "First", 1990),
        "second": make_rg(jdb, artist, "Second", 1992),
        "ep": make_rg(jdb, artist, "Little EP", 1993, "EP"),
        "single": make_rg(jdb, artist, "Hit Single", 1994, "Single"),
        "live": make_rg(jdb, artist, "Live in Town", 1995, "Album", ("Live",)),
        "comp": make_rg(jdb, artist, "Best Of", 1999, "Album", ("Compilation",)),
        "ost": make_rg(jdb, artist, "The Movie", 2000, "Album", ("Soundtrack",)),
    }
    return artist, rgs


def titles(groups):
    return [g.title for g in groups]


def test_default_types_are_studio_albums_and_eps(jdb, studio):
    artist, _ = studio
    assert get_missing_types(jdb) == list(DEFAULT_MISSING_TYPES) == ["album", "ep"]
    assert titles(missing_release_groups(jdb, artist.id, get_missing_types(jdb))) == [
        "First",
        "Second",
        "Little EP",
    ]


def test_owned_albums_are_not_missing(jdb, studio):
    artist, rgs = studio
    make_album(jdb, artist, "First", year=1990)
    match_artist_albums(jdb, artist.id)
    jdb.commit()
    assert rgs["first"].id in owned_release_group_ids(jdb, artist.id)
    assert titles(missing_release_groups(jdb, artist.id, ["album", "ep"])) == [
        "Second",
        "Little EP",
    ]


def test_changing_the_types_changes_the_result_immediately(jdb, studio):
    artist, _ = studio
    assert titles(missing_release_groups(jdb, artist.id, ["album"])) == ["First", "Second"]
    assert titles(missing_release_groups(jdb, artist.id, ["single"])) == ["Hit Single"]
    assert titles(missing_release_groups(jdb, artist.id, ["live", "compilation"])) == [
        "Live in Town",
        "Best Of",
    ]
    assert titles(missing_release_groups(jdb, artist.id, ["soundtrack"])) == ["The Movie"]
    assert missing_release_groups(jdb, artist.id, []) == []
    assert len(missing_release_groups(jdb, artist.id, CATEGORIES)) == 7


def test_missing_albums_are_sorted_by_year_then_title(jdb):
    artist = make_artist(jdb, "Sorted")
    for title, year in (("B", 2000), ("A", 2000), ("Z", 1990), ("Undated", None)):
        make_rg(jdb, artist, title, year)
    assert titles(missing_release_groups(jdb, artist.id, ["album"])) == ["Z", "A", "B", "Undated"]


def test_tentative_matches_count_as_owned_but_unmatched_do_not(jdb):
    artist = make_artist(jdb, "Twin")
    make_rg(jdb, artist, "Same Name", 1990)
    make_rg(jdb, artist, "Same Name", 1991, mbid="bbbbbbbb-0000-4000-8000-000000000000")
    make_album(jdb, artist, "Same Name")  # no year: ambiguous -> tentative
    make_album(jdb, artist, "Not On MB", path="x/2")
    match_artist_albums(jdb, artist.id)
    jdb.commit()
    assert len(owned_release_group_ids(jdb, artist.id)) == 1
    assert len(missing_release_groups(jdb, artist.id, ["album"])) == 1


def test_albums_declared_not_on_musicbrainz_own_nothing(jdb, studio):
    artist, rgs = studio
    from app.models import AlbumMatchOverride
    from app.services.musicbrainz.titles import album_key

    make_album(jdb, artist, "First", year=1990)
    jdb.add(AlbumMatchOverride(artist_id=artist.id, album_key=album_key("First")))
    jdb.commit()
    match_artist_albums(jdb, artist.id)
    jdb.commit()
    assert rgs["first"].id not in owned_release_group_ids(jdb, artist.id)


def test_global_setting_and_validation(jdb):
    set_missing_types(jdb, ["live", "album", "nonsense", "album"])
    jdb.commit()
    assert get_missing_types(jdb) == ["album", "live"]  # canonical order, unknown dropped
    set_missing_types(jdb, [])
    jdb.commit()
    assert get_missing_types(jdb) == []


def test_each_user_can_override_the_global_choice(jdb, studio):
    artist, _ = studio
    ann = User(username="ann", password_hash="x", role="user")
    bob = User(username="bob", password_hash="x", role="user")
    jdb.add_all([ann, bob])
    jdb.commit()
    set_missing_types(jdb, ["album"])  # global
    set_missing_types(jdb, ["single", "live"], user_id=ann.id)
    jdb.commit()
    assert get_missing_types(jdb, ann.id) == ["single", "live"]
    assert get_missing_types(jdb, bob.id) == ["album"]  # inherits the global choice
    assert get_missing_types(jdb) == ["album"]
    assert titles(missing_release_groups(jdb, artist.id, get_missing_types(jdb, ann.id))) == [
        "Hit Single",
        "Live in Town",
    ]
    assert titles(missing_release_groups(jdb, artist.id, get_missing_types(jdb, bob.id))) == [
        "First",
        "Second",
    ]


def test_user_override_can_be_cleared(jdb):
    user = User(username="ann", password_hash="x", role="user")
    jdb.add(user)
    jdb.commit()
    set_missing_types(jdb, ["live"], user_id=user.id)
    jdb.commit()
    assert has_user_override(jdb, user.id)
    clear_user_missing_types(jdb, user.id)
    jdb.commit()
    assert not has_user_override(jdb, user.id) and get_missing_types(jdb, user.id) == [
        "album",
        "ep",
    ]


def test_a_user_may_choose_nothing(jdb):
    user = User(username="ann", password_hash="x", role="user")
    jdb.add(user)
    jdb.commit()
    set_missing_types(jdb, [], user_id=user.id)
    jdb.commit()
    assert get_missing_types(jdb, user.id) == []  # an empty personal list is a choice


def test_missing_counts_for_a_page_of_artists(jdb, studio):
    artist, _ = studio
    other = make_artist(jdb, "Other")
    make_rg(jdb, other, "Only One", 2000)
    assert missing_counts(jdb, [artist.id, other.id], ["album"]) == {artist.id: 2, other.id: 1}
    assert missing_counts(jdb, [artist.id, other.id], []) == {artist.id: 0, other.id: 0}
    assert missing_counts(jdb, [], ["album"]) == {}


def test_realistic_discography_has_a_plausible_missing_list(jdb):
    artist = make_artist(jdb, mbid=ARETHA, status="resolved")
    load_discography(jdb, artist, "rg_browse_aretha_p0.json", "rg_browse_aretha_p1.json")
    for title, year in (("Lady Soul", 1968), ("Spirit in the Dark", 1970), ("Amazing Grace", 1972)):
        make_album(jdb, artist, title, year=year)
    match_artist_albums(jdb, artist.id)
    jdb.commit()
    studio_rgs = list(
        jdb.scalars(
            select(ReleaseGroup).where(
                ReleaseGroup.artist_id == artist.id, ReleaseGroup.category == "album"
            )
        )
    )
    missing = missing_release_groups(jdb, artist.id, ["album"])
    assert 0 < len(missing) < len(studio_rgs)
    assert "Lady Soul" not in titles(missing) and "Spirit in the Dark" not in titles(missing)
    assert "Aretha Arrives" in titles(missing)
    assert all(g.category == "album" for g in missing)
