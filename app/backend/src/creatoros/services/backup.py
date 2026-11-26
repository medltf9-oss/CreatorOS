"""Audit trail and backup.

Both services exist to make claims verifiable: `audit` records who did what to
which resource, and `backup` produces a restorable copy plus an integrity check
so "your data is safe" is a tested statement rather than a promise.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..core.logging import redact_value
from ..core.paths import ensure_dir
from ..db.models import AuditEvent


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditService:
    """Append-only audit writer. There is no update or delete path, by design."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str | None = None,
        outcome: str = "success",
        actor: str = "local-user",
        metadata: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            metadata_redacted_json=redact_value(metadata or {}),
            correlation_id=correlation_id,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def query(
        self,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        stmt = sa.select(AuditEvent).order_by(AuditEvent.created_at.desc())
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        if resource_type:
            stmt = stmt.where(AuditEvent.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(AuditEvent.resource_id == resource_id)
        stmt = stmt.limit(min(limit, 500)).offset(max(0, offset))
        return list(self.session.scalars(stmt))


@dataclass(slots=True)
class BackupResult:
    path: Path
    sha256: str
    size_bytes: int
    integrity: str
    tables: int
    created_at: datetime


class BackupService:
    """Consistent online backups of the SQLite database via VACUUM INTO."""

    def __init__(self, db_path: Path, backup_dir: Path | None = None, keep: int = 10) -> None:
        self.db_path = db_path
        self.backup_dir = backup_dir or ensure_dir("backups")
        self.keep = max(1, keep)

    def create(self, label: str = "manual") -> BackupResult:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        suffix = uuid.uuid4().hex[:6]
        target = self.backup_dir / f"creatoros-{stamp}-{label}-{suffix}.db"
        if target.exists():  # VACUUM INTO refuses to overwrite
            target.unlink()

        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute("VACUUM INTO ?", (str(target),))
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]

        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest = {
            "created_at": _utcnow().isoformat(),
            "source": str(self.db_path),
            "file": target.name,
            "sha256": digest,
            "integrity": integrity,
            "label": label,
        }
        (self.backup_dir / f"{target.stem}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.prune()

        with closing(sqlite3.connect(target)) as check:
            tables = check.execute(
                "select count(*) from sqlite_master where type='table' and name not like 'sqlite_%'"
            ).fetchone()[0]
        return BackupResult(
            path=target,
            sha256=digest,
            size_bytes=target.stat().st_size,
            integrity=integrity,
            tables=tables,
            created_at=_utcnow(),
        )

    def prune(self) -> list[Path]:
        backups = sorted(self.backup_dir.glob("creatoros-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        removed: list[Path] = []
        for stale in backups[self.keep :]:
            stale.unlink(missing_ok=True)
            (self.backup_dir / f"{stale.stem}.json").unlink(missing_ok=True)
            removed.append(stale)
        return removed

    def verify(self, backup_path: Path) -> dict[str, Any]:
        """Restore-drill: open the backup, check integrity, count rows in key tables."""
        counts: dict[str, int] = {}
        try:
            with closing(sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)) as con:
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                for table in ("media_asset", "content_project", "platform_variant", "publish_job", "audit_event"):
                    try:
                        counts[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
                    except sqlite3.Error:
                        counts[table] = -1
        except sqlite3.Error as exc:
            return {"integrity": f"unreadable: {exc}", "counts": {}, "ok": False}
        return {"integrity": integrity, "counts": counts, "ok": integrity == "ok"}

    def restore(self, backup_path: Path, restore_to: Path | None = None) -> Path:
        """Copy a verified backup into place (or to a scratch path for a drill)."""
        verification = self.verify(backup_path)
        if not verification["ok"]:
            raise ValueError(f"backup failed integrity check: {verification['integrity']}")
        target = restore_to or self.db_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target)
        return target
