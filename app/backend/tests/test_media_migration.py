"""Migration tests against a copy of the real database, not just a fresh schema.

Phase 4 shipped a migration that was correct on an empty database and wrong on the real
one, because the real file had already been through earlier versions. These tests therefore
copy the production database (if it exists), run the migration chain on the copy, and check
integrity, foreign keys and that no table references a temporary/missing table.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from migration_helpers import alembic_head

PRODUCTION_DB = Path(os.environ.get("CREATOROS_ROOT") or Path.home() / "CreatorOS") / "data" / "database" / "creatoros.db"


def _alembic_config(root: Path):
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    return config


def _point_at(root: Path) -> None:
    from creatoros.core import settings as settings_mod
    from creatoros.db import session as session_mod

    os.environ["CREATOROS_ROOT"] = str(root)
    settings_mod.get_settings.cache_clear()
    settings_mod.resolve_root.cache_clear()
    session_mod.reset_engine()


def _dangling_references(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    dangling: list[tuple[str, str]] = []
    for name, sql in connection.execute("select name, sql from sqlite_master where type='table'"):
        for match in re.finditer(r'REFERENCES\s+"?([A-Za-z0-9_]+)"?', sql or ""):
            target = match.group(1)
            if target not in tables:
                dangling.append((name, target))
    return dangling


@pytest.fixture()
def copied_production_db(tmp_path, monkeypatch):
    if not PRODUCTION_DB.exists():
        pytest.skip("no production database on this machine")
    root = tmp_path / "prod-copy-root"
    (root / "data" / "database").mkdir(parents=True)
    shutil.copy2(PRODUCTION_DB, root / "data" / "database" / "creatoros.db")
    # A copy is only useful if it is byte-identical to the real file at the start.
    original = PRODUCTION_DB.read_bytes()
    assert (root / "data" / "database" / "creatoros.db").read_bytes() == original
    _point_at(root)
    yield root
    _point_at(Path(os.environ["CREATOROS_ROOT"]))


def test_migration_upgrades_a_copy_of_the_real_database(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")

    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        head = connection.execute("select version_num from alembic_version").fetchone()[0]
        assert head == alembic_head()

        columns = {row[1] for row in connection.execute("pragma table_info(media_asset)")}
        assert {"kind", "intake_state", "probe_json", "probe_version", "quarantine_reason"} <= columns
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert "media_derivative" in tables
        # Phase 6 tables travel with the same head.
        assert {"provider_app_config", "account_capability", "credential_rotation"} <= tables

        # The Phase 4 data must survive untouched.
        assert connection.execute("select count(*) from agent_definition").fetchone()[0] == 111
        assert connection.execute("select count(*) from agent_department").fetchone()[0] == 10
        assert _dangling_references(connection) == []

        # Inbound foreign keys still point at media_asset, not at a scratch table.
        transcript_fks = [row[2] for row in connection.execute("pragma foreign_key_list(transcript)")]
        assert "media_asset" in transcript_fks


def test_existing_rows_keep_working_after_the_migration(copied_production_db):
    """A pre-migration asset row must still be readable, with sensible defaults."""
    from alembic import command

    config = _alembic_config(copied_production_db)
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            insert into media_asset (
                sha256, source_path, original_filename, size_bytes, rotation, has_audio,
                loudness_json, rights_status, tags_json, archive_state, id, created_at, updated_at
            ) values (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "a" * 64,
                "<DATA_ROOT>/data/media/incoming/legacy.mp4",
                "legacy.mp4",
                4096,
                0,
                1,
                "{}",
                "uncertain",
                "[]",
                "active",
                "01M80ZZZZZZZZZZZZZZZZZZZZA",
                "2026-11-01 10:00:00",
                "2026-11-01 10:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "select kind, intake_state, probe_json from media_asset where id = ?",
            ("01M80ZZZZZZZZZZZZZZZZZZZZA",),
        ).fetchone()
    assert row[0] == "unknown"
    assert row[1] == "pending"
    assert row[2] in ("{}", b"{}", None) or str(row[2]) == "{}"


def test_migration_is_reversible_on_a_copy_of_the_real_database(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    db = copied_production_db / "data" / "database" / "creatoros.db"
    command.upgrade(config, "head")
    command.downgrade(config, "b7f1c02d4e61")

    with sqlite3.connect(db) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == "b7f1c02d4e61"
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert "media_derivative" not in tables
        columns = {row[1] for row in connection.execute("pragma table_info(media_asset)")}
        assert "kind" not in columns
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert _dangling_references(connection) == []

    # And forward again, proving the round trip leaves a usable database.
    command.upgrade(config, "head")
    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("select count(*) from agent_definition").fetchone()[0] == 111


def test_check_constraints_are_enforced_on_the_migrated_database(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into media_asset (
                    sha256, source_path, original_filename, size_bytes, rotation, has_audio,
                    loudness_json, rights_status, tags_json, archive_state, id, created_at, updated_at, kind
                ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("b" * 64, "x", "x.mp4", 1, 0, 0, "{}", "uncertain", "[]", "active",
                 "01M80ZZZZZZZZZZZZZZZZZZZZB", "2026-11-01 10:00:00", "2026-11-01 10:00:00", "hologram"),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into media_asset (
                    sha256, source_path, original_filename, size_bytes, rotation, has_audio,
                    loudness_json, rights_status, tags_json, archive_state, id, created_at, updated_at, intake_state
                ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("c" * 64, "x", "x.mp4", 1, 0, 0, "{}", "uncertain", "[]", "active",
                 "01M80ZZZZZZZZZZZZZZZZZZZZC", "2026-11-01 10:00:00", "2026-11-01 10:00:00", "half-imported"),
            )
        connection.rollback()


def test_derivative_unique_key_is_enforced(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            """
            insert into media_asset (
                sha256, source_path, original_filename, size_bytes, rotation, has_audio,
                loudness_json, rights_status, tags_json, archive_state, id, created_at, updated_at
            ) values (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("d" * 64, "x", "x.mp4", 1, 0, 0, "{}", "uncertain", "[]", "active",
             "01M80ZZZZZZZZZZZZZZZZZZZZD", "2026-11-01 10:00:00", "2026-11-01 10:00:00"),
        )
        insert = """
            insert into media_derivative (
                asset_id, kind, profile, version, state, size_bytes, meta_json, id, created_at, updated_at
            ) values (?,?,?,?,?,?,?,?,?,?)
        """
        params = ("01M80ZZZZZZZZZZZZZZZZZZZZD", "thumbnail", "480px", 2, "ready", 10, "{}",
                  "01M80ZZZZZZZZZZZZZZZZZZZZE", "2026-11-01 10:00:00", "2026-11-01 10:00:00")
        connection.execute(insert, params)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, (*params[:7], "01M80ZZZZZZZZZZZZZZZZZZZZF", *params[8:]))
        connection.rollback()
