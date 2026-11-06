import pytest
from sqlalchemy import select

from app.models import (
    MATCH_MANUAL,
    MATCH_MATCHED,
    MATCH_NONE,
    MATCH_TENTATIVE,
    MATCH_UNMATCHED,
    AlbumMatchOverride,
    LibraryAlbum,
    ReleaseGroup,
)
from app.services.musicbrainz.matching import (
    candidates_for_review,
    decide,
    match_artist_albums,
    rank_candidates,
    status_counts,
)
from app.services.musicbrainz.titles import album_key
from tests.dbhelpers import load_discography, make_album, make_artist, make_rg
from tests.mbdata import ARETHA, LADY_SOUL_RG


@pytest.fixture
def aretha(jdb):
    artist = make_artist(jdb, mbid=ARETHA, status="resolved")
    load_discography(jdb, artist, "rg_browse_aretha_p0.json", "rg_browse_aretha_p1.json")
    return artist


def match(db, artist, title, **kw):
    album = make_album(db, artist, title, **kw)
    match_artist_albums(db, artist.id)
    db.commit()
    db.refresh(album)
    return album


def rg_of(db, album):
    return db.get(ReleaseGroup, album.release_group_id) if album.release_group_id else None


def test_exact_title_and_year_is_a_confident_match(jdb, aretha):
    album = match(jdb, aretha, "Lady Soul", year=1968)
    assert album.match_status == MATCH_MATCHED and album.matched_confidence >= 0.95
    assert rg_of(jdb, album).mbid == LADY_SOUL_RG


@pytest.mark.parametrize(
    "title",
    [
        "Lady Soul (Deluxe Edition)",
        "Lady Soul [Remastered 2011]",
        "lady soul",
        "LADY SOUL - Expanded Edition",
    ],
)
def test_editions_and_remasters_land_on_the_same_release_group(jdb, aretha, title):
    album = match(jdb, aretha, title, year=2011)  # a remaster carries a later year
    assert album.match_status == MATCH_MATCHED
    assert rg_of(jdb, album).mbid == LADY_SOUL_RG


def test_year_picks_between_albums_with_the_same_title(jdb, aretha):
    for year, expected in ((1961, "1961-02-27"), (1980, "1980-09-30"), (1986, "1986-11-03")):
        album = match(jdb, aretha, "Aretha", year=year, path=f"x/{year}")
        assert album.match_status == MATCH_MATCHED
        assert rg_of(jdb, album).first_release_date == expected


def test_same_title_without_a_year_is_only_tentative(jdb, aretha):
    album = match(jdb, aretha, "Aretha")
    assert album.match_status == MATCH_TENTATIVE and album.release_group_id is not None


def test_unknown_album_is_unmatched(jdb, aretha):
    album = match(jdb, aretha, "A Totally Different Record", year=1999)
    assert (album.match_status, album.release_group_id) == (MATCH_UNMATCHED, None)


def test_a_similar_but_not_identical_title_is_tentative_not_confident(jdb):
    artist = make_artist(jdb, "Daft Punk")
    make_rg(jdb, artist, "Discoveries", 2001)
    album = match(jdb, artist, "Discovery", year=2001)
    assert album.match_status in (MATCH_TENTATIVE, MATCH_UNMATCHED)
    assert album.match_status != MATCH_MATCHED


def test_compilation_flag_prefers_compilation_release_groups(jdb):
    artist = make_artist(jdb, "Someone")
    plain = make_rg(jdb, artist, "Greatest Hits", 2000, "Album", ())
    comp = make_rg(jdb, artist, "Greatest Hits", 2000, "Album", ("Compilation",))
    album = match(jdb, artist, "Greatest Hits", year=2000, compilation=True)
    assert album.release_group_id == comp.id
    other = match(jdb, artist, "Greatest Hits", year=2000, compilation=False, path="x/2")
    assert other.release_group_id == plain.id


def test_release_group_tag_wins_immediately(jdb, aretha):
    album = match(jdb, aretha, "Some Weird Local Title", mbid_rg=LADY_SOUL_RG)
    assert (album.match_status, album.matched_confidence) == (MATCH_MATCHED, 1.0)
    assert rg_of(jdb, album).mbid == LADY_SOUL_RG


def test_a_tag_pointing_outside_the_discography_falls_back_to_the_title(jdb, aretha):
    album = match(
        jdb, aretha, "Lady Soul", year=1968, mbid_rg="00000000-0000-4000-8000-000000000000"
    )
    assert rg_of(jdb, album).mbid == LADY_SOUL_RG


def test_two_owned_editions_may_share_a_release_group(jdb, aretha):
    a = match(jdb, aretha, "Lady Soul", year=1968)
    b = match(jdb, aretha, "Lady Soul (Deluxe Edition)", year=2011, path="x/deluxe")
    assert a.release_group_id == b.release_group_id


def test_manual_override_replaces_the_automatic_choice(jdb, aretha):
    target = jdb.scalar(select(ReleaseGroup).where(ReleaseGroup.title == "Aretha Now"))
    album = make_album(jdb, aretha, "Lady Soul", year=1968)
    jdb.add(
        AlbumMatchOverride(
            artist_id=aretha.id, album_key=album_key("Lady Soul"), release_group_id=target.id
        )
    )
    jdb.commit()
    match_artist_albums(jdb, aretha.id)
    jdb.commit()
    jdb.refresh(album)
    assert (album.match_status, album.release_group_id, album.matched_confidence) == (
        MATCH_MANUAL,
        target.id,
        1.0,
    )


def test_override_none_means_not_on_musicbrainz(jdb, aretha):
    album = make_album(jdb, aretha, "Lady Soul", year=1968)
    jdb.add(
        AlbumMatchOverride(
            artist_id=aretha.id, album_key=album_key("Lady Soul"), release_group_id=None
        )
    )
    jdb.commit()
    match_artist_albums(jdb, aretha.id)
    jdb.commit()
    jdb.refresh(album)
    assert (album.match_status, album.release_group_id) == (MATCH_NONE, None)


def test_override_survives_the_album_being_recreated(jdb, aretha):
    """A rescan that recreates the album row must not lose the admin's decision."""
    target = jdb.scalar(select(ReleaseGroup).where(ReleaseGroup.title == "Spirit in the Dark"))
    old = make_album(jdb, aretha, "My Own Title For It")
    jdb.add(
        AlbumMatchOverride(
            artist_id=aretha.id,
            album_key=album_key("My Own Title For It"),
            release_group_id=target.id,
        )
    )
    jdb.commit()
    jdb.delete(old)
    jdb.commit()
    fresh = match(jdb, aretha, "My Own Title For It (Deluxe)", path="elsewhere")  # same key
    assert (fresh.match_status, fresh.release_group_id) == (MATCH_MANUAL, target.id)


def test_override_pointing_to_a_vanished_release_group_is_ignored(jdb, aretha):
    other = make_artist(jdb, "Other", mbid="11111111-1111-4111-8111-111111111111")
    stray = make_rg(jdb, other, "Elsewhere", 2000)
    album = make_album(jdb, aretha, "Lady Soul", year=1968)
    jdb.add(
        AlbumMatchOverride(
            artist_id=aretha.id, album_key=album_key("Lady Soul"), release_group_id=stray.id
        )
    )
    jdb.commit()
    match_artist_albums(jdb, aretha.id)
    jdb.commit()
    jdb.refresh(album)
    assert album.match_status == MATCH_MATCHED  # falls back to the automatic match


def test_release_groups_of_other_artists_are_never_candidates(jdb, aretha):
    other = make_artist(jdb, "Someone Else")
    make_rg(jdb, other, "Quantum Spreadsheet Symphony", 2020)
    album = match(jdb, aretha, "Quantum Spreadsheet Symphony", year=2020)
    assert album.match_status == MATCH_UNMATCHED


def test_rematching_is_idempotent_and_counts_statuses(jdb, aretha):
    for title, year in (("Lady Soul", 1968), ("Aretha Now", 1968), ("Nope Nope", 2000)):
        make_album(jdb, aretha, title, year=year)
    first = match_artist_albums(jdb, aretha.id)
    jdb.commit()
    second = match_artist_albums(jdb, aretha.id)
    assert (first.matched, first.unmatched) == (second.matched, second.unmatched) == (2, 1)
    counts = status_counts(jdb, aretha.id)
    assert counts[MATCH_MATCHED] == 2 and counts[MATCH_UNMATCHED] == 1


def test_albums_of_an_artist_without_release_groups_stay_unmatched(jdb):
    artist = make_artist(jdb, "Obscure Band")
    album = match(jdb, artist, "Demo Tape")
    assert album.match_status == MATCH_UNMATCHED


def test_candidates_for_review_are_the_closest_titles(jdb, aretha):
    album = make_album(jdb, aretha, "Lady Soul", year=1968)
    top = candidates_for_review(jdb, album, limit=3)
    assert top[0][0].mbid == LADY_SOUL_RG and top[0][1] > top[1][1]
    assert len(top) == 3


def test_decide_edge_cases():
    assert decide([]) == (None, MATCH_UNMATCHED)
    from app.services.musicbrainz.matching import Candidate

    sure = Candidate(1, 0.95, 100)
    rival = Candidate(2, 0.94, 100)
    assert decide([sure])[1] == MATCH_MATCHED
    assert decide([sure, rival])[1] == MATCH_TENTATIVE  # too close to call
    assert decide([sure, Candidate(3, 0.70, 95)])[1] == MATCH_MATCHED
    assert decide([Candidate(1, 0.90, 85)])[1] == MATCH_TENTATIVE  # confident score, vague title
    assert decide([Candidate(1, 0.55, 80)]) == (None, MATCH_UNMATCHED)


def test_library_album_rows_are_untouched_apart_from_the_match_fields(jdb, aretha):
    album = match(jdb, aretha, "Lady Soul", year=1968)
    assert album.title == "Lady Soul" and album.year == 1968 and album.track_count == 10
    assert jdb.scalar(select(LibraryAlbum).where(LibraryAlbum.id == album.id)) is album


def test_rank_candidates_is_deterministic(jdb, aretha):
    album = make_album(jdb, aretha, "Aretha")
    groups = list(jdb.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == aretha.id)))
    first = [c.release_group_id for c in rank_candidates(album, groups)]
    assert first == [c.release_group_id for c in rank_candidates(album, list(reversed(groups)))]


def test_an_album_and_a_single_with_the_same_name_are_told_apart_by_size(jdb):
    """'Title' the album and 'Title' the single, both from the same year (a very common case)."""
    artist = make_artist(jdb, "Pop Band")
    album_rg = make_rg(jdb, artist, "Hit Song", 1990, "Album")
    single_rg = make_rg(jdb, artist, "Hit Song", 1990, "Single")
    full = make_album(jdb, artist, "Hit Song", year=1990, path="x/full")
    full.track_count = 11
    two_tracks = make_album(jdb, artist, "Hit Song", year=1990, path="x/single")
    two_tracks.track_count = 2
    jdb.commit()
    match_artist_albums(jdb, artist.id)
    jdb.commit()
    jdb.refresh(full)
    jdb.refresh(two_tracks)
    assert (full.release_group_id, full.match_status) == (album_rg.id, MATCH_MATCHED)
    assert (two_tracks.release_group_id, two_tracks.match_status) == (single_rg.id, MATCH_MATCHED)


def test_without_a_track_count_the_same_name_stays_ambiguous(jdb):
    artist = make_artist(jdb, "Pop Band")
    make_rg(jdb, artist, "Hit Song", 1990, "Album")
    make_rg(jdb, artist, "Hit Song", 1990, "Single")
    unknown = make_album(jdb, artist, "Hit Song", year=1990)
    unknown.track_count = 0
    jdb.commit()
    match_artist_albums(jdb, artist.id)
    jdb.commit()
    jdb.refresh(unknown)
    assert unknown.match_status == MATCH_TENTATIVE


@pytest.mark.parametrize(
    ("tracks", "expected"),
    [
        (None, None),
        (0, None),
        (1, "single"),
        (3, "single"),
        (4, "ep"),
        (6, "ep"),
        (7, "album"),
        (20, "album"),
    ],
)
def test_size_class(tracks, expected):
    from app.services.musicbrainz.matching import size_class

    assert size_class(tracks) == expected
