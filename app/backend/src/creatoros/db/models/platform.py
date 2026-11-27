"""Account, secret-reference, capability and OAuth-session tables.

No token material is ever stored here: secrets live in the DPAPI vault and are referenced by
`secret_ref.id` (ADR-07 / ADR-22). A row in `secret_ref` is metadata only - provider, purpose,
fingerprint and state - so the database alone can never be used to authenticate.

Phase 6 additions:
* `provider_app_config` - per provider/route developer-app registration (client id is public,
  the client secret is a `secret_ref` row).
* `account_capability` - the *current* normalized state of one capability on one account.
  `capability_snapshot` stays the historical evidence of each check run.
* `credential_rotation` - the refresh journal, so a token rotation that is interrupted can be
  finished or rolled back instead of corrupting an account.
* `oauth_session` stores a hashed state value and a vault reference to the PKCE verifier, never
  the plaintext of either.
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, IdMixin, TimestampMixin, UtcDateTime, enum_column
from ..enums import (
    AccountType,
    CapabilityState,
    ConnectionStatus,
    OAuthRoute,
    Platform,
    RotationState,
    SecretStatus,
    TokenState,
)


class SecretRef(Base, IdMixin, TimestampMixin):
    __tablename__ = "secret_ref"

    provider: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    key_alias: Mapped[str] = mapped_column(sa.String(160), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    cipher: Mapped[str] = mapped_column(sa.String(32), default="dpapi", nullable=False)
    dpapi_scope: Mapped[str] = mapped_column(sa.String(32), default="user", nullable=False)
    # Phase 6
    purpose: Mapped[str | None] = mapped_column(sa.String(40))
    schema_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    status: Mapped[SecretStatus] = mapped_column(
        enum_column(SecretStatus, "secret_status"), default=SecretStatus.ACTIVE, nullable=False
    )
    byte_size: Mapped[int | None] = mapped_column(sa.Integer)
    unavailable_reason: Mapped[str | None] = mapped_column(sa.String(300))
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    rotated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class ProviderAppConfig(Base, IdMixin, TimestampMixin):
    """Developer-app registration for one provider route.

    `client_id` is public by design (it appears in the authorization URL). The client secret is
    only ever a `secret_ref` row: this table can be exported without leaking anything.
    """

    __tablename__ = "provider_app_config"
    __table_args__ = (
        sa.UniqueConstraint("provider", "route", name="uq_provider_app_route"),
        sa.Index("ix_provider_app_provider", "provider"),
    )

    provider: Mapped[Platform] = mapped_column(enum_column(Platform, "app_provider"), nullable=False)
    route: Mapped[OAuthRoute] = mapped_column(enum_column(OAuthRoute, "app_route"), nullable=False)
    client_id: Mapped[str] = mapped_column(sa.String(190), nullable=False)
    client_secret_ref_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("secret_ref.id", ondelete="SET NULL")
    )
    redirect_uri: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    scopes_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    api_version: Mapped[str | None] = mapped_column(sa.String(16))
    app_mode: Mapped[str] = mapped_column(sa.String(24), default="development", nullable=False)
    login_config_id: Mapped[str | None] = mapped_column(sa.String(190))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    client_secret_ref: Mapped[SecretRef | None] = relationship()


class SocialAccount(Base, IdMixin, TimestampMixin):
    __tablename__ = "social_account"
    __table_args__ = (
        sa.UniqueConstraint("platform", "provider_account_id", name="uq_account_provider"),
        sa.Index("ix_social_account_platform_status", "platform", "connection_status"),
        sa.Index("ix_social_account_token_state", "token_state"),
    )

    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "account_platform"), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(sa.String(190), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    username: Mapped[str | None] = mapped_column(sa.String(200))
    avatar_url: Mapped[str | None] = mapped_column(sa.String(1024))
    account_type: Mapped[AccountType] = mapped_column(
        enum_column(AccountType, "account_type"), default=AccountType.PERSONAL_UNSUPPORTED, nullable=False
    )
    parent_page_id: Mapped[str | None] = mapped_column(sa.String(190))
    scopes_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    capabilities_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    token_ref_id: Mapped[str | None] = mapped_column(sa.ForeignKey("secret_ref.id", ondelete="SET NULL"))
    token_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    connection_status: Mapped[ConnectionStatus] = mapped_column(
        enum_column(ConnectionStatus, "connection_status"), default=ConnectionStatus.DISCONNECTED, nullable=False
    )
    health_detail: Mapped[str | None] = mapped_column(sa.Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Phase 6
    route: Mapped[OAuthRoute | None] = mapped_column(enum_column(OAuthRoute, "account_route"))
    internal_label: Mapped[str | None] = mapped_column(sa.String(200))
    group_label: Mapped[str | None] = mapped_column(sa.String(120))
    app_config_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("provider_app_config.id", ondelete="SET NULL")
    )
    refresh_ref_id: Mapped[str | None] = mapped_column(sa.ForeignKey("secret_ref.id", ondelete="SET NULL"))
    token_state: Mapped[TokenState] = mapped_column(
        enum_column(TokenState, "token_state"), default=TokenState.UNKNOWN, nullable=False
    )
    token_state_reason: Mapped[str | None] = mapped_column(sa.String(300))
    api_version: Mapped[str | None] = mapped_column(sa.String(16))
    scope_source: Mapped[str | None] = mapped_column(sa.String(190))
    last_refresh_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    disconnected_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    meta_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)

    token_ref: Mapped[SecretRef | None] = relationship(foreign_keys=[token_ref_id])
    refresh_ref: Mapped[SecretRef | None] = relationship(foreign_keys=[refresh_ref_id])
    snapshots: Mapped[list[CapabilitySnapshot]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    capabilities: Mapped[list[AccountCapability]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )


class CapabilitySnapshot(Base, IdMixin, TimestampMixin):
    __tablename__ = "capability_snapshot"

    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    docs_version: Mapped[str | None] = mapped_column(sa.String(64))
    checks_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(sa.String(1024))
    # Phase 6
    trigger: Mapped[str | None] = mapped_column(sa.String(40))
    api_version: Mapped[str | None] = mapped_column(sa.String(16))
    overall_state: Mapped[CapabilityState | None] = mapped_column(
        enum_column(CapabilityState, "snapshot_overall_state")
    )
    granted_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)

    account: Mapped[SocialAccount] = relationship(back_populates="snapshots")


class AccountCapability(Base, IdMixin, TimestampMixin):
    """Current normalized state of one capability on one account.

    Upserted by the capability engine; `capability_snapshot` keeps the history. Phase 7 reads
    this table to decide whether a publish path exists at all.
    """

    __tablename__ = "account_capability"
    __table_args__ = (
        sa.UniqueConstraint("account_id", "capability", name="uq_account_capability"),
        sa.Index("ix_account_capability_state", "capability", "state"),
    )

    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), nullable=False, index=True
    )
    capability: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[CapabilityState] = mapped_column(enum_column(CapabilityState, "capability_state"), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text)
    required_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    granted_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    missing_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    api_version: Mapped[str | None] = mapped_column(sa.String(16))
    evidence_ref: Mapped[str | None] = mapped_column(sa.String(512))
    docs_reference: Mapped[str | None] = mapped_column(sa.String(512))

    account: Mapped[SocialAccount] = relationship(back_populates="capabilities")


class OAuthSession(Base, IdMixin, TimestampMixin):
    __tablename__ = "oauth_session"
    __table_args__ = (
        sa.Index("ix_oauth_session_state_hash", "state_hash"),
        sa.Index("ix_oauth_session_platform_created", "platform", "created_at"),
    )

    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "oauth_platform"), nullable=False)
    # SHA-256 of the state value. The value itself exists only in the authorization URL.
    state_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    # The PKCE verifier lives in the vault, never in a queryable column.
    code_verifier_ref_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("secret_ref.id", ondelete="SET NULL")
    )
    redirect_uri: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Phase 6
    route: Mapped[OAuthRoute | None] = mapped_column(enum_column(OAuthRoute, "oauth_route"))
    app_config_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("provider_app_config.id", ondelete="SET NULL")
    )
    scopes_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    pkce_method: Mapped[str | None] = mapped_column(sa.String(16))
    return_route: Mapped[str | None] = mapped_column(sa.String(200))
    callback_received_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Lifecycle: pending | succeeded | failed | expired | cancelled.
    status: Mapped[str] = mapped_column(sa.String(24), default="pending", nullable=False)
    error_code: Mapped[str | None] = mapped_column(sa.String(64))
    error_message: Mapped[str | None] = mapped_column(sa.String(300))
    account_id: Mapped[str | None] = mapped_column(sa.ForeignKey("social_account.id", ondelete="SET NULL"))
    result_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)


class CredentialRotation(Base, IdMixin, TimestampMixin):
    """Refresh journal.

    A refresh that rotates the refresh token is not atomic: the provider may invalidate the old
    token the moment it answers. This row records the intent before the call and the new secret
    reference after it, so an interrupted refresh can be resolved instead of guessed at.
    """

    __tablename__ = "credential_rotation"
    __table_args__ = (
        sa.Index("ix_credential_rotation_account", "account_id", "started_at"),
        sa.Index("ix_credential_rotation_state", "state"),
    )

    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[Platform] = mapped_column(enum_column(Platform, "rotation_provider"), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(24), default="refresh", nullable=False)
    state: Mapped[RotationState] = mapped_column(
        enum_column(RotationState, "rotation_state"), default=RotationState.PENDING, nullable=False
    )
    lock_token: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    old_ref_id: Mapped[str | None] = mapped_column(sa.ForeignKey("secret_ref.id", ondelete="SET NULL"))
    new_ref_id: Mapped[str | None] = mapped_column(sa.ForeignKey("secret_ref.id", ondelete="SET NULL"))
    journal_ref_id: Mapped[str | None] = mapped_column(sa.ForeignKey("secret_ref.id", ondelete="SET NULL"))
    error_code: Mapped[str | None] = mapped_column(sa.String(64))
    error_detail: Mapped[str | None] = mapped_column(sa.Text)
    meta_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
