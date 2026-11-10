"""Giving an album to the right artist by hand, and keeping it across scans."""

import pytest
from sqlalchemy import select

from app.models import (
    AlbumArtistOverride,
    AlbumMatchOverride,
    Artist,
    AuditLog,
    LibraryAlbum,
    utcnow,
)
from app.services import album_artist as svc
from app.services.library.filesystem import FilesystemProvider
from app.services.library.provider import RecordingReporter
from app.services.library.scan import run_scan
from tests.audio import make_track
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg

MBID_A = "a" * 8 + "-0000-4000-8000-00000000000a"
MBID_B = "b" * 8 + "-0000-4000-8000-00000000000b"


@pytest.fixture
def namesakes(db):
    """Two artists called Nirvana; the album is under the wrong one (the US band)."""
    wrong = make_artist(db, "Nirvana", mbid=MBID_A, status="resolved")
    right = make_artist(db, "Nirvana", mbid=MBID_B, status="resolved")
    for a in (wrong, right):
        a.metadata_synced_at = utcnow()
    make_rg(db, wrong, "Nevermind", 1991)
    target_rg = make_rg(db, right, "Sheep", 1992)
    album = make_album(db, wrong, "Sheep", year=1992, path="Nirvana/Sheep")
    db.commit()
    return {"wrong": wrong, "right": right, "album": album, "rg": target_rg}


def test_an_album_is_given_to_the_right_namesake_and_matched_there(db, namesakes, admin_client):
    from app.models import User

    admin = db.scalar(select(User))
    album, right, wrong = namesakes["album"], namesakes["right"], namesakes["wrong"]
    svc.reassign(db, album, admin, artist_id=right.id)
    db.commit()
    db.refresh(album)
    assert album.artist_id == right.id
    assert album.release_group_id == namesakes["rg"].id and album.match_status != "unmatched"
    override = db.scalar(select(AlbumArtistOverride))
    assert (override.path, override.title, override.artist_id) == (
        "Nirvana/Sheep",
        "Sheep",
        right.id,
    )
    db.refresh(wrong)
    assert wrong.in_library is False  # nothing of it is left here
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "album.artist"))
    assert "Sheep" in entry.target_label


def test_a_new_artist_can_be_created_even_when_another_has_that_name(db, namesakes, admin_client):
    from app.models import User

    admin = db.scalar(select(User))
    target = svc.reassign(db, namesakes["album"], admin, new_name="  Nirvana   (UK) ")
    db.commit()
    assert (
        target.name == "Nirvana (UK)"
        and target.in_library
        and target.id
        not in (
            namesakes["wrong"].id,
            namesakes["right"].id,
        )
    )
    assert db.get(LibraryAlbum, namesakes["album"].id).artist_id == target.id


def test_bad_choices_are_refused_and_change_nothing(db, namesakes, admin_client):
    from app.models import User

    admin = db.scalar(select(User))
    album = namesakes["album"]
    for kwargs, code in (
        ({"artist_id": 99999}, "artist_invalid"),
        ({"new_name": "   "}, "artist_name_empty"),
        ({"artist_id": namesakes["wrong"].id}, "artist_same"),
    ):
        with pytest.raises(svc.ArtistChoiceError) as exc:
            svc.reassign(db, album, admin, **kwargs)
        assert exc.value.code == code
    assert db.scalar(select(AlbumArtistOverride.id)) is None


def test_the_match_decision_of_the_old_artist_goes_with_the_move(db, namesakes, admin_client):
    from app.models import User

    admin = db.scalar(select(User))
    db.add(
        AlbumMatchOverride(
            artist_id=namesakes["wrong"].id, album_key="sheep", release_group_id=None
        )
    )
    db.commit()
    svc.reassign(db, namesakes["album"], admin, artist_id=namesakes["right"].id)
    db.commit()
    assert db.scalar(select(AlbumMatchOverride.id)) is None


def test_a_search_finds_artists_by_a_part_of_the_name_without_the_current_one(db, namesakes):
    choices = svc.find_artists(db, "nirv", exclude_id=namesakes["wrong"].id)
    assert [c.artist.id for c in choices] == [namesakes["right"].id]
    assert choices[0].albums == 0
    assert svc.find_artists(db, "!!!") == []


# ---------------------------------------------------------------------------- scans


def tag_album(root, artist, album="Sheep"):
    make_track(
        root / f"{artist}/{album}/01.flac",
        artist=artist,
        albumartist=artist,
        album=album,
        title="Song",
        track=1,
    )


def test_a_scan_keeps_an_album_with_the_artist_it_was_given_to(db, session_factory, tmp_path):
    root = tmp_path / "music"
    tag_album(root, "Nirvana")
    wrong = make_artist(db, "Nirvana", mbid=MBID_A, status="resolved")
    right = make_artist(db, "Nirvana", mbid=MBID_B, status="resolved")
    db.add(AlbumArtistOverride(path="Nirvana/Sheep", title="Sheep", artist_id=right.id))
    db.commit()
    for _ in range(2):  # and again: stable
        run_scan(session_factory, FilesystemProvider(root), RecordingReporter())
        db.expire_all()
        [album] = db.scalars(select(LibraryAlbum)).all()
        assert album.artist_id == right.id  # the tags say "Nirvana": the first one would win
    assert db.get(Artist, right.id).in_library and db.query(Artist).count() == 2
    assert wrong.id != right.id


def test_an_album_that_moved_is_matched_again_by_the_scan_not_kept_with_the_old_group(
    db, session_factory, tmp_path
):
    root = tmp_path / "music"
    tag_album(root, "Nirvana")
    wrong = make_artist(db, "Nirvana", mbid=MBID_A, status="resolved")
    wrong.metadata_synced_at = utcnow()
    old_group = make_rg(db, wrong, "Sheep", 1992)
    right = make_artist(db, "Nirvana", mbid=MBID_B, status="resolved")
    right.metadata_synced_at = utcnow()
    new_group = make_rg(db, right, "Sheep", 1992)
    run_scan(session_factory, FilesystemProvider(root), RecordingReporter())
    db.expire_all()
    [album] = db.scalars(select(LibraryAlbum)).all()
    assert album.artist_id == wrong.id and album.release_group_id == old_group.id
    db.add(AlbumArtistOverride(path="Nirvana/Sheep", title="Sheep", artist_id=right.id))
    db.commit()
    run_scan(session_factory, FilesystemProvider(root), RecordingReporter())
    db.expire_all()
    album = db.get(LibraryAlbum, album.id)
    assert album.artist_id == right.id and album.release_group_id == new_group.id


# --------------------------------------------------------------------------- the page


def test_the_artist_section_finds_and_moves(admin_client, user_client, db, namesakes):
    album = namesakes["album"]
    url = f"/admin/albums/{album.id}/match"
    page = admin_client.get(url).text
    assert (
        "data-artist-search" in page
        and 'value="Nirvana"' in page  # the field starts from the artist the album has now
        and "data-artist-choices" not in page
    )
    found = admin_client.get(
        url + "?artist_q=nirv"
    ).text  # no MusicBrainz contact: only the library
    assert 'data-artist-state="not_configured"' in found and "data-artist-reset" in found
    assert f'data-artist="{namesakes["right"].id}"' in found
    assert f'data-artist="{namesakes["wrong"].id}"' not in found  # it is the current one
    r = post(
        admin_client,
        f"{url.replace('/match', '')}/artist",
        {"target": f"local:{namesakes['right'].id}", "artist_q": "nirv"},
        page=url,
    )
    assert r.status_code == 303 and r.headers["location"] == f"{url}?flash=artist_moved"
    db.expire_all()
    assert db.get(LibraryAlbum, album.id).artist_id == namesakes["right"].id
    assert "now belongs to the artist you chose" in admin_client.get(r.headers["location"]).text


def test_the_artist_move_is_for_administrators_and_validates(
    admin_client, user_client, db, namesakes
):
    album = namesakes["album"]
    base = f"/admin/albums/{album.id}"
    assert user_client.get(base + "/match?artist_q=x").status_code == 403
    r = post(user_client, base + "/artist", {"artist_id": "1"}, page="/dashboard")
    assert r.status_code == 403
    r = post(admin_client, base + "/artist", {"artist_id": "99999"}, page=base + "/match")
    assert r.status_code == 400 and "does not exist" in r.text
    r = post(admin_client, base + "/artist", {"new_name": " "}, page=base + "/match")
    assert r.status_code == 400 and "Write the name" in r.text
    db.expire_all()
    assert db.get(LibraryAlbum, album.id).artist_id == namesakes["wrong"].id
