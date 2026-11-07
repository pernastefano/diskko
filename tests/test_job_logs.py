"""Job labels, the search in the logs, the routine filter and the retention of old jobs."""

from datetime import timedelta

from markupsafe import Markup

from app.jobs import engine
from app.jobs.labels import job_label
from app.models import STATUS_FAILED, STATUS_SUCCEEDED, utcnow
from app.web.templating import mark
from tests.dbhelpers import make_artist, make_rg


def add(db, type_="test.steps", status=STATUS_SUCCEEDED, age_days=0.0, label=None, lines=()):
    job = engine.enqueue(db, type_, {}, label=label)
    job.status = status
    job.finished_at = utcnow() - timedelta(days=age_days)
    for line in lines:
        engine.add_log(db, job.id, "info", line)
    db.commit()
    return job


# ----------------------------------------------------------------------------- labels


def test_a_job_is_labelled_with_what_it_works_on(db):
    artist = make_artist(db, "Aretha Franklin")
    group = make_rg(db, artist, "Lady Soul", 1968)
    db.commit()
    assert job_label(db, "fetch_covers", {"artist_id": artist.id}) == "Aretha Franklin"
    assert job_label(db, "sync_artist_metadata", {"artist_id": artist.id}) == "Aretha Franklin"
    assert (
        job_label(db, "fetch_tracklist", {"release_group_id": group.id})
        == "Aretha Franklin – Lady Soul"
    )
    assert job_label(db, "search_artists", {"query": "aretha"}) == "“aretha”"
    assert job_label(db, "integration_check", {"name": "plex"}) == "plex"
    assert job_label(db, "scan_library", {"force": True}) is None
    assert job_label(db, "fetch_covers", {"artist_id": 9999}) is None
    assert job_label(db, "fetch_covers", {"artist_id": "not a number"}) is None


def test_a_request_job_is_labelled_with_the_album_and_marks_upgrades(db):
    from app.models import User
    from app.services import downloads as svc

    user = User(username="u", password_hash="x", role="user")
    db.add(user)
    group = make_rg(db, make_artist(db, "Aretha Franklin"), "Lady Soul", 1968)
    request, _ = svc.create_request(db, user.id, group.id)
    db.commit()
    assert (
        job_label(db, "slskd_search", {"request_id": request.id}) == "Aretha Franklin – Lady Soul"
    )
    request.upgrade_from = "mp3_320"
    assert job_label(db, "slskd_search", {"request_id": request.id}).endswith("(upgrade)")


def test_queued_jobs_get_their_label_and_an_explicit_one_wins(db):
    artist = make_artist(db, "Aretha Franklin")
    db.commit()
    assert engine.enqueue(db, "fetch_covers", {"artist_id": artist.id}).label == "Aretha Franklin"
    assert (
        engine.enqueue(db, "fetch_covers", {"artist_id": artist.id}, label="Mine").label == "Mine"
    )
    assert engine.enqueue(db, "scan_library", {}).label is None


def test_the_label_is_shown_in_the_list_and_the_detail(admin_client, db):
    job = add(db, label="Aretha Franklin")
    assert "data-job-label" in admin_client.get("/admin/jobs").text
    assert "Aretha Franklin" in admin_client.get("/admin/jobs").text
    assert admin_client.get(f"/admin/jobs/{job.id}").status_code == 200


# ----------------------------------------------------------------------------- search


def test_the_search_finds_log_lines_labels_and_types(db):
    a = add(db, lines=["Cover of Lady Soul saved"])
    b = add(db, label="Aretha Franklin")
    c = add(db, type_="fetch_covers")
    add(db, lines=["nothing relevant"])
    found = lambda text: {j.id for j in engine.list_jobs(db, search=text)[0]}  # noqa: E731
    assert found("lady soul") == {a.id}  # a log line, any case
    assert found("ARETHA") == {b.id}  # the label
    assert found("fetch_cov") == {c.id}  # the type
    assert found("zzzz") == set()
    assert engine.list_jobs(db, search="lady soul")[1] == 1


def test_short_searches_are_ignored(db):
    add(db, lines=["ab"])
    add(db)
    assert engine.list_jobs(db, search="ab")[1] == 2 and engine.list_jobs(db, search=" ")[1] == 2


def test_wildcards_in_the_search_are_plain_characters(db):
    add(db, lines=["100% done"])
    add(db, lines=["under_score"])
    add(db, lines=["something else"])
    assert engine.list_jobs(db, search="100%")[1] == 1
    assert engine.list_jobs(db, search="u_d")[1] == 0  # as a wildcard it would match "und"
    assert engine.list_jobs(db, search="er_sc")[1] == 1


def test_the_search_combines_with_the_other_filters(db):
    add(db, type_="fetch_covers", lines=["Cover of Lady Soul"])
    add(db, type_="fetch_tracklist", lines=["Tracks of Lady Soul"], status=STATUS_FAILED)
    assert engine.list_jobs(db, search="lady soul", type="fetch_covers")[1] == 1
    assert engine.list_jobs(db, search="lady soul", status=STATUS_FAILED)[1] == 1
    assert engine.list_jobs(db, search="lady soul")[1] == 2


def test_matching_lines_are_returned_per_job(db):
    j = add(
        db,
        lines=[
            "first match here",
            "other",
            "second match here",
            "third match here",
            "fourth match here",
        ],
    )
    found = engine.matching_lines(db, [j.id], "match here", per_job=3)
    assert found == {j.id: ["first match here", "second match here", "third match here"]}
    assert (
        engine.matching_lines(db, [j.id], "ab") == {}
        and engine.matching_lines(db, [], "match") == {}
    )


def test_the_page_shows_the_matches_highlighted(admin_client, db):
    add(db, lines=["Cover of <b>Lady Soul</b> saved"])
    text = admin_client.get("/admin/jobs?q=lady+soul").text
    assert "<mark>Lady Soul</mark>" in text and "<b>" not in text  # escaped, then highlighted
    assert "data-job-match" in text and "data-search-info" in text
    assert "1 job(s) with “lady soul”" in text


def test_the_live_rows_keep_the_search(admin_client, db):
    add(db, lines=["needle in a haystack"])
    add(db, lines=["hay only"])
    fragment = admin_client.get("/admin/jobs/rows?q=needle").text
    assert (
        "<html" not in fragment and fragment.count("<tr") == 1 and "<mark>needle</mark>" in fragment
    )
    assert "/admin/jobs/rows?q=needle" in admin_client.get("/admin/jobs?q=needle").text


def test_a_too_short_search_says_so(admin_client, db):
    add(db)
    text = admin_client.get("/admin/jobs?q=ab").text
    assert "data-search-short" in text and "at least 3 characters" in text


def test_the_search_box_is_on_the_page(admin_client):
    text = admin_client.get("/admin/jobs").text
    assert 'name="q"' in text and "Search the logs" in text


def test_the_highlighter_escapes_everything():
    out = mark("a <script>x</script> b", "script")
    assert isinstance(out, Markup) and "<script>" not in out and "<mark>script</mark>" in out
    assert str(mark("plain & text", "zz")) == "plain &amp; text"
    assert str(mark("keep", "")) == "keep"
    assert str(mark("Abc abc", "abc")) == "<mark>Abc</mark> <mark>abc</mark>"


# ---------------------------------------------------------------------- routine jobs


def test_routine_jobs_are_hidden_unless_asked_for(db, admin_client):
    add(db, type_="check_integrations")
    add(db, type_="housekeeping")
    keep = add(db, type_="fetch_covers")
    assert [j.id for j in engine.list_jobs(db, routine=False)[0]] == [keep.id]
    assert engine.list_jobs(db, routine=True)[1] == 3
    assert (
        engine.list_jobs(db, routine=False, type="check_integrations")[1] == 1
    )  # asked for by type
    assert 'name="routine"' in admin_client.get("/admin/jobs").text
    shown = admin_client.get("/admin/jobs?routine=1").text
    assert shown.count("check_integrations") >= 1 and shown.count("housekeeping") >= 1
    hidden = admin_client.get("/admin/jobs").text
    assert "check_integrations</div>" not in hidden
