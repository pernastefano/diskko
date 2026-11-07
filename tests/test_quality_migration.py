"""Migration 0007 creates the quality profiles and re-classifies the existing library."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import PROJECT_ROOT, make_engine, run_migrations


@pytest.fixture
def old_db(settings):
    url = settings.database_url
    run_migrations(url, "0006")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artists (id, name, sort_name, name_key, mb_status, in_library, created_at, updated_at)"
                " VALUES (1, 'A', 'A', 'a', 'pending', 1, '2026-01-01', '2026-01-01')"
            )
        )
    yield url, engine
    engine.dispose()


def add_album(conn, album_id, title, tracks, tier="flac_standard"):
    conn.execute(
        text(
            "INSERT INTO library_albums (id, artist_id, source, path, title, quality_tier, track_count,"
            " disc_count, total_size, is_compilation, scanned_at, match_status)"
            " VALUES (:id, 1, 'filesystem', :path, :title, :tier, :n, 1, 0, 0, '2026-01-01', 'unmatched')"
        ),
        {"id": album_id, "path": f"A/{title}", "title": title, "n": len(tracks), "tier": tier},
    )
    for i, (lossless, depth, rate, bitrate, old_tier) in enumerate(tracks):
        conn.execute(
            text(
                "INSERT INTO library_tracks (album_id, source, path, file_size, file_mtime_ns, title, lossless,"
                " bit_depth, sample_rate, bitrate, quality_tier, compilation)"
                " VALUES (:a, 'filesystem', :path, 1, 1, 't', :ll, :bd, :sr, :br, :qt, 0)"
            ),
            {
                "a": album_id,
                "path": f"A/{title}/{i}.flac",
                "ll": lossless,
                "bd": depth,
                "sr": rate,
                "br": bitrate,
                "qt": old_tier,
            },
        )


def column(engine, sql):
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(sql))]


def test_the_seven_initial_profiles_are_created(old_db):
    url, engine = old_db
    run_migrations(url)
    assert column(engine, "SELECT key FROM quality_profiles ORDER BY rank") == [
        "any",
        "mp3_192",
        "mp3_256",
        "mp3_320",
        "flac_standard",
        "flac_hires",
        "flac_master",
    ]
    assert column(engine, "SELECT name FROM quality_profiles ORDER BY rank DESC")[:3] == [
        "FLAC Master (24/192)",
        "FLAC Hi-Res (24/96)",
        "FLAC Standard (16/44.1)",
    ]
    assert column(engine, "SELECT accept_v0 FROM quality_profiles ORDER BY rank") == [
        0,
        0,
        0,
        1,
        0,
        0,
        0,
    ]


def test_tracks_are_reclassified_from_their_raw_fields(old_db):
    url, engine = old_db
    with engine.begin() as conn:
        add_album(conn, 1, "cd", [(1, 16, 44100, None, "flac_standard")])
        add_album(conn, 2, "studio", [(1, 24, 44100, None, "flac_studio")], "flac_studio")
        add_album(conn, 3, "hires", [(1, 24, 96000, None, "flac_hires")], "flac_hires")
        add_album(conn, 4, "master", [(1, 24, 192000, None, "flac_master")], "flac_master")
        add_album(conn, 5, "unknown", [(1, None, None, None, "flac_standard")])
    run_migrations(url)
    assert column(engine, "SELECT quality_tier FROM library_tracks ORDER BY id") == [
        "flac_standard",
        "flac_standard",  # the old Studio level folds into Standard
        "flac_hires",
        "flac_master",
        "flac_standard",
    ]
    assert column(engine, "SELECT quality_tier FROM library_albums ORDER BY id") == [
        "flac_standard",
        "flac_standard",
        "flac_hires",
        "flac_master",
        "flac_standard",
    ]


def test_an_album_takes_its_weakest_track(old_db):
    url, engine = old_db
    with engine.begin() as conn:
        add_album(conn, 1, "mixed", [(1, 24, 96000, None, "x"), (1, 16, 44100, None, "x")])
        add_album(conn, 2, "mp3", [(0, None, 44100, 320, "x"), (0, None, 44100, 192, "x")])
        add_album(
            conn, 3, "lossy+lossless", [(1, 24, 96000, None, "x"), (0, None, 44100, 256, "x")]
        )
        add_album(conn, 4, "empty", [], "lossless")
        add_album(conn, 5, "empty-unknown", [], "mystery")
    run_migrations(url)
    assert column(engine, "SELECT quality_tier FROM library_albums ORDER BY id") == [
        "flac_standard",
        "mp3_192",
        "mp3_256",
        "flac_standard",  # old name of an album without tracks
        "any",
    ]


def test_old_mp3_320_files_below_320_are_remembered_as_v0(old_db):
    url, engine = old_db
    with engine.begin() as conn:
        add_album(
            conn,
            1,
            "v0",
            [
                (0, None, 44100, 245, "mp3_320"),
                (0, None, 44100, 320, "mp3_320"),
                (0, None, 44100, 192, "mp3_192"),
            ],
        )
    run_migrations(url)
    assert column(engine, "SELECT v0 FROM library_tracks ORDER BY id") == [1, 0, 0]
    assert column(engine, "SELECT quality_tier FROM library_tracks ORDER BY id") == [
        "mp3_320",
        "mp3_320",
        "mp3_192",
    ]


def test_a_saved_default_that_no_longer_exists_falls_back(old_db):
    url, engine = old_db
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO settings (key, value_json, scope, user_id) "
                "VALUES ('config.default_quality', '\"flac_studio\"', 'global', NULL)"
            )
        )
    run_migrations(url)
    assert column(
        engine, "SELECT value_json FROM settings WHERE key = 'config.default_quality'"
    ) == ['"flac_standard"']


def test_the_migration_can_be_undone(old_db):
    url, engine = old_db
    with engine.begin() as conn:
        add_album(conn, 1, "hires", [(1, 24, 96000, None, "flac_hires")], "flac_hires")
    run_migrations(url)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "0006")
    assert column(engine, "SELECT quality_tier FROM library_albums") == ["flac_hires"]
    assert column(engine, "SELECT name FROM sqlite_master WHERE name = 'quality_profiles'") == []
