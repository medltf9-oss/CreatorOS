"""Read models for the publishing GUI and API.

Everything here is a projection of `publish_job` and its ledger tables: no method publishes, and no
serialiser can emit a secret. Three rules are applied by hand in every projection:

* no tokens, no upload URLs, no signed URLs, no provider request bodies - only decisions and ids;
* no local media paths. A caller that needs the bytes asks the media API, which has its own access
  check; a path in a JSON body would be a second, unaudited way in;
* times are UTC instants plus the zone the user chose, so a renderer never has to guess.

The detail projection is the one the "why is this stuck?" question is answered from, so it always
carries the attempt ledger, the delivery ledger, the reconciliation cases and the approval record
instead of a single `error` string.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import (
    NEEDS_HUMAN_PUBLISH_STATES,
    Platform,
    PublishMode,
    PublishState,
)
from ...db.models import (
    ApprovalRecord,
    ContentProject,
    MediaAsset,
    MediaDerivative,
    PlatformVariant,
    PublishAttempt,
    PublishDelivery,
    PublishJob,
    PublishReconciliation,
    SocialAccount,
)
from ..backup import AuditService

#: Keys that must never leave the backend, whatever a JSON column happens to contain.
REDACTED_KEYS = frozenset(
    {
        "access_token",
        "token",
        "secret",
        "client_secret",
        "upload_url",
        "signed_url",
        "url",
        "presigned",
        "authorization",
        "password",
        "refresh_token",
        "code_verifier",
    }
)
ATTENTION_STATES = tuple(sorted(NEEDS_HUMAN_PUBLISH_STATES | {PublishState.FAILED}, key=str))
OPEN_STATES = (
    PublishState.DRAFT,
    PublishState.PREPARING,
    PublishState.VALIDATION_PENDING,
    PublishState.AWAITING_APPROVAL,
    PublishState.APPROVED,
    PublishState.SCHEDULED,
    PublishState.READY,
    PublishState.UPLOADING,
    PublishState.UPLOADED,
    PublishState.PROCESSING,
    PublishState.UNKNOWN_REMOTE_STATE,
    PublishState.NEEDS_RECONCILIATION,
    PublishState.REQUIRES_USER_ACTION,
    PublishState.BLOCKED,
    PublishState.FAILED,
)


def redact(value: Any) -> Any:
    """Strip anything that looks like a credential from a structure that came from the database."""
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if str(key).lower() not in REDACTED_KEYS}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass(slots=True)
class JobQuery:
    platform: Platform | None = None
    account_id: str | None = None
    campaign_id: str | None = None
    project_id: str | None = None
    state: Sequence[PublishState] = field(default_factory=tuple)
    mode: PublishMode | None = None
    since: datetime | None = None
    until: datetime | None = None
    search: str | None = None
    limit: int = 50
    offset: int = 0

    def clauses(self) -> list[Any]:
        where: list[Any] = []
        if self.platform is not None:
            where.append(PublishJob.platform == self.platform)
        if self.account_id is not None:
            where.append(PublishJob.account_id == self.account_id)
        if self.project_id is not None:
            where.append(PlatformVariant.project_id == self.project_id)
        if self.state:
            where.append(PublishJob.job_state.in_(list(self.state)))
        if self.mode is not None:
            where.append(PublishJob.publish_mode == self.mode)
        if self.since is not None:
            where.append(PublishJob.created_at >= self.since)
        if self.until is not None:
            where.append(PublishJob.created_at <= self.until)
        if self.search:
            needle = f"%{self.search.strip()}%"
            where.append(
                sa.or_(
                    PublishJob.caption.ilike(needle),
                    PublishJob.title.ilike(needle),
                    PlatformVariant.caption.ilike(needle),
                )
            )
        if self.campaign_id is not None:
            where.append(ContentProject.campaign_id == self.campaign_id)
        return where


class PublishHistory:
    """Every read the publishing UI does, in one place so the redaction rules are in one place."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ summary
    def summary(self) -> dict[str, Any]:
        counts = dict(
            self.session.execute(
                sa.select(PublishJob.job_state, sa.func.count()).group_by(PublishJob.job_state)
            ).all()
        )
        by_state = {str(state): int(total) for state, total in counts.items()}
        attention = sum(by_state.get(str(state), 0) for state in ATTENTION_STATES)
        published = by_state.get(str(PublishState.PUBLISHED), 0)
        now = datetime.now(UTC)
        upcoming = self.session.scalar(
            sa.select(sa.func.count())
            .select_from(PublishJob)
            .where(
                PublishJob.scheduled_at_utc.is_not(None),
                PublishJob.scheduled_at_utc > now,
                PublishJob.job_state.in_([PublishState.SCHEDULED, PublishState.APPROVED]),
            )
        )
        published_week = self.session.scalar(
            sa.select(sa.func.count())
            .select_from(PublishJob)
            .where(
                PublishJob.job_state == PublishState.PUBLISHED,
                PublishJob.provider_published_at.is_not(None),
                PublishJob.provider_published_at >= now - timedelta(days=7),
            )
        )
        return {
            "by_state": by_state,
            "total": sum(by_state.values()),
            "open": sum(by_state.get(str(state), 0) for state in OPEN_STATES),
            "needs_attention": int(attention),
            "published": int(published),
            "published_last_7d": int(published_week or 0),
            "scheduled_upcoming": int(upcoming or 0),
            "platforms": self._platform_counts(),
        }

    def _platform_counts(self) -> list[dict[str, Any]]:
        rows = self.session.execute(
            sa.select(PublishJob.platform, PublishJob.job_state, sa.func.count()).group_by(
                PublishJob.platform, PublishJob.job_state
            )
        ).all()
        buckets: dict[str, dict[str, int]] = {}
        for platform, state, total in rows:
            bucket = buckets.setdefault(str(platform), {})
            bucket[str(state)] = int(total)
        return [{"platform": platform, "by_state": states} for platform, states in sorted(buckets.items())]

    # --------------------------------------------------------------------- lists
    def jobs(self, query: JobQuery) -> tuple[list[dict[str, Any]], int]:
        base = (
            sa.select(PublishJob, PlatformVariant)
            .join(PlatformVariant, PlatformVariant.id == PublishJob.variant_id)
            .join(ContentProject, ContentProject.id == PlatformVariant.project_id, isouter=True)
        )
        count_stmt = (
            sa.select(sa.func.count())
            .select_from(PublishJob)
            .join(PlatformVariant, PlatformVariant.id == PublishJob.variant_id)
            .join(ContentProject, ContentProject.id == PlatformVariant.project_id, isouter=True)
        )
        for clause in query.clauses():
            base = base.where(clause)
            count_stmt = count_stmt.where(clause)
        total = int(self.session.scalar(count_stmt) or 0)
        rows = self.session.execute(
            base.order_by(PublishJob.created_at.desc()).limit(query.limit).offset(query.offset)
        ).all()
        accounts = self._accounts({job.account_id for job, _ in rows})
        return [self._job_row(job, variant, accounts) for job, variant in rows], total

    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self.session.execute(
            sa.select(PublishJob, PlatformVariant, ContentProject)
            .join(PlatformVariant, PlatformVariant.id == PublishJob.variant_id)
            .join(ContentProject, ContentProject.id == PlatformVariant.project_id, isouter=True)
            .where(PublishJob.id == job_id)
        ).first()
        if row is None:
            return None
        job, variant, project = row
        accounts = self._accounts({job.account_id})
        detail = self._job_row(job, variant, accounts)
        asset, derivative = self._media(job)
        detail.update(
            {
                "project": None
                if project is None
                else {
                    "id": project.id,
                    "title": project.title,
                    "campaign_id": project.campaign_id,
                    "status_note": getattr(project, "status_note", None),
                    "status": str(project.status) if project.status is not None else None,
                },
                "variant": {
                    "id": variant.id,
                    "aspect_ratio": variant.aspect_ratio,
                    "duration_ms": variant.duration_ms,
                    "width": variant.width,
                    "height": variant.height,
                    "privacy_setting": variant.privacy_setting,
                    "cover_path_present": bool(variant.cover_path),
                },
                "media": {
                    "asset_id": job.asset_id,
                    "asset_sha256": job.asset_sha256,
                    "derivative_id": job.derivative_id,
                    "derivative_sha256": job.derivative_sha256,
                    "derivative_profile": job.derivative_profile,
                    "bytes": asset.size_bytes if asset is not None else None,
                    "duration_ms": derivative.duration_ms if derivative is not None else None,
                    "kind": str(asset.kind) if asset is not None else None,
                },
                "approval_record": self._approval(job),
                "attempts_log": self.attempts(job_id),
                "deliveries": self.deliveries(job_id),
                "reconciliations": self.reconciliations(job_id),
                "options": redact(dict(job.options_json or {})),
                "metadata": redact(dict(job.metadata_json or {})),
                "field_provenance": redact(dict(job.field_provenance_json or {})),
                "reconcile_attempts": int(job.reconcile_attempts or 0),
                "remote_checked_at": job.remote_checked_at,
            }
        )
        return detail

    def attempts(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            sa.select(PublishAttempt)
            .where(PublishAttempt.job_id == job_id)
            .order_by(PublishAttempt.attempt_no.desc())
        )
        return [
            {
                "id": row.id,
                "attempt_no": row.attempt_no,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "outcome": row.outcome,
                "provider_operation": row.provider_operation,
                "http_status": row.http_status,
                "request_id": row.request_id,
                "response_code": row.response_code,
                "error_category": None if row.error_category is None else str(row.error_category),
                "response_excerpt": row.response_excerpt_redacted,
                "remote_snapshot": redact(dict(row.remote_snapshot_json or {})),
            }
            for row in rows
        ]

    def deliveries(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            sa.select(PublishDelivery)
            .where(PublishDelivery.job_id == job_id)
            .order_by(PublishDelivery.created_at.desc())
        )
        now = datetime.now(UTC)
        return [
            {
                "id": row.id,
                "provider": row.provider,
                "bucket": row.bucket,
                "object_key": row.object_key,
                "state": str(row.state),
                "bytes": int(row.bytes or 0),
                "checksum": row.sha256,
                "uploaded_at": row.uploaded_at,
                "verified_at": row.verified_at,
                "expires_at": row.url_expires_at,
                "expired": bool(row.url_expires_at and row.url_expires_at <= now),
                "deleted_at": row.deleted_at,
                "delete_attempts": int(row.delete_attempts or 0),
                "error_detail": row.error_detail,
            }
            for row in rows
        ]

    def deliveries_for_all(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The delivery ledger across destinations: what is still in the bucket, and until when."""
        rows = self.session.scalars(
            sa.select(PublishDelivery).order_by(PublishDelivery.created_at.desc()).limit(limit)
        )
        now = datetime.now(UTC)
        return [
            {
                "id": row.id,
                "job_id": row.job_id,
                "provider": row.provider,
                "bucket": row.bucket,
                "object_key": row.object_key,
                "state": str(row.state),
                "bytes": int(row.bytes or 0),
                "uploaded_at": row.uploaded_at,
                "expires_at": row.url_expires_at,
                "expired": bool(row.url_expires_at and row.url_expires_at <= now),
                "deleted_at": row.deleted_at,
                "error_detail": row.error_detail,
            }
            for row in rows
        ]

    def reconciliations(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            sa.select(PublishReconciliation)
            .where(PublishReconciliation.job_id == job_id)
            .order_by(PublishReconciliation.checked_at.desc())
        )
        return [
            {
                "id": row.id,
                "checked_at": row.checked_at,
                "provider_state": row.provider_state,
                "decision": str(row.decision),
                "resolved_by": row.resolved_by,
                "evidence": redact(dict(row.evidence_json or {})),
            }
            for row in rows
        ]

    # ----------------------------------------------------------------- aggregates
    def attention(self, *, limit: int = 50) -> list[dict[str, Any]]:
        query = JobQuery(state=ATTENTION_STATES, limit=limit)
        rows, _ = self.jobs(query)
        return rows

    def queue_snapshot(self, *, limit: int = 50) -> dict[str, Any]:
        running = JobQuery(state=(PublishState.READY, PublishState.UPLOADING, PublishState.UPLOADED, PublishState.PROCESSING), limit=limit)
        unknown = JobQuery(state=(PublishState.UNKNOWN_REMOTE_STATE, PublishState.NEEDS_RECONCILIATION), limit=limit)
        running_rows, running_total = self.jobs(running)
        unknown_rows, unknown_total = self.jobs(unknown)
        return {
            "running": running_rows,
            "running_total": running_total,
            "unknown": unknown_rows,
            "unknown_total": unknown_total,
            "leases_held": sum(1 for row in running_rows if row["lease"]["held"]),
        }

    def schedule(self, *, days: int = 14) -> dict[str, Any]:
        now = datetime.now(UTC)
        upcoming = JobQuery(
            state=(PublishState.SCHEDULED, PublishState.APPROVED),
            since=None,
            limit=200,
        )
        upcoming_rows, _ = self.jobs(upcoming)
        horizon = now + timedelta(days=days)
        scheduled = [
            row
            for row in upcoming_rows
            if row["scheduled_at_utc"] is not None and row["scheduled_at_utc"] <= horizon
        ]
        missed, missed_total = self.jobs(JobQuery(state=(PublishState.REQUIRES_USER_ACTION,), limit=50))
        return {
            "now": now,
            "scheduled": sorted(scheduled, key=lambda row: row["scheduled_at_utc"] or now),
            "missed": [row for row in missed if row["missed_at"] is not None],
            "missed_total": missed_total,
        }

    def published(self, query: JobQuery) -> tuple[list[dict[str, Any]], int]:
        return self.jobs(replace(query, state=(PublishState.PUBLISHED,)))

    def audit_trail(self, job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        from ...db.models import AuditEvent

        rows = self.session.scalars(
            sa.select(AuditEvent)
            .where(AuditEvent.resource_id == job_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(limit)
        )
        return [
            {
                "id": row.id,
                "action": row.action,
                "actor": row.actor,
                "outcome": row.outcome,
                "created_at": row.created_at,
                "metadata": redact(dict(row.metadata_redacted_json or {})),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ internals
    def _job_row(
        self, job: PublishJob, variant: PlatformVariant, accounts: dict[str, SocialAccount]
    ) -> dict[str, Any]:
        account = accounts.get(job.account_id)
        return {
            "id": job.id,
            "variant_id": job.variant_id,
            "project_id": variant.project_id if variant is not None else None,
            "platform": str(job.platform),
            "mode": str(job.publish_mode),
            "account_id": job.account_id,
            "account_name": None if account is None else account.display_name,
            "account_provider_id": None if account is None else account.provider_account_id,
            "state": str(job.job_state),
            "state_reason": job.state_reason,
            "title": job.title,
            "caption": job.caption,
            "hashtags": list(job.hashtags_json or []),
            "scheduled_at_utc": job.scheduled_at_utc,
            "display_timezone": job.display_timezone,
            "missed_at": job.missed_at,
            "attempts": int(job.attempts or 0),
            "provider_media_id": job.provider_media_id,
            "provider_permalink": job.provider_permalink,
            "provider_published_at": job.provider_published_at,
            "error_category": None if job.error_category is None else str(job.error_category),
            "last_error_code": job.last_error_code,
            "last_error_detail": job.last_error_detail,
            "upload": {
                "bytes_total": int(job.upload_bytes_total or 0),
                "bytes_done": int(job.upload_bytes_done or 0),
                "percent": _percent(job),
            },
            "lease": {
                "owner": job.lease_owner,
                "expires_at": job.lease_expires_at,
                "held": bool(job.lease_expires_at and job.lease_expires_at > datetime.now(UTC)),
            },
            "approval": {
                "requested": job.approval_request_id is not None,
                "approved": job.approval_record_id is not None,
                "payload_hash": job.approval_payload_hash,
            },
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    def _accounts(self, ids: Iterable[str]) -> dict[str, SocialAccount]:
        wanted = [account_id for account_id in ids if account_id]
        if not wanted:
            return {}
        rows = self.session.scalars(sa.select(SocialAccount).where(SocialAccount.id.in_(wanted)))
        return {row.id: row for row in rows}

    def _media(self, job: PublishJob) -> tuple[MediaAsset | None, MediaDerivative | None]:
        asset = self.session.get(MediaAsset, job.asset_id) if job.asset_id else None
        derivative = self.session.get(MediaDerivative, job.derivative_id) if job.derivative_id else None
        return asset, derivative

    def _approval(self, job: PublishJob) -> dict[str, Any] | None:
        if job.approval_record_id is None:
            return None
        record = self.session.get(ApprovalRecord, job.approval_record_id)
        if record is None:
            return None
        return {
            "id": record.id,
            "actor": record.actor,
            "action": record.action,
            "result": record.result,
            "approved_at": record.approved_at,
            "expires_at": record.expires_at,
            "payload_hash": record.payload_hash,
            "consent_text_version": record.consent_text_version,
        }


def _percent(job: PublishJob) -> int | None:
    total = int(job.upload_bytes_total or 0)
    if total <= 0:
        return None
    return max(0, min(100, int(100 * int(job.upload_bytes_done or 0) / total)))


def state_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        sa.select(PublishJob.job_state, sa.func.count()).group_by(PublishJob.job_state)
    ).all()
    return {str(state): int(total) for state, total in rows}


def audit_for(session: Session, job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    return PublishHistory(session).audit_trail(job_id, limit=limit)


def record_read(session: Session, job_id: str, *, actor: str = "local-user") -> None:
    """A read that matters is still an event: opening a destination's history is worth recording."""
    AuditService(session).record(
        "publish.history.read", resource_type="publish_job", resource_id=job_id, actor=actor
    )


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
