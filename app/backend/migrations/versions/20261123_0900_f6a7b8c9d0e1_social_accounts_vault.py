"""phase 6: provider app config, account capabilities, OAuth sessions and refresh journal

Design notes:

* Existing tables are extended with ``ALTER TABLE ... ADD COLUMN`` only. Phase 4 showed that a
  table rebuild rewrites every inbound foreign key to point at the scratch name, so the one table
  that must be rebuilt (``oauth_session``) is rebuilt by creating a new table, copying rows with
  explicit column lists, dropping the old one and renaming. Nothing references ``oauth_session``,
  so that rename is safe - and the migration verifies it with ``PRAGMA foreign_key_check``.
* ``oauth_session`` used to store the OAuth ``state`` and the PKCE verifier in plaintext. Both are
  gone: the state is stored as SHA-256, and the verifier now lives in the credential vault. Any
  surviving pre-6 session is marked failed instead of being silently upgraded, because its
  verifier cannot be moved into the vault from inside a migration and a 10-minute session that
  old is dead anyway.
* Enum columns carry CHECK constraints that list exactly the values the application can write.

Revision ID: f6a7b8c9d0e1
Revises: c4a91f7d2b60
Create Date: 2026-11-23 09:00:00.000000
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "c4a91f7d2b60"
branch_labels = None
depends_on = None

SECRET_REF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("purpose", "VARCHAR(40)"),
    ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
    (
        "status",
        "VARCHAR(11) NOT NULL DEFAULT 'active' "
        "CONSTRAINT ck_secret_ref_status CHECK (status IN ('active', 'unavailable', 'revoked', 'deleted'))",
    ),
    ("byte_size", "INTEGER"),
    ("unavailable_reason", "VARCHAR(300)"),
    ("last_verified_at", "DATETIME"),
    ("last_used_at", "DATETIME"),
)

SOCIAL_ACCOUNT_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "route",
        "VARCHAR(20) CONSTRAINT ck_social_account_route "
        "CHECK (route IS NULL OR route IN ('facebook_login', 'instagram_login', 'tiktok_login'))",
    ),
    ("internal_label", "VARCHAR(200)"),
    ("group_label", "VARCHAR(120)"),
    (
        "app_config_id",
        "VARCHAR(26) REFERENCES provider_app_config(id) ON DELETE SET NULL",
    ),
    ("refresh_ref_id", "VARCHAR(26) REFERENCES secret_ref(id) ON DELETE SET NULL"),
    (
        "token_state",
        "VARCHAR(25) NOT NULL DEFAULT 'unknown' CONSTRAINT ck_social_account_token_state CHECK "
        "(token_state IN ('valid', 'expiring_soon', 'expired', 'refresh_required', "
        "'refresh_in_progress', 'refresh_failed', 'revoked', 'reauthorization_required', "
        "'credential_unavailable', 'unknown'))",
    ),
    ("token_state_reason", "VARCHAR(300)"),
    ("api_version", "VARCHAR(16)"),
    ("scope_source", "VARCHAR(190)"),
    ("last_refresh_at", "DATETIME"),
    ("disconnected_at", "DATETIME"),
    ("meta_json", "JSON NOT NULL DEFAULT '{}'"),
)

CAPABILITY_SNAPSHOT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trigger", "VARCHAR(40)"),
    ("api_version", "VARCHAR(16)"),
    (
        "overall_state",
        "VARCHAR(27) CONSTRAINT ck_capability_snapshot_overall_state CHECK "
        "(overall_state IS NULL OR overall_state IN ('available', 'needs_user_consent', "
        "'requires_developer_approval', 'unsupported', 'unavailable', 'unknown', 'expired', "
        "'revoked', 'manual_only', 'eligible', 'missing_permission', 'needs_app_review', "
        "'unsupported_account_type', 'token_expired', 'rate_limited'))",
    ),
    ("granted_json", "JSON NOT NULL DEFAULT '[]'"),
)

CAPABILITY_STATE_VALUES = (
    "'available', 'needs_user_consent', 'requires_developer_approval', 'unsupported', "
    "'unavailable', 'unknown', 'expired', 'revoked', 'manual_only', 'eligible', "
    "'missing_permission', 'needs_app_review', 'unsupported_account_type', 'token_expired', "
    "'rate_limited'"
)


def _add_columns(bind, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()}
    for name, definition in columns:
        if name in existing:
            continue
        bind.execute(sa.text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'))


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "provider_app_config",
        sa.Column(
            "provider",
            sa.Enum(
                "facebook",
                "instagram",
                "tiktok",
                name="app_provider",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "route",
            sa.Enum(
                "facebook_login",
                "instagram_login",
                "tiktok_login",
                name="app_route",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(190), nullable=False),
        sa.Column(
            "client_secret_ref_id",
            sa.String(26),
            sa.ForeignKey("secret_ref.id", ondelete="SET NULL"),
        ),
        sa.Column("redirect_uri", sa.String(500), nullable=False),
        sa.Column("scopes_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("api_version", sa.String(16)),
        sa.Column("app_mode", sa.String(24), nullable=False, server_default="development"),
        sa.Column("login_config_id", sa.String(190)),
        sa.Column("notes", sa.Text()),
        sa.Column("last_verified_at", sa.DateTime()),
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("provider", "route", name="uq_provider_app_route"),
    )
    op.create_index("ix_provider_app_provider", "provider_app_config", ["provider"])

    _add_columns(bind, "secret_ref", SECRET_REF_COLUMNS)
    _add_columns(bind, "social_account", SOCIAL_ACCOUNT_COLUMNS)
    _add_columns(bind, "capability_snapshot", CAPABILITY_SNAPSHOT_COLUMNS)

    op.create_table(
        "account_capability",
        sa.Column(
            "account_id",
            sa.String(26),
            sa.ForeignKey("social_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "available",
                "needs_user_consent",
                "requires_developer_approval",
                "unsupported",
                "unavailable",
                "unknown",
                "expired",
                "revoked",
                "manual_only",
                "eligible",
                "missing_permission",
                "needs_app_review",
                "unsupported_account_type",
                "token_expired",
                "rate_limited",
                name="capability_state",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text()),
        sa.Column("required_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("granted_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("missing_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.Column("api_version", sa.String(16)),
        sa.Column("evidence_ref", sa.String(512)),
        sa.Column("docs_reference", sa.String(512)),
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("account_id", "capability", name="uq_account_capability"),
    )
    op.create_index("ix_account_capability_account_id", "account_capability", ["account_id"])
    op.create_index("ix_account_capability_state", "account_capability", ["capability", "state"])

    op.create_table(
        "credential_rotation",
        sa.Column(
            "account_id",
            sa.String(26),
            sa.ForeignKey("social_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Enum(
                "facebook",
                "instagram",
                "tiktok",
                name="rotation_provider",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False, server_default="refresh"),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "in_progress",
                "completed",
                "failed",
                "abandoned",
                name="rotation_state",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("lock_token", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("old_ref_id", sa.String(26), sa.ForeignKey("secret_ref.id", ondelete="SET NULL")),
        sa.Column("new_ref_id", sa.String(26), sa.ForeignKey("secret_ref.id", ondelete="SET NULL")),
        sa.Column("journal_ref_id", sa.String(26), sa.ForeignKey("secret_ref.id", ondelete="SET NULL")),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("meta_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_credential_rotation_account", "credential_rotation", ["account_id", "started_at"])
    op.create_index("ix_credential_rotation_state", "credential_rotation", ["state"])

    _rebuild_oauth_session(bind)

    violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(f"foreign key violations after phase 6 migration: {violations[:5]}")


def _rebuild_oauth_session(bind) -> None:
    """Replace the plaintext-state session table with the hashed-state shape."""
    rows = bind.execute(
        sa.text(
            "SELECT id, platform, state, code_verifier, redirect_uri, expires_at, consumed_at, "
            "created_at, updated_at FROM oauth_session"
        )
    ).fetchall()

    op.create_table(
        "oauth_session_p6",
        sa.Column(
            "platform",
            sa.Enum(
                "facebook",
                "instagram",
                "tiktok",
                name="oauth_platform",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "code_verifier_ref_id",
            sa.String(26),
            sa.ForeignKey("secret_ref.id", ondelete="SET NULL"),
        ),
        sa.Column("redirect_uri", sa.String(1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column(
            "route",
            sa.Enum(
                "facebook_login",
                "instagram_login",
                "tiktok_login",
                name="oauth_route",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
        ),
        sa.Column(
            "app_config_id",
            sa.String(26),
            sa.ForeignKey("provider_app_config.id", ondelete="SET NULL"),
        ),
        sa.Column("scopes_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("pkce_method", sa.String(16)),
        sa.Column("return_route", sa.String(200)),
        sa.Column("callback_received_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(300)),
        sa.Column(
            "account_id",
            sa.String(26),
            sa.ForeignKey("social_account.id", ondelete="SET NULL"),
        ),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_oauth_session_state_hash", "oauth_session_p6", ["state_hash"])
    op.create_index("ix_oauth_session_platform_created", "oauth_session_p6", ["platform", "created_at"])

    for row in rows:
        mapping = row._mapping
        state_hash = hashlib.sha256(str(mapping["state"]).encode("utf-8")).hexdigest()
        bind.execute(
            sa.text(
                "INSERT INTO oauth_session_p6 (id, platform, state_hash, code_verifier_ref_id, "
                "redirect_uri, expires_at, consumed_at, route, scopes_json, pkce_method, status, "
                "error_code, error_message, result_json, attempts, created_at, updated_at) "
                "VALUES (:id, :platform, :state_hash, NULL, :redirect_uri, :expires_at, "
                ":consumed_at, NULL, '[]', NULL, 'failed', 'MIGRATION_VERIFIER_UNRETRIABLE', "
                "'pre-6 session cannot be upgraded: its PKCE verifier was stored in plaintext', "
                "'{}', 0, :created_at, :updated_at)"
            ),
            {
                "id": mapping["id"],
                "platform": mapping["platform"],
                "state_hash": state_hash,
                "redirect_uri": mapping["redirect_uri"],
                "expires_at": mapping["expires_at"],
                "consumed_at": mapping["consumed_at"],
                "created_at": mapping["created_at"],
                "updated_at": mapping["updated_at"],
            },
        )

    op.drop_table("oauth_session")
    op.rename_table("oauth_session_p6", "oauth_session")


def downgrade() -> None:
    bind = op.get_bind()

    # Put oauth_session back into its pre-6 shape. A downgrade cannot restore plaintext states that
    # were never kept, so surviving rows get an unusable placeholder state and are already failed.
    rows = bind.execute(
        sa.text("SELECT id, platform, state_hash, redirect_uri, expires_at, consumed_at, created_at, updated_at FROM oauth_session")
    ).fetchall()
    op.create_table(
        "oauth_session_pre6",
        sa.Column(
            "platform",
            sa.Enum(
                "facebook",
                "instagram",
                "tiktok",
                name="oauth_platform",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("state", sa.String(128), nullable=False, unique=True),
        sa.Column("code_verifier", sa.String(256), nullable=False),
        sa.Column("redirect_uri", sa.String(1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    for row in rows:
        mapping = row._mapping
        bind.execute(
            sa.text(
                "INSERT INTO oauth_session_pre6 (id, platform, state, code_verifier, redirect_uri, "
                "expires_at, consumed_at, created_at, updated_at) "
                "VALUES (:id, :platform, :state, 'downgraded-no-verifier', :redirect_uri, "
                ":expires_at, :consumed_at, :created_at, :updated_at)"
            ),
            {
                "id": mapping["id"],
                "platform": mapping["platform"],
                "state": mapping["state_hash"],
                "redirect_uri": mapping["redirect_uri"],
                "expires_at": mapping["expires_at"],
                "consumed_at": mapping["consumed_at"],
                "created_at": mapping["created_at"],
                "updated_at": mapping["updated_at"],
            },
        )
    op.drop_table("oauth_session")
    op.rename_table("oauth_session_pre6", "oauth_session")

    op.drop_index("ix_credential_rotation_state", table_name="credential_rotation")
    op.drop_index("ix_credential_rotation_account", table_name="credential_rotation")
    op.drop_table("credential_rotation")

    op.drop_index("ix_account_capability_state", table_name="account_capability")
    op.drop_index("ix_account_capability_account_id", table_name="account_capability")
    op.drop_table("account_capability")

    for table, columns in (
        ("capability_snapshot", CAPABILITY_SNAPSHOT_COLUMNS),
        ("social_account", SOCIAL_ACCOUNT_COLUMNS),
        ("secret_ref", SECRET_REF_COLUMNS),
    ):
        existing = {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()}
        for name, _definition in reversed(columns):
            if name in existing:
                bind.execute(sa.text(f'ALTER TABLE "{table}" DROP COLUMN "{name}"'))

    op.drop_index("ix_provider_app_provider", table_name="provider_app_config")
    op.drop_table("provider_app_config")
