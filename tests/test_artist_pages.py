import pytest
from sqlalchemy import select

from app.models import (
    MB_PENDING,
    MB_RESOLVED,
    MB_REVIEW,
    MB_UNMATCHED,
    AlbumMatchOverride,
    Artist,
    ArtistReview,
    Job,
    LibraryAlbum,
    ReleaseGroup,
    User,
    utcnow,
)
from app.services.musicbrainz import sync
from app.services.musicbrainz.matching import match_artist_albums
from app.services.musicbrainz.missing import (
    get_missing_types,
    missing_release_groups,
    set_missing_types,
)
from tests.conftest import MB_UA, add_user, login, post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.mbdata import ARETHA, NIRVANA_UK, NIRVANA_US


@pytest.fixture
def mb_on(app):
    app.state.settings.mb_user_agent = MB_UA


@pytest.fixture
def band(app, admin_client):
    """An identified artist with a small discography; one album owned, one not on MusicBrainz."""
    with app.state.session_factory() as db:
        artist = make_artist(db, "The Band", mbid=ARETHA, status=MB_RESOLVED)
        artist.metadata_synced_at = utcnow()
        rgs = {
            "first": make_rg(db, artist, "First Record", 1990),
            "second": make_rg(db, artist, "Second Record", 1992),
            "ep": make_rg(db, artist, "Little EP", 1993, "EP"),
            "single": make_rg(db, artist, "Hit Single", 1994, "Single"),
            "live": make_rg(db, artist, "Live at Home", 1995, "Album", ("Live",)),
        }
        first = make_album(db, artist, "First Record", year=1990)
        stray = make_album(db, artist, "Local Bootleg", year=1991, path="x/boot")
        match_artist_albums(db, artist.id)
        db.commit()
        return {
            "artist": artist.id,
            "rgs": {k: v.id for k, v in rgs.items()},
            "first": first.id,
            "stray": stray.id,
        }


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


# ------------------------------------------------------------------- list


def test_empty_artist_list(admin_client):
    assert "No artists found." in page(admin_client, "/admin/artists")


def test_list_shows_status_albums_and_missing_counts(admin_client, band):
    text = page(admin_client, "/admin/artists")
    assert "The Band" in text and "Identified" in text
    assert f"https://musicbrainz.org/artist/{ARETHA}" in text
    # default types album+ep: Second Record + Little EP are missing, First Record is owned
    assert 'tabular-nums">2</td>' in text
    assert "?1" in text  # one album without a MusicBrainz match


def test_list_filters(app, admin_client, band):
    with app.state.session_factory() as db:
        make_artist(db, "Zed Zeppelin", status=MB_PENDING)
        make_artist(db, "100% Pure", status=MB_UNMATCHED)
        make_artist(db, "Out Of Library", in_library=False)
    assert "Zed Zeppelin" in page(admin_client, "/admin/artists?q=zed")
    assert "The Band" not in page(admin_client, "/admin/artists?q=zed")
    assert "100% Pure" in page(admin_client, "/admin/artists?q=%25")  # a literal %, not a wildcard
    assert "Zed Zeppelin" not in page(admin_client, "/admin/artists?q=%25")
    assert "Zed Zeppelin" in page(admin_client, "/admin/artists?status=pending")
    assert "The Band" not in page(admin_client, "/admin/artists?status=pending")
    assert "The Band" in page(admin_client, "/admin/artists?attention=1")  # has an unmatched album
    assert "Zed Zeppelin" not in page(admin_client, "/admin/artists?attention=1")
    assert "Out Of Library" not in page(admin_client, "/admin/artists")
    assert "The Band" in page(admin_client, "/admin/artists?status=bogus")  # bad filter ignored


def test_list_pagination(app, admin_client):
    with app.state.session_factory() as db:
        for i in range(55):
            make_artist(db, f"Artist {i:03}")
    first = page(admin_client, "/admin/artists")
    assert "Page 1 of 2" in first and "Artist 000" in first and "Artist 054" not in first
    assert "Artist 054" in page(admin_client, "/admin/artists?page=2")


def test_review_queue_badge(app, admin_client):
    with app.state.session_factory() as db:
        artist = make_artist(db, "Nirvana", status=MB_PENDING)
        sync.open_review(db, artist.id, "ambiguous", [])
        db.commit()
    assert ">1</span>" in page(admin_client, "/admin/artists")


# ----------------------------------------------------------------- detail


def test_detail_shows_owned_albums_and_the_missing_list(admin_client, band):
    text = page(admin_client, f"/admin/artists/{band['artist']}")
    assert (
        "First Record" in text
        and "Matched" in text
        and "Local Bootleg" in text
        and "No match" in text
    )
    missing = text.split("Missing albums", 1)[1]
    assert "Second Record" in missing and "Little EP" in missing
    assert "Hit Single" not in missing and "Live at Home" not in missing  # types not selected
    assert "First Record" not in missing  # owned


def test_changing_the_shared_types_changes_the_missing_list_at_once(admin_client, band):
    url = f"/admin/artists/{band['artist']}"
    assert "Hit Single" not in page(admin_client, url).split("Missing albums", 1)[1]
    r = post(
        admin_client,
        "/admin/library/missing-types",
        {"types": ["single", "live"]},
        page="/admin/library",
    )
    assert r.status_code == 303
    missing = page(admin_client, url).split("Missing albums", 1)[1]
    assert "Hit Single" in missing and "Live at Home" in missing
    assert "Second Record" not in missing  # albums are no longer selected


def test_detail_shows_covers_when_they_exist(app, admin_client, band):
    with app.state.session_factory() as db:
        rg = db.get(ReleaseGroup, band["rgs"]["second"])
        rg.cover_status, rg.cover_path = "ok", f"{rg.mbid}.jpg"
        db.commit()
        name = rg.cover_path
    assert f'src="/covers/{name}"' in page(admin_client, f"/admin/artists/{band['artist']}")


def test_detail_warns_when_musicbrainz_is_not_configured(admin_client, band):
    assert "MB_USER_AGENT" in page(admin_client, f"/admin/artists/{band['artist']}")


def test_unknown_artist_is_404(admin_client):
    assert admin_client.get("/admin/artists/9999").status_code == 404
    assert post(admin_client, "/admin/artists/9999/sync").status_code == 404


# ------------------------------------------------------------------ actions


def test_refresh_queues_a_forced_sync(admin_client, band, mb_on, db):
    r = post(
        admin_client, f"/admin/artists/{band['artist']}/sync", {"force": "1"}, page="/admin/artists"
    )
    job = db.scalar(select(Job).where(Job.type == "sync_artist_metadata"))
    assert r.status_code == 303 and r.headers["location"] == f"/admin/jobs/{job.id}"
    assert job.payload_json == f'{{"artist_id": {band["artist"]}, "force": true}}'
    assert job.queue == "musicbrainz" and job.created_by is not None
    post(
        admin_client, f"/admin/artists/{band['artist']}/sync", {"force": "1"}, page="/admin/artists"
    )
    assert len(db.scalars(select(Job)).all()) == 1  # already queued


def test_sync_without_a_user_agent_is_refused(admin_client, band, db):
    r = post(admin_client, f"/admin/artists/{band['artist']}/sync", page="/admin/artists")
    assert r.status_code == 409
    assert db.scalar(select(Job)) is None


def test_actions_need_csrf(admin_client, band):
    for action in ("sync", "resolve", "dismiss", "reset"):
        assert admin_client.post(f"/admin/artists/{band['artist']}/{action}").status_code == 403


@pytest.mark.parametrize(
    "typed",
    [
        NIRVANA_US,
        NIRVANA_US.upper(),
        f"https://musicbrainz.org/artist/{NIRVANA_US}",
        f"  {NIRVANA_US}/details ",
    ],
)
def test_choosing_an_mbid_resolves_the_artist_and_queues_a_sync(
    app, admin_client, mb_on, db, typed
):
    with app.state.session_factory() as s:
        artist = make_artist(s, "Nirvana", status=MB_REVIEW)
        sync.open_review(s, artist.id, "ambiguous", [])
        s.commit()
        artist_id = artist.id
    r = post(admin_client, f"/admin/artists/{artist_id}/resolve", {"mbid": typed, "next": "review"})
    assert (
        r.status_code == 303
        and r.headers["location"] == "/admin/artists/review?flash=review_resolved"
    )
    db.expire_all()
    artist = db.get(Artist, artist_id)
    assert (artist.mbid, artist.mb_status, artist.mb_source) == (NIRVANA_US, MB_RESOLVED, "manual")
    assert db.scalar(select(ArtistReview.status)) == "resolved"
    assert db.scalar(select(Job).where(Job.type == "sync_artist_metadata")) is not None


@pytest.mark.parametrize("typed", ["", "not an id", "1234", "https://example.com/artist/abc"])
def test_an_invalid_mbid_is_rejected(app, admin_client, mb_on, typed):
    with app.state.session_factory() as s:
        artist = make_artist(s, "Nirvana", status=MB_REVIEW)
        artist_id = artist.id
    r = post(admin_client, f"/admin/artists/{artist_id}/resolve", {"mbid": typed})
    assert r.headers["location"] == f"/admin/artists/{artist_id}?error=mbid_invalid"
    assert "not a valid MusicBrainz ID" in admin_client.get(r.headers["location"]).text


def test_an_mbid_used_by_another_artist_is_refused(app, admin_client, mb_on):
    with app.state.session_factory() as s:
        make_artist(s, "Nirvana", mbid=NIRVANA_US, status=MB_RESOLVED)
        other = make_artist(s, "Nirvana Tribute", status=MB_REVIEW)
        other_id = other.id
    r = post(admin_client, f"/admin/artists/{other_id}/resolve", {"mbid": NIRVANA_US})
    assert r.headers["location"] == f"/admin/artists/{other_id}?error=mbid_in_use"


def test_dismissing_marks_the_artist_unmatched(app, admin_client, db):
    with app.state.session_factory() as s:
        artist = make_artist(s, "Obscure", status=MB_REVIEW)
        sync.open_review(s, artist.id, "no_match", [])
        s.commit()
        artist_id = artist.id
    r = post(admin_client, f"/admin/artists/{artist_id}/dismiss", {"next": "review"})
    assert r.headers["location"] == "/admin/artists/review?flash=artist_dismissed"
    db.expire_all()
    assert db.get(Artist, artist_id).mb_status == MB_UNMATCHED


def test_reset_forgets_the_identity_and_searches_again(admin_client, band, mb_on, db):
    r = post(admin_client, f"/admin/artists/{band['artist']}/reset")
    assert r.status_code == 303
    db.expire_all()
    artist = db.get(Artist, band["artist"])
    assert (artist.mbid, artist.mb_status) == (None, MB_PENDING)
    assert db.scalar(select(Job).where(Job.type == "sync_artist_metadata")).payload_json.endswith(
        "true}"
    )


# --------------------------------------------------------- review page


def test_review_page_lists_candidates(app, admin_client):
    with app.state.session_factory() as s:
        artist = make_artist(s, "Nirvana", status=MB_PENDING)
        sync.open_review(
            s,
            artist.id,
            "ambiguous",
            [
                {
                    "mbid": NIRVANA_US,
                    "name": "Nirvana",
                    "disambiguation": "US grunge band",
                    "country": "US",
                    "type": "Group",
                    "score": 100,
                    "similarity": 100,
                    "overlap": 3,
                },
                {
                    "mbid": NIRVANA_UK,
                    "name": "Nirvana",
                    "disambiguation": "60s band from the UK",
                    "country": "GB",
                    "type": "Group",
                    "score": 75,
                    "similarity": 100,
                    "overlap": 0,
                },
            ],
        )
        s.commit()
    text = page(admin_client, "/admin/artists/review")
    assert "Several artists share this name" in text and "US grunge band" in text
    assert f"https://musicbrainz.org/artist/{NIRVANA_UK}" in text
    assert text.count('value="' + NIRVANA_US + '"') == 1 and "Not on MusicBrainz" in text


def test_review_page_empty_state(admin_client):
    assert "Nothing to review." in page(admin_client, "/admin/artists/review")


def test_review_page_shows_form_errors(admin_client):
    assert "not a valid MusicBrainz ID" in page(
        admin_client, "/admin/artists/review?error=mbid_invalid"
    )
    assert "not a valid" not in page(admin_client, "/admin/artists/review?error=bogus")


# ------------------------------------------------------- manual album match


def override_of(db, artist_id):
    db.expire_all()
    return db.scalars(
        select(AlbumMatchOverride).where(AlbumMatchOverride.artist_id == artist_id)
    ).all()


def album(db, album_id):
    db.expire_all()
    return db.get(LibraryAlbum, album_id)


def test_choosing_a_release_group_for_an_album(admin_client, band, db):
    target = band["rgs"]["second"]
    r = post(
        admin_client,
        f"/admin/artists/{band['artist']}/albums/{band['stray']}/match",
        {"choice": str(target)},
    )
    assert r.headers["location"] == f"/admin/artists/{band['artist']}?flash=match_saved"
    stray = album(db, band["stray"])
    assert (stray.match_status, stray.release_group_id, stray.matched_confidence) == (
        "manual",
        target,
        1.0,
    )
    assert len(override_of(db, band["artist"])) == 1
    text = page(admin_client, f"/admin/artists/{band['artist']}")
    assert (
        "Manual" in text and "Second Record" not in text.split("Missing albums", 1)[1]
    )  # now owned


def test_declaring_an_album_not_on_musicbrainz(admin_client, band, db):
    post(
        admin_client,
        f"/admin/artists/{band['artist']}/albums/{band['first']}/match",
        {"choice": "none"},
    )
    first = album(db, band["first"])
    assert (first.match_status, first.release_group_id) == ("none", None)
    assert (
        "First Record"
        in page(admin_client, f"/admin/artists/{band['artist']}").split("Missing albums", 1)[1]
    )


def test_going_back_to_automatic_removes_the_override(admin_client, band, db):
    url = f"/admin/artists/{band['artist']}/albums/{band['first']}/match"
    post(admin_client, url, {"choice": "none"})
    post(admin_client, url, {"choice": "auto"})
    assert override_of(db, band["artist"]) == []
    assert album(db, band["first"]).match_status == "matched"
    post(admin_client, url, {"choice": "auto"})  # idempotent


def test_a_second_choice_replaces_the_first(admin_client, band, db):
    url = f"/admin/artists/{band['artist']}/albums/{band['stray']}/match"
    post(admin_client, url, {"choice": str(band["rgs"]["ep"])})
    post(admin_client, url, {"choice": str(band["rgs"]["second"])})
    [only] = override_of(db, band["artist"])
    assert only.release_group_id == band["rgs"]["second"]


@pytest.mark.parametrize("choice", ["abc", "999999", "-1"])
def test_invalid_choices_are_rejected(admin_client, band, db, choice):
    r = post(
        admin_client,
        f"/admin/artists/{band['artist']}/albums/{band['stray']}/match",
        {"choice": choice},
    )
    assert (
        r.status_code == 400
        and "does not belong" in r.text.replace("&#39;", "'")
        or r.status_code == 400
    )
    assert override_of(db, band["artist"]) == []


def test_release_groups_of_another_artist_are_rejected(app, admin_client, band, db):
    with app.state.session_factory() as s:
        other = make_artist(s, "Someone Else")
        foreign = make_rg(s, other, "Not Yours", 2000)
        foreign_id = foreign.id
    r = post(
        admin_client,
        f"/admin/artists/{band['artist']}/albums/{band['stray']}/match",
        {"choice": str(foreign_id)},
    )
    assert r.status_code == 400


def test_an_album_of_another_artist_is_not_found(app, admin_client, band):
    with app.state.session_factory() as s:
        other = make_artist(s, "Someone Else")
        other_id = other.id
    r = post(
        admin_client, f"/admin/artists/{other_id}/albums/{band['stray']}/match", {"choice": "none"}
    )
    assert r.status_code == 404


# --------------------------------------------------------------- preferences


def test_preferences_start_with_the_shared_default(admin_client):
    text = page(admin_client, "/account/preferences")
    assert "Use the shared default" in text and "Albums" in text and "EPs" in text
    assert 'value="album" checked' in text and 'value="live" checked' not in text


def test_a_user_can_choose_their_own_types(admin_client, user_client, db):
    r = post(
        user_client, "/account/preferences", {"mode": "custom", "types": ["live", "compilation"]}
    )
    assert (
        r.status_code == 303
        and r.headers["location"] == "/account/preferences?flash=preferences_saved"
    )
    text = page(user_client, "/account/preferences")
    assert 'value="live" checked' in text and 'value="album" checked' not in text
    assert "Preferences saved." in user_client.get(r.headers["location"]).text
    bob = db.scalar(select(User).where(User.username == "bob"))
    assert get_missing_types(db, bob.id) == ["compilation", "live"]
    assert get_missing_types(db) == ["album", "ep"]  # the shared default is untouched


def test_two_users_with_different_preferences_see_different_missing_albums(
    app, admin_client, user_client, band, db
):
    post(user_client, "/account/preferences", {"mode": "custom", "types": ["single"]})
    with app.state.session_factory() as s:
        bob = s.scalar(select(User).where(User.username == "bob")).id
        admin = s.scalar(select(User).where(User.username == "admin")).id
        assert [
            g.title for g in missing_release_groups(s, band["artist"], get_missing_types(s, bob))
        ] == ["Hit Single"]
        assert [
            g.title for g in missing_release_groups(s, band["artist"], get_missing_types(s, admin))
        ] == ["Second Record", "Little EP"]


def test_going_back_to_the_default(user_client, db):
    post(user_client, "/account/preferences", {"mode": "custom", "types": ["live"]})
    post(user_client, "/account/preferences", {"mode": "default"})
    assert "Use the shared default" in page(user_client, "/account/preferences")
    assert 'value="album" checked' in page(user_client, "/account/preferences")


def test_choosing_nothing_is_allowed(user_client):
    post(user_client, "/account/preferences", {"mode": "custom"})
    assert 'type="checkbox" name="types" value="album" checked' not in page(
        user_client, "/account/preferences"
    )


def test_unknown_types_are_dropped(admin_client, db):
    post(
        admin_client,
        "/admin/library/missing-types",
        {"types": ["live", "bogus"]},
        page="/admin/library",
    )
    assert get_missing_types(db) == ["live"]


def test_only_admins_set_the_shared_default(user_client):
    assert post(user_client, "/admin/library/missing-types", {"types": ["live"]}).status_code == 403


def test_the_library_page_shows_the_shared_choice(admin_client):
    post(admin_client, "/admin/library/missing-types", {"types": ["live"]}, page="/admin/library")
    text = page(admin_client, "/admin/library")
    assert "Missing album types" in text and 'value="live" checked' in text


def test_pages_in_italian(admin_client, band):
    post(admin_client, "/lang", {"lang": "it", "next": "/"})
    assert "Coda di revisione" in page(admin_client, "/admin/artists")
    assert "Album mancanti" in page(admin_client, f"/admin/artists/{band['artist']}")
    assert "Scelgo io" in page(admin_client, "/account/preferences")
    assert "Raccolte" in page(admin_client, "/account/preferences")


def test_menu_links(admin_client, user_client):
    assert 'href="/admin/artists"' in page(admin_client, "/library")
    assert 'href="/admin/artists"' not in page(user_client, "/library")
    assert 'href="/account/preferences"' in page(user_client, "/library")


def test_sync_button_exists_only_when_logged_in_as_admin(user_client, band):
    assert user_client.get(f"/admin/artists/{band['artist']}").status_code == 403
    assert user_client.get("/admin/artists/review").status_code == 403


def test_admins_see_each_others_decisions(app, admin_client, new_client, band, db):
    add_user(admin_client, "boss", role="admin")
    post(
        admin_client,
        f"/admin/artists/{band['artist']}/albums/{band['stray']}/match",
        {"choice": "none"},
    )
    other = new_client()
    login(other, "boss")
    assert "Not on MusicBrainz" in page(other, f"/admin/artists/{band['artist']}")
    set_missing_types(db, ["album"])
    db.commit()
