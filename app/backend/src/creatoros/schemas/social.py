"""Social account, OAuth, capability and vault schemas.

Every response model here is written so that a secret cannot be included by accident: there is no
`token`, `secret`, `code` or `verifier` field anywhere, and the one request that accepts a value
(`ProviderAppSecretUpdate`) is annotated so it is never echoed back. `test_social_api.py` asserts
that no response body contains a stored value.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..db.enums import (
    AccountType,
    CapabilityState,
    ConnectionStatus,
    OAuthRoute,
    Platform,
    TokenState,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ shared value objects


class CapabilityOut(StrictModel):
    capability: str
    state: CapabilityState
    reason: str | None = None
    required_scopes: list[str] = Field(default_factory=list)
    granted_scopes: list[str] = Field(default_factory=list)
    missing_scopes: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    api_version: str | None = None
    docs_reference: str | None = None
    evidence_ref: str | None = None


class TokenStatusOut(StrictModel):
    state: TokenState
    reason: str | None = None
    expires_at: str | None = None
    refresh_expires_at: str | None = None
    has_refresh_token: bool
    can_refresh: bool
    can_use: bool


# ------------------------------------------------------------------ accounts


class AccountSummary(StrictModel):
    id: str
    platform: Platform
    route: OAuthRoute | None = None
    provider_account_id: str
    display_name: str
    label: str
    group: str | None = None
    username: str | None = None
    avatar_url: str | None = None
    account_type: AccountType
    connection_status: ConnectionStatus
    token_state: TokenState
    token_state_reason: str | None = None
    token_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    scope_source: str | None = None
    api_version: str | None = None
    last_verified_at: datetime | None = None
    last_refresh_at: datetime | None = None
    disconnected_at: datetime | None = None
    capabilities: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class AccountDetail(AccountSummary):
    capabilities_detail: list[CapabilityOut] = Field(default_factory=list)
    token: TokenStatusOut | None = None
    secret_health: list[dict] = Field(default_factory=list)
    latest_snapshot: dict[str, Any] | None = None


class AccountListResponse(StrictModel):
    items: list[AccountSummary]
    total: int
    limit: int
    offset: int
    summary: dict[str, Any]


class AccountLabelUpdate(StrictModel):
    label: str | None = Field(default=None, max_length=200)
    group: str | None = Field(default=None, max_length=120)


class DisconnectRequest(StrictModel):
    revoke_at_provider: bool = True


class DisconnectResponse(StrictModel):
    account_id: str
    connection_status: ConnectionStatus
    revoked_at_provider: bool
    detail: str
    secrets_revoked: int


# ------------------------------------------------------------------ capabilities


class CapabilityReportResponse(StrictModel):
    account_id: str
    overall_state: CapabilityState
    checked_at: str
    snapshot_id: str
    capabilities: list[dict[str, Any]]


class CapabilityMatrixResponse(StrictModel):
    capabilities: list[str]
    accounts: list[dict[str, Any]]
    totals: dict[str, int]
    catalogue: list[dict[str, Any]]


class AccountCapabilitiesResponse(StrictModel):
    account_id: str
    platform: Platform
    route: OAuthRoute | None = None
    overall_state: CapabilityState
    capabilities: list[CapabilityOut]
    snapshots: list[dict[str, Any]]


# ------------------------------------------------------------------ tokens


class TokenRefreshResponse(StrictModel):
    account_id: str
    token: TokenStatusOut
    rotated: bool
    message: str


class RotationOut(StrictModel):
    id: str
    account_id: str
    provider: Platform
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    attempts: int
    error_code: str | None = None
    error_detail: str | None = None


class RotationListResponse(StrictModel):
    items: list[RotationOut]
    pending: int


# ------------------------------------------------------------------ oauth


class OAuthStartRequest(StrictModel):
    platform: Platform
    route: OAuthRoute | None = None
    scopes: list[str] | None = None
    return_route: str | None = Field(default=None, max_length=200)

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [scope.strip() for scope in value if scope and scope.strip()]
        if len(cleaned) > 20:
            raise ValueError("at most 20 scopes may be requested")
        return cleaned


class OAuthStartResponse(StrictModel):
    session_id: str
    authorization_url: str
    redirect_uri: str
    expires_at: str
    scopes_requested: list[str]
    platform: Platform
    route: OAuthRoute
    pkce: str | None = None
    label: str
    notes: str
    app_mode: str
    poll_url: str
    # P8.20: a start may be an account-bound permission upgrade rather than a new connection.
    purpose: str = "account_connect"
    account_id: str | None = None


class OAuthTransaction(StrictModel):
    session_id: str
    platform: Platform
    route: OAuthRoute | None = None
    status: str
    created_at: str
    expires_at: str
    consumed_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    accounts: list[dict[str, Any]] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    unsupported: list[str] = Field(default_factory=list)


class OAuthTransactionList(StrictModel):
    items: list[dict[str, Any]]


# ------------------------------------------------------------------ provider apps


class ProviderAppUpsert(StrictModel):
    client_id: str = Field(min_length=1, max_length=190)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    redirect_uri: str | None = Field(default=None, max_length=500)
    scopes: list[str] | None = None
    api_version: str | None = Field(default=None, max_length=16)
    app_mode: str = Field(default="development", pattern="^(development|live)$")
    login_config_id: str | None = Field(default=None, max_length=190)
    notes: str | None = Field(default=None, max_length=2000)


class ProviderAppSecretUpdate(StrictModel):
    client_secret: str = Field(min_length=1, max_length=4096)


class SecretAdoptRequest(StrictModel):
    purpose: str = Field(default="provider_app_secret", max_length=64)
    provider: str = Field(max_length=32)
    alias: str | None = Field(default=None, max_length=160)


class SecretAdoptResponse(StrictModel):
    id: str
    purpose: str
    provider: str
    status: str
    fingerprint: str
    bytes: int
    alg: str


class ProviderAppSecretRef(StrictModel):
    secret_id: str = Field(min_length=1, max_length=64)


class ProviderAppOut(StrictModel):
    id: str
    provider: Platform
    route: OAuthRoute
    label: str
    client_id: str
    client_id_suffix: str | None = None
    redirect_uri: str
    scopes: list[str]
    api_version: str | None = None
    app_mode: str
    login_config_id: str | None = None
    has_client_secret: bool
    client_secret_status: str | None = None
    last_verified_at: str | None = None
    notes: str | None = None
    docs_url: str
    pkce_required: bool
    requires_app_review_for_live: bool


class ProviderAppListResponse(StrictModel):
    items: list[ProviderAppOut]
    routes: list[dict[str, Any]]


class ProviderAppVerifyResponse(StrictModel):
    provider: Platform
    route: OAuthRoute
    configured: bool
    checks: list[dict[str, Any]]
    problems: list[str]
    docs_url: str
    pkce_required: bool


# ------------------------------------------------------------------ vault


class VaultHealthResponse(StrictModel):
    available: bool
    backend: str
    schema_version: int | None = None
    alg: str | None = None
    entries: int | None = None
    corrupt: int | None = None
    reencrypted: int | None = None
    operations: int | None = None
    failures: int | None = None
    last_operation: str | None = None
    last_operation_at: str | None = None
    protection: str
    reason: str | None = None
    # The desktop vault reports where it keeps its records; it is a path, never a value.
    directory: str | None = None
    secrets: dict[str, Any] = Field(default_factory=dict)
    callbacks: dict[str, Any] = Field(default_factory=dict)


class SecretReservation(StrictModel):
    secret_id: str
    purpose: str
    max_bytes: int
    purpose_options: list[str]


class SecretVerifyResponse(StrictModel):
    id: str
    present: bool
    ok: bool
    code: str | None = None
    purpose: str | None = None
    bytes: int | None = None
    alg: str | None = None


# ------------------------------------------------------------------ diagnostics


class AccountDiagnosticsResponse(StrictModel):
    account_id: str
    platform: Platform
    route: OAuthRoute | None = None
    provider_report: dict[str, Any]
    local: dict[str, Any]
    generated_at: str


class SocialOverviewResponse(StrictModel):
    accounts: dict[str, Any]
    vault: VaultHealthResponse
    callbacks: dict[str, Any]
    oauth_recent: list[dict[str, Any]]
    providers: list[dict[str, Any]]
    phase7: dict[str, Any]
