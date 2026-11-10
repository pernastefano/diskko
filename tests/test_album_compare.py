"""Your album next to a MusicBrainz release group, on the page that matches it by hand."""

from types import SimpleNamespace

import pytest
import respx

from app.models import LibraryTrack
from app.services import album_compare as cmp
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.mbdata import ARETHA
from tests.test_discovery_flow import (
    MB,
    UA,
    drain,
    jobs,
    mount_releases,
    no_waiting,  # noqa: F401  (autouse fixture)
)

TITLES = [
    "Chain of Fools",
    "Money Won’t Change You",
    "People Get Ready",
    "Niki Hoeky",
    "(You Make Me Feel Like) A Natural Woman",
    "Sweet Sweet Baby (Since You’ve Been Gone)",
    "Good to Me as I Am to You",
    "Come Back Baby",
    "Groovin’",
    "Ain’t No Way",
]


@pytest.fixture
def settings(settings):
    return settings.model_copy(
        update={"mb_user_agent": UA, "musicbrainz_url": MB, "job_poll_interval": 0.02}
    )


def add_tracks(db, album, titles, start=1):
    for n, title in enumerate(titles, start=start):
        db.add(
            LibraryTrack(
                album_id=album.id,
                source="filesystem",
                path=f"{album.path}/{n:02d}.flac",
                title=title,
                track_no=n,
                disc_no=1,
                duration=None,
            )
        )
    db.commit()


@pytest.fixture
def world(db):
    """Nine of the ten tracks of Lady Soul (one with a title MusicBrainz spells differently), and
    one file MusicBrainz does not list."""
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
    album = make_album(db, artist, "Lady Soul (Deluxe)", year=2013)
    group = make_rg(db, artist, "Lady Soul", 1968)
    local = [*TITLES[:9], "Bonus Jam"]
    local[1] = "Money Wont Change You"
    add_tracks(db, album, local)
    album.track_count = 10
    db.commit()
    return {"artist": artist, "album": album, "group": group}


def compare_url(world, group=None):
    return f"/admin/albums/{world['album'].id}/match/compare"


def start(client, world, group=None, page=None):
    group = group or world["group"]
    return post(
        client,
        compare_url(world),
        {"rg": str(group.id)},
        page=page or f"/admin/albums/{world['album'].id}/match",
    )


# ---------------------------------------------------------------------- the pairing


def track(i, title, no, disc=1, ms=None):
    return SimpleNamespace(
        id=i,
        title=title,
        track_no=no,
        disc_no=disc,
        position=no,
        disc=disc,
        duration=ms,
        length_ms=ms,
    )


def test_tracks_are_paired_by_title_and_what_is_left_is_shown_on_each_side():
    local = [track(1, "Alpha", 1), track(2, "Beta (feat. Guest)", 2), track(3, "Stray", 3)]
    remote = [track(11, "Alpha", 1), track(12, "Beta", 2), track(13, "Omega", 3)]
    pairs = {(a.id, b.id) for a, b in cmp.pair(local, remote)}
    assert pairs == {(1, 11), (2, 12)}  # the guest credit does not hide the title
    assert cmp.pair([], remote) == []


def test_a_title_in_another_place_is_paired_and_flagged():
    local = [track(1, "Beta", 1), track(2, "Alpha", 2)]
    remote = [track(11, "Alpha", 1), track(12, "Beta", 2)]
    album = SimpleNamespace(title="X", year=2000, track_count=2)
    group = SimpleNamespace(title="X", year=2000)
    result = cmp.compare(album, local, group, remote)
    assert [r.state for r in result.rows] == ["differs", "differs"]
    assert all(r.reasons == ["place"] for r in result.rows) and result.verdict == "close"


# ---------------------------------------------------------------------- the page


def test_the_current_match_can_be_compared_from_the_page(admin_client, world, db):
    world["album"].release_group_id = world["group"].id
    world["album"].match_status = "manual"
    db.commit()
    html = admin_client.get(f"/admin/albums/{world['album'].id}/match").text
    assert "data-current-compare" in html and f'"rg": "{world["group"].id}"' in html


def test_comparing_reads_the_track_list_with_a_job_then_shows_both_sides(
    app, admin_client, world, db
):
    r = start(admin_client, world)
    assert r.status_code == 200 and 'data-compare-state="loading"' in r.text
    start(admin_client, world)  # asking again does not queue it again
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock)
        drain(app)
    assert len(jobs(db, "fetch_tracklist")) == 1
    html = admin_client.get(f"{compare_url(world)}?rg={world['group'].id}").text
    assert 'data-compare-state="ready"' in html and 'data-verdict="close"' in html
    assert "7 of 10 MusicBrainz tracks agree with your 10 file(s)." in html
    assert 'data-fact="title" data-same="yes"' in html  # an edition note does not make it another
    assert (
        'data-fact="year" data-same="no"' in html and 'data-fact="tracks" data-same="yes"' in html
    )
    assert html.count('data-row="same"') == 7
    # the apostrophe, and a track MusicBrainz names the other way round
    assert html.count('data-row="differs"') == 2 and html.count("different title") == 2
    assert 'data-row="local_only"' in html and "Bonus Jam" in html and "not on this release" in html
    assert 'data-row="remote_only"' in html and "Ain’t No Way" in html and "no file" in html


def test_a_track_of_another_length_is_flagged(app, admin_client, world, db):
    track = db.query(LibraryTrack).filter_by(title="Chain of Fools").one()
    track.duration = 167.8 + 40  # a different cut
    db.commit()
    start(admin_client, world)
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock)
        drain(app)
    html = admin_client.get(f"{compare_url(world)}?rg={world['group'].id}").text
    assert html.count("different length") == 1 and html.count('data-row="differs"') == 3


def test_reading_the_comparison_never_queues_anything(admin_client, world, db):
    html = admin_client.get(f"{compare_url(world)}?rg={world['group'].id}").text
    assert 'data-compare-state="idle"' in html and not jobs(db, "fetch_tracklist")


def test_a_group_of_another_artist_cannot_be_compared(admin_client, world, db):
    other = make_artist(db, "Someone Else")
    stranger = make_rg(db, other, "Other Album", 1999)
    assert admin_client.get(f"{compare_url(world)}?rg={stranger.id}").status_code == 404
    assert start(admin_client, world, stranger).status_code == 404
    assert admin_client.get(f"{compare_url(world)}?rg=x").status_code == 404


def test_an_album_of_a_fallback_provider_has_no_track_list(admin_client, world, db):
    world["group"].mbid = "deezer:123"
    db.commit()
    assert 'data-compare-state="unavailable"' in start(admin_client, world).text
    assert not jobs(db, "fetch_tracklist")


def test_only_an_admin_can_compare(user_client, world):
    assert user_client.get(f"{compare_url(world)}?rg={world['group'].id}").status_code == 403
    assert start(user_client, world, page="/dashboard").status_code == 403
