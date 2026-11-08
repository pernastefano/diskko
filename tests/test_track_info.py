"""The details of a track's file, and the player of an owned album."""

import json
import re

import pytest
from sqlalchemy import select

from app.models import LibraryTrack
from tests.audio import make_track
from tests.dbhelpers import make_album, make_artist, make_rg


@pytest.fixture
def settings(settings, tmp_path):
    return settings.model_copy(update={"music_dir": tmp_path / "music"})


@pytest.fixture
def owned(db, settings):
    """An owned album with a real FLAC, a real MP3, a track Plex knows only by address and a
    file that is not there."""
    artist = make_artist(db, "The Band")
    group = make_rg(db, artist, "Record", 2001)
    album = make_album(db, artist, "Record", year=2001, path="The Band/Record")
    album.release_group_id, album.match_status = group.id, "matched"
    root = settings.music_dir / "The Band/Record"
    make_track(
        root / "01 One.flac",
        title="One",
        artist="The Band",
        album="Record",
        track=1,
        date="2001",
        mbid_release_group="22222222-2222-4222-8222-222222222222",
    )
    make_track(
        root / "02 Two.mp3", title="Two", artist="The Band", album="Record", track=2, _bitrate=320
    )
    rows = []
    for n, (path, title, codec) in enumerate(
        (
            ("The Band/Record/01 One.flac", "One", "flac"),
            ("The Band/Record/02 Two.mp3", "Two", "mp3"),
            ("plex:track:9", "Three", "flac"),
            ("The Band/Record/04 Gone.flac", "Four", "flac"),
        ),
        1,
    ):
        t = LibraryTrack(
            album_id=album.id,
            source="filesystem",
            path=path,
            title=title,
            track_no=n,
            codec=codec,
            lossless=codec == "flac",
            duration=180.0,
            quality_tier="flac_standard",
            file_size=1234,
            bitrate=900,
            sample_rate=44100,
            bit_depth=16,
            artist="The Band",
            album_title="Record",
            year=2001,
        )
        db.add(t)
        rows.append(t)
    db.commit()
    return {"album": album, "group": group, "tracks": rows}


def info(client, track):
    r = client.get(f"/track/{track.id}/info")
    assert r.status_code == 200, r.text
    return r.text


def fields(html):
    return dict(re.findall(r'<dd[^>]*data-field="(\w+)">(.*?)</dd>', html, re.S))


def test_a_flac_shows_the_file_the_audio_and_the_tags(admin_client, owned):
    text = info(admin_client, owned["tracks"][0])
    f = fields(text)
    assert f["filename"] == "01 One.flac" and f["extension"] == "FLAC"
    assert "bytes" in f["size"] and f["modified"].endswith("UTC")
    assert f["codec"] == "FLAC" and f["lossless"] == "Yes"
    assert "44,100 Hz (44.1 kHz)" in f["sample_rate"] and f["bit_depth"].endswith("bit")
    assert f["channels"].startswith("2 (stereo)") and "kbps" in f["bitrate"]
    assert f["title"] == "One" and f["artist"] == "The Band" and f["album"] == "Record"
    assert f["musicbrainz_release_group"] == "22222222-2222-4222-8222-222222222222"
    assert f["cover"] == "none"
    assert f["quality"] and "data-all-tags" in text  # every tag, folded away


def test_an_mp3_shows_its_version_and_bitrate_mode(admin_client, owned):
    f = fields(info(admin_client, owned["tracks"][1]))
    assert f["codec"].startswith("MP3 (MPEG-") and "Layer III" in f["codec"]
    assert f["lossless"] == "No" and f["extension"] == "MP3"
    assert "bitrate" in f and f["sample_rate"]


def test_a_track_without_a_file_shows_what_the_library_stored(admin_client, owned):
    for track, shown in ((owned["tracks"][2], ""), (owned["tracks"][3], "04 Gone.flac")):
        text = info(admin_client, track)
        f = fields(text)
        assert "data-unreadable" in text and "stored when it was scanned" in text
        assert f.get("filename", "") == shown
        assert f["codec"] == track.codec.upper() and f["bitrate"] == "900 kbps"
        assert f["sample_rate"] == "44,100 Hz (44.1 kHz)" and f["bit_depth"] == "16 bit"
        assert f["size"].startswith("1.2 KB")


def test_only_administrators_see_the_folder(admin_client, user_client, owned):
    track = owned["tracks"][0]
    assert "path" in fields(info(admin_client, track))
    assert fields(info(admin_client, track))["path"] == "The Band/Record/01 One.flac"
    assert "path" not in fields(info(user_client, track))
    assert "filename" in fields(info(user_client, track))


def test_the_info_needs_a_login_and_a_real_track(admin_client, new_client, owned):
    assert admin_client.get("/track/999999/info").status_code == 404
    anonymous = new_client()
    assert anonymous.get(f"/track/{owned['tracks'][0].id}/info").status_code == 303


def test_a_path_that_leaves_the_library_shows_nothing_from_outside(
    admin_client, db, owned, settings
):
    (settings.music_dir.parent / "secret.flac").write_bytes(b"secret")
    track = owned["tracks"][3]
    track.path = "../secret.flac"
    db.commit()
    text = info(admin_client, track)
    # nothing was read from the outside file: no modification time, no audio details of it
    assert "data-unreadable" in text and 'data-field="modified"' not in text


# ---------------------------------------------------------------------------- the player


def page(client, group):
    r = client.get(f"/album/{group.id}")
    assert r.status_code == 200
    return r.text


def test_an_owned_album_has_the_player_bar_and_row_buttons_with_icons(admin_client, owned):
    text = page(admin_client, owned["group"])
    for part in (
        "data-player",
        "data-shuffle",
        "data-prev",
        "data-toggle",
        "data-next",
        "data-repeat",
        "data-seek",
        "data-info-modal",
        'id="track-info-body"',
        'x-data="albumPlayer"',
    ):
        assert part in text, part
    assert "/static/player.js" in text and "<audio" not in text  # one player, not one per row
    rows = re.findall(r'<tr data-track="\d+".*?</tr>', text, re.S)
    assert len(rows) == 4
    playable = [
        r
        for r in rows
        if "data-play>" in r or "data-play\n" in r or re.search(r"data-play(?![\w-])", r)
    ]
    assert (
        len(playable) == 3
    )  # the FLAC, the MP3 and the one whose file is gone (it fails when played)
    for row in rows:
        assert re.search(r"data-info(?![\w-])", row)  # everyone has the details button
        buttons = re.findall(
            r"<button[^>]*(?:data-play|data-info)(?![\w-])[^>]*>(.*?)</button>", row, re.S
        )
        for b in buttons:  # icons, not words
            assert "<svg" in b and not re.sub(r"<[^>]+>", "", b).strip()


def test_the_queue_has_the_tracks_that_have_a_playable_file(admin_client, owned):
    text = page(admin_client, owned["group"])
    data = json.loads(re.search(r'id="player-data">(.*?)</script>', text, re.S).group(1))
    assert data["album"] == "Record" and data["artist"] == "The Band"
    assert [(q["row"], q["title"]) for q in data["queue"]] == [(1, "One"), (2, "Two"), (4, "Four")]
    assert data["queue"][0]["url"] == f"/stream/{owned['tracks'][0].id}"
    assert {"play", "pause", "repeat", "repeat_off", "repeat_all", "repeat_one"} <= set(
        data["labels"]
    )


def test_the_player_script_has_shuffle_and_the_three_repeat_modes(client):
    js = client.get("/static/player.js").text
    assert "toggleShuffle" in js and "cycleRepeat" in js
    assert "off: 'all', all: 'one', one: 'off'" in js
    assert (
        "repeat === 'one'" in js and "repeat === 'all'" in js
    )  # what happens at the end of a track


def test_an_album_not_owned_keeps_its_previews_and_has_no_player(admin_client, db):
    artist = make_artist(db, "Other Band")
    group = make_rg(db, artist, "Unowned", 2002)
    text = page(admin_client, group)
    assert "data-player" not in text and "albumPlayer" not in text


def test_a_local_album_has_the_player_too(admin_client, db, settings):
    artist = make_artist(db, "Home Band")
    album = make_album(db, artist, "Home Recording", 2001, path="Home Band/Home Recording")
    make_track(
        settings.music_dir / "Home Band/Home Recording/01.flac",
        title="A",
        artist="Home Band",
        album="Home Recording",
        track=1,
    )
    t = LibraryTrack(
        album_id=album.id,
        source="filesystem",
        path="Home Band/Home Recording/01.flac",
        title="A",
        track_no=1,
        codec="flac",
        duration=1.0,
        quality_tier="flac_standard",
    )
    db.add(t)
    db.commit()
    text = admin_client.get(f"/album/local/{album.id}").text
    assert "data-player" in text and f'hx-get="/track/{t.id}/info"' in text
    assert db.scalars(select(LibraryTrack)).all() == [t]
