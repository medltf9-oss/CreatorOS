"""Social accounts, OAuth, capabilities, tokens, provider apps and the vault API.

Everything here is behind the per-launch session token. Two kinds of route deserve a note:

* The OAuth **callback** is not here. It arrives on the separate loopback listener
  (``services/social/callback_server.py``) because providers require a fixed redirect URI.
* The vault routes never accept or return a secret value. The renderer hands values to the
  Electron main process, which writes them to the vault and returns metadata; this API can only
  reserve an id, read an id's health, or delete it.

No route publishes, uploads, posts or messages. Routes that would need that in Phase 7 return
``NOT_IMPLEMENTED`` today rather than a plausible-looking success.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status

from ...db.enums import (
    CapabilityState,
    ConnectionStatus,
    OAuthRoute,
    Platform,
    TokenState,
)
from ...db.models.platform import AccountCapability, CredentialRotation, SecretRef, SocialAccount
from ...integrations.social import config as social_config
from ...integrations.social.base import AdapterRegistry
from ...integrations.social.config import CAPABILITY_CATALOGUE, ROUTE_SPECS
from ...integrations.social.errors import SocialError
from ...schemas.social import (
    AccountCapabilitiesResponse,
    AccountDetail,
    AccountDiagnosticsResponse,
    AccountLabelUpdate,
    AccountListResponse,
    AccountSummary,
    CapabilityMatrixResponse,
    CapabilityOut,
    DisconnectRequest,
    DisconnectResponse,
    OAuthStartRequest,
    OAuthStartResponse,
    OAuthTransaction,
    OAuthTransactionList,
    ProviderAppListResponse,
    ProviderAppOut,
    ProviderAppSecretRef,
    ProviderAppSecretUpdate,
    ProviderAppUpsert,
    ProviderAppVerifyResponse,
    RotationListResponse,
    RotationOut,
    SecretAdoptRequest,
    SecretAdoptResponse,
    SecretReservation,
    SecretVerifyResponse,
    SocialOverviewResponse,
    TokenRefreshResponse,
    TokenStatusOut,
    VaultHealthResponse,
)
from ...services.backup import AuditService
from ...services.social.accounts import AccountService
from ...services.social.apps import AppRegistration, ProviderAppService
from ...services.social.callback_server import get_listener
from ...services.social.capabilities import CapabilityService, stored_page_tasks
from ...services.social.oauth_engine import OAuthEngine
from ...services.social.secrets import SecretStore
from ...services.social.tokens import TokenService, compute_status
from ...services.social.vault import Vault, VaultError
from ..deps import Audit, DbSession, Paging

router = APIRouter(prefix="/social", tags=["social"])
log = logging.getLogger(__name__)

# The vault is resolved per request from the app state that main.py installs at startup, so a
# backend started without the desktop shell reports `unavailable` instead of failing to boot.
_VAULT: Vault | None = None


def set_vault(vault: Vault) -> None:
    global _VAULT
    _VAULT = vault


def get_vault() -> Vault:
    from ...services.social.vault import vault_from_env

    if _VAULT is not None:
        return _VAULT
    return vault_from_env()


def _callback_dispatch(session_factory, vault: Vault):
    """Build the async handler the loopback listener calls, with its own DB session.

    The listener runs on a worker thread and hands over the coroutine; each callback gets a fresh
    session so a failed exchange cannot poison an open transaction for the rest of the app.
    """

    async def handler(route: str, params: dict[str, str]) -> dict:
        session = session_factory()
        try:
            engine = _engine(session, vault)
            outcome = await engine.handle_callback(route, params)
            session.commit()
            return outcome
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return handler


def _engine(session, vault: Vault) -> OAuthEngine:
    from ...core.settings import get_settings

    settings = get_settings()
    return OAuthEngine(
        session,
        vault,
        callback_port=settings.social_callback_port,
        state_ttl_s=settings.social_oauth_state_ttl_s,
    )


def _raise(error: SocialError) -> None:
    raise HTTPException(status_code=error.http_status, detail={"code": error.code, "message": error.message})


def _platform(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "UNKNOWN_PLATFORM", "message": f"unknown platform {value}"})


def _route(platform: Platform, value: str | None) -> OAuthRoute:
    if value:
        try:
            return OAuthRoute(value)
        except ValueError:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"code": "UNKNOWN_ROUTE", "message": f"unknown login route {value}"},
            )
    spec = social_config.routes_for_platform(platform)
    if not spec:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "UNKNOWN_ROUTE", "message": f"{platform.value} has no login route"},
        )
    return spec[0].route


# ------------------------------------------------------------------ serialisation


def account_summary(account: SocialAccount) -> AccountSummary:
    status = compute_status(account)
    return AccountSummary(
        id=account.id,
        platform=account.platform,
        route=account.route,
        provider_account_id=account.provider_account_id,
        display_name=account.display_name,
        label=account.internal_label or account.display_name,
        group=account.group_label,
        username=account.username,
        avatar_url=account.avatar_url,
        account_type=account.account_type,
        connection_status=account.connection_status,
        token_state=status.state,
        token_state_reason=status.reason,
        token_expires_at=account.token_expires_at,
        refresh_expires_at=account.refresh_expires_at,
        scopes=list(account.scopes_json or []),
        scope_source=account.scope_source,
        api_version=account.api_version,
        last_verified_at=account.last_verified_at,
        last_refresh_at=account.last_refresh_at,
        disconnected_at=account.disconnected_at,
        capabilities={
            key: (value.value if hasattr(value, "value") else str(value))
            for key, value in (account.capabilities_json or {}).items()
        },
        meta=_public_meta(account.meta_json or {}),
    )


def _public_meta(meta: dict) -> dict:
    """Drop nested blobs that are only useful internally."""
    return {key: value for key, value in meta.items() if key not in {"profile"}}


def _capability_out(row: AccountCapability) -> CapabilityOut:
    return CapabilityOut(
        capability=row.capability,
        state=row.state,
        reason=row.reason,
        required_scopes=list(row.required_json or []),
        granted_scopes=list(row.granted_json or []),
        missing_scopes=list(row.missing_json or []),
        verified_at=row.verified_at,
        api_version=row.api_version,
        docs_reference=row.docs_reference,
        evidence_ref=row.evidence_ref,
    )


def _token_out(status) -> TokenStatusOut:
    return TokenStatusOut(**status.to_dict())


# ------------------------------------------------------------------ overview


@router.get("/overview", response_model=SocialOverviewResponse)
async def overview(session: DbSession) -> SocialOverviewResponse:
    vault = get_vault()
    accounts = AccountService(session, vault)
    secrets = SecretStore(session, vault)
    engine = _engine(session, vault)
    vault_health = await secrets.health()
    listener = get_listener()
    providers = []
    apps = ProviderAppService(session, vault)
    for (platform, route), spec in ROUTE_SPECS.items():
        config = apps.get(platform, route)
        providers.append(
            {
                "platform": platform.value,
                "route": route.value,
                "label": spec.label,
                "configured": config is not None,
                "app_mode": config.app_mode if config else None,
                "has_client_secret": bool(config.client_secret_ref_id) if config else False,
                "docs_url": spec.docs_url,
                "pkce_required": spec.pkce_required,
                "scopes": list(spec.scopes),
                "notes": spec.notes,
            }
        )
    return SocialOverviewResponse(
        accounts=accounts.summary(),
        vault=VaultHealthResponse(
            **{
                **vault_health,
                "secrets": secrets.summary(),
                "callbacks": listener.status() if listener else {"running": False, "port": None},
            }
        ),
        callbacks=listener.status() if listener else {"running": False, "port": None, "url_base": None},
        oauth_recent=engine.recent(limit=10),
        providers=providers,
        phase7=_phase7_readiness(session),
    )


# ------------------------------------------------------------------ accounts


@router.get("/accounts", response_model=AccountListResponse)
def list_accounts(
    session: DbSession,
    paging: Paging,
    platform: str | None = Query(default=None),
    connection_status: str | None = Query(default=None),
    include_disconnected: bool = Query(default=True),
) -> AccountListResponse:
    vault = get_vault()
    service = AccountService(session, vault)
    platform_value = _platform(platform) if platform else None
    status_value = None
    if connection_status:
        try:
            status_value = ConnectionStatus(connection_status)
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"code": "UNKNOWN_STATUS", "message": f"unknown connection status {connection_status}"},
            )
    rows, total = service.list(
        platform=platform_value,
        status=status_value,
        include_disconnected=include_disconnected,
        limit=paging.limit,
        offset=paging.offset,
    )
    return AccountListResponse(
        items=[account_summary(row) for row in rows],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
        summary=service.summary(),
    )


@router.get("/accounts/{account_id}", response_model=AccountDetail)
async def get_account(account_id: str, session: DbSession) -> AccountDetail:
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    capabilities = CapabilityService(session)
    summary = account_summary(account)
    token_status = compute_status(account)
    secrets = SecretStore(session, vault)
    health: list[dict] = []
    for ref_id, kind in ((account.token_ref_id, "access_token"), (account.refresh_ref_id, "refresh_token")):
        if not ref_id:
            continue
        ref = session.get(SecretRef, ref_id)
        if ref is None:
            health.append({"kind": kind, "present": False, "status": "missing", "code": "VAULT_NOT_FOUND"})
            continue
        verdict = await secrets.verify(ref)
        health.append(
            {
                "kind": kind,
                "present": bool(verdict.get("present")),
                "status": ref.status.value,
                "fingerprint": ref.fingerprint[:12],
                "bytes": ref.byte_size,
                "rotated_at": ref.rotated_at.isoformat() if ref.rotated_at else None,
                "last_verified_at": ref.last_verified_at.isoformat() if ref.last_verified_at else None,
                "code": verdict.get("code"),
            }
        )
    snapshots = capabilities.snapshots(account_id, limit=1)
    latest = (
        {
            "checked_at": snapshots[0].checked_at.isoformat(),
            "trigger": snapshots[0].trigger,
            "overall_state": snapshots[0].overall_state.value if snapshots[0].overall_state else None,
            "api_version": snapshots[0].api_version,
        }
        if snapshots
        else None
    )
    return AccountDetail(
        **summary.model_dump(),
        capabilities_detail=[_capability_out(row) for row in capabilities.current(account_id)],
        token=_token_out(token_status),
        secret_health=health,
        latest_snapshot=latest,
    )


@router.patch("/accounts/{account_id}", response_model=AccountSummary)
def update_account(account_id: str, body: AccountLabelUpdate, session: DbSession) -> AccountSummary:
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    try:
        AccountService(session, vault).rename(account, label=body.label, group=body.group)
    except SocialError as error:
        _raise(error)
    return account_summary(account)


@router.post("/accounts/{account_id}/disconnect", response_model=DisconnectResponse)
async def disconnect_account(account_id: str, body: DisconnectRequest, session: DbSession) -> DisconnectResponse:
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    secrets = SecretStore(session, vault)
    before = secrets.summary().get("active", 0)
    result = await AccountService(session, vault).disconnect(account, revoke_at_provider=body.revoke_at_provider)
    after = secrets.summary().get("active", 0)
    return DisconnectResponse(
        account_id=account.id,
        connection_status=account.connection_status,
        revoked_at_provider=result["revoked_at_provider"],
        detail=result["detail"],
        secrets_revoked=max(before - after, 0),
    )


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: str, session: DbSession) -> None:
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    if account.connection_status != ConnectionStatus.DISCONNECTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "ACCOUNT_STILL_CONNECTED",
                "message": "disconnect the account before deleting it, so its credential is revoked first",
            },
        )
    AuditService(session).record("account.deleted", resource_type="social_account", resource_id=account.id)
    # Delete children first, explicitly: cascades depend on `PRAGMA foreign_keys`, and an account
    # delete that half-succeeds would leave capability evidence pointing at nothing.
    from ...db.models.platform import CapabilitySnapshot

    session.execute(sa.delete(AccountCapability).where(AccountCapability.account_id == account.id))
    session.execute(sa.delete(CapabilitySnapshot).where(CapabilitySnapshot.account_id == account.id))
    session.execute(sa.delete(CredentialRotation).where(CredentialRotation.account_id == account.id))
    session.delete(account)


@router.post("/accounts/{account_id}/verify", response_model=AccountDetail)
async def verify_account(account_id: str, session: DbSession) -> AccountDetail:
    """Re-read the account from the provider and re-evaluate its capabilities."""
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    tokens = TokenService(session, vault)
    try:
        token = await tokens.access_token(account)
    except SocialError as error:
        _raise(error)
    apps = ProviderAppService(session, vault)
    try:
        _config, credentials = await apps.resolve_credentials(
            account.platform, account.route or _route(account.platform, None)
        )
    except SocialError as error:
        _raise(error)
    adapter = AdapterRegistry().get(account.platform, account.route or _route(account.platform, None))
    try:
        profile = await adapter.fetch_profile(credentials, token, account.provider_account_id)
    except SocialError as error:
        if error.code in {"TOKEN_EXPIRED", "REAUTHORIZATION_REQUIRED"}:
            tokens.accounts.mark_token_problem(account, TokenState.REAUTHORIZATION_REQUIRED, error.message)
        _raise(error)
    finally:
        await adapter.aclose()
    account.display_name = profile.display_name or account.display_name
    account.username = profile.username or account.username
    account.avatar_url = profile.avatar_url or account.avatar_url
    account.account_type = profile.account_type or account.account_type
    session.flush()
    await _reevaluate(session, vault, account, trigger="manual_verify", adapter=adapter)
    return await get_account(account_id, session)


# ------------------------------------------------------------------ capabilities


@router.get("/accounts/{account_id}/capabilities", response_model=AccountCapabilitiesResponse)
def account_capabilities(account_id: str, session: DbSession) -> AccountCapabilitiesResponse:
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    service = CapabilityService(session)
    rows = service.current(account_id)
    snapshots = service.snapshots(account_id, limit=10)
    overall = CapabilityState.UNKNOWN
    if snapshots and snapshots[0].overall_state:
        overall = snapshots[0].overall_state
    return AccountCapabilitiesResponse(
        account_id=account_id,
        platform=account.platform,
        route=account.route,
        overall_state=overall,
        capabilities=[_capability_out(row) for row in rows],
        snapshots=[
            {
                "id": snapshot.id,
                "checked_at": snapshot.checked_at.isoformat(),
                "trigger": snapshot.trigger,
                "overall_state": snapshot.overall_state.value if snapshot.overall_state else None,
                "api_version": snapshot.api_version,
                "granted_scopes": list(snapshot.granted_json or []),
            }
            for snapshot in snapshots
        ],
    )


@router.post("/accounts/{account_id}/capabilities/refresh", response_model=AccountCapabilitiesResponse)
async def refresh_capabilities(account_id: str, session: DbSession) -> AccountCapabilitiesResponse:
    """Re-evaluate from stored facts. No network call, no model, same inputs -> same verdict."""
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    await _reevaluate(session, vault, account, trigger="manual_refresh")
    return account_capabilities(account_id, session)


@router.get("/capabilities/matrix", response_model=CapabilityMatrixResponse)
def capability_matrix(session: DbSession) -> CapabilityMatrixResponse:
    service = CapabilityService(session)
    matrix = service.matrix()
    return CapabilityMatrixResponse(
        capabilities=matrix["capabilities"],
        accounts=matrix["accounts"],
        totals=matrix["totals"],
        catalogue=[
            {
                "key": definition.key,
                "label": definition.label,
                "platform": definition.platform.value,
                "routes": [route.value for route in definition.routes],
                "required_scopes": list(definition.required_scopes),
                "docs_url": definition.docs_url,
                "manual_only": definition.manual_only,
                "notes": definition.notes,
            }
            for definition in CAPABILITY_CATALOGUE
        ],
    )


async def _reevaluate(session, vault, account: SocialAccount, *, trigger: str, adapter=None) -> None:
    apps = ProviderAppService(session, vault)
    route = account.route or _route(account.platform, None)
    config = apps.get(account.platform, route)
    owns_adapter = adapter is None
    adapter = adapter or AdapterRegistry().get(account.platform, route)
    try:
        status = compute_status(account)
        tasks = stored_page_tasks(account)
        verdicts = adapter.evaluate_capabilities(
            account_type=account.account_type,
            granted_scopes=list(account.scopes_json or []),
            token_state=status.state.value,
            extra={
                "app_mode": config.app_mode if config else "development",
                "api_version": account.api_version,
                "scope_source": account.scope_source,
                # The Page role list the provider last reported (or None when never reported), so a
                # manual refresh and the consent callback agree about the ANALYZE task.
                "page_tasks": list(tasks) if tasks is not None else None,
            },
        )
    finally:
        if owns_adapter:
            await adapter.aclose()
    CapabilityService(session).record(
        account,
        verdicts,
        trigger=trigger,
        api_version=account.api_version,
        evidence_ref=f"account:{account.id}",
    )


# ------------------------------------------------------------------ tokens


@router.get("/accounts/{account_id}/token", response_model=TokenStatusOut)
def token_status(account_id: str, session: DbSession) -> TokenStatusOut:
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    return _token_out(compute_status(account))


@router.post("/accounts/{account_id}/token/refresh", response_model=TokenRefreshResponse)
async def refresh_token(account_id: str, session: DbSession) -> TokenRefreshResponse:
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    service = TokenService(session, vault)
    before = account.refresh_ref_id
    try:
        renewed = await service.refresh(account)
    except SocialError as error:
        _raise(error)
    return TokenRefreshResponse(
        account_id=account.id,
        token=_token_out(renewed),
        rotated=account.refresh_ref_id != before,
        message=(
            "the provider issued a new access and refresh token"
            if account.refresh_ref_id != before
            else "the access token was renewed; this provider does not rotate the refresh token"
        ),
    )


@router.get("/rotations", response_model=RotationListResponse)
def list_rotations(session: DbSession, limit: int = Query(default=50, ge=1, le=200)) -> RotationListResponse:
    rows = list(
        session.execute(
            sa.select(CredentialRotation).order_by(CredentialRotation.started_at.desc()).limit(limit)
        ).scalars()
    )
    pending = int(
        session.execute(
            sa.select(sa.func.count())
            .select_from(CredentialRotation)
            .where(CredentialRotation.state.in_(["pending", "in_progress"]))
        ).scalar_one()
    )
    return RotationListResponse(
        items=[
            RotationOut(
                id=row.id,
                account_id=row.account_id,
                provider=row.provider,
                state=row.state.value if hasattr(row.state, "value") else str(row.state),
                started_at=row.started_at,
                finished_at=row.finished_at,
                attempts=row.attempts,
                error_code=row.error_code,
                error_detail=row.error_detail,
            )
            for row in rows
        ],
        pending=pending,
    )


@router.post("/rotations/{account_id}/resolve", response_model=dict)
def resolve_rotation(account_id: str, session: DbSession) -> dict:
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    return TokenService(session, vault).resolve_interrupted(account)


# ------------------------------------------------------------------ oauth


@router.get("/oauth/routes", response_model=dict)
def oauth_routes(session: DbSession) -> dict:
    vault = get_vault()
    apps = ProviderAppService(session, vault)
    listener = get_listener()
    items = []
    for (platform, route), spec in ROUTE_SPECS.items():
        config = apps.get(platform, route)
        items.append(
            {
                "platform": platform.value,
                "route": route.value,
                "label": spec.label,
                "scopes": list(spec.scopes),
                "extra_scopes": list(spec.extra_scopes),
                "pkce_required": spec.pkce_required,
                "account_types": [account.value for account in spec.account_types],
                "docs_url": spec.docs_url,
                "notes": spec.notes,
                "configured": config is not None,
                "app_mode": config.app_mode if config else None,
                "redirect_uri": config.redirect_uri if config else None,
            }
        )
    return {
        "items": items,
        "callback": listener.status() if listener else {"running": False},
        "vault_available": bool(getattr(vault, "available", False)),
    }


@router.post("/oauth/start", response_model=OAuthStartResponse)
async def oauth_start(body: OAuthStartRequest, session: DbSession) -> OAuthStartResponse:
    vault = get_vault()
    route = _route(body.platform, body.route.value if body.route else None)
    engine = _engine(session, vault)
    try:
        payload = await engine.start(
            body.platform, route, scopes=body.scopes, return_route=body.return_route
        )
    except SocialError as error:
        _raise(error)
    return OAuthStartResponse(**payload)


@router.get("/oauth/transactions", response_model=OAuthTransactionList)
def oauth_transactions(session: DbSession, limit: int = Query(default=20, ge=1, le=100)) -> OAuthTransactionList:
    engine = _engine(session, get_vault())
    return OAuthTransactionList(items=engine.recent(limit=limit))


@router.get("/oauth/transactions/{session_id}", response_model=OAuthTransaction)
def oauth_transaction(session_id: str, session: DbSession) -> OAuthTransaction:
    engine = _engine(session, get_vault())
    try:
        return OAuthTransaction(**engine.transaction(session_id))
    except SocialError as error:
        _raise(error)


@router.post("/oauth/transactions/{session_id}/cancel", response_model=OAuthTransaction)
def oauth_cancel(session_id: str, session: DbSession) -> OAuthTransaction:
    engine = _engine(session, get_vault())
    try:
        return OAuthTransaction(**engine.cancel(session_id))
    except SocialError as error:
        _raise(error)


@router.get("/callback", response_model=dict)
def callback_status() -> dict:
    listener = get_listener()
    if listener is None:
        return {
            "running": False,
            "port": None,
            "url_base": None,
            "hint": "the listener starts when the backend starts; if it is off, OAuth cannot complete",
        }
    return listener.status()


# ------------------------------------------------------------------ provider apps


@router.get("/provider-apps", response_model=ProviderAppListResponse)
def list_provider_apps(session: DbSession) -> ProviderAppListResponse:
    vault = get_vault()
    service = ProviderAppService(session, vault)
    return ProviderAppListResponse(
        items=[ProviderAppOut(**service.describe(config)) for config in service.list()],
        routes=[
            {
                "platform": platform.value,
                "route": route.value,
                "label": spec.label,
                "scopes": list(spec.scopes),
                "extra_scopes": list(spec.extra_scopes),
                "pkce_required": spec.pkce_required,
                "docs_url": spec.docs_url,
                "notes": spec.notes,
            }
            for (platform, route), spec in ROUTE_SPECS.items()
        ],
    )


@router.put("/provider-apps/{platform}/{route}", response_model=ProviderAppOut)
async def upsert_provider_app(
    platform: str, route: str, body: ProviderAppUpsert, session: DbSession
) -> ProviderAppOut:
    vault = get_vault()
    platform_value = _platform(platform)
    route_value = _route(platform_value, route)
    service = ProviderAppService(session, vault)
    try:
        config = await service.upsert(
            AppRegistration(
                provider=platform_value,
                route=route_value,
                client_id=body.client_id,
                client_secret=body.client_secret,
                redirect_uri=body.redirect_uri,
                api_version=body.api_version,
                app_mode=body.app_mode,
                scopes=body.scopes or [],
                login_config_id=body.login_config_id,
                notes=body.notes,
            )
        )
    except SocialError as error:
        _raise(error)
    except VaultError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, error.to_dict())
    return ProviderAppOut(**service.describe(config))


@router.post("/provider-apps/{platform}/{route}/secret", response_model=ProviderAppOut)
async def set_provider_app_secret(
    platform: str, route: str, body: ProviderAppSecretUpdate, session: DbSession, audit: Audit
) -> ProviderAppOut:
    vault = get_vault()
    platform_value = _platform(platform)
    route_value = _route(platform_value, route)
    service = ProviderAppService(session, vault)
    try:
        config = await service.set_secret(platform_value, route_value, body.client_secret)
    except SocialError as error:
        _raise(error)
    except VaultError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, error.to_dict())
    audit.record(
        "provider_app.secret_received",
        resource_type="provider_app_config",
        metadata={"provider": platform_value.value, "route": route_value.value},
    )
    return ProviderAppOut(**service.describe(config))


@router.put("/provider-apps/{platform}/{route}/secret-ref", response_model=ProviderAppOut)
async def link_provider_app_secret(
    platform: str, route: str, body: ProviderAppSecretRef, session: DbSession, audit: Audit
) -> ProviderAppOut:
    """Link a record the desktop shell sealed itself. No secret value is accepted here."""
    vault = get_vault()
    platform_value = _platform(platform)
    route_value = _route(platform_value, route)
    service = ProviderAppService(session, vault)
    try:
        config = await service.attach_secret_ref(platform_value, route_value, body.secret_id)
    except SocialError as error:
        _raise(error)
    except VaultError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, error.to_dict())
    audit.record(
        "provider_app.secret_linked",
        resource_type="provider_app_config",
        resource_id=config.id,
        metadata={"provider": platform_value.value, "route": route_value.value},
    )
    return ProviderAppOut(**service.describe(config))


@router.post("/provider-apps/{platform}/{route}/verify", response_model=ProviderAppVerifyResponse)
async def verify_provider_app(platform: str, route: str, session: DbSession) -> ProviderAppVerifyResponse:
    vault = get_vault()
    platform_value = _platform(platform)
    route_value = _route(platform_value, route)
    report = await ProviderAppService(session, vault).verify(platform_value, route_value)
    return ProviderAppVerifyResponse(
        provider=platform_value,
        route=route_value,
        configured=report["configured"],
        checks=report["checks"],
        problems=report["problems"],
        docs_url=report["docs_url"],
        pkce_required=report["pkce_required"],
    )


@router.delete("/provider-apps/{platform}/{route}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_app(platform: str, route: str, session: DbSession) -> None:
    vault = get_vault()
    platform_value = _platform(platform)
    route_value = _route(platform_value, route)
    try:
        removed = await ProviderAppService(session, vault).delete(platform_value, route_value)
    except SocialError as error:
        _raise(error)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "no such app registration"})


# ------------------------------------------------------------------ vault


@router.get("/vault/health", response_model=VaultHealthResponse)
async def vault_health(session: DbSession) -> VaultHealthResponse:
    vault = get_vault()
    secrets = SecretStore(session, vault)
    health = await secrets.health()
    listener = get_listener()
    return VaultHealthResponse(
        **{
            **health,
            "secrets": secrets.summary(),
            "callbacks": listener.status() if listener else {"running": False, "port": None},
        }
    )


@router.post("/vault/secrets/reserve", response_model=SecretReservation)
def reserve_secret(session: DbSession) -> SecretReservation:
    """Hand the renderer an id it can ask the Electron main process to fill."""
    from ...core.ids import new_ulid

    vault = get_vault()
    if not getattr(vault, "available", False):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {
                "code": "VAULT_UNAVAILABLE",
                "message": "the credential vault is not available; the desktop shell must be running",
            },
        )
    return SecretReservation(
        secret_id=new_ulid(),
        purpose="provider_app_secret",
        max_bytes=64 * 1024,
        purpose_options=sorted(
            [
                "provider_app_secret",
                "access_token",
                "refresh_token",
                "oauth_pkce_verifier",
                "rotation_journal",
                "delivery_credentials",
                "provider_upload_session",
            ]
        ),
    )


@router.post("/vault/secrets/{secret_id}/adopt", response_model=SecretAdoptResponse)
async def adopt_secret(
    secret_id: str, body: SecretAdoptRequest, session: DbSession, audit: Audit
) -> SecretAdoptResponse:
    """Record metadata for a value the desktop shell already sealed in the vault."""
    vault = get_vault()
    try:
        ref = await SecretStore(session, vault).adopt(
            secret_id,
            purpose=body.purpose,
            provider=body.provider,
            alias=body.alias or f"{body.purpose}:{body.provider}",
        )
    except SocialError as error:
        _raise(error)
    except VaultError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, error.to_dict())
    audit.record(
        "vault.secret_adopted",
        resource_type="secret_ref",
        resource_id=ref.id,
        metadata={"purpose": ref.purpose, "provider": ref.provider},
    )
    return SecretAdoptResponse(
        id=ref.id,
        purpose=ref.purpose or body.purpose,
        provider=ref.provider or body.provider,
        status=ref.status.value,
        fingerprint=ref.fingerprint or "",
        bytes=int(ref.byte_size or 0),
        alg=ref.cipher or "dpapi",
    )


@router.get("/vault/secrets/{secret_id}", response_model=SecretVerifyResponse)
async def verify_secret(secret_id: str, session: DbSession) -> SecretVerifyResponse:
    vault = get_vault()
    ref = session.get(SecretRef, secret_id)
    if ref is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "no such secret reference"})
    result = await SecretStore(session, vault).verify(ref)
    return SecretVerifyResponse(
        id=secret_id,
        present=bool(result.get("present")),
        ok=bool(result.get("ok")),
        code=result.get("code"),
        purpose=result.get("purpose") or ref.purpose,
        bytes=result.get("bytes") or ref.byte_size,
        alg=result.get("alg") or ref.cipher,
    )


@router.delete("/vault/secrets/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(secret_id: str, session: DbSession) -> None:
    vault = get_vault()
    ref = session.get(SecretRef, secret_id)
    if ref is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "no such secret reference"})
    in_use = session.execute(
        sa.select(sa.func.count())
        .select_from(SocialAccount)
        .where(
            sa.or_(
                SocialAccount.token_ref_id == secret_id,
                SocialAccount.refresh_ref_id == secret_id,
            ),
            SocialAccount.connection_status != ConnectionStatus.DISCONNECTED,
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "SECRET_IN_USE",
                "message": "a connected account still uses this credential; disconnect it first",
            },
        )
    await SecretStore(session, vault).revoke(ref, reason="deleted from the vault page")


# ------------------------------------------------------------------ diagnostics


@router.get("/accounts/{account_id}/diagnostics", response_model=AccountDiagnosticsResponse)
async def account_diagnostics(account_id: str, session: DbSession) -> AccountDiagnosticsResponse:
    vault = get_vault()
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND", "message": "account not found"})
    route = account.route or _route(account.platform, None)
    apps = ProviderAppService(session, vault)
    local = {
        "connection_status": account.connection_status.value,
        "token_state": compute_status(account).to_dict(),
        "route": route.value,
        "account_type": account.account_type.value,
        "scopes_stored": list(account.scopes_json or []),
        "scope_source": account.scope_source,
        "capabilities": {
            key: (value.value if hasattr(value, "value") else str(value))
            for key, value in (account.capabilities_json or {}).items()
        },
        "api_version": account.api_version,
        "parent_page_id": account.parent_page_id,
    }
    provider_report: dict[str, Any]
    try:
        _config, credentials = await apps.resolve_credentials(account.platform, route)
        adapter = AdapterRegistry().get(account.platform, route)
        try:
            token = await TokenService(session, vault).access_token(account)
        except SocialError:
            token = None
        provider_report = await adapter.diagnostics(credentials, token)
        provider_report["revocation"] = (
            "the provider exposes a revoke endpoint"
            if account.platform != Platform.INSTAGRAM or route == OAuthRoute.FACEBOOK_LOGIN
            else "Instagram Business Login documents no revoke endpoint; removal happens at Instagram"
        )
        await adapter.aclose()
    except SocialError as error:
        provider_report = {"error": error.to_dict(), "checks": []}
    except VaultError as error:
        provider_report = {"error": error.to_dict(), "checks": []}
    return AccountDiagnosticsResponse(
        account_id=account.id,
        platform=account.platform,
        route=route,
        provider_report=provider_report,
        local=local,
        generated_at=datetime.now(UTC).isoformat(),
    )


# ------------------------------------------------------------------ phase 7 readiness


def _phase7_readiness(session) -> dict:
    """What Phase 7 needs, stated as facts rather than intentions."""
    accounts = list(session.execute(sa.select(SocialAccount)).scalars())
    connected = [account for account in accounts if account.connection_status == ConnectionStatus.CONNECTED]
    capability_rows = list(session.execute(sa.select(AccountCapability)).scalars())
    publish_capabilities = {
        "facebook.page.publish_video",
        "instagram.publish_reels",
        "tiktok.upload_draft",
        "tiktok.direct_post",
    }
    publish_ready = sorted(
        {
            row.account_id
            for row in capability_rows
            if row.capability in publish_capabilities and row.state == CapabilityState.AVAILABLE
        }
    )
    return {
        "accounts_connected": len(connected),
        "accounts_needing_attention": sum(
            1 for account in accounts if account.connection_status not in {ConnectionStatus.CONNECTED, ConnectionStatus.DISCONNECTED}
        ),
        "publish_capable_accounts": len(publish_ready),
        "publish_capable_account_ids": publish_ready,
        "approval_gate": "verify_for_use/consume exist and are untested against real publishes (Phase 7)",
        "blockers": [
            blocker
            for blocker in (
                None
                if len(connected) > 0
                else "no account is connected, so nothing can be published",
                None
                if publish_ready
                else "no connected account has an available publish capability yet",
                "TikTok Direct Post needs the Direct Post add-on; manual export remains the fallback",
            )
            if blocker
        ],
    }


__all__ = ["account_summary", "get_vault", "router", "set_vault"]
