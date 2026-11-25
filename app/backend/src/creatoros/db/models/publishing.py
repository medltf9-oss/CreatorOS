"""Publishing tables: jobs, attempt ledger, approvals and webhook events.

Idempotency (INV-9) is enforced by a UNIQUE constraint on `idempotency_key`, and
every attempt is appended to `publish_attempt` so an ambiguous timeout can be
reconciled instead of blindly retried.
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, IdMixin, TimestampMixin, UtcDateTime, enum_column
from ..enums import (
    DeliveryAssetState,
    Platform,
    PublishErrorCategory,
    PublishMode,
    PublishState,
    ReconciliationDecision,
)


class PublishJob(Base, IdMixin, TimestampMixin):
    __tablename__ = "publish_job"
    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_publish_job_idempotency"),
        sa.Index("ix_publish_job_due", "job_state", "scheduled_at_utc"),
        sa.Index("ix_publish_job_account", "account_id", "created_at"),
    )

    variant_id: Mapped[str] = mapped_column(
        sa.ForeignKey("platform_variant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "job_platform"), nullable=False)
    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="RESTRICT"), nullable=False
    )
    scheduled_at_utc: Mapped[datetime | None] = mapped_column(UtcDateTime)
    display_timezone: Mapped[str] = mapped_column(sa.String(64), default="UTC", nullable=False)
    job_state: Mapped[PublishState] = mapped_column(
        enum_column(PublishState, "publish_state"), default=PublishState.DRAFT, nullable=False
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    remote_container_id: Mapped[str | None] = mapped_column(sa.String(190))
    remote_post_id: Mapped[str | None] = mapped_column(sa.String(190))
    remote_status_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(sa.String(80))
    last_error_detail: Mapped[str | None] = mapped_column(sa.Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    lease_owner: Mapped[str | None] = mapped_column(sa.String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # --- Phase 7: publishing engine -------------------------------------------
    # `intent_key` is the publishing intent: the same (project, platform, account) triple. It is
    # deliberately NOT globally unique, because republishing on purpose must stay possible; the
    # protection against *accidental* duplicates is the unique `idempotency_key` plus the queue's
    # one-open-job rule.
    intent_key: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    asset_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("media_asset.id", ondelete="SET NULL"), index=True
    )
    asset_sha256: Mapped[str | None] = mapped_column(sa.String(64))
    derivative_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("media_derivative.id", ondelete="SET NULL")
    )
    derivative_sha256: Mapped[str | None] = mapped_column(sa.String(64))
    derivative_profile: Mapped[str | None] = mapped_column(sa.String(60))
    derivative_version: Mapped[int | None] = mapped_column(sa.Integer)
    publish_mode: Mapped[PublishMode] = mapped_column(
        enum_column(PublishMode, "publish_mode"), default=PublishMode.VIDEO, nullable=False
    )
    title: Mapped[str | None] = mapped_column(sa.String(500))
    caption: Mapped[str] = mapped_column(sa.Text, default="", nullable=False)
    hashtags_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    options_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    # Which agent (if any) wrote which field, so a human can see what was machine-written.
    field_provenance_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    approval_request_id: Mapped[str | None] = mapped_column(sa.String(26))
    approval_payload_hash: Mapped[str | None] = mapped_column(sa.String(64))
    approval_record_id: Mapped[str | None] = mapped_column(sa.String(26))
    # Hash of the exact outbound payload: the anchor for "is this the thing that was approved?".
    payload_hash: Mapped[str | None] = mapped_column(sa.String(64))
    provider_upload_session_id: Mapped[str | None] = mapped_column(sa.String(190))
    provider_media_id: Mapped[str | None] = mapped_column(sa.String(190))
    provider_permalink: Mapped[str | None] = mapped_column(sa.String(1024))
    provider_published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    remote_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    upload_bytes_total: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    upload_bytes_done: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    upload_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    upload_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    error_category: Mapped[PublishErrorCategory | None] = mapped_column(
        enum_column(PublishErrorCategory, "publish_error_category")
    )
    state_reason: Mapped[str | None] = mapped_column(sa.String(300))
    missed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    reconcile_attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)

    attempts_log: Mapped[list[PublishAttempt]] = relationship(back_populates="job", cascade="all, delete-orphan")
    deliveries: Mapped[list[PublishDelivery]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    reconciliations: Mapped[list[PublishReconciliation]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class PublishAttempt(Base, IdMixin, TimestampMixin):
    __tablename__ = "publish_attempt"

    job_id: Mapped[str] = mapped_column(sa.ForeignKey("publish_job.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    http_status: Mapped[int | None] = mapped_column(sa.Integer)
    request_id: Mapped[str | None] = mapped_column(sa.String(190))
    response_code: Mapped[str | None] = mapped_column(sa.String(120))
    response_excerpt_redacted: Mapped[str | None] = mapped_column(sa.Text)
    outcome: Mapped[str] = mapped_column(sa.String(40), default="unknown", nullable=False)
    # --- Phase 7 ---------------------------------------------------------------
    provider_operation: Mapped[str | None] = mapped_column(sa.String(60))
    error_category: Mapped[PublishErrorCategory | None] = mapped_column(
        enum_column(PublishErrorCategory, "attempt_error_category")
    )
    # What the provider actually said, not only what failed. Reconciliation reads this.
    remote_snapshot_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)

    job: Mapped[PublishJob] = relationship(back_populates="attempts_log")


class PublishDelivery(Base, IdMixin, TimestampMixin):
    """A temporary cloud copy of one derivative, used where the provider fetches by URL.

    Rows are the audit trail for "what did we hand to the internet, when, and did we delete it".
    The object key is derived from the derivative hash, never from a filename.
    """

    __tablename__ = "publish_delivery"
    __table_args__ = (
        sa.UniqueConstraint("job_id", "object_key", name="uq_publish_delivery_object"),
        sa.Index("ix_publish_delivery_expiry", "state", "url_expires_at"),
    )

    job_id: Mapped[str] = mapped_column(
        sa.ForeignKey("publish_job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    bucket: Mapped[str] = mapped_column(sa.String(190), nullable=False)
    object_key: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    bytes: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    state: Mapped[DeliveryAssetState] = mapped_column(
        enum_column(DeliveryAssetState, "delivery_asset_state"),
        default=DeliveryAssetState.PENDING,
        nullable=False,
    )
    url_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    uploaded_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    delete_attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    error_detail: Mapped[str | None] = mapped_column(sa.String(500))

    job: Mapped[PublishJob] = relationship(back_populates="deliveries")


class PublishReconciliation(Base, IdMixin, TimestampMixin):
    """One attempt to establish what actually exists at the provider.

    Kept separate from `publish_attempt` because it records a *decision from evidence*, not a
    request we sent. `evidence_json` holds the redacted provider answer it was based on.
    """

    __tablename__ = "publish_reconciliation"

    job_id: Mapped[str] = mapped_column(
        sa.ForeignKey("publish_job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[str | None] = mapped_column(sa.String(26))
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    provider_state: Mapped[str | None] = mapped_column(sa.String(80))
    decision: Mapped[ReconciliationDecision] = mapped_column(
        enum_column(ReconciliationDecision, "reconciliation_decision"), nullable=False
    )
    evidence_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(sa.String(120))

    job: Mapped[PublishJob] = relationship(back_populates="reconciliations")


class ApprovalRecord(Base, IdMixin, TimestampMixin):
    """Immutable consent record. `payload_hash` binds approval to exact content."""

    __tablename__ = "approval_record"

    actor: Mapped[str] = mapped_column(sa.String(120), default="local-user", nullable=False)
    action: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    target_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    consent_text_version: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    result: Mapped[str] = mapped_column(sa.String(40), default="granted", nullable=False)


class WebhookEvent(Base, IdMixin, TimestampMixin):
    __tablename__ = "webhook_event"
    __table_args__ = (sa.UniqueConstraint("platform", "event_key", name="uq_webhook_event_key"),)

    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "webhook_platform"), nullable=False)
    event_key: Mapped[str] = mapped_column(sa.String(190), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(sa.String(1024))
    processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
