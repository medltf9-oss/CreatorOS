"""Phase 6 migration tests against a copy of the real database.

A migration that works on an empty schema proves nothing about the database that already holds 111
agents, media assets and audit history. These tests copy the real file, upgrade the copy, and check
that nothing existing was damaged: integrity, foreign keys, no dangling references, the preserved
row counts, the new columns, the rebuilt OAuth table and the CHECK constraints.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from migration_helpers import alembic_head

PRODUCTION_DB = Path(os.environ.get("CREATOROS_ROOT") or Path.home() / "CreatorOS") / "data" / "database" / "creatoros.db"
# Phase-agnostic: the head moves every phase, and a stale constant only fails later.
HEAD = alembic_head()


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
    original = PRODUCTION_DB.read_bytes()
    assert (root / "data" / "database" / "creatoros.db").read_bytes() == original
    previous_root = os.environ.get("CREATOROS_ROOT")
    _point_at(root)
    yield root
    if previous_root is not None:
        os.environ["CREATOROS_ROOT"] = previous_root
    _point_at(Path(previous_root or ".")) if previous_root else None


def test_upgrade_keeps_the_real_database_intact(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")

    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == HEAD
        assert _dangling_references(connection) == []

        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert {"provider_app_config", "account_capability", "credential_rotation"} <= tables

        # Phase 4 workforce and Phase 5 media data are untouched.
        assert connection.execute("select count(*) from agent_definition").fetchone()[0] == 111
        assert connection.execute("select count(*) from agent_department").fetchone()[0] == 10
        # The tool registry is seeded at boot, so the copy may hold the 12 tools from the last
        # recorded run; what matters is that the migration did not delete any.
        assert connection.execute("select count(*) from tool_registry").fetchone()[0] >= 12
        assert connection.execute("select count(*) from media_derivative").fetchone()[0] >= 0

        social_columns = {row[1] for row in connection.execute("pragma table_info(social_account)")}
        assert {
            "route",
            "token_state",
            "refresh_ref_id",
            "app_config_id",
            "internal_label",
            "group_label",
            "api_version",
            "disconnected_at",
            "meta_json",
        } <= social_columns

        secret_columns = {row[1] for row in connection.execute("pragma table_info(secret_ref)")}
        assert {"purpose", "status", "schema_version", "byte_size", "last_verified_at"} <= secret_columns

        oauth_columns = {row[1] for row in connection.execute("pragma table_info(oauth_session)")}
        assert "state_hash" in oauth_columns
        assert "state" not in oauth_columns
        assert "code_verifier" not in oauth_columns


def test_plaintext_oauth_state_and_verifier_are_gone_from_the_schema(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        schema = " ".join(row[0] or "" for row in connection.execute("select sql from sqlite_master"))
    # A plaintext PKCE verifier column and the plaintext OAuth state column are both gone; the
    # reference column `code_verifier_ref_id` is expected and is not a verifier.
    assert "code_verifier VARCHAR" not in schema
    assert "state VARCHAR(128)" not in schema
    assert "state_hash" in schema


def test_a_pre_phase6_session_row_is_failed_not_silently_upgraded(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    db = copied_production_db / "data" / "database" / "creatoros.db"
    # Step back to the schema that still had plaintext state/verifier columns, so the row inserted
    # below is exactly what a pre-Phase-6 installation would have left behind.
    command.downgrade(config, "c4a91f7d2b60")
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            insert into oauth_session (platform, state, code_verifier, redirect_uri, expires_at, id, created_at, updated_at)
            values ('facebook', 'legacy-state', 'legacy-verifier',
                    'http://127.0.0.1:47821/oauth/callback/facebook_login',
                    '2026-01-01 00:00:00', '01M80ZZZZZZZZZZZZZZZZZZZZA',
                    '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "select state_hash, status, error_code from oauth_session where id = '01M80ZZZZZZZZZZZZZZZZZZZZA'"
        ).fetchone()
    assert row is not None
    assert row[0] == hashlib.sha256(b"legacy-state").hexdigest()
    assert row[1] == "failed"
    assert row[2] == "MIGRATION_VERIFIER_UNRETRIABLE"


def test_migration_is_reversible(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    db = copied_production_db / "data" / "database" / "creatoros.db"
    command.upgrade(config, "head")
    command.downgrade(config, "c4a91f7d2b60")
    with sqlite3.connect(db) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == "c4a91f7d2b60"
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert not ({"provider_app_config", "account_capability", "credential_rotation"} & tables)
        columns = {row[1] for row in connection.execute("pragma table_info(social_account)")}
        assert "token_state" not in columns
        oauth_columns = {row[1] for row in connection.execute("pragma table_info(oauth_session)")}
        assert "state" in oauth_columns and "code_verifier" in oauth_columns
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert _dangling_references(connection) == []

    command.upgrade(config, "head")
    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("select count(*) from agent_definition").fetchone()[0] == 111


def test_check_constraints_are_enforced(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        connection.execute("pragma foreign_keys=on")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into social_account (
                    platform, provider_account_id, display_name, account_type, scopes_json,
                    capabilities_json, connection_status, token_state, id, created_at, updated_at
                ) values ('facebook','p','P','facebook_page','[]','{}','connected','teleported',
                          '01M80ZZZZZZZZZZZZZZZZZZZZB','2026-01-01 00:00:00','2026-01-01 00:00:00')
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into secret_ref (provider, key_alias, fingerprint, cipher, dpapi_scope, status,
                                        id, created_at, updated_at)
                values ('facebook','a','f','dpapi','user','maybe',
                        '01M80ZZZZZZZZZZZZZZZZZZZZC','2026-01-01 00:00:00','2026-01-01 00:00:00')
                """
            )
        connection.rollback()


def test_unique_keys_are_enforced(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        connection.execute("pragma foreign_keys=on")
        insert = """
            insert into account_capability (account_id, capability, state, required_json, granted_json,
                                            missing_json, verified_at, id, created_at, updated_at)
            values ('01M80ZZZZZZZZZZZZZZZZZZZZD','k','available','[]','[]','[]',
                    '2026-01-01 00:00:00',?, '2026-01-01 00:00:00','2026-01-01 00:00:00')
        """
        # The account does not exist, so even the first insert must fail on the foreign key.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, ("01M80ZZZZZZZZZZZZZZZZZZZZE",))
        connection.rollback()

        connection.execute(
            """
            insert into social_account (platform, provider_account_id, display_name, account_type,
                scopes_json, capabilities_json, connection_status, token_state, id, created_at, updated_at)
            values ('facebook','p','P','facebook_page','[]','{}','disconnected','unknown',
                '01M80ZZZZZZZZZZZZZZZZZZZZF','2026-01-01 00:00:00','2026-01-01 00:00:00')
            """
        )
        connection.execute(insert.replace("'01M80ZZZZZZZZZZZZZZZZZZZZD'", "'01M80ZZZZZZZZZZZZZZZZZZZZF'"),
                           ("01M80ZZZZZZZZZZZZZZZZZZZZG",))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert.replace("'01M80ZZZZZZZZZZZZZZZZZZZZD'", "'01M80ZZZZZZZZZZZZZZZZZZZZF'"),
                               ("01M80ZZZZZZZZZZZZZZZZZZZZH",))
        connection.rollback()
