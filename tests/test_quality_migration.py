"""Migration 0005 re-classifies existing lossless tracks from their stored resolution."""

import pytest
from sqlalchemy import text

from app.db import make_engine, run_migrations


@pytest.fixture
def old_db(settings):
    url = settings.database_url
    run_migrations(url, "0004")
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


def add_album(conn, album_id, title, tracks):
    conn.execute(
        text(
            "INSERT INTO library_albums (id, artist_id, source, path, title, quality_tier, track_count,"
            " disc_count, total_size, is_compilation, scanned_at, match_status)"
            " VALUES (:id, 1, 'filesystem', :path, :title, 'lossless', :n, 1, 0, 0, '2026-01-01', 'unmatched')"
        ),
        {"id": album_id, "path": f"A/{title}", "title": title, "n": len(tracks)},
    )
    for i, (lossless, depth, rate, old_tier) in enumerate(tracks):
        conn.execute(
            text(
                "INSERT INTO library_tracks (album_id, source, path, file_size, file_mtime_ns, title, lossless,"
                " bit_depth, sample_rate, quality_tier, compilation)"
                " VALUES (:a, 'filesystem', :path, 1, 1, 't', :ll, :bd, :sr, :qt, 0)"
            ),
            {
                "a": album_id,
                "path": f"A/{title}/{i}.flac",
                "ll": lossless,
                "bd": depth,
                "sr": rate,
                "qt": old_tier,
            },
        )


def tiers(engine, sql):
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(sql))]


def test_existing_tracks_are_reclassified_by_resolution(old_db):
    url, engine = old_db
    with engine.begin() as conn:
        add_album(conn, 1, "cd", [(1, 16, 44100, "lossless")])
        add_album(conn, 2, "studio", [(1, 24, 44100, "lossless_24"), (1, 16, 48000, "lossless")])
        add_album(conn, 3, "hires", [(1, 24, 96000, "lossless_24")])
        add_album(conn, 4, "master", [(1, 24, 192000, "lossless_24")])
        add_album(conn, 5, "unknown", [(1, None, None, "lossless")])
    run_migrations(url)
    assert tiers(engine, "SELECT quality_tier FROM library_tracks ORDER BY id") == [
        "flac_standard",
        "flac_studio",
        "flac_studio",
        "flac_hires",
        "flac_master",
        "flac_standard",
    ]
    assert tiers(engine, "SELECT quality_tier FROM library_albums ORDER BY id") == [
        "flac_standard",
        "flac_studio",
        "flac_hires",
        "flac_master",
        "flac_standard",
    ]


def test_an_album_takes_its_weakest_track_and_lossy_albums_are_untouched(old_db):
    url, engine = old_db
    with engine.begin() as conn:
        add_album(conn, 1, "mixed", [(1, 24, 96000, "lossless_24"), (1, 16, 44100, "lossless")])
        add_album(conn, 2, "mp3", [(0, None, 44100, "mp3_320"), (0, None, 44100, "mp3_192")])
        add_album(
            conn, 3, "lossy+lossless", [(1, 24, 96000, "lossless_24"), (0, None, 44100, "mp3_256")]
        )
        add_album(conn, 4, "empty", [])
    run_migrations(url)
    assert tiers(engine, "SELECT quality_tier FROM library_albums ORDER BY id") == [
        "flac_standard",
        "mp3_192",
        "mp3_256",
        "lossless",
    ]  # the empty album is left as it was


def test_the_migration_can_be_undone(old_db):
    from alembic import command
    from alembic.config import Config

    url, engine = old_db
    with engine.begin() as conn:
        add_album(conn, 1, "hires", [(1, 24, 96000, "lossless_24")])
    run_migrations(url)
    from app.db import PROJECT_ROOT

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "0004")
    assert tiers(engine, "SELECT quality_tier FROM library_albums") == ["lossless_24"]
    assert tiers(engine, "SELECT quality_tier FROM library_tracks") == ["lossless_24"]
