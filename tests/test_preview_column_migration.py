"""Migration 0010 repairs a database that applied the first version of migration 0008."""

from sqlalchemy import inspect, text

from app.db import make_engine, run_migrations


def columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("preview_lookups")}


def test_a_database_without_the_column_gets_it(settings):
    url = settings.database_url
    run_migrations(url, "0009")
    engine = make_engine(url)
    with engine.begin() as conn:  # what the first version of 0008 left behind
        conn.execute(text("ALTER TABLE preview_lookups DROP COLUMN provider_url"))
        conn.execute(
            text(
                "INSERT INTO artists (id, name, sort_name, name_key, mb_status, in_library, created_at, updated_at)"
                " VALUES (1, 'A', 'A', 'a', 'pending', 1, '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO release_groups (id, mbid, artist_id, title, secondary_types_json, disambiguation,"
                " category, cover_status, synced_at) VALUES (7, 'm', 1, 'T', '[]', '', 'album', 'unknown', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO preview_lookups (release_group_id, provider, provider_album_id, status, checked_at)"
                " VALUES (7, 'itunes', '1', 'found', '2026-01-01')"
            )
        )
    assert "provider_url" not in columns(engine)
    run_migrations(url)
    assert "provider_url" in columns(engine)
    with engine.connect() as conn:  # nothing that was stored is lost
        row = conn.execute(
            text("SELECT provider, provider_album_id, provider_url FROM preview_lookups")
        ).one()
    assert tuple(row) == ("itunes", "1", None)
    engine.dispose()


def test_a_fresh_database_is_not_touched(settings):
    run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    assert "provider_url" in columns(engine)
    run_migrations(settings.database_url)  # running it again changes nothing
    engine.dispose()
