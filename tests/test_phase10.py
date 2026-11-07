"""Phase 10: the requests list, the quality of one's own requests, the audit log, notifications."""

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog,
    DownloadRequest,
    Job,
    Notification,
    User,
    utcnow,
)
from app.services import audit
from app.services import downloads as svc
from app.services import notifications as notes
from app.services.app_settings import get_setting
from tests.conftest import add_user, login, post
from tests.dbhelpers import make_artist, make_rg

HX = {"HX-Request": "true"}


@pytest.fixture
def settings(settings):
    return settings.model_copy(
        update={"slskd_url": "http://slskd.test:5030", "slskd_api_key": "k" * 20}
    )


@pytest.fixture
def two_users(admin_client, user_client, new_client):
    """admin (the setup account), bob (user_client) and carol (another user)."""
    add_user(admin_client, "carol")
    carol = new_client()
    assert login(carol, "carol").status_code == 303
    return admin_client, user_client, carol


def user_id(db, name):
    return db.scalar(select(User.id).where(User.username == name))


def make_requests(
    db, owner, count, *, status="completed", artist="Aretha Franklin", prefix="Album"
):
    art = make_artist(db, f"{artist} {owner}") if count else None
    ids = []
    for i in range(count):
        group = make_rg(db, art, f"{prefix} {owner} {i}", 1960 + i % 40)
        request = DownloadRequest(
            user_id=owner, release_group_id=group.id, status=status, quality_key="flac_standard"
        )
        db.add(request)
        db.flush()
        ids.append(request.id)
    db.commit()
    return ids


def rows(html):
    return [int(i) for i in re.findall(r'<tr data-request="(\d+)"', html)]


# ---------------------------------------------------------------- the requests list


def test_a_user_sees_only_their_own_requests_and_an_admin_all(two_users, db):
    admin, bob, carol = two_users
    mine = make_requests(db, user_id(db, "bob"), 2)
    hers = make_requests(db, user_id(db, "carol"), 3)
    assert sorted(rows(bob.get("/requests").text)) == sorted(mine)
    assert sorted(rows(carol.get("/requests").text)) == sorted(hers)
    assert sorted(rows(admin.get("/requests").text)) == sorted(mine + hers)
    assert (
        "Everyone" in admin.get("/requests").text and 'name="user"' not in bob.get("/requests").text
    )


def test_the_list_can_be_filtered_by_state(two_users, db):
    admin, bob, _ = two_users
    uid = user_id(db, "bob")
    done = make_requests(db, uid, 2, status="completed")
    running = make_requests(db, uid, 1, status="downloading", artist="B")
    failed = make_requests(db, uid, 1, status="failed", artist="C")
    no_hits = make_requests(db, uid, 1, status="no_results", artist="D")
    got = lambda status: sorted(rows(bob.get(f"/requests?status={status}").text))  # noqa: E731
    assert got("all") == sorted(done + running + failed + no_hits)
    assert got("active") == running
    assert got("finished") == sorted(done)
    assert got("failed") == sorted(failed + no_hits)
    assert got("nonsense") == got("all")  # an unknown filter shows everything


def test_an_admin_can_pick_a_user_and_others_cannot(two_users, db):
    admin, bob, carol = two_users
    mine = make_requests(db, user_id(db, "bob"), 2)
    hers = make_requests(db, user_id(db, "carol"), 2, artist="X")
    assert sorted(rows(admin.get("/requests?user=bob").text)) == sorted(mine)
    assert sorted(rows(admin.get("/requests?user=CAROL").text)) == sorted(hers)
    assert rows(admin.get("/requests?user=nobody").text) == []
    assert sorted(rows(bob.get("/requests?user=carol").text)) == sorted(mine)  # ignored


def test_the_list_is_paged_and_the_links_keep_the_filters(two_users, db):
    admin, bob, _ = two_users
    uid = user_id(db, "bob")
    ids = make_requests(db, uid, 120, status="completed")
    make_requests(db, uid, 1, status="failed", artist="Z")
    first = bob.get("/requests?status=finished")
    assert len(rows(first.text)) == 50 and "120 request(s)" in first.text
    assert rows(first.text) == sorted(ids, reverse=True)[:50]  # newest first
    assert 'href="/requests?status=finished&amp;page=2"' in first.text
    last = bob.get("/requests?status=finished&page=3").text
    assert len(rows(last)) == 20 and "Page 3 of 3" in last and "Next" not in last
    assert rows(bob.get("/requests?status=finished&page=99").text) == rows(last)  # clamped
    assert len(rows(bob.get("/requests?status=finished&page=-4").text)) == 50
    assert "data-pages" not in bob.get("/requests?status=failed").text  # one page: no paging


def test_each_row_has_the_action_that_fits_its_state(two_users, db):
    admin, bob, _ = two_users
    uid = user_id(db, "bob")
    running, failed, done = (
        make_requests(db, uid, 1, status=s, artist=s)[0]
        for s in ("downloading", "failed", "completed")
    )
    html = bob.get("/requests").text
    row = lambda i: re.search(rf'<tr data-request="{i}".*?</tr>', html, re.S).group(0)  # noqa: E731
    assert "data-row-cancel" in row(running) and "data-row-retry" not in row(running)
    assert "data-row-retry" in row(failed) and "data-row-cancel" not in row(failed)
    assert "data-row-cancel" not in row(done) and "data-row-retry" not in row(done)


def test_cancelling_from_the_list_returns_to_the_same_list(two_users, db):
    admin, bob, _ = two_users
    [rid] = make_requests(db, user_id(db, "bob"), 1, status="queued")
    back = "/requests?status=active&page=1"
    r = post(bob, f"/requests/{rid}/cancel", {"next": back}, page="/requests")
    assert r.status_code == 303 and r.headers["location"] == back
    db.expire_all()
    assert db.get(DownloadRequest, rid).status == "cancelled"
    # the way back is only ever a list of requests
    [other] = make_requests(db, user_id(db, "bob"), 1, status="queued", artist="Y")
    for evil in ("https://evil.example/", "//evil.example", "/admin/users", "javascript:1"):
        r = post(bob, f"/requests/{other}/cancel", {"next": evil}, page="/requests")
        assert r.headers["location"] == f"/requests/{other}", evil
        db.expire_all()
        db.get(DownloadRequest, other).status = "queued"
        db.commit()


def test_an_admin_may_cancel_anyones_request_and_a_user_not_other_peoples(two_users, db):
    admin, bob, carol = two_users
    [hers] = make_requests(db, user_id(db, "carol"), 1, status="queued")
    assert post(bob, f"/requests/{hers}/cancel", page="/requests").status_code == 404
    assert post(admin, f"/requests/{hers}/cancel", page="/requests").status_code == 303
    db.expire_all()
    assert db.get(DownloadRequest, hers).status == "cancelled"


def test_an_admin_retrying_keeps_the_owner_of_the_request(two_users, db):
    admin, bob, _ = two_users
    uid = user_id(db, "bob")
    [rid] = make_requests(db, uid, 1, status="failed")
    r = post(admin, f"/requests/{rid}/retry", {}, page="/requests")
    assert r.status_code == 303
    db.expire_all()
    new = db.scalar(select(DownloadRequest).where(DownloadRequest.id != rid))
    assert new.user_id == uid  # bob's, not the admin's
    job = db.scalar(select(Job).where(Job.type == "slskd_search"))
    assert job.created_by == user_id(db, "admin")  # the admin did the retrying
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "request.retried"))
    assert entry.actor_name == "admin" and json.loads(entry.details_json)["owner"] == "bob"
    assert rows(bob.get("/requests").text).count(new.id) == 1  # and it shows in bob's list


# ------------------------------------------------------- the quality of my requests


def test_each_user_has_their_own_default_quality(two_users, db):
    admin, bob, carol = two_users
    page = bob.get("/account/preferences").text
    assert 'name="quality"' in page and "The system default (FLAC Standard)" in page
    assert re.search(r'<option value="flac_master"', page)
    r = post(
        bob,
        "/account/preferences",
        {"language": "en", "theme": "auto", "quality": "mp3_320"},
        page="/account/preferences",
    )
    assert r.status_code == 303
    uid_bob, uid_carol = user_id(db, "bob"), user_id(db, "carol")
    assert svc.user_quality(db, uid_bob) == "mp3_320"
    assert svc.user_quality(db, uid_carol) == "flac_standard"  # carol keeps the system's
    assert '<option value="mp3_320" selected' in bob.get("/account/preferences").text
    assert get_setting(db, "default_quality", "", uid_carol) == ""


def test_a_request_aims_for_the_quality_of_the_user_who_makes_it(two_users, db):
    admin, bob, carol = two_users
    post(
        bob,
        "/account/preferences",
        {"language": "en", "theme": "auto", "quality": "mp3_320"},
        page="/account/preferences",
    )
    artist = make_artist(db, "A")
    group = make_rg(db, artist, "One", 2000)
    second = make_rg(db, artist, "Two", 2001)
    post(bob, f"/artist/{artist.id}/request/{group.id}", page=f"/artist/{artist.id}")
    post(carol, f"/artist/{artist.id}/request/{second.id}", page=f"/artist/{artist.id}")
    db.expire_all()
    made = {r.release_group_id: r.quality_key for r in db.scalars(select(DownloadRequest))}
    assert made == {group.id: "mp3_320", second.id: "flac_standard"}  # independent requests


def test_an_unknown_quality_is_refused_and_a_removed_one_falls_back(two_users, db):
    admin, bob, _ = two_users
    bad = post(
        bob,
        "/account/preferences",
        {"language": "en", "theme": "auto", "quality": "nope"},
        page="/account/preferences",
    )
    assert bad.status_code == 400
    uid = user_id(db, "bob")
    from app.services.app_settings import set_setting

    set_setting(db, "default_quality", "deleted_profile", uid)
    db.commit()
    assert svc.user_quality(db, uid) == "flac_standard"  # the profile no longer exists


def test_the_album_types_stay_the_same_for_everybody(two_users):
    admin, bob, _ = two_users
    page = bob.get("/account/preferences").text
    assert 'name="types"' not in page and 'name="missing_types"' not in page


# ------------------------------------------------------------------------ audit log


def entries(db, action=None):
    db.expire_all()
    stmt = select(AuditLog).order_by(AuditLog.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return db.scalars(stmt).all()


def test_signing_in_and_failing_to_sign_in_are_recorded_without_passwords(
    admin_client, db, new_client
):
    other = new_client()
    assert login(other, "admin", "wrong-password-123").status_code == 400
    assert login(other, "nobody", "whatever-password").status_code == 400
    assert login(new_client(), "admin").status_code == 303
    failed = entries(db, "login.failed")
    assert [e.actor_name for e in failed] == ["admin", "nobody"]
    assert all(
        "password" not in (e.details_json or "").lower() or "reason" in e.details_json
        for e in failed
    )
    blob = " ".join(f"{e.actor_name} {e.details_json}" for e in entries(db))
    assert "wrong-password-123" not in blob and "whatever-password" not in blob
    assert [e.actor_name for e in entries(db, "login.ok")].count("admin") >= 1


def test_requests_are_recorded_with_who_and_what(two_users, db):
    admin, bob, _ = two_users
    artist = make_artist(db, "Aretha Franklin")
    group = make_rg(db, artist, "Lady Soul", 1968)
    post(bob, f"/artist/{artist.id}/request/{group.id}", page=f"/artist/{artist.id}")
    [created] = entries(db, "request.created")
    assert created.actor_name == "bob" and created.target_type == "request"
    assert created.target_label == "Aretha Franklin – Lady Soul"
    rid = created.target_id
    post(bob, f"/requests/{rid}/cancel", page="/requests")
    [cancelled] = entries(db, "request.cancelled")
    assert cancelled.actor_name == "bob" and cancelled.target_id == rid
    db.expire_all()
    db.get(DownloadRequest, rid).status = "failed"
    db.commit()
    post(bob, f"/requests/{rid}/retry", {}, page="/requests")
    assert len(entries(db, "request.retried")) == 1


def test_account_changes_are_recorded(admin_client, db):
    add_user(admin_client, "dave")
    uid = user_id(db, "dave")
    post(admin_client, f"/admin/users/{uid}/role", {"role": "admin"}, page="/settings/users")
    post(admin_client, f"/admin/users/{uid}/active", {"active": "0"}, page="/settings/users")
    post(
        admin_client,
        f"/admin/users/{uid}/password",
        {"new_password": "a-new-password-123"},
        page="/settings/users",
    )
    post(admin_client, f"/admin/users/{uid}/delete", {}, page="/settings/users")
    got = [(e.action, e.target_label) for e in entries(db) if e.action.startswith("user.")]
    assert got == [
        ("user.created", "dave"),
        ("user.role", "dave"),
        ("user.active", "dave"),
        ("user.password_reset", "dave"),
        ("user.deleted", "dave"),
    ]
    blob = " ".join(e.details_json or "" for e in entries(db))
    assert "a-new-password-123" not in blob


def test_saving_settings_records_the_fields_and_never_a_secret(admin_client, db, app):
    post(
        admin_client,
        "/settings/integrations/lastfm",
        {"lastfm_api_key": "super-secret-key-xyz"},
        page="/settings/integrations",
    )
    post(
        admin_client,
        "/settings/albums/types",
        {"types": ["album", "live"]},
        page="/settings/albums",
    )
    saved = entries(db, "settings.saved")
    assert [e.target_label for e in saved] == ["integrations", "albums"]
    assert json.loads(saved[0].details_json)["fields"] == ["lastfm_api_key"]  # the name only
    assert json.loads(saved[1].details_json)["types"] == ["album", "live"]
    assert "super-secret-key-xyz" not in " ".join(e.details_json or "" for e in entries(db))


def test_scans_and_the_quality_profiles_are_recorded(admin_client, db):
    post(admin_client, "/settings/library/scan", {"force": "1"}, page="/settings/library")
    post(admin_client, "/settings/quality/restore", {}, page="/settings/quality")
    assert json.loads(entries(db, "library.scan")[0].details_json)["full"] is True
    assert len(entries(db, "quality.changed")) == 1


def test_secret_looking_details_are_dropped_by_the_service(db):
    entry = audit.record(
        db,
        None,
        "settings.saved",
        actor_name="x",
        details={"api_key": "abc", "token": "t", "password": "p", "ok": 1},
    )
    assert json.loads(entry.details_json) == {"ok": 1}
    assert audit.record(db, None, "login.ok", actor_name="y").details_json is None


def test_the_audit_page_is_for_admins_and_lists_the_entries(two_users, db):
    admin, bob, _ = two_users
    assert bob.get("/admin/audit").status_code == 403
    admin.get("/dashboard")
    html = admin.get("/admin/audit").text
    assert "Audit log" in html and 'data-audit="login.ok"' in html and "Signed in" in html
    assert "data-audit-link" in admin.get("/settings/library").text  # reachable from the settings
    assert "data-audit-link" not in bob.get("/dashboard").text


def test_the_audit_page_filters_and_pages(admin_client, db):
    for i in range(60):
        db.add(
            AuditLog(
                actor_name="eve" if i % 2 else "frank",
                action="request.created",
                target_type="request",
                target_id=i + 1,
                target_label=f"Album {i}",
            )
        )
    db.add(AuditLog(actor_name="eve", action="user.created", at=utcnow() - timedelta(days=40)))
    db.commit()
    page = lambda q="": admin_client.get("/admin/audit" + q).text  # noqa: E731
    first = page("?action=request.created")
    assert first.count("<tr data-audit=") == 50 and "60 entr(ies)" in first
    assert "Page 1 of 2" in first and "action=request.created&amp;page=2" in first
    assert page("?action=request.created&page=2").count("<tr data-audit=") == 10
    assert page("?user=eve&action=request.created").count("<tr data-audit=") == 30
    assert 'href="/requests/60"' in page("?action=request.created")  # the target opens
    since = (utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
    assert page(f"?since={since}&user=eve").count('<tr data-audit="user.created"') == 0
    until = (utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    assert page(f"?until={until}").count('<tr data-audit="user.created"') == 1
    assert page("?action=bogus").count("<tr data-audit=") == 50  # unknown action: no filter


def test_old_audit_entries_and_read_notifications_are_removed_by_the_cleanup(db):
    old = utcnow() - timedelta(days=400)
    db.add_all(
        [
            AuditLog(actor_name="a", action="login.ok", at=old),
            AuditLog(actor_name="a", action="login.ok"),
        ]
    )
    db.commit()
    assert audit.purge(db, 365) == 1
    db.commit()
    assert db.query(AuditLog).count() == 1


async def test_the_housekeeping_job_runs_both_cleanups(settings, session_factory):
    from app.jobs.worker import JobWorker
    from tests.test_jobs_worker import enqueue, logs, wait_for_status

    with session_factory() as db:
        user = User(username="u", password_hash="x", role="user")
        db.add(user)
        db.flush()
        db.add(AuditLog(actor_name="a", action="login.ok", at=utcnow() - timedelta(days=500)))
        db.add(
            Notification(
                user_id=user.id,
                kind="completed",
                album_label="A – B",
                created_at=utcnow() - timedelta(days=60),
                read_at=utcnow() - timedelta(days=45),
            )
        )
        db.commit()
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "housekeeping")
    worker.notify()
    await wait_for_status(session_factory, job_id, "succeeded")
    await worker.stop()
    with session_factory() as db:
        assert db.query(AuditLog).count() == 0 and db.query(Notification).count() == 0
    assert any(
        "audit entr(ies)" in m and "1 read notification" in m for m in logs(session_factory, job_id)
    )


def test_the_audit_retention_is_a_job_setting(admin_client, app):
    html = admin_client.get("/settings/jobs").text
    assert 'name="audit_retention_days"' in html
    r = post(admin_client, "/settings/jobs", {"audit_retention_days": "90"}, page="/settings/jobs")
    assert r.status_code == 303 and app.state.settings.audit_retention_days == 90
    assert (
        post(
            admin_client, "/settings/jobs", {"audit_retention_days": "0"}, page="/settings/jobs"
        ).status_code
        == 400
    )


# ----------------------------------------------------------------------- notifications


def new_request(db, owner, status="queued", artist="A", title="Album"):
    art = make_artist(db, artist)
    group = make_rg(db, art, title, 2000)
    request = DownloadRequest(
        user_id=owner, release_group_id=group.id, status=status, quality_key="flac_standard"
    )
    db.add(request)
    db.commit()
    return request


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        ("completed", "completed"),
        ("failed", "failed"),
        ("no_results", "no_results"),
        ("awaiting_choice", "needs_choice"),
    ],
)
def test_the_owner_is_told_when_a_request_needs_news(db, two_users, status, kind):
    uid = user_id(db, "bob")
    req = new_request(db, uid, artist="Aretha Franklin", title="Lady Soul")
    svc.set_status(db, req.id, status, error="because" if status == "failed" else None)
    db.commit()
    [note] = db.scalars(select(Notification)).all()
    assert (note.user_id, note.kind, note.request_id) == (uid, kind, req.id)
    assert note.album_label == "Aretha Franklin – Lady Soul" and note.read_at is None
    assert (note.detail == "because") == (status == "failed")


def test_statuses_that_are_no_news_make_no_notification_and_news_is_told_once(db, two_users):
    req = new_request(db, user_id(db, "bob"))
    for status in (
        "searching",
        "candidate_found",
        "downloading",
        "downloaded",
        "postprocessing",
        "importing",
        "cancelled",
    ):
        svc.set_status(db, req.id, status)
    db.commit()
    assert db.query(Notification).count() == 0
    svc.set_status(db, req.id, "completed")
    svc.set_status(db, req.id, "completed")  # a second time
    db.commit()
    assert db.query(Notification).count() == 1


def test_a_request_whose_candidates_run_out_notifies_the_failure(db, two_users):
    req = new_request(db, user_id(db, "bob"))
    req.candidates_json = "[]"
    req.candidate_index = -1
    db.commit()
    assert svc.next_candidate(db, req.id, "nobody could send it") is False
    db.commit()
    [note] = db.scalars(select(Notification)).all()
    assert note.kind == "failed" and "nobody could send it" in note.detail


def test_the_bell_counts_my_unread_notifications_only(two_users, db):
    admin, bob, carol = two_users
    uid_bob, uid_carol = user_id(db, "bob"), user_id(db, "carol")
    for owner, n in ((uid_bob, 2), (uid_carol, 1)):
        for i in range(n):
            svc.set_status(db, new_request(db, owner, artist=f"{owner}{i}").id, "completed")
    db.commit()
    html = bob.get("/nav/notifications").text
    assert 'data-unread="2"' in html and "data-unread-count>2<" in html
    assert 'data-unread="1"' in carol.get("/nav/notifications").text
    assert "data-unread" not in admin.get("/nav/notifications").text  # nothing for the admin
    assert "every 30s" in html and 'hx-get="/nav/notifications"' in html
    assert 'id="nav-bell"' in bob.get("/dashboard").text  # every page has it
    anonymous = bob.__class__(bob.app, follow_redirects=False)
    assert anonymous.get("/nav/notifications").status_code == 303


def test_the_page_lists_the_notifications_and_links_to_the_request(two_users, db):
    admin, bob, carol = two_users
    req = new_request(db, user_id(db, "bob"), artist="Aretha Franklin", title="Lady Soul")
    svc.set_status(db, req.id, "completed")
    other = new_request(db, user_id(db, "carol"), artist="X", title="Secret")
    svc.set_status(db, other.id, "failed", error="x")
    db.commit()
    html = bob.get("/notifications").text
    assert "Your album is in the library" in html and "Aretha Franklin – Lady Soul" in html
    assert f'href="/requests/{req.id}"' in html and "Secret" not in html  # not carol's
    assert "data-unread" in html and ">new<" in html
    assert "Nothing new" in admin.get("/notifications").text


def test_marking_notifications_read_one_by_one_or_all_and_never_someone_elses(two_users, db):
    admin, bob, carol = two_users
    uid_bob, uid_carol = user_id(db, "bob"), user_id(db, "carol")
    for i in range(3):
        svc.set_status(db, new_request(db, uid_bob, artist=f"b{i}").id, "completed")
    svc.set_status(db, new_request(db, uid_carol, artist="c").id, "completed")
    db.commit()
    mine = db.scalars(select(Notification).where(Notification.user_id == uid_bob)).all()
    hers = db.scalar(select(Notification).where(Notification.user_id == uid_carol))
    r = post(bob, "/notifications/read", {"id": str(mine[0].id)}, page="/notifications")
    assert r.status_code == 303 and r.headers["location"] == "/notifications"
    assert notes.unread_count(db, uid_bob) == 2
    post(
        bob, "/notifications/read", {"id": str(hers.id)}, page="/notifications"
    )  # not hers to read
    db.expire_all()
    assert db.get(Notification, hers.id).read_at is None
    post(bob, "/notifications/read", {}, page="/notifications")  # all of bob's
    assert notes.unread_count(db, uid_bob) == 0 and notes.unread_count(db, uid_carol) == 1
    assert "data-read-all" not in bob.get("/notifications").text  # nothing left to mark
    assert admin_post_needs_token(bob)


def admin_post_needs_token(client):
    return client.post("/notifications/read", data={}).status_code == 403


def test_read_notifications_are_forgotten_after_a_month_and_unread_ones_are_kept(db, two_users):
    uid = user_id(db, "bob")
    for days_read, name in ((45, "old-read"), (5, "recent-read"), (None, "unread")):
        req = new_request(db, uid, artist=name)
        svc.set_status(db, req.id, "completed")
        db.flush()
        note = db.scalar(select(Notification).where(Notification.request_id == req.id))
        note.created_at = utcnow() - timedelta(days=60)
        note.read_at = utcnow() - timedelta(days=days_read) if days_read else None
    db.commit()
    assert notes.purge(db) == 1
    db.commit()
    assert sorted(n.album_label.split(" – ")[0] for n in db.scalars(select(Notification))) == [
        "recent-read",
        "unread",
    ]


def test_deleting_a_user_deletes_their_notifications(admin_client, db):
    add_user(admin_client, "gina")
    uid = user_id(db, "gina")
    svc.set_status(db, new_request(db, uid).id, "completed")
    db.commit()
    post(admin_client, f"/admin/users/{uid}/delete", {}, page="/settings/users")
    db.expire_all()
    assert db.query(Notification).count() == 0


def test_the_bell_and_the_audit_link_are_translated(two_users):
    admin, bob, _ = two_users
    post(bob, "/lang", {"lang": "it", "next": "/notifications"})
    assert "Notifiche" in bob.get("/notifications").text
    post(admin, "/lang", {"lang": "it", "next": "/admin/audit"})
    assert "Registro attività" in admin.get("/admin/audit").text
