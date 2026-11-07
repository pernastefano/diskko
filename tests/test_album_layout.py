"""The album page: the admin buttons, quality instead of file format, the fixed-width track
table and "title – artist" (compilations), and the top 10 in the same style."""

import json
import re

import respx
from sqlalchemy import select

from app.db import make_engine, run_migrations
from app.integrations.musicbrainz_client import _parse_release
from app.jobs.worker import JobWorker
from app.models import Job, ReleaseGroup, utcnow
from app.services import albums as svc
from app.services.tracklists import load_tracks, store_tracklist
from tests import test_album_page as base
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.mbdata import ARETHA
from tests.test_album_page import MB, add_track, jobs_of
from tests.test_jobs_worker import enqueue, logs, wait_for_status

# the fixtures of the album page tests (an owned album, an album that is not owned)
settings = base.settings
no_waiting = base.no_waiting
owned = base.owned
remote = base.remote


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


# ---------------------------------------------------------------- the admin buttons


def test_admins_get_update_and_match_buttons_on_an_owned_album(admin_client, owned):
    _, group, album, _ = owned
    html = page(admin_client, f"/album/{group.id}")
    assert "data-album-refresh" in html and f'action="/album/{group.id}/refresh"' in html
    assert f'href="/admin/artists/{album.artist_id}#album-{album.id}"' in html
    assert "Update metadata" in html and "Manage match" in html


def test_ordinary_users_get_neither_button(user_client, owned):
    _, group, _, _ = owned
    html = page(user_client, f"/album/{group.id}")
    assert "data-album-admin" not in html and "/refresh" not in html and "Manage match" not in html
    assert (
        post(user_client, f"/album/{group.id}/refresh", page=f"/album/{group.id}").status_code
        == 403
    )


def test_an_album_that_is_not_owned_has_nothing_to_match(admin_client, remote):
    _, group = remote
    html = page(admin_client, f"/album/{group.id}")
    assert "data-album-refresh" in html and "data-album-match" not in html


def test_a_local_album_offers_the_match(admin_client, db):
    artist = make_artist(db, "Obscure Band")
    album = make_album(db, artist, "Homemade")
    html = page(admin_client, f"/album/local/{album.id}")
    assert "data-album-match" in html and "data-album-refresh" not in html  # no release group


def test_the_match_link_lands_on_the_album_row(admin_client, owned):
    _, _, album, _ = owned
    detail = page(admin_client, f"/admin/artists/{album.artist_id}")
    assert f'<tr id="album-{album.id}">' in detail


def test_refreshing_queues_one_job_and_goes_back_to_the_album(admin_client, owned, db):
    _, group, _, _ = owned
    r = post(admin_client, f"/album/{group.id}/refresh", page=f"/album/{group.id}")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/album/{group.id}?flash=")
    post(admin_client, f"/album/{group.id}/refresh", page=f"/album/{group.id}")
    jobs = jobs_of(db, "refresh_album")
    assert len(jobs) == 1 and json.loads(jobs[0].payload_json) == {"release_group_id": group.id}
    assert "being refreshed" in page(admin_client, r.headers["location"])
    assert admin_client.post(f"/album/{group.id}/refresh").status_code == 403  # needs the token


async def test_the_refresh_job_updates_the_details_and_queues_the_rest(
    settings, session_factory, db
):
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
    group = make_rg(db, artist, "Lady Soul", 1968)
    group.title, group.cover_status = "Old title", "missing"
    db.commit()
    s = settings.model_copy(update={"job_poll_interval": 0.02})
    with respx.mock(assert_all_called=False) as mock:
        lookup = mock.get(f"{MB}/release-group/{group.mbid}").respond(
            json={
                "id": group.mbid,
                "title": "Lady Soul (renamed)",
                "primary-type": "Album",
                "secondary-types": ["Live"],
                "first-release-date": "1968-01-22",
                "disambiguation": "",
            }
        )
        worker = JobWorker(s, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "refresh_album", {"release_group_id": group.id})
        worker.notify()
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert lookup.call_count == 1
    with session_factory() as fresh:
        updated = fresh.get(ReleaseGroup, group.id)
        assert (updated.title, updated.category, updated.year, updated.cover_status) == (
            "Lady Soul (renamed)",
            "live",
            1968,
            "unknown",
        )
        children = {j.type: j for j in fresh.scalars(select(Job)) if j.parent_job_id == job_id}
    assert set(children) == {"fetch_tracklist", "resolve_previews", "fetch_covers"}
    assert json.loads(children["fetch_tracklist"].payload_json)["force"] is True
    assert json.loads(children["resolve_previews"].payload_json)["force"] is True
    assert any("details updated" in m for m in logs(session_factory, job_id))


async def test_a_deezer_album_is_refreshed_without_musicbrainz(settings, session_factory, db):
    artist = make_artist(db, "Obscure Band")
    group = make_rg(db, artist, "Third Record", 2005, mbid="deezer:13")
    group.source = "deezer"
    db.commit()
    s = settings.model_copy(update={"job_poll_interval": 0.02})
    with respx.mock(assert_all_called=False) as mock:
        any_mb = mock.get(url__regex=rf"{MB}/.*").respond(500)
        worker = JobWorker(s, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "refresh_album", {"release_group_id": group.id})
        worker.notify()
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert any_mb.call_count == 0
    with session_factory() as fresh:
        kinds = {j.type for j in fresh.scalars(select(Job)) if j.parent_job_id == job_id}
    assert kinds == {"resolve_previews", "fetch_covers"}  # no MusicBrainz track list for it


# ---------------------------------------------------------- quality, not file format


def test_the_header_shows_the_quality_profile_and_not_the_file_format(admin_client, owned):
    _, group, _, _ = owned
    header = page(admin_client, f"/album/{group.id}").split("Tracks", 1)[0]
    assert 'data-quality="flac_standard"' in header and "FLAC Standard" in header
    assert not re.search(r'<span class="text-xs text-slate-500">(FLAC|MP3|M4A)', header)


def test_the_track_table_has_a_quality_column_instead_of_the_format(admin_client, owned):
    _, group, _, _ = owned
    html = page(admin_client, f"/album/{group.id}")
    heads = re.findall(r"<th[^>]*>([^<]*)</th>", html.split("data-track-table", 1)[1])
    assert heads[:5] == ["#", "Title", "Quality", "Length", "Play"]
    assert "Format" not in heads
    cells = re.findall(r'data-quality="flac_standard"', html.split("data-track-table", 1)[1])
    assert len(cells) == 3
    assert not re.search(r">\s*FLAC\s*<", html.split("data-track-table", 1)[1])  # no codec text


def test_the_albums_list_tiles_do_not_show_the_format_either(admin_client, owned):
    html = page(admin_client, "/albums")
    assert 'data-quality="flac_standard"' in html
    assert not re.search(r'<span class="text-xs text-slate-500">(FLAC|MP3)', html)


# ---------------------------------------------------------------- the track table


def test_the_table_has_fixed_columns_and_no_wrapping(admin_client, owned):
    _, group, _, _ = owned
    html = page(admin_client, f"/album/{group.id}")
    table = re.search(r"<table[^>]*data-track-table[^>]*>", html).group(0)
    assert "table-fixed" in table and "whitespace-nowrap" in table
    cols = re.findall(r"<col(?:\s[^>]*)?>", html.split("data-track-table", 1)[1])
    assert cols == [
        '<col class="w-12">',
        "<col>",
        '<col class="w-56">',
        '<col class="w-16">',
        '<col class="w-64">',
    ]
    body = html.split("data-track-table", 1)[1]
    assert 'class="px-3 py-2 text-right tabular-nums text-slate-500"' in body  # the length
    assert 'class="flex justify-end"' in body  # the player sits at the right
    assert 'class="flex min-w-0 gap-1" title="Song 1 – Aretha Franklin"' in body
    assert 'class="min-w-0 truncate font-medium">Song 1</span>' in body  # title and artist
    assert 'class="min-w-0 truncate text-slate-500">–' in body  # shrink one beside the other


def test_a_not_owned_album_has_no_quality_column(admin_client, remote, db):
    _, group = remote
    store_tracklist(db, group.id, release_with_credits(group))
    db.commit()
    html = page(admin_client, f"/album/{group.id}")
    body = html.split("data-track-table", 1)[1]
    assert re.findall(r"<col(?:\s[^>]*)?>", body) == [
        '<col class="w-12">',
        "<col>",
        '<col class="w-16">',
        '<col class="w-64">',
    ]
    assert "Quality" not in re.findall(r"<th[^>]*>([^<]*)</th>", body)


# ------------------------------------------------------------------ title – artist


def release_with_credits(group):
    from app.integrations.musicbrainz_client import ReleaseData, TrackData

    return ReleaseData(
        "11111111-1111-4111-8111-111111111111",
        "1968",
        "US",
        "Official",
        [
            TrackData(1, 1, "1", "Opening", 180000, None, [("Aretha Franklin", ARETHA, "")]),
            TrackData(
                1,
                2,
                "2",
                "Duet",
                200000,
                None,
                [
                    ("Aretha Franklin", ARETHA, " feat. "),
                    ("Some Guest", "b" * 8 + "-0000-4000-8000-000000000009", ""),
                ],
            ),
            TrackData(1, 3, "3", "Nobody Credited", 100000, None),
        ],
    )


def test_each_track_shows_its_artist_with_a_link_when_we_know_them(admin_client, remote, db):
    artist, group = remote
    guest = make_artist(db, "Some Guest", mbid="b" * 8 + "-0000-4000-8000-000000000009")
    store_tracklist(db, group.id, release_with_credits(group))
    db.commit()
    html = page(admin_client, f"/album/{group.id}")
    rows = re.findall(r"<tr data-track=.*?</tr>", html, re.S)
    assert len(rows) == 3
    first = re.search(r"data-track-title>(.*?)</div>", rows[0], re.S).group(1)
    assert "Opening" in first and f'href="/artist/{artist.id}"' in first  # linked by MBID
    duet = re.search(r"data-track-title>(.*?)</div>", rows[1], re.S).group(1)
    assert f'href="/artist/{artist.id}"' in duet and f'href="/artist/{guest.id}"' in duet
    assert " feat. " in duet and "Duet" in duet  # the join text MusicBrainz gives
    assert 'title="Duet – Aretha Franklin feat. Some Guest"' in rows[1]
    assert "Aretha Franklin" in rows[2] and f'href="/artist/{artist.id}"' in rows[2]  # the fallback


def test_an_artist_we_do_not_have_is_plain_text(admin_client, remote, db):
    _, group = remote
    store_tracklist(db, group.id, release_with_credits(group))
    db.commit()
    rows = re.findall(r"<tr data-track=.*?</tr>", page(admin_client, f"/album/{group.id}"), re.S)
    assert "Some Guest" in rows[1] and "data-track-artist>Some Guest" in rows[1]
    assert not re.search(r"<a [^>]*>Some Guest</a>", rows[1])


def test_a_compilation_shows_the_artist_of_every_file(admin_client, db, settings):
    artist = make_artist(db, "Various Artists")
    group = make_rg(db, artist, "Hits", 1999, secondary=("Compilation",))
    album = make_album(db, artist, "Hits", 1999, compilation=True)
    album.release_group_id, album.match_status = group.id, "matched"
    other = make_artist(db, "Singer One")
    for n, name in ((1, "Singer One"), (2, "Singer Two"), (3, None)):
        row = add_track(db, album, n)
        row.artist = name
    db.commit()
    html = page(admin_client, f"/album/{group.id}")
    rows = re.findall(r"<tr data-track=.*?</tr>", html, re.S)
    assert f'href="/artist/{other.id}"' in rows[0] and "Singer One" in rows[0]
    assert "Singer Two" in rows[1] and "<a " not in re.search(
        r"data-track-title>(.*?)</div>", rows[1], re.S
    ).group(1)
    assert "Various Artists" in rows[2]  # no tag: the album's artist


def test_artists_are_linked_by_name_when_there_is_no_mbid(db):
    artist = make_artist(db, "Aretha Franklin")
    other = make_artist(db, "Nina Simone")
    rows = [
        svc.TrackRow(1, "1", "A", artists=[svc.ArtistRef("nina  SIMONE")]),
        svc.TrackRow(1, "2", "B", artists=[svc.ArtistRef("Unknown Person")]),
        svc.TrackRow(1, "3", "C"),
    ]
    svc.attach_artists(db, rows, artist)
    assert [r.artists[0].artist_id for r in rows] == [other.id, None, artist.id]


# ---------------------------------------------------- credits in MusicBrainz data


def test_the_musicbrainz_parser_reads_the_artist_credit_of_each_track():
    raw = {
        "id": "11111111-1111-4111-8111-111111111111",
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "position": 1,
                        "number": "1",
                        "title": "Duet",
                        "length": 1000,
                        "artist-credit": [
                            {
                                "name": "Aretha",
                                "joinphrase": " & ",
                                "artist": {"id": ARETHA, "name": "Aretha Franklin"},
                            },
                            {
                                "name": "Guest",
                                "joinphrase": "",
                                "artist": {"id": "x", "name": "Guest"},
                            },
                        ],
                    },
                    {"position": 2, "number": "2", "title": "No credit"},
                ],
            }
        ],
    }
    tracks = _parse_release(raw).tracks
    assert tracks[0].credits == [("Aretha", ARETHA, " & "), ("Guest", "x", "")]
    assert tracks[1].credits == []


def test_credits_survive_the_database(db, remote):
    _, group = remote
    store_tracklist(db, group.id, release_with_credits(group))
    db.commit()
    stored = load_tracks(db, group.id)
    assert json.loads(stored[1].artist_credit_json)[1]["name"] == "Some Guest"
    assert stored[2].artist_credit_json is None


def test_migration_0015_adds_the_column_and_clears_the_track_list_cache(settings):
    from sqlalchemy import inspect, text

    url = settings.database_url
    run_migrations(url, "0014")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artists (id, name, sort_name, name_key, mb_status, in_library, created_at,"
                " updated_at) VALUES (1, 'A', 'A', 'a', 'pending', 1, '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO release_groups (id, mbid, artist_id, title, secondary_types_json,"
                " disambiguation, category, cover_status, synced_at, source) VALUES"
                " (7, 'm', 1, 'T', '[]', '', 'album', 'unknown', '2026-01-01', 'musicbrainz')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO release_tracks (release_group_id, disc, position, title)"
                " VALUES (7, 1, 1, 'Song')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tracklist_fetches (release_group_id, fetched_at, track_count)"
                " VALUES (7, '2026-11-01', 1)"
            )
        )
    run_migrations(url)
    run_migrations(url)  # idempotent
    assert "artist_credit_json" in {
        c["name"] for c in inspect(engine).get_columns("release_tracks")
    }
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tracklist_fetches")).scalar_one() == 0
        assert conn.execute(text("SELECT title FROM release_tracks")).scalar_one() == "Song"
    engine.dispose()


# ---------------------------------------------------------------- the top 10 list


def test_the_top_tracks_use_the_same_title_artist_style(app, admin_client):
    from app.models import ArtistTopTrack

    with app.state.session_factory() as db:
        artist = make_artist(db, "Radiohead", mbid="a" * 8 + "-0000-4000-8000-000000000001")
        artist.top_checked_at = utcnow()
        db.add(
            ArtistTopTrack(
                artist_id=artist.id,
                position=1,
                title="Creep",
                album_title="Pablo Honey",
                duration_s=238,
                provider="deezer",
                fetched_at=utcnow(),
                preview_url="https://cdnt-preview.dzcdn.net/x.mp3",
                preview_provider="deezer",
            )
        )
        db.commit()
        artist_id = artist.id
    html = page(admin_client, f"/artist/{artist_id}/top")
    assert 'title="Creep – Radiohead"' in html and "data-track-artist>Radiohead" in html
    row = re.search(r"<li [^>]*data-top-track.*?</li>", html, re.S).group(0)
    assert "flex w-64 shrink-0 justify-end" in row and "w-12 shrink-0 text-right" in row
    assert "whitespace-nowrap" in html and "flex-wrap" not in row


async def test_the_refresh_does_not_look_for_a_cover_the_album_already_has(
    settings, session_factory, db
):
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
    group = make_rg(db, artist, "Lady Soul", 1968)
    group.cover_status, group.cover_path = "ok", f"{group.mbid}.jpg"
    db.commit()
    s = settings.model_copy(update={"job_poll_interval": 0.02})
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{MB}/release-group/{group.mbid}").respond(
            json={"id": group.mbid, "title": "Lady Soul", "primary-type": "Album"}
        )
        worker = JobWorker(s, session_factory)
        await worker.start()
        job_id = enqueue(session_factory, "refresh_album", {"release_group_id": group.id})
        worker.notify()
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    with session_factory() as fresh:
        kinds = {j.type for j in fresh.scalars(select(Job)) if j.parent_job_id == job_id}
        assert fresh.get(ReleaseGroup, group.id).cover_status == "ok"
    assert kinds == {"fetch_tracklist", "resolve_previews"}  # no fetch_covers
