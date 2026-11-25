"""Phase 7 migration tests against a copy of the real database.

The interesting part of this migration is not the columns: it is that `publish_job` and
`publish_attempt` are *rebuilt* to widen a CHECK constraint while `publish_attempt` holds a foreign
key to `publish_job`. These tests copy the real file, put real-shaped rows in it (including an
attempt), migrate, and prove that nothing was lost, no foreign key dangles, and the widened CHECK
constraint accepts the new states while still rejecting nonsense.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from migration_helpers import alembic_head, ensure_revision, revision_before

PRODUCTION_DB = Path(os.environ.get("CREATOROS_ROOT") or Path.home() / "CreatorOS") / "data" / "database" / "creatoros.db"
HEAD = alembic_head()
# The revision this phase added; the tests step back to it to seed pre-Phase-7 rows.
PREVIOUS = revision_before(HEAD)

ACCOUNT = "01M90AAAAAAAAAAAAAAAAAAAAA"
PROJECT = "01M90BBBBBBBBBBBBBBBBBBBBB"
VARIANT = "01M90CCCCCCCCCCCCCCCCCCCCC"
JOB = "01M90DDDDDDDDDDDDDDDDDDDDD"
ATTEMPT = "01M90EEEEEEEEEEEEEEEEEEEEE"
STAMP = "2026-01-01 00:00:00"


def _alembic_config(root: Path):
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    return config


def _point_at(root: Path) -> None:
    import os

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
            if match.group(1) not in tables:
                dangling.append((name, match.group(1)))
    return dangling


def _seed_legacy_publishing_row(connection: sqlite3.Connection, state: str = "queued") -> None:
    """A pre-7 journal with one job and one attempt, with every foreign key satisfied."""
    connection.execute("pragma foreign_keys=on")
    connection.execute(
        """
        insert into social_account (platform, provider_account_id, display_name, account_type,
            scopes_json, capabilities_json, connection_status, token_state, id, created_at, updated_at)
        values ('facebook', 'page-1', 'Page One', 'facebook_page', '[]', '{}', 'connected', 'valid', ?, ?, ?)
        """,
        (ACCOUNT, STAMP, STAMP),
    )
    connection.execute(
        "insert into content_project (title, status, id, created_at, updated_at) values ('P', 'draft', ?, ?, ?)",
        (PROJECT, STAMP, STAMP),
    )
    connection.execute(
        """
        insert into platform_variant (project_id, platform, target_account_id, aspect_ratio, caption,
            hashtags_json, privacy_setting, validation_report_json, approval_status,
            id, created_at, updated_at)
        values (?, 'facebook', ?, '9:16', 'legacy caption', '[]', 'public', '{}', 'draft', ?, ?, ?)
        """,
        (PROJECT, ACCOUNT, VARIANT, STAMP, STAMP),
    )
    connection.execute(
        """
        insert into publish_job (variant_id, platform, account_id, scheduled_at_utc, display_timezone,
            job_state, attempts, idempotency_key, remote_status_json, id, created_at, updated_at)
        values (?, 'facebook', ?, ?, 'Europe/Paris', ?, 1, 'legacy-key-1', '{}', ?, ?, ?)
        """,
        (VARIANT, ACCOUNT, STAMP, state, JOB, STAMP, STAMP),
    )
    connection.execute(
        """
        insert into publish_attempt (job_id, attempt_no, started_at, finished_at, http_status,
            response_code, outcome, id, created_at, updated_at)
        values (?, 1, ?, ?, 200, 'OK', 'unknown', ?, ?, ?)
        """,
        (JOB, STAMP, STAMP, ATTEMPT, STAMP, STAMP),
    )
    connection.commit()


@pytest.fixture()
def copied_production_db(tmp_path):
    if not PRODUCTION_DB.exists():
        pytest.skip("no production database on this machine")
    root = tmp_path / "prod-copy-root"
    target = root / "data" / "database"
    target.mkdir(parents=True)
    # The WAL holds the most recent commits; copying only the .db file silently loses them.
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(str(PRODUCTION_DB) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target / f"creatoros.db{suffix}")
    assert (target / "creatoros.db").read_bytes() == PRODUCTION_DB.read_bytes()
    previous_root = os.environ.get("CREATOROS_ROOT")
    _point_at(root)
    yield root
    if previous_root is not None:
        os.environ["CREATOROS_ROOT"] = previous_root
        _point_at(Path(previous_root))


def _upgrade_to_previous(config) -> None:
    ensure_revision(config, PREVIOUS)


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
        assert {"publish_delivery", "publish_reconciliation"} <= tables
        assert "publish_job_p7" not in tables and "publish_job_pre7" not in tables

        job_columns = {row[1] for row in connection.execute("pragma table_info(publish_job)")}
        assert {
            "intent_key",
            "asset_id",
            "asset_sha256",
            "derivative_id",
            "derivative_sha256",
            "publish_mode",
            "caption",
            "hashtags_json",
            "approval_request_id",
            "approval_payload_hash",
            "payload_hash",
            "provider_upload_session_id",
            "provider_media_id",
            "provider_permalink",
            "upload_bytes_done",
            "error_category",
            "state_reason",
            "reconcile_attempts",
        } <= job_columns

        attempt_columns = {row[1] for row in connection.execute("pragma table_info(publish_attempt)")}
        assert {"provider_operation", "error_category", "remote_snapshot_json"} <= attempt_columns

        variant_columns = {row[1] for row in connection.execute("pragma table_info(platform_variant)")}
        assert {"title", "metadata_json", "options_json", "source_derivative_id"} <= variant_columns

        # The rebuild must keep the attempt table pointing at the real job table.
        fks = connection.execute("pragma foreign_key_list(publish_attempt)").fetchall()
        assert [row[2] for row in fks] == ["publish_job"]

        # Phase 4/5/6 data is untouched.
        assert connection.execute("select count(*) from agent_definition").fetchone()[0] == 111
        assert connection.execute("select count(*) from agent_department").fetchone()[0] == 10


def test_a_legacy_job_and_attempt_survive_the_table_rebuild(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    ensure_revision(config, PREVIOUS)
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        _seed_legacy_publishing_row(connection, state="queued")

    command.upgrade(config, "head")

    with sqlite3.connect(db) as connection:
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        job = connection.execute(
            "select job_state, attempts, idempotency_key, caption, publish_mode, upload_bytes_total"
            " from publish_job where id = ?",
            (JOB,),
        ).fetchone()
        attempt = connection.execute(
            "select outcome, http_status, remote_snapshot_json from publish_attempt where id = ?",
            (ATTEMPT,),
        ).fetchone()
    assert job is not None
    # The legacy state is preserved verbatim; normalisation happens in the application, not here.
    assert job[0] == "queued"
    assert job[1] == 1
    assert job[2] == "legacy-key-1"
    assert job[3] == "" and job[4] == "video" and job[5] == 0
    assert attempt is not None
    assert attempt[0] == "unknown" and attempt[1] == 200 and attempt[2] == "{}"


def test_normalizer_maps_the_legacy_state_the_migration_preserved():
    # Pure function: this one deliberately does not touch a database, so it cannot be affected by
    # the fixture's schema version.
    from creatoros.db.enums import PublishState, normalize_publish_state

    assert normalize_publish_state("queued") is PublishState.READY
    assert normalize_publish_state("validation") is PublishState.VALIDATION_PENDING
    assert normalize_publish_state("manual_publish_required") is PublishState.REQUIRES_USER_ACTION


def test_phase7_states_are_accepted_and_downgrade_collapses_them(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        _seed_legacy_publishing_row(connection, state="draft")
        connection.execute("update publish_job set job_state='approved' where id=?", (JOB,))
        connection.commit()

    # Phase 8 undoes itself first (phase-agnostic), then Phase 7's own downgrade collapses the
    # nine new states. Downgrading straight to PREVIOUS only worked while PREVIOUS was head.
    ensure_revision(config, PREVIOUS)
    command.downgrade(config, revision_before(PREVIOUS))
    with sqlite3.connect(db) as connection:
        assert connection.execute("select job_state from publish_job where id=?", (JOB,)).fetchone()[0] == (
            "awaiting_approval"
        )
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert not ({"publish_delivery", "publish_reconciliation"} & tables)

    command.upgrade(config, "head")
    with sqlite3.connect(db) as connection:
        assert connection.execute("select job_state from publish_job where id=?", (JOB,)).fetchone()[0] == (
            "awaiting_approval"
        )
        assert connection.execute("select count(*) from publish_attempt where id=?", (ATTEMPT,)).fetchone()[0] == 1
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"


def test_state_and_category_check_constraints_are_enforced(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        _seed_legacy_publishing_row(connection, state="draft")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "update publish_job set job_state='teleported' where id=?", (JOB,)
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("update publish_job set publish_mode='hologram' where id=?", (JOB,))
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("update publish_job set error_category='vibes' where id=?", (JOB,))
        connection.rollback()
        # A legal Phase 7 state and a legal category must be accepted.
        connection.execute(
            "update publish_job set job_state='ready', error_category='rate_limit' where id=?", (JOB,)
        )
        connection.commit()
        assert connection.execute("select job_state from publish_job where id=?", (JOB,)).fetchone()[0] == "ready"


def test_delivery_assets_are_unique_per_job_and_object(copied_production_db):
    from alembic import command

    config = _alembic_config(copied_production_db)
    command.upgrade(config, "head")
    db = copied_production_db / "data" / "database" / "creatoros.db"
    with sqlite3.connect(db) as connection:
        _seed_legacy_publishing_row(connection, state="draft")
        insert = """
            insert into publish_delivery (job_id, provider, bucket, object_key, sha256, bytes, state,
                id, created_at, updated_at)
            values (?, 's3', 'bucket', 'publish/abc.mp4', ?, 10, 'pending', ?, ?, ?)
        """
        connection.execute(insert, (JOB, "a" * 64, "01M90FFFFFFFFFFFFFFFFFFFFF", STAMP, STAMP))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, (JOB, "b" * 64, "01M90GGGGGGGGGGGGGGGGGGGGG", STAMP, STAMP))
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into publish_delivery (job_id, provider, bucket, object_key, sha256, bytes, state,
                    id, created_at, updated_at)
                values (?, 's3', 'bucket', 'publish/def.mp4', ?, 10, 'orbiting', ?, ?, ?)
                """,
                (JOB, "c" * 64, "01M90HHHHHHHHHHHHHHHHHHHHH", STAMP, STAMP),
            )
        connection.rollback()
