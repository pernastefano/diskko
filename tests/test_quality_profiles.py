"""Quality profiles: the service, the re-classification job and the settings page."""

import re

import pytest
from sqlalchemy import select

from app.jobs.worker import JobWorker
from app.models import (
    STATUS_SUCCEEDED,
    Artist,
    Job,
    LibraryAlbum,
    LibraryTrack,
    QualityProfile,
    Setting,
)
from app.quality import SEED_PROFILES, Profile, ProfileSet
from app.services import quality_profiles as svc
from tests.conftest import post
from tests.test_jobs_worker import enqueue, wait_for_status

LOSSLESS = {"name": "FLAC Plus", "kind": "lossless", "color": "teal"}


def form(**kw):
    base = {"name": "X", "kind": "any", "color": "slate"}
    return {**base, **kw}


def keys(db):
    return [p.key for p in svc.load_profiles(db).profiles]


# ------------------------------------------------------------------ the service


def test_the_migration_seeds_the_profiles(db):
    assert [p.key for p in svc.load_profiles(db).profiles] == [p.key for p in SEED_PROFILES]
    assert svc.default_key(db) == "flac_standard"
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "flac_hires"))
    assert (row.min_bit_depth, row.min_sample_rate, row.color) == (24, 88200, "purple")


def test_create_a_lossless_profile(db):
    data = svc.parse_form(form(**LOSSLESS, min_bit_depth="24", min_sample_rate="96"))
    row = svc.create_profile(db, data)
    db.commit()
    assert row.key == "flac_plus" and row.rank == 8  # added on top of the ladder
    assert (row.min_bit_depth, row.min_sample_rate, row.kind) == (24, 96000, "lossless")
    assert (
        svc.load_profiles(db).classify(
            lossless=True, bitrate_kbps=None, bit_depth=24, sample_rate=96000
        )
        == "flac_plus"
    )


def test_create_a_lossy_profile_with_v0(db):
    row = svc.create_profile(
        db,
        svc.parse_form(form(name="MP3 top", kind="lossy", min_bitrate="256", accept_v0="1")),
    )
    assert (row.min_bitrate, row.accept_v0, row.min_bit_depth) == (256, True, None)


def test_keys_are_stable_when_the_name_changes(db):
    row = svc.create_profile(db, svc.parse_form(form(name="First")))
    key = row.key
    svc.update_profile(db, row.id, svc.parse_form(form(name="Second", color="red")))
    db.commit()
    assert row.key == key and row.name == "Second" and row.color == "red"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"name": "  "}, "quality_name_required"),
        ({"name": "x" * 81}, "quality_name_invalid"),
        ({"name": "bad\x00name"}, "quality_name_invalid"),
        ({"kind": "video"}, "quality_kind"),
        ({"color": "pink"}, "quality_color"),
        ({"kind": "lossless", "min_bit_depth": "", "min_sample_rate": "44.1"}, "quality_number"),
        ({"kind": "lossless", "min_bit_depth": "0", "min_sample_rate": "44.1"}, "quality_number"),
        ({"kind": "lossless", "min_bit_depth": "16", "min_sample_rate": "abc"}, "quality_number"),
        ({"kind": "lossless", "min_bit_depth": "16", "min_sample_rate": "nan"}, "quality_number"),
        ({"kind": "lossless", "min_bit_depth": "99", "min_sample_rate": "44.1"}, "quality_number"),
        ({"kind": "lossy", "min_bitrate": "-5"}, "quality_number"),
        ({"kind": "lossy", "min_bitrate": "300"}, "quality_number"),  # not a standard value
        ({"kind": "lossless", "min_bit_depth": "17", "min_sample_rate": "44.1"}, "quality_number"),
        ({"kind": "lossless", "min_bit_depth": "16", "min_sample_rate": "45"}, "quality_number"),
        ({"kind": "lossy", "min_bitrate": ""}, "quality_number"),
    ],
)
def test_invalid_input_is_refused(changes, code):
    with pytest.raises(svc.ProfileError) as exc:
        svc.parse_form(form(**changes))
    assert exc.value.code == code


def test_names_must_be_unique_ignoring_case(db):
    with pytest.raises(svc.ProfileError) as exc:
        svc.create_profile(db, svc.parse_form(form(name="flac standard (16/44.1)")))
    assert exc.value.code == "quality_name_taken"
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "mp3_256"))
    svc.update_profile(db, row.id, svc.parse_form(form(name=row.name)))  # its own name is fine


def test_move_swaps_neighbours_and_stops_at_the_ends(db):
    ids = {p.key: p.id for p in db.scalars(select(QualityProfile))}
    svc.move_profile(db, ids["flac_standard"], "up")
    assert keys(db)[-3:] == ["flac_hires", "flac_standard", "flac_master"]
    svc.move_profile(db, ids["flac_master"], "up")  # already on top
    svc.move_profile(db, ids["any"], "down")  # already at the bottom
    assert keys(db)[0] == "any" and keys(db)[-1] == "flac_master"
    svc.move_profile(db, ids["flac_standard"], "down")
    assert keys(db)[-3:] == ["flac_standard", "flac_hires", "flac_master"]


def test_delete_rules(db):
    ids = {p.key: p.id for p in db.scalars(select(QualityProfile))}
    with pytest.raises(svc.ProfileError) as exc:
        svc.delete_profile(db, ids["flac_standard"])  # the default
    assert exc.value.code == "quality_delete_default"
    assert svc.delete_profile(db, ids["mp3_192"]) == "mp3_192"
    svc.set_default(db, "any")
    for key in [k for k in keys(db) if k != "any"]:
        svc.delete_profile(db, ids[key])
    with pytest.raises(svc.ProfileError) as exc:
        svc.delete_profile(db, ids["any"])
    assert exc.value.code == "quality_delete_default"
    with pytest.raises(svc.ProfileError):
        svc.set_default(db, "gone")


def test_the_last_profile_cannot_be_deleted(db):
    from app.services.app_settings import set_setting

    ids = {p.key: p.id for p in db.scalars(select(QualityProfile))}
    set_setting(db, svc.DEFAULT_KEY_SETTING, "ghost")  # a default that points nowhere
    for key in [k for k in keys(db) if k != "any"]:
        svc.delete_profile(db, ids[key])
    with pytest.raises(svc.ProfileError) as exc:
        svc.delete_profile(db, ids["any"])
    assert exc.value.code == "quality_delete_last"


def test_restore_defaults(db):
    ids = {p.key: p.id for p in db.scalars(select(QualityProfile))}
    svc.create_profile(db, svc.parse_form(form(name="Mine")))
    svc.set_default(db, "mp3_256")
    svc.delete_profile(db, ids["any"])
    svc.restore_defaults(db)
    db.commit()
    assert keys(db) == [p.key for p in SEED_PROFILES]
    assert svc.default_key(db) == "flac_standard"


# --------------------------------------------------------------- re-classifying


def add_album(db, artist, title, tracks):
    album = LibraryAlbum(
        artist_id=artist.id, source="filesystem", path=f"A/{title}", title=title, quality_tier="any"
    )
    db.add(album)
    db.flush()
    for i, (lossless, depth, rate, bitrate, v0) in enumerate(tracks):
        db.add(
            LibraryTrack(
                album_id=album.id,
                source="filesystem",
                path=f"A/{title}/{i}",
                title="t",
                lossless=lossless,
                bit_depth=depth,
                sample_rate=rate,
                bitrate=bitrate,
                v0=v0,
                quality_tier="any",
            )
        )
    return album


@pytest.fixture
def library(db):
    artist = Artist(name="A", sort_name="A", name_key="a", mb_status="pending", in_library=True)
    db.add(artist)
    db.flush()
    albums = {
        "cd": add_album(db, artist, "cd", [(True, 16, 44100, None, False)] * 2),
        "hires": add_album(db, artist, "hires", [(True, 24, 96000, None, False)]),
        "mixed": add_album(
            db, artist, "mixed", [(True, 24, 192000, None, False), (False, None, 44100, 256, False)]
        ),
        "v0": add_album(db, artist, "v0", [(False, None, 44100, 245, True)]),
    }
    db.commit()
    return albums


def tiers(db):
    db.expire_all()
    return {a.title: a.quality_tier for a in db.scalars(select(LibraryAlbum))}


def test_reclassify_sets_tracks_and_albums(db, library):
    changed = svc.reclassify(db, svc.load_profiles(db))
    db.commit()
    assert tiers(db) == {
        "cd": "flac_standard",
        "hires": "flac_hires",
        "mixed": "mp3_256",  # the weakest track
        "v0": "mp3_320",
    }
    assert changed == (6, 4)
    assert svc.reclassify(db, svc.load_profiles(db)) == (0, 0)  # nothing left to do


def test_reclassify_follows_the_profiles(db, library):
    # Hi-Res now needs 24-bit and 192 kHz; the 96 kHz album drops to Standard
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "flac_hires"))
    row.min_sample_rate = 192000
    db.commit()
    svc.reclassify(db, svc.load_profiles(db))
    db.commit()
    assert tiers(db)["hires"] == "flac_standard"


def test_reclassify_with_a_single_profile(db, library):
    only = ProfileSet([Profile("only", "Only", "any", 1)])
    svc.reclassify(db, only)
    db.commit()
    assert set(tiers(db).values()) == {"only"}


async def test_the_job_recomputes_the_library(settings, session_factory):
    with session_factory() as db:
        artist = Artist(name="A", sort_name="A", name_key="a", mb_status="pending", in_library=True)
        db.add(artist)
        db.flush()
        add_album(db, artist, "x", [(True, 24, 192000, None, False)])
        db.commit()
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "reclassify_quality", {})
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    assert job.queue == "library"
    with session_factory() as db:
        assert db.scalar(select(LibraryAlbum.quality_tier)) == "flac_master"


# ------------------------------------------------------------------- the page


def page(client, path="/settings/quality"):
    r = client.get(path)
    assert r.status_code == 200, r.status_code
    return r.text


def rows(text):
    return re.findall(r'data-profile="([a-z0-9_]+)"', text)


def go(client, path, data=None):
    return post(client, path, data or {}, page="/settings/quality")


def test_the_page_lists_the_profiles_best_first(admin_client):
    text = page(admin_client)
    assert rows(text) == [
        "flac_master",
        "flac_hires",
        "flac_standard",
        "mp3_320",
        "mp3_256",
        "mp3_192",
        "any",
    ]
    assert "FLAC Standard (16/44.1)" in text and "Qualsiasi / bassa" in text
    assert text.count("data-default") == 1
    assert "Lossless, at least 24-bit and 176.4 kHz" in text
    assert "At least 320 kbps, V0 or lossless" in text


def test_the_navigation_has_the_quality_section(admin_client):
    text = page(admin_client, "/settings/library")
    assert 'href="/settings/quality"' in text and ">Quality<" in text
    assert ">Albums<" in page(admin_client, "/settings/albums")


def test_adding_a_profile(app, admin_client, db):
    r = go(
        admin_client,
        "/settings/quality/profiles",
        {
            "name": "FLAC Plus",
            "kind": "lossless",
            "color": "teal",
            "min_bit_depth": "24",
            "min_sample_rate": "96",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings/quality?flash=quality_saved"
    assert rows(page(admin_client))[0] == "flac_plus"
    assert [p.key for p in app.state.quality_profiles.profiles][-1] == "flac_plus"
    job = db.scalar(select(Job).where(Job.type == "reclassify_quality"))
    assert job is not None and job.queue == "library"


def test_the_reclassify_job_is_not_duplicated(admin_client, db):
    go(admin_client, "/settings/quality/profiles", form(name="One"))
    go(admin_client, "/settings/quality/profiles", form(name="Two"))
    assert len(db.scalars(select(Job).where(Job.type == "reclassify_quality")).all()) == 1
    assert "data-recomputing" in page(admin_client)


def test_a_failed_add_reopens_the_dialog_with_the_error(admin_client, db):
    r = go(
        admin_client,
        "/settings/quality/profiles",
        {"name": "Broken", "kind": "lossy", "color": "slate", "min_bitrate": "abc"},
    )
    assert r.status_code == 400
    assert "data-modal-error" in r.text and "Enter a valid number" in r.text
    assert 'value="Broken"' in r.text and "open: true" in r.text
    assert "Broken" not in keys(db)
    assert db.scalar(select(QualityProfile).where(QualityProfile.name == "Broken")) is None


def test_a_taken_name_is_reported_in_the_dialog(admin_client):
    r = go(admin_client, "/settings/quality/profiles", form(name="MP3 ≥ 192"))
    assert r.status_code == 400 and "already has this name" in r.text


def test_editing_a_profile(app, admin_client, db):
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "mp3_256"))
    r = go(
        admin_client,
        f"/settings/quality/profiles/{row.id}",
        {"name": "MP3 good", "kind": "lossy", "color": "indigo", "min_bitrate": "224"},
    )
    assert r.status_code == 303
    db.expire_all()
    assert (row.name, row.min_bitrate, row.color, row.key) == ("MP3 good", 224, "indigo", "mp3_256")
    assert app.state.quality_profiles.name("mp3_256") == "MP3 good"
    bad = go(admin_client, f"/settings/quality/profiles/{row.id}", form(name="", kind="any"))
    assert bad.status_code == 400 and "data-modal-error" in bad.text
    missing = go(admin_client, "/settings/quality/profiles/9999", form())
    assert missing.status_code == 303 and "error=quality_not_found" in missing.headers["location"]


def test_moving_a_profile(admin_client, db):
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "any"))
    assert (
        go(
            admin_client, f"/settings/quality/profiles/{row.id}/move", {"direction": "up"}
        ).status_code
        == 303
    )
    assert rows(page(admin_client))[-2:] == ["any", "mp3_192"]
    r = go(admin_client, f"/settings/quality/profiles/{row.id}/move", {"direction": "sideways"})
    assert r.status_code == 404


def test_the_default_profile_cannot_be_deleted(admin_client, db):
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "flac_standard"))
    r = go(admin_client, f"/settings/quality/profiles/{row.id}/delete")
    assert (
        r.status_code == 303
        and r.headers["location"] == "/settings/quality?error=quality_delete_default"
    )
    text = page(admin_client, r.headers["location"])
    assert "default profile cannot be deleted" in text and "flac_standard" in rows(text)


def test_deleting_and_changing_the_default(app, admin_client, db):
    go(admin_client, "/settings/quality/default", {"key": "mp3_320"})
    assert app.state.settings.default_quality == "mp3_320"
    assert db.scalar(select(Setting).where(Setting.key == "config.default_quality")) is not None
    assert db.scalar(select(Job).where(Job.type == "reclassify_quality")) is None  # not needed
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "flac_standard"))
    assert go(admin_client, f"/settings/quality/profiles/{row.id}/delete").status_code == 303
    assert "flac_standard" not in rows(page(admin_client))
    bad = go(admin_client, "/settings/quality/default", {"key": "nope"})
    assert "error=quality_not_found" in bad.headers["location"]


def test_restoring_the_initial_profiles(app, admin_client):
    go(admin_client, "/settings/quality/profiles", form(name="Mine"))
    go(admin_client, "/settings/quality/default", {"key": "mine"})
    go(admin_client, "/settings/quality/restore")
    assert rows(page(admin_client))[0] == "flac_master" and "mine" not in rows(page(admin_client))
    assert app.state.settings.default_quality == "flac_standard"


def test_the_page_is_in_italian(admin_client):
    post(admin_client, "/lang", {"lang": "it", "next": "/dashboard"})
    text = page(admin_client)
    assert "Profili di qualità" in text and "Aggiungi profilo" in text
    assert "Ripristina i profili iniziali" in text


def test_badges_use_the_profile_name_and_colour(app, admin_client, db):
    row = db.scalar(select(QualityProfile).where(QualityProfile.key == "mp3_256"))
    go(
        admin_client,
        f"/settings/quality/profiles/{row.id}",
        {"name": "Renamed", "kind": "lossy", "color": "fuchsia", "min_bitrate": "256"},
    )
    from jinja2 import Environment

    from app.web.templating import templates

    html = templates.env.get_template("_quality_badge.html").render(
        quality_profiles=app.state.quality_profiles, tier="mp3_256"
    )
    assert "Renamed" in html and "bg-fuchsia-100" in html
    gone = templates.env.get_template("_quality_badge.html").render(
        quality_profiles=app.state.quality_profiles, tier="deleted_one"
    )
    assert "deleted_one" in gone and "bg-slate-100" in gone and Environment  # grey, with the key


# ------------------------------------------------------------------ passwords


def test_eight_characters_are_enough_for_a_password(settings, admin_client, db):
    assert settings.min_password_length == 8
    ok = post(
        admin_client,
        "/admin/users",
        {"username": "eight", "password": "12345678", "role": "user"},
        page="/settings/users",
    )
    assert ok.status_code == 303
    short = post(
        admin_client,
        "/admin/users",
        {"username": "seven", "password": "1234567", "role": "user"},
        page="/settings/users",
    )
    assert short.status_code in (303, 400, 409)
    from app.models import User

    assert db.scalar(select(User).where(User.username == "seven")) is None
    assert db.scalar(select(User).where(User.username == "eight")) is not None
    assert "At least 8 characters" in page(admin_client, "/settings/users")


def test_the_dialog_offers_standard_values_in_comboboxes(admin_client):
    text = page(admin_client)
    for field in ("min_bit_depth", "min_sample_rate", "min_bitrate"):
        assert (
            f'<select id="profile-{ {"min_bit_depth": "bits", "min_sample_rate": "rate", "min_bitrate": "bitrate"}[field] }" name="{field}"'
            in text
        )
    assert not re.search(r'<input[^>]*name="min_(bit_depth|sample_rate|bitrate)"', text)
    assert '<option value="44.1" selected>44.1 kHz</option>' in text
    assert '<option value="176.4"' in text and '<option value="24"' in text
    assert '<option value="320" selected>320 kbps</option>' in text
