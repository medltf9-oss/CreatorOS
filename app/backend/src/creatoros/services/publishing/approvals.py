"""Approval binding for publishing (D4).

Two different tables answer two different questions, and the phase needs both:

* `approval_request` (Phase 4, :class:`creatoros.services.approvals.ApprovalService`) is the **gate**:
  a hash-bound, single-use, expiring request that a human approves or rejects.
* `approval_record` is the **ledger**: one immutable row saying "this actor consented to this exact
  payload at this time, under this consent text".

The published payload is a canonical JSON document. Anything that changes what the provider would
receive changes the hash, so the old consent simply stops matching - it is not "re-checked and
found mostly fine". The list of things that count as a change is the tamper matrix in the tests, and
it matches :data:`~creatoros.services.publishing.composer.CONTENT_FIELDS` on purpose.

Why the hash is computed from live rows rather than from a cached copy: a cached copy would be a
second source of truth, and the whole point of the hash is that it is recomputed from what will
actually be sent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import ApprovalState, PublishMode, PublishState, can_transition
from ...db.models import (
    AccountCapability,
    ApprovalRecord,
    ApprovalRequest,
    CapabilitySnapshot,
    PlatformVariant,
    PublishJob,
    SocialAccount,
)
from ...services.approvals import (
    ApprovalExpired,
    ApprovalHashMismatch,
    ApprovalNotFound,
    ApprovalService,
    ApprovalStateError,
)
from ..backup import AuditService
from .composer import PUBLISH_CAPABILITY
from .errors import PublishingError

#: Bumped whenever the wording a human agrees to changes. Stored on every ledger row so an old
#: consent is never re-read as if the person had agreed to the new wording.
CONSENT_TEXT_VERSION = "publish-consent-1"

CONSENT_TEXT = (
    "I understand that approving this destination publishes the selected media and caption to the "
    "named social account through the official provider API. Publication is public according to "
    "the account's privacy setting, the provider may take minutes to process it, and removing it "
    "afterwards is a manual action in the provider's own application. Approval applies to this "
    "exact content: editing the caption, media, options or schedule cancels it."
)

PAYLOAD_VERSION = "publish-payload-1"


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()
    return str(value)


class PublishApprovalService:
    """Request, decide, verify and spend human consent for one destination."""

    def __init__(self, session: Session, audit: AuditService | None = None) -> None:
        self.session = session
        self.approvals = ApprovalService(session)
        self.audit = audit or self.approvals.audit

    # ------------------------------------------------------------------ payload
    def canonical_payload(self, job: PublishJob) -> dict[str, Any]:
        """The exact document a human consents to, derived from the rows the executor will read."""
        variant = self.session.get(PlatformVariant, job.variant_id)
        if variant is None:
            raise PublishingError(
                "DESTINATION_NOT_FOUND",
                "this destination no longer exists",
                http_status=404,
                detail={"variant_id": job.variant_id},
            )
        account = self.session.get(SocialAccount, job.account_id)
        if account is None:
            raise PublishingError(
                "ACCOUNT_NOT_FOUND",
                "the destination's account no longer exists",
                http_status=404,
                detail={"account_id": job.account_id},
            )
        capability = self.session.scalar(
            sa.select(AccountCapability).where(
                AccountCapability.account_id == account.id,
                AccountCapability.capability
                == PUBLISH_CAPABILITY.get((str(job.platform), self._mode(job)), ""),
            )
        )
        snapshot = self.session.scalar(
            sa.select(CapabilitySnapshot)
            .where(CapabilitySnapshot.account_id == account.id)
            .order_by(CapabilitySnapshot.checked_at.desc())
            .limit(1)
        )
        report = variant.validation_report_json or {}
        return {
            "version": PAYLOAD_VERSION,
            "consent_text_version": CONSENT_TEXT_VERSION,
            "action": "publish.post",
            "intent_id": job.intent_key or variant.project_id,
            "destination_id": variant.id,
            "job_id": job.id,
            "platform": str(job.platform),
            "publish_mode": str(job.publish_mode),
            "account": {
                "id": account.id,
                "display_name": account.display_name,
                "account_type": str(account.account_type),
                "provider_account_id": account.provider_account_id,
            },
            "content": {
                "title": job.title,
                "caption": job.caption,
                "hashtags": list(job.hashtags_json or []),
            },
            "media": {
                "asset_id": job.asset_id,
                "asset_sha256": job.asset_sha256,
                "derivative_id": job.derivative_id,
                "derivative_sha256": job.derivative_sha256,
                "derivative_profile": job.derivative_profile,
                "derivative_version": job.derivative_version,
            },
            "options": dict(job.options_json or {}),
            "privacy_setting": variant.privacy_setting,
            "schedule": {
                "scheduled_at_utc": _iso(job.scheduled_at_utc),
                "display_timezone": job.display_timezone,
            },
            "capabilities": {
                "capability": capability.capability if capability else None,
                "state": str(capability.state) if capability else None,
                "snapshot_id": snapshot.id if snapshot else None,
                "checked_at": _iso(snapshot.checked_at) if snapshot else None,
            },
            "validation": {
                "outcome": report.get("outcome"),
                "codes": sorted({finding.get("code") for finding in report.get("findings", [])}),
            },
        }

    def payload_hash(self, job: PublishJob) -> str:
        return hash_payload(self.canonical_payload(job))

    # ------------------------------------------------------------------ request
    def request(
        self,
        job: PublishJob,
        *,
        requested_by: str = "local-user",
        ttl_hours: int = 72,
        summary: str | None = None,
        risks: list[str] | None = None,
    ) -> Any:
        """Create (or refresh) the approval request for the destination's current content."""
        if job.job_state not in {
            PublishState.DRAFT,
            PublishState.PREPARING,
            PublishState.VALIDATION_PENDING,
            PublishState.AWAITING_APPROVAL,
        }:
            raise PublishingError(
                "DESTINATION_NOT_AWAITING_APPROVAL",
                "This destination is past the approval stage.",
                http_status=409,
                detail={"state": str(job.job_state)},
            )

        report = (self.session.get(PlatformVariant, job.variant_id).validation_report_json or {})
        if report.get("outcome") == "failed":
            raise PublishingError(
                "VALIDATION_FAILED",
                "This destination does not pass validation, so there is nothing to approve yet.",
                http_status=422,
                detail={
                    "codes": sorted(
                        finding.get("code") for finding in report.get("findings", []) if finding.get("severity") == "failed"
                    )
                },
            )

        payload = self.canonical_payload(job)
        digest = hash_payload(payload)
        previous = job.approval_request_id

        if previous:
            existing = self.session.get(ApprovalRequest, previous)
            if existing is not None and existing.payload_hash == digest and existing.state == ApprovalState.PENDING:
                # Same content, same pending question: do not spam the human with a second one.
                job.approval_payload_hash = digest
                job.payload_hash = digest
                self._move(job, PublishState.AWAITING_APPROVAL, reason="awaiting_approval")
                return existing
            if existing is not None and existing.state == ApprovalState.PENDING:
                self.approvals.cancel(previous, actor=requested_by)

        row = self.approvals.create(
            action="publish.post",
            target_type="publish_job",
            target_id=job.id,
            summary=summary
            or (
                f"Publish a {str(job.publish_mode).replace('_', ' ')} to "
                f"{payload['account']['display_name']} ({payload['platform']})"
            ),
            payload=payload,
            rationale=f"Destination {job.id} for intent {payload['intent_id']}.",
            risks=risks
            or [
                "The post becomes visible to the audience selected by the account's privacy setting.",
                "The provider may process the upload for several minutes after this approval.",
            ],
            ttl_hours=ttl_hours,
            requested_by=requested_by,
        )
        job.approval_request_id = row.id
        job.approval_payload_hash = row.payload_hash
        job.payload_hash = row.payload_hash
        job.approval_record_id = None
        if job.job_state is PublishState.DRAFT:
            self._move(job, PublishState.VALIDATION_PENDING, reason="validation_after_edit")
        self._move(job, PublishState.AWAITING_APPROVAL, reason="awaiting_approval")
        self.audit.record(
            "publish.approval.request",
            resource_type="publish_job",
            resource_id=job.id,
            actor=requested_by,
            metadata={"request_id": row.id, "payload_hash": row.payload_hash},
        )
        return row

    # ------------------------------------------------------------------- decide
    def decide(
        self,
        job: PublishJob,
        *,
        approve: bool,
        decided_by: str = "local-user",
        note: str | None = None,
    ) -> Any:
        if not job.approval_request_id:
            raise PublishingError(
                "APPROVAL_MISSING",
                "There is no approval request for this destination.",
                http_status=409,
            )
        row = self.approvals.decide(
            job.approval_request_id, approve=approve, decided_by=decided_by, note=note
        )
        if approve:
            self._move(job, PublishState.APPROVED, reason="approved_by_human")
        else:
            self._move(job, PublishState.DRAFT, reason="approval_rejected")
        self.audit.record(
            "publish.approval.decide",
            resource_type="publish_job",
            resource_id=job.id,
            outcome="success" if approve else "denied",
            actor=decided_by,
            metadata={"request_id": row.id, "payload_hash": row.payload_hash},
        )
        return row

    # ------------------------------------------------------------------- verify
    def verify(self, job: PublishJob) -> Any:
        """Re-derive the payload and check it against the consent. Fails closed."""
        if not job.approval_request_id:
            raise PublishingError("APPROVAL_MISSING", "This destination has no approval request.", http_status=409)
        digest = self.payload_hash(job)
        if job.approval_payload_hash and job.approval_payload_hash != digest:
            raise PublishingError(
                "APPROVAL_HASH_MISMATCH",
                "The destination changed after approval; request approval again.",
                http_status=409,
                detail={"approved_hash": job.approval_payload_hash, "current_hash": digest},
            )
        if job.payload_hash and job.payload_hash != digest:
            raise PublishingError(
                "APPROVAL_HASH_MISMATCH",
                "The destination's stored payload hash no longer matches its content.",
                http_status=409,
                detail={"stored_hash": job.payload_hash, "current_hash": digest},
            )
        request_row = self.approvals.get(job.approval_request_id)
        stored_digest = hash_payload(request_row.payload_json or {})
        if stored_digest != request_row.payload_hash:
            # The stored copy of what the human read no longer hashes to what they signed.
            raise PublishingError(
                "APPROVAL_HASH_MISMATCH",
                "The stored approval payload was modified after it was decided.",
                http_status=409,
                detail={"stored_hash": request_row.payload_hash, "recomputed_hash": stored_digest},
            )
        payload = self.canonical_payload(job)
        try:
            return self.approvals.verify_for_use(job.approval_request_id, payload)
        except ApprovalHashMismatch as exc:
            raise PublishingError(
                "APPROVAL_HASH_MISMATCH",
                "The approved payload does not match the destination any more.",
                http_status=409,
                detail={"current_hash": digest},
            ) from exc
        except ApprovalExpired as exc:
            raise PublishingError(
                "APPROVAL_EXPIRED", "This approval expired; request a fresh one.", http_status=409
            ) from exc
        except ApprovalStateError as exc:
            raise PublishingError(
                "APPROVAL_NOT_USABLE", str(exc), http_status=409
            ) from exc
        except ApprovalNotFound as exc:
            raise PublishingError(
                "APPROVAL_MISSING", "The approval request no longer exists.", http_status=409
            ) from exc

    # ------------------------------------------------------------------ consume
    def consume(self, job: PublishJob, *, actor: str = "local-user") -> Any:
        """Spend the consent and write the ledger row. Called immediately before the first request."""
        row = self.verify(job)
        self.approvals.consume(row.id, payload=self.canonical_payload(job))
        record = self._ledger_row(job, row, actor=actor)
        job.approval_record_id = record.id
        self.audit.record(
            "publish.approval.consume",
            resource_type="publish_job",
            resource_id=job.id,
            actor=actor,
            metadata={"request_id": row.id, "record_id": record.id, "payload_hash": row.payload_hash},
        )
        self.session.flush()
        return record

    def _ledger_row(self, job: PublishJob, request_row: Any, *, actor: str) -> Any:
        existing = self.session.scalar(
            sa.select(ApprovalRecord).where(
                ApprovalRecord.target_type == "publish_job",
                ApprovalRecord.target_id == job.id,
                ApprovalRecord.payload_hash == request_row.payload_hash,
            )
        )
        if existing is not None:
            return existing
        record = ApprovalRecord(
            actor=actor,
            action="publish.post",
            target_type="publish_job",
            target_id=job.id,
            payload_hash=request_row.payload_hash,
            consent_text_version=CONSENT_TEXT_VERSION,
            approved_at=request_row.decided_at or datetime.now(UTC),
            expires_at=request_row.expires_at,
            result="granted",
        )
        self.session.add(record)
        self.session.flush()
        return record

    # ------------------------------------------------------------------ helpers
    def _move(self, job: PublishJob, state: PublishState, *, reason: str) -> None:
        current = PublishState(job.job_state)
        if current is state:
            return
        if not can_transition(current, state):
            raise PublishingError(
                "ILLEGAL_STATE_TRANSITION",
                f"cannot move a destination from {current.value} to {state.value}",
                http_status=409,
                detail={"from": current.value, "to": state.value},
            )
        job.job_state = state
        job.state_reason = reason[:300]

    @staticmethod
    def _mode(job: PublishJob) -> PublishMode:
        return job.publish_mode if isinstance(job.publish_mode, PublishMode) else PublishMode(str(job.publish_mode))
