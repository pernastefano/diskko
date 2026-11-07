from app.quality import DEFAULT_PROFILES
from app.services.library.grouping import (
    UNKNOWN_ALBUM,
    UNKNOWN_ARTIST,
    VARIOUS_ARTISTS,
    album_dir_of,
    name_key,
    sort_name_for,
)
from app.services.library.grouping import (
    group_tracks as _group_tracks,
)
from app.services.library.provider import ScannedTrack


def group_tracks(tracks):
    return _group_tracks(tracks, DEFAULT_PROFILES)


MBID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MBID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def T(path, **kw):
    kw.setdefault("title", path.rsplit("/", 1)[-1])
    return ScannedTrack(path=path, **kw)


def albums_of(artists):
    return [(a.name, al.title, len(al.tracks)) for a in artists for al in a.albums]


def test_one_album(tmp_path):
    tracks = [
        T(
            f"Aretha/Lady Soul/{i:02}.flac",
            artist="Aretha",
            album_artist="Aretha",
            album_title="Lady Soul",
            track_no=i,
            year=1968,
            codec="flac",
            lossless=True,
            quality_tier="flac_standard",
            file_size=100,
        )
        for i in (3, 1, 2)
    ]
    [artist] = group_tracks(tracks)
    [album] = artist.albums
    assert (artist.name, album.title, album.year, album.format) == (
        "Aretha",
        "Lady Soul",
        1968,
        "flac",
    )
    assert [t.track_no for t in album.tracks] == [1, 2, 3]  # sorted
    assert (album.total_size, album.path, album.quality_tier) == (
        300,
        "Aretha/Lady Soul",
        "flac_standard",
    )


def test_multi_disc_folders_make_one_album():
    tracks = [
        T(
            "Pink Floyd/The Wall/CD1/01.flac",
            album_artist="Pink Floyd",
            album_title="The Wall",
            disc_no=1,
        ),
        T(
            "Pink Floyd/The Wall/CD1/02.flac",
            album_artist="Pink Floyd",
            album_title="The Wall",
            disc_no=1,
        ),
        T(
            "Pink Floyd/The Wall/CD2/01.flac",
            album_artist="Pink Floyd",
            album_title="The Wall",
            disc_no=2,
        ),
    ]
    [artist] = group_tracks(tracks)
    [album] = artist.albums
    assert len(album.tracks) == 3 and album.disc_count == 2
    assert album.path == "Pink Floyd/The Wall"


def test_album_dir_detection():
    assert album_dir_of("A/B/01.flac") == "A/B"
    assert album_dir_of("A/B/CD1/01.flac") == "A/B"
    assert album_dir_of("A/B/Disc 2/01.flac") == "A/B"
    assert album_dir_of("A/B/disk-3/01.flac") == "A/B"
    assert album_dir_of("A/B/CD 1 - Bonus/01.flac") == "A/B"
    assert album_dir_of("A/B/Discography/01.flac") == "A/B/Discography"  # not a disc folder
    assert album_dir_of("01.flac") == ""


def test_compilation_flag_goes_to_various_artists():
    tracks = [
        T(f"Hits/{i}.mp3", artist=f"Band {i}", album_title="Hits 2001", compilation=True)
        for i in range(4)
    ]
    [artist] = group_tracks(tracks)
    assert artist.name == VARIOUS_ARTISTS and artist.albums[0].is_compilation


def test_various_artists_tag_variants():
    for tag in ("Various Artists", "various", "VA", "V.A."):
        tracks = [T("Hits/1.mp3", album_artist=tag, artist="X", album_title="Hits")]
        assert group_tracks(tracks)[0].name == VARIOUS_ARTISTS, tag


def test_two_albums_same_artist_sorted_by_year():
    tracks = [
        T("A/Later/1.flac", album_artist="A", album_title="Later", year=2000),
        T("A/Early/1.flac", album_artist="A", album_title="Early", year=1990),
    ]
    [artist] = group_tracks(tracks)
    assert [a.title for a in artist.albums] == ["Early", "Later"]


def test_same_album_title_different_artists_stay_apart():
    tracks = [
        T("X/Greatest Hits/1.flac", album_artist="X", album_title="Greatest Hits"),
        T("Y/Greatest Hits/1.flac", album_artist="Y", album_title="Greatest Hits"),
    ]
    assert len(group_tracks(tracks)) == 2


def test_two_albums_in_one_folder():
    tracks = [
        T("Singles/1.mp3", album_artist="A", album_title="One"),
        T("Singles/2.mp3", album_artist="A", album_title="Two"),
    ]
    [artist] = group_tracks(tracks)
    assert sorted(a.title for a in artist.albums) == ["One", "Two"]
    assert {a.path for a in artist.albums} == {"Singles"}


def test_untagged_files_fall_back_to_folder_structure():
    [artist] = group_tracks([T("Radiohead/OK Computer/01.wav"), T("Radiohead/OK Computer/02.wav")])
    assert (artist.name, artist.albums[0].title, len(artist.albums[0].tracks)) == (
        "Radiohead",
        "OK Computer",
        2,
    )


def test_untagged_deeper_and_shallower_paths():
    [deep] = group_tracks([T("Rock/Nirvana/Nevermind/1.wav")])
    assert (deep.name, deep.albums[0].title) == ("Nirvana", "Nevermind")
    [one] = group_tracks([T("Some Album/1.wav")])
    assert (one.name, one.albums[0].title) == (UNKNOWN_ARTIST, "Some Album")
    [root] = group_tracks([T("1.wav")])
    assert (root.name, root.albums[0].title) == (UNKNOWN_ARTIST, UNKNOWN_ALBUM)


def test_artist_name_variants_are_one_artist():
    tracks = [
        T("a/1.flac", album_artist="Beyoncé", album_title="One"),
        T("b/1.flac", album_artist="BEYONCE ", album_title="Two"),
        T("c/1.flac", album_artist="AC/DC", album_title="Three"),
        T("d/1.flac", album_artist="AC-DC", album_title="Four"),
    ]
    artists = group_tracks(tracks)
    assert len(artists) == 2 and all(len(a.albums) == 2 for a in artists)


def test_mbid_distinguishes_same_name():
    tracks = [
        T("a/1.flac", album_artist="Nirvana", mbid_album_artist=MBID_A, album_title="X"),
        T("b/1.flac", album_artist="Nirvana", mbid_album_artist=MBID_B, album_title="Y"),
    ]
    assert len(group_tracks(tracks)) == 2


def test_untagged_mbid_albums_join_the_mbid_artist():
    tracks = [
        T("a/1.flac", album_artist="Nirvana", mbid_album_artist=MBID_A, album_title="X"),
        T("b/1.flac", album_artist="nirvana", album_title="Y"),
    ]
    [artist] = group_tracks(tracks)
    assert artist.mbid == MBID_A and len(artist.albums) == 2


def test_album_aggregates():
    tracks = [
        T(
            "A/B/1.flac",
            album_artist="A",
            album_title="B",
            codec="flac",
            quality_tier="flac_hires",
            bitrate=900,
            year=2001,
        ),
        T(
            "A/B/2.mp3",
            album_artist="A",
            album_title="B",
            codec="mp3",
            quality_tier="mp3_256",
            bitrate=256,
            year=2001,
        ),
        T(
            "A/B/3.flac",
            album_artist="A",
            album_title="B",
            codec="flac",
            quality_tier="flac_standard",
            bitrate=800,
            year=2002,
        ),
    ]
    [album] = group_tracks(tracks)[0].albums
    assert album.format == "flac" and album.quality_tier == "mp3_256"  # weakest link
    assert album.bitrate == 652 and album.year == 2001  # average / most common


def test_release_mbids_are_taken_from_tracks():
    tracks = [
        T(
            "A/B/1.flac",
            album_artist="A",
            album_title="B",
            mbid_release=MBID_A,
            mbid_release_group=MBID_B,
        )
    ]
    [album] = group_tracks(tracks)[0].albums
    assert (album.mbid_release, album.mbid_release_group) == (MBID_A, MBID_B)


def test_artist_without_album_artist_uses_most_common_track_artist():
    tracks = [
        T("x/1.mp3", artist="Solo", album_title="Z"),
        T("x/2.mp3", artist="Solo", album_title="Z"),
        T("x/3.mp3", artist="Guest", album_title="Z"),
    ]
    assert group_tracks(tracks)[0].name == "Solo"


def test_sort_name_and_name_key():
    assert sort_name_for("The Beatles") == "Beatles, The"
    assert sort_name_for("Theatre of Tragedy") == "Theatre of Tragedy"
    assert sort_name_for("The") == "The"
    assert name_key("  Björk ") == name_key("BJORK")
    assert name_key("!!!") == "!!!"  # punctuation-only names keep an identity
    assert (
        group_tracks([T("The Who/Tommy/1.flac", album_artist="The Who")])[0].sort_name == "Who, The"
    )


def test_empty_input():
    assert group_tracks([]) == []
