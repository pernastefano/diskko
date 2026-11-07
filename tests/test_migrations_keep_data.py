"""Upgrading an existing installation must never lose data.

SQLite alters tables by recreating them; with foreign keys enabled that cascade-deletes the
rows pointing to the recreated table. The migrations run with foreign keys off to prevent it.
"""

from sqlalchemy import text

from app.db import make_engine, run_migrations


def seed(conn):
    conn.execute(
        text(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at)"
            " VALUES (1, 'ann', 'x', 'admin', 1, '2026-01-01')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen, expires_at)"
            " VALUES ('h', 1, 'c', '2026-01-01', '2026-01-01', '2099-01-01')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO settings (key, value_json, scope, user_id) VALUES"
            " ('lang_note', '1', 'user', 1), ('global_note', '2', 'global', NULL)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO jobs (type, queue, payload_json, status, progress_pct, created_by, created_at, attempts,"
            " max_attempts, cancel_requested) VALUES ('housekeeping', 'default', '{}', 'queued', 0, 1, '2026-01-01', 0, 3, 0)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO artists (id, name, sort_name, name_key, in_library, created_at, updated_at)"
            " VALUES (1, 'A', 'A', 'a', 1, '2026-01-01', '2026-01-01')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO library_albums (id, artist_id, source, path, title, quality_tier, track_count, disc_count,"
            " total_size, is_compilation, scanned_at) VALUES (1, 1, 'filesystem', 'A/B', 'B', 'lossless', 1, 1, 0, 0,"
            " '2026-01-01')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO library_tracks (album_id, source, path, file_size, file_mtime_ns, title, lossless,"
            " bit_depth, sample_rate, quality_tier, compilation) VALUES (1, 'filesystem', 'A/B/1.flac', 1, 1, 't', 1,"
            " 24, 96000, 'lossless_24', 0)"
        )
    )


COUNTS = ("users", "sessions", "settings", "jobs", "artists", "library_albums", "library_tracks")


def counts(engine):
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNTS}


def test_upgrading_from_the_library_phase_keeps_everything(settings):
    url = settings.database_url
    run_migrations(url, "0003")  # the schema of the first release with a library
    engine = make_engine(url)
    with engine.begin() as conn:
        seed(conn)
    before = counts(engine)
    assert before == {
        "users": 1,
        "sessions": 1,
        "settings": 2,
        "jobs": 1,
        "artists": 1,
        "library_albums": 1,
        "library_tracks": 1,
    }
    run_migrations(url)  # 0004 (MusicBrainz), 0005 (quality), 0006 (theme)
    assert counts(engine) == before  # nothing was cascaded away
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT quality_tier FROM library_albums")).scalar() == "flac_hires"
        )
        assert conn.execute(text("SELECT mb_status FROM artists")).scalar() == "pending"
        assert conn.execute(text("SELECT match_status FROM library_albums")).scalar() == "unmatched"
        assert conn.execute(text("SELECT theme FROM users")).scalar() == "auto"
        assert conn.execute(text("SELECT created_by FROM jobs")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM sessions WHERE user_id = 1")).scalar() == 1
    engine.dispose()


def test_upgrading_one_step_at_a_time_keeps_everything(settings):
    url = settings.database_url
    run_migrations(url, "0003")
    engine = make_engine(url)
    with engine.begin() as conn:
        seed(conn)
    before = counts(engine)
    for revision in ("0004", "0005", "0006"):
        run_migrations(url, revision)
        assert counts(engine) == before, revision
    engine.dispose()


def test_the_application_still_enforces_foreign_keys(app, client):
    """Only the migrations turn them off: the running app keeps cascading deletes."""
    with app.state.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
