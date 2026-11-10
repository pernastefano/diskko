"""When a request waits for the user: the notification, the detail of what is offered, the search
of the network by hand."""

import json

import pytest

from app.models import Job, Notification
from app.services import downloads as svc
from app.services import notifications
from tests import test_requests_pages
from tests.conftest import post
from tests.test_requests_pages import HX, jobs, request_it, requests_in, with_candidates

album_ids = test_requests_pages.album_ids  # fixtures of the requests tests
settings = test_requests_pages.settings

OPTION = {
    "mbid": "11111111-1111-4111-8111-111111111111",
    "title": "Album",
    "date": "2000",
    "country": "US",
    "tracks": 4,
    "paired": 3,
    "missing": ["Bonus"],
    "unmatched": [],
}


def notes(db, kind="needs_choice"):
    db.expire_all()
    return db.query(Notification).filter_by(kind=kind).order_by(Notification.id).all()


# ---------------------------------------------------------------- the notification


def test_a_download_waiting_for_the_release_notifies_its_owner(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    svc.store_release_options(db, req.id, [OPTION])
    db.commit()
    [note] = notes(db)
    assert note.user_id == req.user_id and note.request_id == req.id and note.read_at is None
    assert notifications.unread_count(db, req.user_id) == 1
    bell = admin_client.get("/nav/notifications").text
    assert "A request is waiting for your choice" in bell and "Lady Soul" in bell


def test_the_same_news_is_not_repeated_while_unread_but_comes_again_after_an_answer(
    admin_client, db, album_ids
):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    svc.store_release_options(db, req.id, [OPTION])
    svc.store_release_options(db, req.id, [OPTION])  # asked twice in a row: one notice
    db.commit()
    assert len(notes(db)) == 1
    svc.choose_release_option(db, db.get(type(req), req.id), "none")  # the answer
    db.commit()
    assert notes(db)[0].read_at is not None  # it is no longer news
    svc.store_release_options(db, req.id, [OPTION])  # it asks again later
    db.commit()
    assert len(notes(db)) == 1 and notes(db)[0].read_at is None  # the same notice, unread again


def test_choosing_a_source_clears_its_notification(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    with_candidates(db, req)
    svc.awaiting_choice(db, req.id)
    db.commit()
    assert len(notes(db)) == 1 and notes(db)[0].read_at is None
    post(admin_client, f"/requests/{req.id}/choose", {"index": "0"}, page=f"/requests/{req.id}")
    assert notes(db)[0].read_at is not None


# ------------------------------------------------------- the detail of the candidates


def test_each_candidate_shows_its_files_and_the_rest_can_be_unfolded(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    found = many_candidates(db, req, 8)  # more than the five that are shown
    svc.awaiting_choice(db, req.id)
    db.commit()
    html = admin_client.get(f"/requests/{req.id}").text
    assert html.count("data-candidate-files") == 8 and html.count("data-candidate-detail=") == 8
    first = found[0].files[0]["filename"].split("\\")[-1]
    assert first in html and "912 kbps" in html and "4:05" in html  # the listing of the folder
    hidden = html.count('x-show="all" x-cloak data-candidate-group')
    assert hidden == 3  # the 6th, 7th and 8th wait to be unfolded
    assert "data-show-all" in html and "Show all 8" in html


def test_a_few_candidates_need_no_unfolding(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    with_candidates(db, req, count=3)
    svc.awaiting_choice(db, req.id)
    db.commit()
    assert "data-show-all" not in admin_client.get(f"/requests/{req.id}").text


# ------------------------------------------------------- the search by hand


def many_candidates(db, request, count):
    from app.quality import DEFAULT_PROFILES
    from app.services.candidate_selection import select_candidates
    from tests.test_candidate_selection import GOOD, WANTED, album, resp

    responses = [resp(f"user{i}", album(GOOD, 10), queue=i) for i in range(count)]
    found = select_candidates(responses, WANTED, DEFAULT_PROFILES, limit=count)
    found[0].files[0].update(bit_rate=912, length=245)  # what SLSKD lists for the file
    svc.store_candidates(db, request.id, found, "aretha franklin lady soul")
    db.commit()
    return found


def finish_search_jobs(db):
    for job in jobs(db, "slskd_search"):
        job.status = "succeeded"
    db.commit()


def waiting(admin_client, db, album_ids):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    with_candidates(db, req)
    svc.awaiting_choice(db, req.id)
    finish_search_jobs(db)  # the search that found them is over
    db.commit()
    return req


def test_the_page_offers_a_search_of_the_network(admin_client, db, album_ids):
    req = waiting(admin_client, db, album_ids)
    html = admin_client.get(f"/requests/{req.id}").text
    assert "data-network-search" in html and f'action="/requests/{req.id}/search"' in html


def test_searching_by_hand_queues_the_search_with_the_typed_words(admin_client, db, album_ids):
    req = waiting(admin_client, db, album_ids)
    r = post(
        admin_client,
        f"/requests/{req.id}/search",
        {"query": "  Adams  Reckless "},
        page=f"/requests/{req.id}",
    )
    assert r.status_code == 303 and r.headers["location"] == f"/requests/{req.id}"
    db.expire_all()
    assert db.get(type(req), req.id).status == "searching"
    [job] = jobs(db, "slskd_search")[-1:]
    assert json.loads(job.payload_json) == {
        "request_id": req.id,
        "browse": True,
        "query": "Adams Reckless",
    }
    assert job.dedupe_key == f"slskd_search:{req.id}"


def test_a_request_with_no_results_can_be_searched_by_hand_and_comes_back_to_life(
    admin_client, db, album_ids
):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    svc.no_results(db, req.id, "a | b")
    finish_search_jobs(db)
    db.commit()
    assert "data-network-search" in admin_client.get(f"/requests/{req.id}").text
    post(admin_client, f"/requests/{req.id}/search", {"query": "adams"}, page=f"/requests/{req.id}")
    db.expire_all()
    again = db.get(type(req), req.id)
    assert again.status == "searching" and again.finished_at is None


@pytest.mark.parametrize("status", ["downloading", "completed", "failed", "queued"])
def test_it_is_refused_when_the_request_is_not_waiting(admin_client, db, album_ids, status):
    request_it(admin_client, album_ids, headers=HX)
    [req] = requests_in(db)
    svc.set_status(db, req.id, status)
    db.commit()
    r = post(admin_client, f"/requests/{req.id}/search", {"query": "x"}, page="/requests")
    assert r.headers["location"].endswith("error=request_not_searchable")
    assert not [j for j in jobs(db, "slskd_search") if json.loads(j.payload_json).get("browse")]


def test_an_empty_text_is_refused_and_the_search_belongs_to_the_owner(
    admin_client, user_client, db, album_ids
):
    req = waiting(admin_client, db, album_ids)
    empty = post(admin_client, f"/requests/{req.id}/search", {"query": "  "}, page="/requests")
    assert empty.headers["location"].endswith("error=request_not_searchable")
    other = post(user_client, f"/requests/{req.id}/search", {"query": "x"}, page="/dashboard")
    assert other.status_code == 404  # not their request


def test_choosing_a_folder_below_the_minimum_quality_lowers_the_minimum_for_it(
    admin_client, db, album_ids
):
    req = waiting(admin_client, db, album_ids)
    found = svc.candidates_of(req)
    found[0].quality = "mp3_320"  # what a hand search may offer
    req.candidates_json = json.dumps([c.to_dict() for c in found])
    db.commit()
    assert req.quality_key == "flac_standard"
    post(admin_client, f"/requests/{req.id}/choose", {"index": "0"}, page=f"/requests/{req.id}")
    db.expire_all()
    assert db.get(type(req), req.id).quality_key == "mp3_320"
    assert db.query(Job).filter_by(type="download_monitor").count() == 1
