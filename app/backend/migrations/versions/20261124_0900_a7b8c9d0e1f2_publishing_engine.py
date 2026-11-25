"""phase 7: publishing engine columns, delivery assets and reconciliation ledger

Design notes:

* ``publish_job.job_state`` carries a CHECK constraint that lists the 13 Phase 2 states. Phase 7
  writes nine more, and SQLite cannot alter a CHECK constraint, so the table is rebuilt. It is
  rebuilt by creating the new table, copying rows with an explicit column list, dropping the old
  one and renaming - the same technique Phase 6 used for ``oauth_session``.
* ``publish_attempt`` has a foreign key to ``publish_job``, so it is rebuilt first into
  ``publish_attempt_p7`` *after* ``publish_job_p7`` exists. Copy order is jobs then attempts, so
  every intermediate state satisfies the foreign key, and only then is the old pair dropped.
  Nothing else references ``publish_job``; ``publish_delivery`` and ``publish_reconciliation`` are
  created afterwards so they point at the final table. ``PRAGMA foreign_key_check`` at the end
  proves the result.
* The CHECK constraint keeps the four Phase 2 values that are no longer written (``validation``,
  ``queued``, ``needs_attention``, ``manual_publish_required``) so a pre-7 row copies across
  untouched. The application normalises them on read; rewriting stored history is not this
  migration's business.
* ``platform_variant`` only gains nullable / defaulted columns, so it is not rebuilt.
* Downgrade is lossy by nature: the Phase 7 columns are dropped and the nine new states are mapped
  back onto the old vocabulary, failing closed (``approved`` becomes ``awaiting_approval`` so a
  downgraded job cannot be published without a fresh human decision).

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-11-24 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

# The 13 Phase 2 states plus the 9 Phase 7 states, minus the four that are no longer written but
# must still be accepted from existing rows.
PUBLISH_JOB_STATES = (
    "'draft', 'preparing', 'validation_pending', 'awaiting_approval', 'approved', 'scheduled', "
    "'ready', 'uploading', 'uploaded', 'processing', 'published', 'partially_published', 'failed', "
    "'cancelled', 'blocked', 'requires_user_action', 'unknown_remote_state', "
    "'needs_reconciliation', "
    # legacy, readable but never written
    "'validation', 'queued', 'needs_attention', 'manual_publish_required'"
)

LEGACY_PUBLISH_JOB_STATES = (
    "'draft', 'validation', 'awaiting_approval', 'scheduled', 'queued', 'uploading', 'processing', "
    "'published', 'needs_reconciliation', 'needs_attention', 'manual_publish_required', 'failed', "
    "'cancelled'"
)

PUBLISH_JOB_NEW_COLUMNS = (
    "intent_key",
    "asset_id",
    "asset_sha256",
    "derivative_id",
    "derivative_sha256",
    "derivative_profile",
    "derivative_version",
    "publish_mode",
    "title",
    "caption",
    "hashtags_json",
    "metadata_json",
    "options_json",
    "field_provenance_json",
    "approval_request_id",
    "approval_payload_hash",
    "approval_record_id",
    "payload_hash",
    "provider_upload_session_id",
    "provider_media_id",
    "provider_permalink",
    "provider_published_at",
    "remote_checked_at",
    "upload_bytes_total",
    "upload_bytes_done",
    "upload_started_at",
    "upload_expires_at",
    "error_category",
    "state_reason",
    "missed_at",
    "reconcile_attempts",
)

PUBLISH_JOB_BASE_COLUMNS = (
    "variant_id",
    "platform",
    "account_id",
    "scheduled_at_utc",
    "display_timezone",
    "job_state",
    "attempts",
    "idempotency_key",
    "remote_container_id",
    "remote_post_id",
    "remote_status_json",
    "last_error_code",
    "last_error_detail",
    "next_attempt_at",
    "lease_owner",
    "lease_expires_at",
    "id",
    "created_at",
    "updated_at",
)

PUBLISH_ATTEMPT_BASE_COLUMNS = (
    "job_id",
    "attempt_no",
    "started_at",
    "finished_at",
    "http_status",
    "request_id",
    "response_code",
    "response_excerpt_redacted",
    "outcome",
    "id",
    "created_at",
    "updated_at",
)

PUBLISH_ATTEMPT_NEW_COLUMNS = (
    "provider_operation",
    "error_category",
    "remote_snapshot_json",
)

# New columns copied with literals: the copy cannot invent real values for facts it does not have.
NEW_COLUMN_SELECT = {
    "publish_mode": "'video'",
    "caption": "''",
    "hashtags_json": "'[]'",
    "metadata_json": "'{}'",
    "options_json": "'{}'",
    "field_provenance_json": "'{}'",
    "upload_bytes_total": "0",
    "upload_bytes_done": "0",
    "reconcile_attempts": "0",
}

# Downgrade mapping: every Phase 7-only state collapses onto the closest Phase 2 state, choosing
# the *safer* meaning rather than the prettiest one.
DOWNGRADE_STATE_CASE = (
    "CASE job_state "
    "WHEN 'preparing' THEN 'draft' "
    "WHEN 'validation_pending' THEN 'validation' "
    "WHEN 'approved' THEN 'awaiting_approval' "
    "WHEN 'ready' THEN 'queued' "
    "WHEN 'uploaded' THEN 'processing' "
    "WHEN 'blocked' THEN 'needs_attention' "
    "WHEN 'requires_user_action' THEN 'needs_attention' "
    "WHEN 'unknown_remote_state' THEN 'needs_reconciliation' "
    "WHEN 'partially_published' THEN 'needs_attention' "
    "ELSE job_state END"
)


def _table_exists(bind: sa.Connection, name: str) -> bool:
    return (
        bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"), {"name": name}
        ).fetchone()
        is not None
    )


def _column_names(bind: sa.Connection, table: str) -> set[str]:
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()}


def upgrade() -> None:
    bind = op.get_bind()

    # ---------------------------------------------------------------- publish_job
    op.create_table(
        "publish_job_p7",
        sa.Column("variant_id", sa.String(26), nullable=False),
        sa.Column("platform", sa.String(9), nullable=False),
        sa.Column("account_id", sa.String(26), nullable=False),
        sa.Column("scheduled_at_utc", sa.DateTime(timezone=True)),
        sa.Column("display_timezone", sa.String(64), nullable=False),
        sa.Column("job_state", sa.String(23), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("remote_container_id", sa.String(190)),
        sa.Column("remote_post_id", sa.String(190)),
        sa.Column("remote_status_json", sa.JSON(), nullable=False),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_error_detail", sa.Text()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(80)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        # --- phase 7 ---
        sa.Column("intent_key", sa.String(64)),
        sa.Column("asset_id", sa.String(26)),
        sa.Column("asset_sha256", sa.String(64)),
        sa.Column("derivative_id", sa.String(26)),
        sa.Column("derivative_sha256", sa.String(64)),
        sa.Column("derivative_profile", sa.String(60)),
        sa.Column("derivative_version", sa.Integer()),
        sa.Column("publish_mode", sa.String(11), nullable=False, server_default="video"),
        sa.Column("title", sa.String(500)),
        sa.Column("caption", sa.Text(), nullable=False, server_default=""),
        sa.Column("hashtags_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("options_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("field_provenance_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("approval_request_id", sa.String(26)),
        sa.Column("approval_payload_hash", sa.String(64)),
        sa.Column("approval_record_id", sa.String(26)),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("provider_upload_session_id", sa.String(190)),
        sa.Column("provider_media_id", sa.String(190)),
        sa.Column("provider_permalink", sa.String(1024)),
        sa.Column("provider_published_at", sa.DateTime(timezone=True)),
        sa.Column("remote_checked_at", sa.DateTime(timezone=True)),
        sa.Column("upload_bytes_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("upload_bytes_done", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("upload_started_at", sa.DateTime(timezone=True)),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_category", sa.String(19)),
        sa.Column("state_reason", sa.String(300)),
        sa.Column("missed_at", sa.DateTime(timezone=True)),
        sa.Column("reconcile_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_job"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["social_account.id"],
            name="fk_publish_job_account_id_social_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["platform_variant.id"],
            name="fk_publish_job_variant_id_platform_variant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["media_asset.id"], name="fk_publish_job_asset_id_media_asset", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["derivative_id"],
            ["media_derivative.id"],
            name="fk_publish_job_derivative_id_media_derivative",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_publish_job_idempotency"),
        sa.CheckConstraint(
            "platform IN ('facebook', 'instagram', 'tiktok')", name="ck_publish_job_job_platform"
        ),
        sa.CheckConstraint(f"job_state IN ({PUBLISH_JOB_STATES})", name="ck_publish_job_publish_state"),
        sa.CheckConstraint(
            "publish_mode IN ('video', 'reels', 'direct_post', 'inbox')",
            name="ck_publish_job_publish_mode",
        ),
        sa.CheckConstraint(
            "error_category IS NULL OR error_category IN ('permanent', 'transient', 'authorization', "
            "'permission', 'rate_limit', 'validation', 'media', 'provider_processing', 'ambiguous')",
            name="ck_publish_job_publish_error_category",
        ),
    )

    # ---------------------------------------------------------------- publish_attempt
    op.create_table(
        "publish_attempt_p7",
        sa.Column("job_id", sa.String(26), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("request_id", sa.String(190)),
        sa.Column("response_code", sa.String(120)),
        sa.Column("response_excerpt_redacted", sa.Text()),
        sa.Column("outcome", sa.String(40), nullable=False),
        # --- phase 7 ---
        sa.Column("provider_operation", sa.String(60)),
        sa.Column("error_category", sa.String(19)),
        sa.Column("remote_snapshot_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_attempt"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["publish_job_p7.id"],
            name="fk_publish_attempt_job_id_publish_job",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "error_category IS NULL OR error_category IN ('permanent', 'transient', 'authorization', "
            "'permission', 'rate_limit', 'validation', 'media', 'provider_processing', 'ambiguous')",
            name="ck_publish_attempt_attempt_error_category",
        ),
    )

    # Copy: jobs first (attempts reference them), then attempts. New columns cannot be selected from
    # the old table, so each one gets a literal or NULL.
    job_targets = list(PUBLISH_JOB_BASE_COLUMNS) + list(PUBLISH_JOB_NEW_COLUMNS)
    job_sources = list(PUBLISH_JOB_BASE_COLUMNS) + [
        NEW_COLUMN_SELECT.get(name, "NULL") for name in PUBLISH_JOB_NEW_COLUMNS
    ]
    bind.execute(
        sa.text(
            f"INSERT INTO publish_job_p7 ({', '.join(job_targets)}) "
            f"SELECT {', '.join(job_sources)} FROM publish_job"
        )
    )
    attempt_targets = list(PUBLISH_ATTEMPT_BASE_COLUMNS) + list(PUBLISH_ATTEMPT_NEW_COLUMNS)
    attempt_sources = list(PUBLISH_ATTEMPT_BASE_COLUMNS) + [
        "'{}'" if name == "remote_snapshot_json" else "NULL" for name in PUBLISH_ATTEMPT_NEW_COLUMNS
    ]
    bind.execute(
        sa.text(
            f"INSERT INTO publish_attempt_p7 ({', '.join(attempt_targets)}) "
            f"SELECT {', '.join(attempt_sources)} FROM publish_attempt"
        )
    )

    op.drop_table("publish_attempt")
    op.drop_table("publish_job")
    op.rename_table("publish_job_p7", "publish_job")
    op.rename_table("publish_attempt_p7", "publish_attempt")

    op.create_index("ix_publish_job_variant_id", "publish_job", ["variant_id"])
    op.create_index("ix_publish_job_account", "publish_job", ["account_id", "created_at"])
    op.create_index("ix_publish_job_due", "publish_job", ["job_state", "scheduled_at_utc"])
    op.create_index("ix_publish_job_intent_key", "publish_job", ["intent_key"])
    op.create_index("ix_publish_job_asset_id", "publish_job", ["asset_id"])
    op.create_index("ix_publish_attempt_job_id", "publish_attempt", ["job_id"])

    # ---------------------------------------------------------------- platform_variant
    if "title" not in _column_names(bind, "platform_variant"):
        bind.execute(sa.text("ALTER TABLE platform_variant ADD COLUMN title VARCHAR(500)"))
        bind.execute(
            sa.text("ALTER TABLE platform_variant ADD COLUMN metadata_json JSON NOT NULL DEFAULT '{}'")
        )
        bind.execute(
            sa.text("ALTER TABLE platform_variant ADD COLUMN options_json JSON NOT NULL DEFAULT '{}'")
        )
        bind.execute(
            sa.text(
                "ALTER TABLE platform_variant ADD COLUMN source_derivative_id VARCHAR(26) "
                "REFERENCES media_derivative(id) ON DELETE SET NULL"
            )
        )

    # ---------------------------------------------------------------- new tables
    op.create_table(
        "publish_delivery",
        sa.Column("job_id", sa.String(26), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("bucket", sa.String(190), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(8), nullable=False, server_default="pending"),
        sa.Column("url_expires_at", sa.DateTime(timezone=True)),
        sa.Column("uploaded_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("delete_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_detail", sa.String(500)),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_delivery"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["publish_job.id"], name="fk_publish_delivery_job_id_publish_job", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("job_id", "object_key", name="uq_publish_delivery_object"),
        sa.CheckConstraint(
            "state IN ('pending', 'uploaded', 'verified', 'deleted', 'failed')",
            name="ck_publish_delivery_delivery_asset_state",
        ),
    )
    op.create_index("ix_publish_delivery_job_id", "publish_delivery", ["job_id"])
    op.create_index("ix_publish_delivery_expiry", "publish_delivery", ["state", "url_expires_at"])

    op.create_table(
        "publish_reconciliation",
        sa.Column("job_id", sa.String(26), nullable=False),
        sa.Column("attempt_id", sa.String(26)),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_state", sa.String(80)),
        sa.Column("decision", sa.String(15), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("resolved_by", sa.String(120)),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_reconciliation"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["publish_job.id"],
            name="fk_publish_reconciliation_job_id_publish_job",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "decision IN ('no_remote_object', 'remote_exists', 'undetermined')",
            name="ck_publish_reconciliation_reconciliation_decision",
        ),
    )
    op.create_index("ix_publish_reconciliation_job_id", "publish_reconciliation", ["job_id"])

    problems = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if problems:
        raise RuntimeError(f"foreign key violations after phase 7 migration: {problems}")


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_publish_reconciliation_job_id", table_name="publish_reconciliation")
    op.drop_table("publish_reconciliation")
    op.drop_index("ix_publish_delivery_expiry", table_name="publish_delivery")
    op.drop_index("ix_publish_delivery_job_id", table_name="publish_delivery")
    op.drop_table("publish_delivery")

    if "title" in _column_names(bind, "platform_variant"):
        for name in ("source_derivative_id", "options_json", "metadata_json", "title"):
            bind.execute(sa.text(f'ALTER TABLE platform_variant DROP COLUMN "{name}"'))

    # Rebuild both tables back to the Phase 2 shape.
    op.create_table(
        "publish_job_pre7",
        sa.Column("variant_id", sa.String(26), nullable=False),
        sa.Column("platform", sa.String(9), nullable=False),
        sa.Column("account_id", sa.String(26), nullable=False),
        sa.Column("scheduled_at_utc", sa.DateTime(timezone=True)),
        sa.Column("display_timezone", sa.String(64), nullable=False),
        sa.Column("job_state", sa.String(23), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("remote_container_id", sa.String(190)),
        sa.Column("remote_post_id", sa.String(190)),
        sa.Column("remote_status_json", sa.JSON(), nullable=False),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_error_detail", sa.Text()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(80)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_job"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["social_account.id"],
            name="fk_publish_job_account_id_social_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["platform_variant.id"],
            name="fk_publish_job_variant_id_platform_variant",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_publish_job_idempotency"),
        sa.CheckConstraint(
            "platform IN ('facebook', 'instagram', 'tiktok')", name="ck_publish_job_job_platform"
        ),
        sa.CheckConstraint(
            f"job_state IN ({LEGACY_PUBLISH_JOB_STATES})", name="ck_publish_job_publish_state"
        ),
    )
    op.create_table(
        "publish_attempt_pre7",
        sa.Column("job_id", sa.String(26), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("request_id", sa.String(190)),
        sa.Column("response_code", sa.String(120)),
        sa.Column("response_excerpt_redacted", sa.Text()),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_attempt"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["publish_job_pre7.id"],
            name="fk_publish_attempt_job_id_publish_job",
            ondelete="CASCADE",
        ),
    )

    # Column order matters: job_state sits in the middle of the list, so the CASE replaces it in
    # place instead of being appended at the end.
    downgrade_select = (
        ", ".join(PUBLISH_JOB_BASE_COLUMNS[:5])
        + ", "
        + DOWNGRADE_STATE_CASE
        + ", "
        + ", ".join(PUBLISH_JOB_BASE_COLUMNS[6:])
    )
    bind.execute(
        sa.text(
            f"INSERT INTO publish_job_pre7 ({', '.join(PUBLISH_JOB_BASE_COLUMNS)}) "
            f"SELECT {downgrade_select} FROM publish_job"
        )
    )
    bind.execute(
        sa.text(
            f"INSERT INTO publish_attempt_pre7 ({', '.join(PUBLISH_ATTEMPT_BASE_COLUMNS)}) "
            f"SELECT {', '.join(PUBLISH_ATTEMPT_BASE_COLUMNS)} FROM publish_attempt"
        )
    )

    op.drop_table("publish_attempt")
    op.drop_table("publish_job")
    op.rename_table("publish_job_pre7", "publish_job")
    op.rename_table("publish_attempt_pre7", "publish_attempt")

    op.create_index("ix_publish_job_variant_id", "publish_job", ["variant_id"])
    op.create_index("ix_publish_job_account", "publish_job", ["account_id", "created_at"])
    op.create_index("ix_publish_job_due", "publish_job", ["job_state", "scheduled_at_utc"])
    op.create_index("ix_publish_attempt_job_id", "publish_attempt", ["job_id"])

    problems = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if problems:
        raise RuntimeError(f"foreign key violations after phase 7 downgrade: {problems}")
