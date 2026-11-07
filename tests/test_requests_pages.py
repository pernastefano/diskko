"""Requesting an album, the requests pages, the manual choice, cancel and retry."""

import json

import pytest
import respx
from sqlalchemy import func, select

from app.integrations.base import run_check
from app.integrations.slskd import Slskd
from app.models import (
    DownloadFile,
    DownloadRequest,
    Job,
)
from app.services import downloads as svc
from app.services.candidate_selection import select_candidates
from tests.conftest import post
from tests.dbhelpers import make_artist, make_rg
from tests.fake_slskd import KEY, flac_album
from tests.test_candidate_selection import WANTED, album, resp

HX = {"HX-Request": "true"}


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"slskd_url": "http://slskd.test:5030", "slskd_api_key": KEY})


@pytest.fixture
def album_ids(db):
    artist = make_artist(db, "Aretha Franklin")
    group = make_rg(db, artist, "Lady Soul", 1968)
    return artist.id, group.id


def request_it(client, ids, **kw):
    artist_id, group_id = ids
    return post(
        client, f"/artist/{artist_id}/request/{group_id}", page=f"/artist/{artist_id}", **kw
    )


def requests_in(db):
    db.expire_all()
    return db.scalars(select(DownloadRequest).order_by(DownloadRequest.id)).all()


def jobs(db, type_):
    db.expire_all()
    return db.scalars(select(Job).where(Job.type == type_).order_by(Job.id)).all()


def with_candidates(db, request, count=3):
    from tests.test_candidate_selection import GOOD

    responses = [resp(f"user{i}", album(GOOD, 10), queue=i * 5) for i in range(count)]
    found = select_candidates(
        responses, WANTED, __import__("app.quality", fromlist=["x"]).DEFAULT_PROFILES, limit=5
    )
    svc.store_candidates(db, request.id, found, "aretha franklin lady soul")
    db.commit()
    return found


# ------------------------------------------------------------------- the button


def test_pressing_the_button_queues_a_search(admin_client, db, album_ids):
    r = request_it(admin_client, album_ids, headers=HX)
    assert r.status_code == 200 and 'data-request-status="queued"' in r.text
    assert "<html" not in r.text and 'hx-trigger="every 3s"' in r.text  # it follows the request
    [req] = requests_in(db)
    assert (req.status, req.mode, req.quality_key) == ("queued", "auto", "flac_standard")
    [job] = jobs(db, "slskd_search")
    assert job.queue == "slskd" and json.loads(job.payload_json) == {"request_id": req.id}
    assert job.dedupe_key == f"slskd_search:{req.id}" and job.created_by is not None


def test_pressing_it_twice_makes_one_request(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    again = request_it(admin_client, album_ids, headers=HX)
    assert 'data-request-status="queued"' in again.text
    assert len(requests_in(db)) == 1 and len(jobs(db, "slskd_search")) == 1


def test_the_request_of_one_user_shows_for_everyone(admin_client, user_client, db, album_ids):
    request_it(user_client, album_ids, headers=HX)
    artist_id, _ = album_ids
    assert 'data-request-status="queued"' in admin_client.get(f"/artist/{artist_id}").text
    other = request_it(admin_client, album_ids, headers=HX)  # the album is already requested
    assert 'data-request-status="queued"' in other.text and len(requests_in(db)) == 1


def test_without_javascript_the_button_redirects_with_a_message(admin_client, db, album_ids):
    r = request_it(admin_client, album_ids)
    artist_id, _ = album_ids
    assert (
        r.status_code == 303
        and r.headers["location"] == f"/artist/{artist_id}?flash=request_created"
    )
    assert "Request sent" in admin_client.get(r.headers["location"]).text


def test_the_default_mode_comes_from_the_settings(app, settings, admin_client, db, album_ids):
    app.state.settings = settings.model_copy(update={"slskd_mode": "manual"})
    request_it(admin_client, album_ids, headers=HX)
    assert requests_in(db)[0].mode == "manual"


def test_the_artist_page_shows_the_progress_of_a_download(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    req.status, req.progress = "downloading", 42.0
    db.commit()
    artist_id, _ = album_ids
    text = admin_client.get(f"/artist/{artist_id}").text
    assert 'data-request-status="downloading"' in text and "42%" in text
    assert f'href="/requests/{req.id}"' in text and f'hx-get="/requests/{req.id}/control"' in text


def test_a_failed_request_can_be_tried_again_from_the_card(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    svc.set_status(db, req.id, "no_results")
    db.commit()
    artist_id, _ = album_ids
    assert "data-request-again" in admin_client.get(f"/artist/{artist_id}").text
    again = request_it(admin_client, album_ids, headers=HX)
    assert 'data-request-status="queued"' in again.text and len(requests_in(db)) == 2


def test_the_button_needs_a_login_and_csrf(admin_client, new_client, album_ids):
    artist_id, group_id = album_ids
    url = f"/artist/{artist_id}/request/{group_id}"
    assert admin_client.post(url).status_code == 403
    assert new_client().post(url, data={"csrf_token": "x"}).status_code in (401, 403)


def test_unknown_albums_are_404(admin_client, album_ids):
    artist_id, group_id = album_ids
    assert (
        post(admin_client, f"/artist/{artist_id}/request/9999", page="/library").status_code == 404
    )
    assert (
        post(admin_client, f"/artist/9999/request/{group_id}", page="/library").status_code == 404
    )


# ------------------------------------------------------------------- the list


def test_the_list_is_empty_at_first(admin_client):
    text = admin_client.get("/requests").text
    assert "data-empty" in text and "No requests yet" in text


def test_users_see_only_their_requests_and_admins_see_all(admin_client, user_client, db):
    a1 = make_artist(db, "Artist One")
    a2 = make_artist(db, "Artist Two")
    g1, g2 = make_rg(db, a1, "Album One", 2000), make_rg(db, a2, "Album Two", 2001)
    request_it(admin_client, (a1.id, g1.id), headers=HX)
    request_it(user_client, (a2.id, g2.id), headers=HX)
    mine = user_client.get("/requests").text
    assert "Album Two" in mine and "Album One" not in mine and "User</th>" not in mine
    everyone = admin_client.get("/requests").text
    assert "Album One" in everyone and "Album Two" in everyone and "bob" in everyone
    assert 'hx-trigger="every 5s"' in everyone  # something is running: the list refreshes


def test_the_list_does_not_poll_when_nothing_runs(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    svc.set_status(db, requests_in(db)[0].id, "failed", error="all candidates failed")
    db.commit()
    text = admin_client.get("/requests").text
    assert "every 5s" not in text and "all candidates failed" in text


def test_the_requests_link_is_in_the_menu_for_everyone(admin_client, user_client):
    for c in (admin_client, user_client):
        assert 'href="/requests"' in c.get("/library").text


def test_the_page_follows_the_language(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    post(admin_client, "/lang", {"lang": "it", "next": "/library"})
    text = admin_client.get("/requests").text
    assert "Richieste di download" in text and "In coda" in text


# ------------------------------------------------------------------- one request


def test_a_request_may_only_be_seen_by_its_owner_or_an_admin(
    admin_client, user_client, db, album_ids
):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    assert admin_client.get(f"/requests/{req.id}").status_code == 200
    for path in ("", "/body", "/control"):
        assert user_client.get(f"/requests/{req.id}{path}").status_code == 404
    assert user_client.get("/requests/9999").status_code == 404
    assert post(user_client, f"/requests/{req.id}/cancel", page="/library").status_code == 404


def test_the_detail_shows_candidates_files_and_the_job(admin_client, user_client, db, album_ids):
    request_it(user_client, album_ids, headers=HX)
    [req] = requests_in(db)
    found = with_candidates(db, req)
    svc.choose_candidate(db, req, 0)
    svc.create_files(db, req, found[0])
    svc.set_status(db, req.id, "downloading", progress=30.0)
    db.commit()
    mine, admin = (
        user_client.get(f"/requests/{req.id}").text,
        admin_client.get(f"/requests/{req.id}").text,
    )
    for text in (mine, admin):
        assert 'data-candidate="0"' in text and 'data-candidate="2"' in text
        assert "user0" in text and "FLAC Standard (16/44.1)" in text and "10 · " in text
        assert text.count("data-file=") == 10 and 'hx-get="/requests/' in text
        assert "downloading" in text and "30%" in text
    assert "Job log" in admin and "Job log" not in mine  # the job is for administrators


def test_manual_mode_shows_a_button_per_candidate_and_downloads_the_chosen_one(
    admin_client, db, album_ids
):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    with_candidates(db, req)
    svc.awaiting_choice(db, req.id)
    db.commit()
    text = admin_client.get(f"/requests/{req.id}").text
    assert text.count("data-choose") == 3 and "Pick the folder to download" in text
    r = post(admin_client, f"/requests/{req.id}/choose", {"index": "1"}, page=f"/requests/{req.id}")
    assert r.status_code == 303
    db.expire_all()
    req = db.get(DownloadRequest, req.id)
    assert (req.status, req.candidate_index) == ("candidate_found", 1)
    [job] = jobs(db, "download_monitor")
    assert json.loads(job.payload_json) == {"request_id": req.id}
    assert "data-choose" not in admin_client.get(f"/requests/{req.id}").text


def test_choosing_is_refused_when_it_does_not_apply(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    r = post(admin_client, f"/requests/{req.id}/choose", {"index": "0"}, page="/requests")
    assert r.headers["location"].endswith("error=request_not_choosing")
    with_candidates(db, req)
    svc.awaiting_choice(db, req.id)
    db.commit()
    bad = post(admin_client, f"/requests/{req.id}/choose", {"index": "9"}, page="/requests")
    assert bad.headers["location"].endswith("error=request_bad_candidate")
    missing = post(admin_client, f"/requests/{req.id}/choose", {}, page="/requests")
    assert missing.headers["location"].endswith("error=request_bad_candidate")
    assert jobs(db, "download_monitor") == []
    assert "That candidate does not exist" in admin_client.get(bad.headers["location"]).text


def test_cancelling_a_request_stops_its_jobs(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    r = post(admin_client, f"/requests/{req.id}/cancel", page=f"/requests/{req.id}")
    assert r.status_code == 303
    db.expire_all()
    assert db.get(DownloadRequest, req.id).status == "cancelled"
    [job] = jobs(db, "slskd_search")
    assert job.status == "cancelled" or job.cancel_requested
    again = post(admin_client, f"/requests/{req.id}/cancel", page="/requests")
    assert again.headers["location"].endswith("error=request_not_active")
    # the album can be requested again afterwards
    assert 'data-request-status="queued"' in request_it(admin_client, album_ids, headers=HX).text


def test_a_failed_request_is_searched_again_with_another_text(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [old] = requests_in(db)
    svc.set_status(db, old.id, "no_results")
    db.commit()
    text = admin_client.get(f"/requests/{old.id}").text
    assert "data-retry" in text and 'name="query"' in text
    r = post(
        admin_client,
        f"/requests/{old.id}/retry",
        {"query": "  lady   soul flac ", "mode": "manual"},
        page=f"/requests/{old.id}",
    )
    new = requests_in(db)[-1]
    assert (
        r.status_code == 303 and r.headers["location"] == f"/requests/{new.id}" and new.id != old.id
    )
    assert (new.query_override, new.mode, new.status) == ("lady soul flac", "manual", "queued")
    assert new.quality_key == old.quality_key
    assert len(jobs(db, "slskd_search")) == 2


def test_only_a_finished_unsuccessful_request_can_be_retried(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    r = post(admin_client, f"/requests/{req.id}/retry", {}, page="/requests")
    assert r.headers["location"].endswith("error=request_not_retryable")
    assert "data-retry" not in admin_client.get(f"/requests/{req.id}").text
    assert len(requests_in(db)) == 1


def test_retrying_needs_slskd(app, settings, admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    svc.set_status(db, req.id, "failed", error="x")
    db.commit()
    app.state.settings = settings.model_copy(update={"slskd_url": ""})
    r = post(admin_client, f"/requests/{req.id}/retry", {}, page="/requests")
    assert r.headers["location"].endswith("error=request_no_slskd")


def test_the_control_partial_follows_the_request(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    svc.set_status(db, req.id, "downloading", progress=70.0)
    db.commit()
    text = admin_client.get(f"/requests/{req.id}/control").text
    assert "<html" not in text and "70%" in text and 'hx-trigger="every 3s"' in text
    svc.set_status(db, req.id, "downloaded", progress=100.0)
    db.commit()
    done = admin_client.get(f"/requests/{req.id}/control").text
    assert 'data-request-status="downloaded"' in done


def test_the_detail_does_not_poll_once_finished(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    assert 'hx-trigger="every 3s"' in admin_client.get(f"/requests/{req.id}/body").text
    svc.set_status(db, req.id, "downloaded", progress=100.0)
    db.commit()
    assert "every 3s" not in admin_client.get(f"/requests/{req.id}/body").text


def test_files_are_listed_with_their_state(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    db.add(
        DownloadFile(
            request_id=req.id,
            username="alice",
            filename="a\\b\\01 Song.flac",
            size=1000,
            state="InProgress",
            bytes_transferred=500,
        )
    )
    db.commit()
    text = admin_client.get(f"/requests/{req.id}/body").text
    assert "01 Song.flac" in text and "InProgress" in text and "50%" in text
    assert db.scalar(select(func.count(DownloadFile.id))) == 1


# --------------------------------------------------------------- settings and checks


def test_the_slskd_settings_have_the_new_fields(admin_client):
    text = admin_client.get("/settings/integrations").text
    for name in ("downloads_dir", "slskd_mode", "slskd_search_timeout", "slskd_candidates"):
        assert f'name="{name}"' in text
    assert "The user chooses" in text and "Best candidate automatically" in text


def test_saving_the_slskd_settings_applies_them(app, admin_client):
    r = post(
        admin_client,
        "/settings/integrations/slskd",
        {
            "slskd_url": "http://slskd.test:5030",
            "downloads_dir": "/srv/downloads",
            "slskd_mode": "manual",
            "slskd_search_timeout": "45",
            "slskd_candidates": "8",
        },
        page="/settings/integrations",
    )
    assert r.status_code == 303
    s = app.state.settings
    assert (s.slskd_mode, s.slskd_search_timeout, s.slskd_candidates) == ("manual", 45, 8)
    assert str(s.downloads_dir) == "/srv/downloads"
    bad = post(
        admin_client,
        "/settings/integrations/slskd",
        {"slskd_mode": "robot", "slskd_search_timeout": "2", "slskd_candidates": "99"},
        page="/settings/integrations",
    )
    assert bad.status_code == 400


async def test_the_slskd_check_tells_when_soulseek_is_not_connected(settings):
    s = settings.model_copy(update={"slskd_url": "http://slskd.test:5030", "slskd_api_key": KEY})
    base = "http://slskd.test:5030/api/v0/application"

    def payload(connected):
        return {"version": {"current": "0.26.2.0"}, "server": {"isConnected": connected}}

    with respx.mock() as mock:
        mock.get(base).respond(json=payload(True))
        ok = await run_check(Slskd(s))
    with respx.mock() as mock:
        mock.get(base).respond(json=payload(False))
        offline = await run_check(Slskd(s))
    with respx.mock() as mock:
        mock.get(base).respond(json={"version": "0.20"})  # an older answer without "server"
        old = await run_check(Slskd(s))
    assert (ok.code, ok.detail) == ("ok", "0.26.2.0")
    assert offline.ok is False and offline.code == "soulseek_offline"
    assert old.code == "ok"


def test_the_flac_fixture_helper_matches_the_real_shape():
    r = flac_album("alice", "Band\\Album", 2)
    assert set(r["files"][0]) == {
        "filename",
        "size",
        "extension",
        "code",
        "isLocked",
        "bitDepth",
        "sampleRate",
        "length",
    }
