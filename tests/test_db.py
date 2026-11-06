import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text

from app.config import Settings
from app.main import create_app
from app.models import Base, Setting, User
from app.services.app_settings import get_setting, set_setting


def test_migrations_match_models(app, client):
    with app.state.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migration_creates_expected_tables(app, client):
    with app.state.engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text("select name from sqlite_master"))}
    assert {"users", "sessions", "settings", "alembic_version"} <= tables


def test_sqlite_pragmas(app, client):
    with app.state.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_settings_global_and_per_user(db):
    user = User(username="u1", password_hash="x", role="user")
    db.add(user)
    db.commit()
    assert get_setting(db, "theme", "light") == "light"
    set_setting(db, "theme", "dark")
    set_setting(db, "theme", "dark2")  # upsert, not duplicate
    set_setting(db, "theme", "blue", user_id=user.id)
    db.commit()
    assert get_setting(db, "theme") == "dark2"
    assert get_setting(db, "theme", user_id=user.id) == "blue"
    assert db.scalar(select(func.count(Setting.id))) == 2


def test_settings_store_json_values(db):
    set_setting(db, "types", ["album", "ep"])
    db.commit()
    assert get_setting(db, "types") == ["album", "ep"]


def test_user_settings_deleted_with_user(db):
    user = User(username="u2", password_hash="x", role="user")
    db.add(user)
    db.commit()
    set_setting(db, "lang", "it", user_id=user.id)
    db.commit()
    db.delete(user)
    db.commit()
    assert db.scalar(select(func.count(Setting.id))) == 0


def test_invalid_role_rejected_by_db(db):
    from sqlalchemy.exc import IntegrityError

    db.add(User(username="bad", password_hash="x", role="root"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_default_secret_key_refused_in_production(tmp_path):
    settings = Settings(_env_file=None, base_url="https://diskko.example", config_dir=tmp_path)
    with pytest.raises(RuntimeError):
        create_app(settings)
    Settings(_env_file=None, env="production", config_dir=tmp_path)  # constructing is fine
    with pytest.raises(RuntimeError):
        create_app(Settings(_env_file=None, env="production", config_dir=tmp_path))


def test_default_secret_key_allowed_in_development(tmp_path):
    create_app(Settings(_env_file=None, config_dir=tmp_path))
