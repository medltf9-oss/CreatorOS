"""media intake: asset kind/probe columns and the media_derivative table

Design notes that matter for the existing production database:

* New columns on ``media_asset`` are added with ``ALTER TABLE ... ADD COLUMN`` only, never
  by rebuilding the table. Phase 4 proved why: renaming ``media_asset`` would rewrite the
  foreign keys in ``transcript`` and ``content_project`` to point at the scratch name.
* SQLite validates a column-level CHECK on ADD COLUMN when the default satisfies it, so the
  two enum columns get their CHECK constraints without a rebuild.
* ``media_derivative`` is a new table, so its constraints are created normally.

Revision ID: c4a91f7d2b60
Revises: b7f1c02d4e61
Create Date: 2026-11-22 20:55:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4a91f7d2b60"
down_revision = "b7f1c02d4e61"
branch_labels = None
depends_on = None

MEDIA_ASSET_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "kind",
        "VARCHAR(7) NOT NULL DEFAULT 'unknown' "
        "CONSTRAINT ck_media_asset_kind CHECK (kind IN ('video', 'audio', 'image', 'unknown'))",
    ),
    (
        "intake_state",
        "VARCHAR(11) NOT NULL DEFAULT 'pending' "
        "CONSTRAINT ck_media_asset_intake_state CHECK "
        "(intake_state IN ('pending', 'copying', 'probing', 'ready', 'quarantined', 'rejected'))",
    ),
    ("mime_type", "VARCHAR(120)"),
    ("format_name", "VARCHAR(120)"),
    ("probe_json", "JSON NOT NULL DEFAULT '{}'"),
    ("probe_version", "VARCHAR(20)"),
    ("last_processed_at", "DATETIME"),
    ("quarantine_reason", "TEXT"),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(media_asset)")).fetchall()
    }
    for name, definition in MEDIA_ASSET_COLUMNS:
        if name in existing:
            continue
        bind.execute(sa.text(f'ALTER TABLE media_asset ADD COLUMN "{name}" {definition}'))

    op.create_table(
        "media_derivative",
        sa.Column("asset_id", sa.String(26), sa.ForeignKey("media_asset.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "thumbnail",
                "timeline",
                "preview",
                "proxy",
                "transcode",
                "audio",
                "waveform",
                "waveform_image",
                name="derivative_kind",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("profile", sa.String(60), nullable=False, server_default="default"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "ready",
                "failed",
                "deleted",
                name="derivative_state",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("rel_path", sa.String(1024)),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("duration_ms", sa.BigInteger()),
        sa.Column("mime_type", sa.String(120)),
        sa.Column("job_id", sa.String(26)),
        sa.Column("error_code", sa.String(60)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("meta_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("asset_id", "kind", "profile", "version", name="uq_derivative_key"),
        sa.UniqueConstraint("id", name="pk_media_derivative"),
    )
    op.create_index("ix_media_derivative_asset_id", "media_derivative", ["asset_id"])
    op.create_index("ix_media_derivative_job_id", "media_derivative", ["job_id"])
    op.create_index("ix_media_derivative_asset", "media_derivative", ["asset_id", "kind"])

    violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(f"foreign key violations after media migration: {violations[:5]}")


def downgrade() -> None:
    op.drop_index("ix_media_derivative_asset", table_name="media_derivative")
    op.drop_index("ix_media_derivative_job_id", table_name="media_derivative")
    op.drop_index("ix_media_derivative_asset_id", table_name="media_derivative")
    op.drop_table("media_derivative")

    bind = op.get_bind()
    existing = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(media_asset)")).fetchall()
    }
    for name, _definition in reversed(MEDIA_ASSET_COLUMNS):
        if name in existing:
            # SQLite 3.35+ supports DROP COLUMN directly, which avoids a table rebuild.
            bind.execute(sa.text(f'ALTER TABLE media_asset DROP COLUMN "{name}"'))
