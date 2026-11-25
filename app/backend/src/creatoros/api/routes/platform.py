"""Legacy account paths, kept working on top of the Phase 6 social services.

Phase 1–5 shipped `POST /accounts` (manual registration) and a 501 OAuth stub. Phase 6 makes both
obsolete and, more importantly, dishonest: an account row without a real credential is a lie the
UI would have to explain later. So:

* `POST /accounts` is gone - accounts are only created by a completed OAuth flow;
* `POST /accounts/oauth/{platform}/start` now performs a real authorization start and returns the
  URL the shell must open;
* `POST /accounts/{id}/disconnect` really revokes and deletes the credential.

The full surface lives in :mod:`creatoros.api.routes.social`; these three routes exist so an older
renderer (or a script) does not silently keep calling a stub.
"""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status

from ...db.models import SocialAccount
from ...schemas.api import AccountOut, Page
from ...schemas.social import DisconnectRequest, DisconnectResponse, OAuthStartResponse
from ...services.social.accounts import AccountService
from ...services.social.oauth_engine import OAuthEngine
from ..deps import DbSession, Paging

router = APIRouter(tags=["platform"])


@router.get("/accounts", response_model=Page[AccountOut])
def list_accounts(session: DbSession, paging: Paging) -> Page[AccountOut]:
    base = sa.select(SocialAccount)
    total = session.scalar(sa.select(sa.func.count()).select_from(SocialAccount)) or 0
    items = list(
        session.scalars(
            base.order_by(SocialAccount.created_at.desc(), SocialAccount.id.desc())
            .limit(paging.limit)
            .offset(paging.offset)
        )
    )
    return Page(
        items=[AccountOut.model_validate(item) for item in items],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: str, session: DbSession) -> SocialAccount:
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND: account not found")
    return account


@router.post("/accounts/{account_id}/disconnect", response_model=DisconnectResponse)
async def disconnect_account(
    account_id: str,
    session: DbSession,
    body: DisconnectRequest | None = None,
) -> DisconnectResponse:
    from .social import get_vault

    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND: account not found")
    vault = get_vault()
    result = await AccountService(session, vault).disconnect(
        account, revoke_at_provider=(body.revoke_at_provider if body else True)
    )
    return DisconnectResponse(
        account_id=account.id,
        connection_status=account.connection_status,
        revoked_at_provider=result["revoked_at_provider"],
        detail=result["detail"],
        secrets_revoked=0,
    )


@router.post("/accounts/oauth/{platform}/start", response_model=OAuthStartResponse)
async def legacy_oauth_start(
    platform: str,
    session: DbSession,
    route: str | None = Query(default=None, description="login path, e.g. instagram_login"),
) -> OAuthStartResponse:
    """Formerly a 501 stub. Now it starts the real flow and says which URL to open."""
    from ...core.settings import get_settings
    from ...db.enums import OAuthRoute, Platform
    from ...integrations.social import config as social_config
    from ...integrations.social.errors import SocialError
    from .social import get_vault

    try:
        platform_value = Platform(platform)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "UNKNOWN_PLATFORM: unknown platform")
    route_value = None
    if route:
        try:
            route_value = OAuthRoute(route)
        except ValueError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "UNKNOWN_ROUTE: unknown login route")
    if route_value is None:
        specs = social_config.routes_for_platform(platform_value)
        if not specs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "UNKNOWN_ROUTE: this platform has no login path")
        route_value = specs[0].route

    settings = get_settings()
    engine = OAuthEngine(
        session,
        get_vault(),
        callback_port=settings.social_callback_port,
        state_ttl_s=settings.social_oauth_state_ttl_s,
    )
    try:
        payload = await engine.start(platform_value, route_value)
    except SocialError as error:
        raise HTTPException(error.http_status, f"{error.code}: {error.message}") from error
    return OAuthStartResponse(**payload)
