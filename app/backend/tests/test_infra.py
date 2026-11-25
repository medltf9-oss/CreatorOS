"""Redaction, migration and backup-integrity tests.

The redaction test is the one that must never be weakened: a leaked token in a
log file is a security incident, not a cosmetic bug.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

import pytest

from creatoros.core.logging import RedactionFilter, configure_logging, redact_value

SAMPLE_TOKEN = "EAAG" + "x" * 40
SAMPLE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
SAMPLE_IG = "IGQV" + "y" * 30


def test_redacts_by_key():
    payload = {"access_token": SAMPLE_TOKEN, "nested": {"refresh_token": "abc"}, "ok": "fine"}
    redacted = redact_value(payload)
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["nested"]["refresh_token"] == "[REDACTED]"
    assert redacted["ok"] == "fine"


def test_redacts_by_value_pattern():
    for secret in (SAMPLE_TOKEN, SAMPLE_JWT, SAMPLE_IG, "act.1234567890", "Bearer abcdefghijklmnop"):
        redacted = redact_value(f"prefix {secret} suffix")
        assert secret not in redacted, f"{secret[:12]} leaked"


def test_redacts_nested_lists():
    payload = {"items": [{"token": "x"}, {"value": SAMPLE_TOKEN}]}
    redacted = redact_value(payload)
    assert redacted["items"][0]["token"] == "[REDACTED]"
    assert SAMPLE_TOKEN not in json.dumps(redacted)


def test_log_file_contains_no_secret(tmp_path: Path):
    log_path = configure_logging("INFO", log_dir=tmp_path, to_stdout=False)
    logger = logging.getLogger("creatoros.test")
    logger.info("auth header %s", f"Bearer {SAMPLE_TOKEN}")
    logger.error("response %s", {"access_token": SAMPLE_TOKEN, "jwt": SAMPLE_JWT})
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert SAMPLE_TOKEN not in content
    assert SAMPLE_JWT not in content
    assert "REDACTED" in content


def test_record_extras_are_redacted():
    record = logging.LogRecord(
        name="creatoros.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="call",
        args=(),
        exc_info=None,
    )
    record.token = SAMPLE_TOKEN
    record.correlation_id = "abc"
    RedactionFilter().filter(record)
    assert record.token == "[REDACTED]"
    assert record.correlation_id == "abc"


# --------------------------------------------------------------------- migrations
def _alembic_config():
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    return config


def test_migrations_round_trip(_isolated_root):
    """downgrade base -> upgrade head must work on a scratch database."""
    from alembic import command

    from creatoros.core.paths import ensure_dir
    from creatoros.db.session import database_path, reset_engine

    config = _alembic_config()
    command.downgrade(config, "base")
    db = database_path()
    with sqlite3.connect(db) as con:
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    assert "media_asset" not in tables

    command.upgrade(config, "head")
    reset_engine()
    with sqlite3.connect(db) as con:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    for expected in ("media_asset", "content_project", "platform_variant", "publish_job", "queue_job", "audit_event"):
        assert expected in tables
    assert ensure_dir("database").exists()


def test_migrated_schema_has_no_dangling_foreign_keys(_isolated_root):
    """Every foreign key must point at a table that actually exists.

    A table rebuild that drops the parent before the children leaves SQLite
    foreign keys referencing the scratch table name; nothing fails until a write
    touches the child table, and then the whole transaction dies.
    """
    import creatoros.db.models  # noqa: F401
    from creatoros.core.paths import ensure_dir
    from creatoros.db.session import database_path

    assert ensure_dir("database")

    with sqlite3.connect(database_path()) as con:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        dangling: list[tuple[str, str]] = []
        for name, sql in con.execute("select name, sql from sqlite_master where type='table'"):
            for match in re.finditer(r'REFERENCES\s+"?([A-Za-z0-9_]+)"?', sql or ""):
                if match.group(1) not in tables:
                    dangling.append((name, match.group(1)))
        violations = con.execute("pragma foreign_key_check").fetchall()

    assert dangling == [], f"foreign keys point at missing tables: {dangling}"
    assert violations == [], f"foreign key violations: {violations[:5]}"


def test_all_tables_have_primary_keys(_isolated_root):
    import creatoros.db.models  # noqa: F401
    from creatoros.db.base import Base

    missing = [name for name, table in Base.metadata.tables.items() if not table.primary_key.columns]
    assert missing == [], f"tables without primary key: {missing}"


def test_foreign_keys_declared(_isolated_root):
    import creatoros.db.models  # noqa: F401
    from creatoros.db.base import Base

    # Spot-check the joins that matter for data integrity.
    for table, target in (
        ("platform_variant", "content_project"),
        ("publish_job", "platform_variant"),
        ("publish_job", "social_account"),
        ("agent_task", "agent_definition"),
        ("queue_job", None),
    ):
        fks = {fk.column.table.name for fk in Base.metadata.tables[table].foreign_keys}
        if target:
            assert target in fks, f"{table} missing FK to {target}"


# ------------------------------------------------------------------------ backup
def test_backup_integrity_and_restore_drill(_isolated_root, session):
    from creatoros.db.models import ContentProject
    from creatoros.db.session import database_path
    from creatoros.services.backup import BackupService

    session.add(ContentProject(title="Backup me"))
    session.commit()

    service = BackupService(database_path(), keep=5)
    result = service.create(label="test")
    assert result.integrity == "ok"
    assert result.tables >= 30
    assert len(result.sha256) == 64

    verification = service.verify(result.path)
    assert verification["ok"] is True
    assert verification["counts"]["content_project"] == 1

    drill = result.path.parent / "restored.db"
    service.restore(result.path, restore_to=drill)
    with sqlite3.connect(drill) as con:
        assert con.execute("select count(*) from content_project").fetchone()[0] == 1


def test_backup_prunes_old_copies(_isolated_root, session):
    from creatoros.db.session import database_path
    from creatoros.services.backup import BackupService

    service = BackupService(database_path(), keep=2)
    for _ in range(4):
        service.create(label="prune")
    remaining = list(service.backup_dir.glob("creatoros-*.db"))
    assert len(remaining) <= 2


def test_backup_rejects_corrupt_file(_isolated_root, tmp_path):
    from creatoros.db.session import database_path
    from creatoros.services.backup import BackupService

    service = BackupService(database_path())
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    verification = service.verify(corrupt)
    assert verification["ok"] is False
    with pytest.raises(ValueError):
        service.restore(corrupt)
