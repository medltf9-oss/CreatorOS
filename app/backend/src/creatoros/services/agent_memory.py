"""Scoped agent memory.

Four rules, enforced here rather than documented and hoped for:

1. Every row carries an explicit scope and an owner; nothing is scope-less.
2. A query returns the requested scope plus everything broader, never anything
   narrower. An agent cannot read another agent's task notes by accident.
3. Rows tagged with a project are only visible to queries for that project. Global
   rows (no project) are the only ones shared across projects.
4. Content is scanned for credentials before it is stored. A secret is rejected, not
   redacted and forgotten.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..core.logging import REDACTION_PATTERNS, get_logger, redact_value
from ..db.enums import MemoryScope
from ..db.models import AgentMemory
from .backup import AuditService

log = get_logger(__name__)

# Broader scopes include narrower ones. Ordered narrow -> broad.
SCOPE_ORDER: tuple[MemoryScope, ...] = (
    MemoryScope.TASK,
    MemoryScope.AGENT,
    MemoryScope.DEPARTMENT,
    MemoryScope.PROJECT,
    MemoryScope.GLOBAL,
    MemoryScope.LONG_TERM,
)

# Retention defaults (days) and the per-scope content budget in characters.
RETENTION_DAYS: dict[MemoryScope, int] = {
    MemoryScope.TASK: 7,
    MemoryScope.AGENT: 30,
    MemoryScope.DEPARTMENT: 90,
    MemoryScope.PROJECT: 180,
    MemoryScope.GLOBAL: 365,
    MemoryScope.LONG_TERM: 365,
}
MAX_CHARS_PER_SCOPE = 200_000
MAX_CONTENT_CHARS = 20_000

_EXTRA_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"), "stripe_key"),
    (re.compile(r"(?i)\bghp_[A-Za-z0-9]{20,}"), "github_token"),
    (re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9-]{10,}"), "slack_token"),
    (re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"), "aws_key"),
    (re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private_key"),
)


class SecretInMemory(ValueError):
    code = "SECRET_IN_MEMORY"


class MemoryScopeError(ValueError):
    code = "INVALID_MEMORY_SCOPE"


def detect_secret(text: str) -> str | None:
    """Return the name of the first credential pattern found, or None.

    Names are used rather than the match so a rejection never echoes the secret.
    """
    if not text:
        return None
    for index, (pattern, replacement) in enumerate(REDACTION_PATTERNS):
        if pattern.search(text):
            return f"redaction_pattern_{index}:{replacement}"
    for pattern, name in _EXTRA_SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


@dataclass(slots=True)
class MemoryWriteResult:
    memory_id: str
    scope: str
    owner_id: str | None
    expires_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentMemoryService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)

    # ---------------------------------------------------------------- write
    def write(
        self,
        *,
        scope: str,
        owner_id: str | None,
        content: str,
        title: str | None = None,
        agent_id: str | None = None,
        project_id: str | None = None,
        objective_id: str | None = None,
        task_id: str | None = None,
        sensitivity: str = "normal",
        ttl_days: int | None = None,
        correlation_id: str | None = None,
    ) -> MemoryWriteResult:
        try:
            resolved_scope = MemoryScope(scope)
        except ValueError as exc:
            raise MemoryScopeError(f"unknown memory scope {scope!r}") from exc

        text = (content or "").strip()
        if not text:
            raise ValueError("memory content must not be empty")
        if len(text) > MAX_CONTENT_CHARS:
            text = text[:MAX_CONTENT_CHARS]
        if title and len(title) > 200:
            title = title[:200]

        found = detect_secret(text) or (detect_secret(title or "") and "title")
        if found:
            self.audit.record(
                "agent.memory.rejected",
                resource_type="agent_memory",
                outcome="denied",
                actor=agent_id or "system",
                metadata={"reason": "secret_detected", "pattern": found, "scope": str(resolved_scope)},
                correlation_id=correlation_id,
            )
            self.session.commit()
            raise SecretInMemory("memory content looks like it contains a credential; refusing to store it")

        if resolved_scope in (MemoryScope.AGENT, MemoryScope.DEPARTMENT, MemoryScope.PROJECT) and not owner_id:
            raise MemoryScopeError(f"scope {resolved_scope} requires an owner_id")
        if resolved_scope is MemoryScope.PROJECT and not project_id:
            raise MemoryScopeError("project memory requires a project_id")
        if resolved_scope is MemoryScope.TASK and not task_id and not owner_id:
            raise MemoryScopeError("task memory requires a task_id")

        days = ttl_days if ttl_days is not None else RETENTION_DAYS[resolved_scope]
        expires_at = _utcnow() + timedelta(days=days)

        row = AgentMemory(
            scope=resolved_scope,
            owner_id=owner_id,
            project_id=project_id,
            source_task_id=task_id,
            objective_id=objective_id,
            title=title,
            content=text,
            sensitivity=sensitivity,
            expires_at=expires_at,
        )
        self.session.add(row)
        self.session.flush()
        self.audit.record(
            "agent.memory.write",
            resource_type="agent_memory",
            resource_id=row.id,
            outcome="success",
            actor=agent_id or "system",
            metadata={
                "scope": str(resolved_scope),
                "owner_id": owner_id,
                "project_id": project_id,
                "chars": len(text),
                "sensitivity": sensitivity,
            },
            correlation_id=correlation_id,
        )
        self.session.flush()
        return MemoryWriteResult(
            memory_id=row.id,
            scope=str(resolved_scope),
            owner_id=owner_id,
            expires_at=expires_at,
        )

    # ---------------------------------------------------------------- query
    def query(
        self,
        *,
        scope: str,
        owner_id: str | None = None,
        project_id: str | None = None,
        objective_id: str | None = None,
        search: str | None = None,
        limit: int = 20,
        include_expired: bool = False,
    ) -> list[AgentMemory]:
        try:
            requested = MemoryScope(scope)
        except ValueError as exc:
            raise MemoryScopeError(f"unknown memory scope {scope!r}") from exc

        included = self.scopes_included_by(requested)
        where: list[Any] = [AgentMemory.scope.in_(included)]

        if requested is MemoryScope.TASK:
            if not owner_id:
                raise MemoryScopeError("querying task memory requires the owning task id")
            where.append(AgentMemory.owner_id == owner_id)

        if project_id is not None:
            # A project query may see that project's rows and project-agnostic rows only.
            where.append(sa.or_(AgentMemory.project_id == project_id, AgentMemory.project_id.is_(None)))
        else:
            where.append(AgentMemory.project_id.is_(None))
        if objective_id:
            where.append(sa.or_(AgentMemory.objective_id == objective_id, AgentMemory.objective_id.is_(None)))
        if not include_expired:
            where.append(sa.or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > _utcnow()))
        if search:
            needle = f"%{search.strip().lower()}%"
            where.append(
                sa.or_(
                    sa.func.lower(AgentMemory.content).like(needle),
                    sa.func.lower(sa.func.coalesce(AgentMemory.title, "")).like(needle),
                )
            )

        rows = list(
            self.session.scalars(
                sa.select(AgentMemory)
                .where(*where)
                .order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc())
                .limit(max(1, min(limit, 100)))
            )
        )
        if owner_id:
            # Only agent- and task-level rows are private to their owner. Department rows
            # are owned by the department and are shared by everyone inside it.
            rows = [
                row
                for row in rows
                if row.scope not in (MemoryScope.AGENT, MemoryScope.TASK)
                or row.owner_id in (None, owner_id)
            ]
        return rows

    def scopes_included_by(self, scope: MemoryScope) -> list[MemoryScope]:
        """A query sees its own scope, everything narrower, and the shared layers.

        GLOBAL and LONG_TERM are the only scopes that cross projects, so they are
        always included; everything else stays inside the requested branch.
        """
        index = SCOPE_ORDER.index(scope)
        included = list(SCOPE_ORDER[: index + 1])
        for shared in (MemoryScope.GLOBAL, MemoryScope.LONG_TERM):
            if shared not in included:
                included.append(shared)
        return included

    # ------------------------------------------------------------- retention
    def enforce_retention(self, *, dry_run: bool = False) -> dict[str, int]:
        now = _utcnow()
        deleted = 0
        trimmed = 0
        expired = list(self.session.scalars(sa.select(AgentMemory).where(AgentMemory.expires_at < now)))
        for row in expired:
            if not dry_run:
                self.session.delete(row)
            deleted += 1

        for scope in SCOPE_ORDER:
            rows = list(
                self.session.scalars(
                    sa.select(AgentMemory).where(AgentMemory.scope == scope).order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc())
                )
            )
            total = 0
            for row in rows:
                total += len(row.content or "")
                if total > MAX_CHARS_PER_SCOPE:
                    if not dry_run:
                        self.session.delete(row)
                    trimmed += 1
        if not dry_run and (deleted or trimmed):
            self.audit.record(
                "agent.memory.retention",
                resource_type="agent_memory",
                outcome="success",
                metadata={"expired_deleted": deleted, "over_budget_removed": trimmed},
            )
            self.session.commit()
        return {"expired_deleted": deleted, "over_budget_removed": trimmed, "dry_run": dry_run}

    def stats(self) -> dict[str, Any]:
        by_scope = dict(
            self.session.execute(
                sa.select(AgentMemory.scope, sa.func.count()).group_by(AgentMemory.scope)
            ).all()
        )
        total_chars = self.session.scalar(sa.select(sa.func.coalesce(sa.func.sum(sa.func.length(AgentMemory.content)), 0)))
        now = _utcnow()
        expired = self.session.scalar(
            sa.select(sa.func.count()).select_from(AgentMemory).where(AgentMemory.expires_at < now)
        )
        return {
            "total": int(sum(by_scope.values())),
            "by_scope": {str(key): int(value) for key, value in by_scope.items()},
            "total_chars": int(total_chars or 0),
            "expired": int(expired or 0),
            "budget_chars_per_scope": MAX_CHARS_PER_SCOPE,
            "retention_days": {str(scope): days for scope, days in RETENTION_DAYS.items()},
        }

    def list_recent(self, *, limit: int = 50, scope: str | None = None) -> list[AgentMemory]:
        stmt = sa.select(AgentMemory).order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc())
        if scope:
            stmt = stmt.where(AgentMemory.scope == MemoryScope(scope))
        return list(self.session.scalars(stmt.limit(max(1, min(limit, 200)))))


def redacted_preview(content: str, limit: int = 240) -> str:
    preview = redact_value(content)
    return preview if len(preview) <= limit else preview[:limit] + "…"
