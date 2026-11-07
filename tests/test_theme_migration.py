from sqlalchemy import text

from app.db import make_engine, run_migrations


def test_migration_adds_the_theme_and_drops_personal_album_types(settings):
    url = settings.database_url
    run_migrations(url, "0005")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at)"
                " VALUES (1, 'ann', 'x', 'user', 1, '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO settings (key, value_json, scope, user_id) VALUES"
                " ('missing_types', '[\"live\"]', 'user', 1), ('missing_types', '[\"album\"]', 'global', NULL),"
                " ('theme_note', '1', 'user', 1)"
            )
        )
    run_migrations(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT theme FROM users WHERE id = 1")).scalar() == "auto"
        rows = conn.execute(text("SELECT key, scope FROM settings ORDER BY key, scope")).all()
    assert rows == [
        ("missing_types", "global"),
        ("theme_note", "user"),
    ]  # only the personal types went
    engine.dispose()
