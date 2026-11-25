"""Hash-bound human approvals.

Phase 4 records intent; it never performs the external action. What matters now is
that the record is trustworthy when Phase 7 starts acting on it:

- the payload is hashed, so approving "publish this caption" cannot silently apply
  to a different caption later;
- approvals are single-use (`consumed_at`) and expire;
- the hash is re-verified at consumption time, so a mutated payload is refused;
- only pending requests can be decided, in one direction.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..db.enums import ApprovalState
from ..db.models import ApprovalRequest
from .backup import AuditService


class ApprovalNotFound(KeyError):
    code = "APPROVAL_NOT_FOUND"


class ApprovalStateError(ValueError):
    code = "APPROVAL_WRONG_STATE"


class ApprovalHashMismatch(ValueError):
    code = "APPROVAL_HASH_MISMATCH"


class ApprovalExpired(ValueError):
    code = "APPROVAL_EXPIRED"


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ApprovalService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)

    # --------------------------------------------------------------- create
    def create(
        self,
        *,
        action: str,
        target_type: str,
        target_id: str,
        summary: str,
        payload: dict[str, Any],
        agent_id: str | None = None,
        task_id: str | None = None,
        objective_id: str | None = None,
        rationale: str | None = None,
        risks: list[str] | None = None,
        ttl_hours: int = 72,
        requested_by: str = "agent",
        correlation_id: str | None = None,
    ) -> ApprovalRequest:
        digest = payload_hash(payload)
        duplicate = self.session.scalars(
            sa.select(ApprovalRequest).where(
                ApprovalRequest.payload_hash == digest,
                ApprovalRequest.state == ApprovalState.PENDING,
            )
        ).first()
        if duplicate is not None:
            return duplicate
        row = ApprovalRequest(
            action=action,
            state=ApprovalState.PENDING,
            agent_id=agent_id,
            task_id=task_id,
            objective_id=objective_id,
            target_type=target_type,
            target_id=target_id,
            summary=summary,
            rationale=rationale,
            risks_json=list(risks or []),
            payload_json=payload,
            payload_hash=digest,
            requested_by=requested_by,
            expires_at=_utcnow() + timedelta(hours=max(1, min(ttl_hours, 720))),
        )
        self.session.add(row)
        self.session.flush()
        self.audit.record(
            "approval.request",
            resource_type="approval_request",
            resource_id=row.id,
            outcome="success",
            actor=requested_by,
            metadata={
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "agent_id": agent_id,
                "payload_hash": digest,
            },
            correlation_id=correlation_id,
        )
        self.session.flush()
        return row

    # ----------------------------------------------------------------- read
    def get(self, request_id: str) -> ApprovalRequest:
        row = self.session.get(ApprovalRequest, request_id)
        if row is None:
            raise ApprovalNotFound(request_id)
        return row

    def list(
        self, *, state: str | None = None, objective_id: str | None = None, limit: int = 50
    ) -> list[ApprovalRequest]:
        stmt = sa.select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
        if state:
            stmt = stmt.where(ApprovalRequest.state == ApprovalState(state))
        if objective_id:
            stmt = stmt.where(ApprovalRequest.objective_id == objective_id)
        return list(self.session.scalars(stmt.limit(max(1, min(limit, 200)))))

    # --------------------------------------------------------------- decide
    def decide(
        self,
        request_id: str,
        *,
        approve: bool,
        decided_by: str = "local-user",
        note: str | None = None,
        correlation_id: str | None = None,
    ) -> ApprovalRequest:
        row = self.get(request_id)
        if ApprovalState(row.state) is not ApprovalState.PENDING:
            raise ApprovalStateError(f"approval {request_id} is {row.state}, not pending")
        expires = _aware(row.expires_at)
        if expires is not None and expires < _utcnow():
            row.state = ApprovalState.EXPIRED
            self.session.commit()
            raise ApprovalExpired(f"approval {request_id} expired at {expires.isoformat()}")

        row.state = ApprovalState.APPROVED if approve else ApprovalState.REJECTED
        row.decided_at = _utcnow()
        row.decided_by = decided_by
        row.decision_note = note
        self.audit.record(
            "approval.decide",
            resource_type="approval_request",
            resource_id=row.id,
            outcome="success" if approve else "denied",
            actor=decided_by,
            metadata={"action": row.action, "approved": approve, "payload_hash": row.payload_hash},
            correlation_id=correlation_id,
        )
        self.session.commit()
        return row

    def cancel(self, request_id: str, *, actor: str = "local-user") -> ApprovalRequest:
        row = self.get(request_id)
        if ApprovalState(row.state) is not ApprovalState.PENDING:
            raise ApprovalStateError(f"approval {request_id} is {row.state}, not pending")
        row.state = ApprovalState.CANCELLED
        row.decided_at = _utcnow()
        row.decided_by = actor
        self.session.commit()
        return row

    # --------------------------------------------------------------- consume
    def verify_for_use(self, request_id: str, payload: dict[str, Any] | None = None) -> ApprovalRequest:
        """Re-verify an approval immediately before acting on it.

        Phase 7 calls this before any external request. It fails closed on: wrong
        state, expiry, a payload that no longer matches the approved hash.
        """
        row = self.get(request_id)
        if ApprovalState(row.state) is not ApprovalState.APPROVED:
            raise ApprovalStateError(f"approval {request_id} is {row.state}, not approved")
        if row.consumed_at is not None:
            raise ApprovalStateError(f"approval {request_id} was already used at {row.consumed_at.isoformat()}")
        expires = _aware(row.expires_at)
        if expires is not None and expires < _utcnow():
            raise ApprovalExpired(f"approval {request_id} expired")
        if payload is not None and payload_hash(payload) != row.payload_hash:
            raise ApprovalHashMismatch("the payload changed after approval; ask the human again")
        return row

    def consume(self, request_id: str, *, payload: dict[str, Any] | None = None) -> ApprovalRequest:
        row = self.verify_for_use(request_id, payload)
        row.consumed_at = _utcnow()
        self.audit.record(
            "approval.consume",
            resource_type="approval_request",
            resource_id=row.id,
            outcome="success",
            metadata={"action": row.action, "payload_hash": row.payload_hash},
        )
        self.session.commit()
        return row

    def expire_stale(self) -> int:
        now = _utcnow()
        stale = list(
            self.session.scalars(
                sa.select(ApprovalRequest).where(
                    ApprovalRequest.state == ApprovalState.PENDING,
                    ApprovalRequest.expires_at.is_not(None),
                    ApprovalRequest.expires_at < now,
                )
            )
        )
        for row in stale:
            row.state = ApprovalState.EXPIRED
        if stale:
            self.session.commit()
        return len(stale)
