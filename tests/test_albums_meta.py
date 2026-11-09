"""The "metadata" filter of the Albums page and the check against MusicBrainz that feeds it."""

import re

import pytest
import respx

from app.jobs.handlers.check_tracklists import groups_to_check
from app.models import Job, TracklistFetch
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


@pytest.fixture
def settings(settings):
    return settings.model_copy(
        update={"mb_user_agent": UA, "musicbrainz_url": MB, "job_poll_interval": 0.02}
    )


def titles(client, query):
    html = client.get(f"/albums{query}").text
    return sorted(
        re.findall(
            r'data-album="\d+".*?class="truncate font-medium[^"]*" title="([^"]*)"', html, re.S
        )
    )


@pytest.fixture
def library(db):
    """Full (10 of 10), Short (8 of 10), Long (12 of 10), Unknown (matched, never checked),
    Loose (not matched, no year, no ids)."""
    artist = make_artist(db, "Aretha Franklin", mbid=ARETHA)
    rows = {}
    for name, files, hint in (
        ("Full", 10, 10),
        ("Short", 8, 10),
        ("Long", 12, 10),
        ("Unknown", 9, None),
    ):
        album = make_album(db, artist, name, 1968)
        group = make_rg(db, artist, name, 1968)
        group.track_count_hint, group.cover_status = hint, "ok"
        album.release_group_id, album.match_status = group.id, "matched"
        album.track_count, album.mbid_release = files, "r-" + name
        rows[name] = album
    loose = make_album(db, artist, "Loose")
    loose.track_count = 3
    rows["Loose"] = loose
    db.commit()
    return rows


def test_each_filter_lists_the_albums_it_names(admin_client, library, db):
    assert titles(admin_client, "?meta=incomplete") == ["Short"]
    assert titles(admin_client, "?meta=extra") == ["Long"]
    assert titles(admin_client, "?meta=unchecked") == ["Unknown"]
    assert titles(admin_client, "?meta=no_match") == ["Loose"]
    assert titles(admin_client, "?meta=no_year") == ["Loose"]
    assert titles(admin_client, "?meta=no_mbid") == ["Loose"]
    assert titles(admin_client, "?meta=no_cover") == []
    library["Full"].match_status = "tentative"
    db.commit()
    assert titles(admin_client, "?meta=tentative") == ["Full"]


def test_an_unknown_filter_is_ignored_and_the_menu_keeps_the_choice(admin_client, library):
    assert len(titles(admin_client, "?meta=nonsense")) == 5
    html = admin_client.get("/albums?meta=incomplete").text
    select = re.search(r'<select id="meta".*?</select>', html, re.S).group(0)
    assert 'value="incomplete" selected' in select and select.count("<option") == 9
    assert "Clear filters" in html


def test_the_check_is_queued_by_an_admin_only_and_once(admin_client, user_client, library, db):
    assert "data-check" in admin_client.get("/albums").text
    assert "data-check" not in user_client.get("/albums").text
    assert post(user_client, "/albums/check", page="/albums").status_code == 403
    r = post(admin_client, "/albums/check", page="/albums")
    assert r.status_code == 303 and r.headers["location"] == "/albums?flash=check_queued"
    post(admin_client, "/albums/check", page="/albums")
    assert len(jobs(db, "check_tracklists")) == 1
    assert "data-checking" in admin_client.get("/albums").text


def test_the_check_needs_musicbrainz_to_be_configured(app, admin_client, library, db):
    app.state.settings.mb_user_agent = ""
    r = post(admin_client, "/albums/check", page="/albums")
    assert r.headers["location"] == "/albums?flash=check_no_mb" and not jobs(db, "check_tracklists")


def test_the_job_reads_the_track_count_of_albums_that_were_never_checked(
    app, admin_client, library, db
):
    todo = groups_to_check(db)
    assert len(todo) == 4  # the matched ones; "Loose" has no release group
    post(admin_client, "/albums/check", page="/albums")
    with respx.mock(assert_all_called=False) as mock:
        mount_releases(mock)  # ten tracks, whatever the album
        drain(app)
    db.expire_all()
    job = jobs(db, "check_tracklists")[0]
    assert job.status == "succeeded"
    assert db.query(TracklistFetch).count() == 4 and groups_to_check(db) == []
    # "Unknown" now has 10 tracks to compare with its 9 files
    assert titles(admin_client, "?meta=unchecked") == []
    assert titles(admin_client, "?meta=incomplete") == ["Short", "Unknown"]
    assert job.type == "check_tracklists" and isinstance(job, Job)
