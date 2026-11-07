"""Asking for a better copy of an album we already own."""

import json

import pytest
from sqlalchemy import select

from app.models import DownloadRequest, Job
from app.services import downloads as svc
from tests.conftest import post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.fake_slskd import KEY

HX = {"HX-Request": "true"}


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"slskd_url": "http://slskd.test:5030", "slskd_api_key": KEY})


@pytest.fixture
def owned(db):
    artist = make_artist(db, "Aretha Franklin")
    group = make_rg(db, artist, "Lady Soul", 1968)
    album = make_album(db, artist, "Lady Soul", 1968, mbid_rg=group.mbid)
    album.release_group_id, album.match_status = group.id, "matched"
    album.quality_tier, album.format = "mp3_320", "mp3"
    db.commit()
    return artist, group, album


def upgrade(client, group, target, **kw):
    return post(
        client, f"/album/{group.id}/upgrade", {"target": target}, page=f"/album/{group.id}", **kw
    )


def requests_in(db):
    db.expire_all()
    return db.scalars(select(DownloadRequest).order_by(DownloadRequest.id)).all()


def test_the_targets_are_the_profiles_better_than_the_current_one(app, owned):
    profiles = app.state.quality_profiles
    assert [p.key for p in svc.upgrade_targets(profiles, "mp3_320")] == [
        "flac_master",
        "flac_hires",
        "flac_standard",
    ]
    assert svc.upgrade_targets(profiles, "flac_master") == []
    assert [p.key for p in svc.upgrade_targets(profiles, "any")][-1] == "mp3_192"
    assert len(svc.upgrade_targets(profiles, "unknown-key")) == 7  # unknown ranks below all


def test_the_owned_album_offers_an_upgrade(admin_client, owned):
    _, group, _ = owned
    text = admin_client.get(f"/album/{group.id}").text
    assert 'id="album-upgrade"' in text and "Quality upgrade" in text
    assert "This copy is MP3 320 / V0" in text
    select_html = text[
        text.index('id="upgrade-target"') : text.index(
            "</select>", text.index('id="upgrade-target"')
        )
    ]
    options = [o for o in select_html.split("<option value=")[1:]]
    assert [o.split('"')[1] for o in options] == ["flac_master", "flac_hires", "flac_standard"]
    assert "data-upgrade-button" in text and 'type="submit" disabled' not in text


def test_requesting_an_upgrade_makes_a_download_request_aimed_at_the_target(
    admin_client, db, owned
):
    _, group, _ = owned
    r = upgrade(admin_client, group, "flac_hires", headers=HX)
    assert r.status_code == 200 and 'data-request-status="queued"' in r.text
    assert "to at least FLAC Hi-Res (24/96)" in r.text and 'id="album-upgrade"' in r.text
    [req] = requests_in(db)
    assert (req.quality_key, req.upgrade_from, req.release_group_id) == (
        "flac_hires",
        "mp3_320",
        group.id,
    )
    [job] = db.scalars(select(Job).where(Job.type == "slskd_search")).all()
    assert json.loads(job.payload_json) == {"request_id": req.id}


def test_without_javascript_the_form_redirects_to_the_album(admin_client, db, owned):
    _, group, _ = owned
    r = upgrade(admin_client, group, "flac_standard")
    assert r.status_code == 303 and r.headers["location"] == f"/album/{group.id}"
    assert len(requests_in(db)) == 1


def test_a_running_upgrade_replaces_the_form_and_is_not_repeated(admin_client, db, owned):
    _, group, _ = owned
    upgrade(admin_client, group, "flac_standard", headers=HX)
    again = upgrade(admin_client, group, "flac_master", headers=HX)
    assert len(requests_in(db)) == 1 and requests_in(db)[0].quality_key == "flac_standard"
    assert "data-upgrade-status" in again.text and "data-upgrade-button" not in again.text
    page = admin_client.get(f"/album/{group.id}").text
    assert 'hx-trigger="every 3s"' in page and "data-upgrade-button" not in page


def test_the_upgrade_section_follows_the_request(admin_client, db, owned):
    _, group, _ = owned
    upgrade(admin_client, group, "flac_standard", headers=HX)
    [req] = requests_in(db)
    svc.set_status(db, req.id, "downloading", progress=40.0)
    db.commit()
    text = admin_client.get(f"/album/{group.id}/upgrade").text
    assert "<html" not in text and "40%" in text and 'data-request-status="downloading"' in text
    svc.set_status(db, req.id, "no_results")
    db.commit()
    again = admin_client.get(f"/album/{group.id}/upgrade").text
    assert (
        "found nothing suitable" in again
        and "data-upgrade-button" in again
        and "every 3s" not in again
    )


def test_the_request_page_shows_that_it_is_an_upgrade(admin_client, db, owned):
    _, group, _ = owned
    upgrade(admin_client, group, "flac_standard", headers=HX)
    [req] = requests_in(db)
    assert "data-upgrade" in admin_client.get("/requests").text
    detail = admin_client.get(f"/requests/{req.id}").text
    assert "Upgrade from MP3 320 / V0" in detail and "FLAC Standard (16/44.1)" in detail


def test_an_album_with_the_best_quality_has_nothing_to_upgrade(admin_client, db, owned):
    _, group, album = owned
    album.quality_tier = "flac_master"
    db.commit()
    text = admin_client.get(f"/album/{group.id}").text
    assert "data-upgrade-top" in text and "data-upgrade-button" not in text


@pytest.mark.parametrize("target", ["mp3_320", "mp3_192", "any", "nope", ""])
def test_a_target_that_is_not_better_is_refused(admin_client, db, owned, target):
    _, group, _ = owned
    r = upgrade(admin_client, group, target, headers=HX)
    assert r.status_code == 400 and "better than the current one" in r.text
    assert requests_in(db) == []
    plain = upgrade(admin_client, group, target)
    assert plain.status_code == 303 and plain.headers["location"].endswith(
        "error=request_bad_target"
    )


def test_without_slskd_the_button_is_disabled_and_the_request_refused(
    app, settings, admin_client, db, owned
):
    _, group, _ = owned
    app.state.settings = settings.model_copy(update={"slskd_url": ""})
    text = admin_client.get(f"/album/{group.id}").text
    assert "SLSKD is not set up yet" in text and 'type="submit" disabled' in text
    r = upgrade(admin_client, group, "flac_standard", headers=HX)
    assert r.status_code == 400 and requests_in(db) == []


def test_only_owned_albums_can_be_upgraded(admin_client, db):
    artist = make_artist(db, "Someone")
    group = make_rg(db, artist, "Not Mine", 2000)
    assert upgrade(admin_client, group, "flac_standard").status_code == 404
    assert admin_client.get(f"/album/{group.id}/upgrade").status_code == 404
    assert admin_client.get("/album/9999/upgrade").status_code == 404
    assert (
        "album-upgrade" not in admin_client.get(f"/album/{group.id}").text
    )  # not owned: a download


def test_an_album_not_matched_to_musicbrainz_has_no_upgrade(admin_client, db):
    artist = make_artist(db, "Home")
    album = make_album(db, artist, "Home Tape", 2001)
    db.commit()
    assert "album-upgrade" not in admin_client.get(f"/album/local/{album.id}").text


def test_the_upgrade_needs_a_login_and_a_csrf_token(admin_client, new_client, owned):
    _, group, _ = owned
    assert (
        admin_client.post(f"/album/{group.id}/upgrade", data={"target": "flac_master"}).status_code
        == 403
    )
    anon = new_client()
    assert anon.get(f"/album/{group.id}/upgrade").status_code == 303


def test_ordinary_users_may_ask_for_an_upgrade(user_client, db, owned):
    _, group, _ = owned
    r = upgrade(user_client, group, "flac_standard", headers=HX)
    assert r.status_code == 200 and len(requests_in(db)) == 1


def test_the_search_aims_at_the_target(db, owned):
    """The engine drops candidates below the requested quality: here a FLAC target excludes MP3."""
    from app.quality import DEFAULT_PROFILES
    from app.services.candidate_selection import (
        RemoteFile,
        RemoteResponse,
        Wanted,
        select_candidates,
    )

    mp3s = tuple(
        RemoteFile(f"x\\Lady Soul\\{i:02d}.mp3", 8_000_000, "mp3", 320, None, None, 200)
        for i in range(1, 11)
    )
    wanted = Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "flac_standard")
    assert (
        select_candidates([RemoteResponse("u", mp3s, True, 0, 2_000_000)], wanted, DEFAULT_PROFILES)
        == []
    )
