"""Migration 0016 brings existing installations to the standard quality profiles."""

import pytest
from sqlalchemy import select, text

from app.db import make_engine, make_session_factory, run_migrations
from app.models import Artist, LibraryAlbum, LibraryTrack, QualityProfile
from app.quality import SEED_PROFILES
from app.services.library.grouping import name_key


def seed_library(factory):
    """One album per case, each with the rates that sit on the moved thresholds."""
    with factory() as db:
        artist = Artist(name="A", sort_name="A", name_key=name_key("A"))
        db.add(artist)
        db.flush()
        ids = {}
        cases = {
            "master_176": [("flac_master", 176400, 24)],  # was Master, is Hi-Res now
            "master_192": [("flac_master", 192000, 24)],  # stays Master
            "hires_88": [("flac_hires", 88200, 24)],  # was Hi-Res, is Standard now
            "hires_96": [("flac_hires", 96000, 24)],  # stays Hi-Res
            "mixed": [("flac_hires", 88200, 24), ("flac_master", 192000, 24)],  # weakest wins
            "mp3": [("mp3_320", None, None)],
        }
        for name, tracks in cases.items():
            album = LibraryAlbum(
                artist_id=artist.id, path=name, title=name, quality_tier=tracks[0][0]
            )
            db.add(album)
            db.flush()
            ids[name] = album.id
            for i, (tier, rate, depth) in enumerate(tracks):
                db.add(
                    LibraryTrack(
                        album_id=album.id,
                        path=f"{name}/{i}.flac",
                        title=f"t{i}",
                        quality_tier=tier,
                        sample_rate=rate,
                        bit_depth=depth,
                        lossless=tier.startswith("flac"),
                    )
                )
        db.commit()
        return ids


def tiers(factory):
    with factory() as db:
        return {a.title: a.quality_tier for a in db.scalars(select(LibraryAlbum))}


def profiles(factory):
    with factory() as db:
        return {p.key: (p.name, p.min_sample_rate) for p in db.scalars(select(QualityProfile))}


@pytest.fixture
def old(settings):
    url = settings.database_url
    run_migrations(url, "0015")
    engine = make_engine(url)
    yield url, make_session_factory(engine)
    engine.dispose()


def test_unmodified_profiles_get_the_standard_names_and_thresholds(old):
    url, factory = old
    assert profiles(factory)["flac_hires"] == ("FLAC Hi-Res (24/96)", 88200)  # the old seed
    run_migrations(url)
    assert profiles(factory) == {p.key: (p.name, p.min_sample_rate) for p in SEED_PROFILES}
    assert profiles(factory)["flac_master"] == ("FLAC Master", 192000)
    assert profiles(factory)["any"] == ("Qualsiasi", None)


def test_tracks_and_albums_move_with_the_thresholds(old):
    url, factory = old
    seed_library(factory)
    run_migrations(url)
    assert tiers(factory) == {
        "master_176": "flac_hires",
        "master_192": "flac_master",
        "hires_88": "flac_standard",
        "hires_96": "flac_hires",
        "mixed": "flac_standard",  # an album is as good as its weakest track
        "mp3": "mp3_320",
    }


def test_a_profile_edited_in_the_settings_is_left_alone(old):
    url, factory = old
    with factory() as db:
        row = db.scalar(select(QualityProfile).where(QualityProfile.key == "flac_hires"))
        row.name, row.min_sample_rate = "My Hi-Res", 88200
        db.commit()
    seed_library(factory)
    run_migrations(url)
    found = profiles(factory)
    assert found["flac_hires"] == ("My Hi-Res", 88200)  # theirs
    assert found["flac_master"] == ("FLAC Master", 192000)  # the untouched ones follow
    done = tiers(factory)
    assert done["hires_88"] == "flac_hires"  # their threshold still counts 88.2 kHz as Hi-Res
    assert done["master_176"] == "flac_hires"  # Master moved to 192 kHz


def test_running_it_twice_changes_nothing_more(old):
    url, factory = old
    seed_library(factory)
    run_migrations(url)
    before = (profiles(factory), tiers(factory))
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0015'"))
    engine.dispose()
    run_migrations(url)
    assert (profiles(factory), tiers(factory)) == before


def test_the_migration_can_be_undone(old):
    from alembic import command
    from alembic.config import Config

    from app.db import PROJECT_ROOT

    url, factory = old
    run_migrations(url)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.downgrade(cfg, "0015")
    assert profiles(factory)["flac_hires"] == ("FLAC Hi-Res (24/96)", 88200)
    assert profiles(factory)["any"] == ("Qualsiasi / bassa", None)
