"""Unified token lifecycle with a serialized, journalled refresh (Part 10).

The hard part is not calling a refresh endpoint; it is surviving one that half-succeeded. TikTok
rotates refresh tokens, so the moment the provider answers, the old refresh token is dead. If the
process dies between "provider answered" and "new token stored", the account is unusable and
nothing in the database says why.

So a refresh is a small transaction:

1. write a `credential_rotation` row (`pending`) with a lock token;
2. atomically move it to `in_progress` - only one writer can win that update;
3. write a journal secret (encrypted, `rotation_journal`) naming the old reference and the intent;
4. call the provider;
5. store the new tokens, point the account at them, mark the rotation `completed`;
6. on failure mark `failed` with the provider's code, and decide whether the account can still be
   used (an expired access token with a working refresh token is not a dead account).

A rotation left `in_progress` by a crash is resolved by `resolve_interrupted`: if the account still
has a working credential the rotation is abandoned, otherwise the account is marked as needing
reauthorization. Guessing is never an option.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import RotationState, SecretStatus, TokenState
from ...db.models.platform import CredentialRotation, SecretRef, SocialAccount
from ...integrations.social.errors import SocialError
from ..backup import AuditService
from .accounts import AccountService
from .apps import ProviderAppService
from .secrets import SecretStore
from .vault import Vault, VaultError

# TikTok access tokens only live 24 hours, so a 24-hour warning window would mark every healthy
# TikTok account as "expiring soon". Six hours is short enough to be actionable and long enough to
# not be permanent noise.
EXPIRING_SOON = timedelta(hours=6)
STALE_LOCK = timedelta(minutes=5)
# Refresh tokens that are close to their own expiry cannot be refreshed (TikTok requires the
# refresh token itself to still be valid), so the user has to reconnect before then.
REFRESH_DEAD_ZONE = timedelta(hours=12)


@dataclass(slots=True)
class TokenStatus:
    state: TokenState
    reason: str | None
    expires_at: str | None
    refresh_expires_at: str | None
    has_refresh_token: bool
    can_refresh: bool
    can_use: bool

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "has_refresh_token": self.has_refresh_token,
            "can_refresh": self.can_refresh,
            "can_use": self.can_use,
        }


def compute_status(account: SocialAccount, *, now=None) -> TokenStatus:
    """Pure function: current token state from stored facts, no network."""
    moment = now or utcnow()
    expires = _aware(account.token_expires_at)
    refresh_expires = _aware(account.refresh_expires_at)
    has_refresh = bool(account.refresh_ref_id)
    expired = expires is not None and expires <= moment
    expiring = expires is not None and not expired and expires - moment <= EXPIRING_SOON

    state: TokenState
    reason: str | None = None
    if account.token_state in {TokenState.REVOKED, TokenState.REAUTHORIZATION_REQUIRED}:
        state = account.token_state
        reason = account.token_state_reason or "the provider credential is no longer valid"
    elif account.token_ref_id is None:
        state = TokenState.CREDENTIAL_UNAVAILABLE
        reason = "no access token is stored for this account"
    elif expired and account.refresh_ref_id:
        state = TokenState.EXPIRED
        reason = "the access token has expired and a refresh token is available"
    elif expired:
        state = TokenState.REAUTHORIZATION_REQUIRED
        reason = "the access token has expired and there is no refresh token"
    elif expiring:
        state = TokenState.EXPIRING_SOON
        reason = f"the access token expires within {int(EXPIRING_SOON.total_seconds() // 3600)} hours"
    else:
        state = TokenState.VALID

    # A refresh token that itself expires soon cannot save an expired access token.
    if refresh_expires is not None and refresh_expires - moment <= REFRESH_DEAD_ZONE:
        if state in {TokenState.EXPIRED, TokenState.REFRESH_REQUIRED}:
            state = TokenState.REAUTHORIZATION_REQUIRED
            reason = (
                "the refresh token expires within 12 hours and cannot be renewed before that; "
                "reconnect the account"
            )
        elif state == TokenState.VALID:
            reason = reason or "the access token works, but the refresh token expires within 12 hours"

    can_refresh = has_refresh and state in {
        TokenState.EXPIRED,
        TokenState.EXPIRING_SOON,
        TokenState.REFRESH_REQUIRED,
        TokenState.VALID,
        TokenState.REFRESH_FAILED,
    }
    return TokenStatus(
        state=state,
        reason=reason,
        expires_at=expires.isoformat() if expires else None,
        refresh_expires_at=refresh_expires.isoformat() if refresh_expires else None,
        has_refresh_token=has_refresh,
        can_refresh=can_refresh,
        can_use=state in {TokenState.VALID, TokenState.EXPIRING_SOON},
    )


class TokenService:
    def __init__(self, session: Session, vault: Vault) -> None:
        self.session = session
        self.vault = vault
        self.secrets = SecretStore(session, vault)
        self.accounts = AccountService(session, vault)
        self.apps = ProviderAppService(session, vault)
        self.audit = AuditService(session)

    # -- read --------------------------------------------------------------
    def status(self, account: SocialAccount) -> TokenStatus:
        return compute_status(account)

    async def access_token(self, account: SocialAccount, *, allow_refresh: bool = True) -> str:
        """Return a usable access token, refreshing first if it is expiring."""
        status = compute_status(account)
        if status.state in {TokenState.REVOKED, TokenState.REAUTHORIZATION_REQUIRED, TokenState.CREDENTIAL_UNAVAILABLE}:
            raise SocialError(
                "REAUTHORIZATION_REQUIRED",
                status.reason or "the account must be reconnected",
                http_status=409,
                detail={"token_state": status.state.value},
            )
        if status.state in {TokenState.EXPIRED, TokenState.EXPIRING_SOON, TokenState.REFRESH_FAILED} and allow_refresh:
            if account.refresh_ref_id:
                await self.refresh(account)
            elif status.state != TokenState.EXPIRING_SOON:
                raise SocialError("REAUTHORIZATION_REQUIRED", status.reason or "no refresh token", http_status=409)
        ref = self.session.get(SecretRef, account.token_ref_id) if account.token_ref_id else None
        if ref is None or ref.status != SecretStatus.ACTIVE:
            self.accounts.mark_token_problem(
                account, TokenState.CREDENTIAL_UNAVAILABLE, "the stored access token is no longer usable"
            )
            raise SocialError("CREDENTIAL_UNAVAILABLE", "the stored access token is no longer usable", http_status=409)
        try:
            return await self.secrets.read(ref, "access_token")
        except SocialError as error:
            self.accounts.mark_token_problem(account, TokenState.CREDENTIAL_UNAVAILABLE, error.message)
            raise

    # -- refresh -----------------------------------------------------------
    async def refresh(self, account: SocialAccount, *, force: bool = False) -> TokenStatus:
        if not account.refresh_ref_id:
            raise SocialError(
                "REFRESH_NOT_SUPPORTED",
                "this account has no refresh token; reconnect it instead",
                http_status=409,
            )
        rotation = self._begin(account)
        if rotation is None:
            status = compute_status(account)
            raise SocialError(
                "REFRESH_IN_PROGRESS",
                "another refresh for this account is running",
                http_status=409,
                detail={"state": status.state.value},
            )
        journal_ref = None
        try:
            journal_ref = await self._journal(account, rotation)
            refresh_ref = self.session.get(SecretRef, account.refresh_ref_id)
            if refresh_ref is None or refresh_ref.status != SecretStatus.ACTIVE:
                raise SocialError("REAUTHORIZATION_REQUIRED", "the stored refresh token is no longer usable")
            refresh_token = await self.secrets.read(refresh_ref, "refresh_token")
            _config, credentials = await self.apps.resolve_credentials(
                account.platform, account.route or _default_route(account)
            )
            adapter = _adapter(account)
            try:
                bundle = await adapter.refresh(credentials, refresh_token)
            finally:
                await adapter.aclose()

            old_access = account.token_ref_id
            stored = await self.secrets.create(
                value=bundle.access_token,
                purpose="access_token",
                provider=account.platform.value,
                alias=f"account:{account.platform.value}:{account.provider_account_id}",
            )
            account.token_ref_id = stored.ref.id
            account.token_expires_at = bundle.expires_at
            account.last_refresh_at = utcnow()
            account.token_state = TokenState.VALID
            account.token_state_reason = None
            if bundle.refresh_token:
                old_refresh = account.refresh_ref_id
                new_refresh = await self.secrets.create(
                    value=bundle.refresh_token,
                    purpose="refresh_token",
                    provider=account.platform.value,
                    alias=f"refresh:{account.platform.value}:{account.provider_account_id}",
                )
                account.refresh_ref_id = new_refresh.ref.id
                account.refresh_expires_at = bundle.refresh_expires_at
                # The provider invalidated the old refresh token the moment it answered.
                if old_refresh:
                    previous = self.session.get(SecretRef, old_refresh)
                    if previous is not None and previous.status == SecretStatus.ACTIVE:
                        await self.secrets.revoke(previous, reason="rotated by the provider on refresh")
            elif bundle.refresh_expires_at:
                account.refresh_expires_at = bundle.refresh_expires_at
            self.session.flush()

            if old_access:
                previous_access = self.session.get(SecretRef, old_access)
                if previous_access is not None and previous_access.status == SecretStatus.ACTIVE:
                    await self.secrets.revoke(previous_access, reason="replaced by a refreshed access token")

            rotation.state = RotationState.COMPLETED
            rotation.finished_at = utcnow()
            rotation.new_ref_id = account.token_ref_id
            rotation.error_code = None
            self.session.flush()
            self.audit.record(
                "token.refreshed",
                resource_type="social_account",
                resource_id=account.id,
                metadata={
                    "rotated": bool(bundle.refresh_token),
                    "expires_at": bundle.expires_at.isoformat() if bundle.expires_at else None,
                    "rotation_id": rotation.id,
                },
            )
            return compute_status(account)
        except SocialError as error:
            self._fail(rotation, error.code, error.message)
            state = (
                TokenState.REAUTHORIZATION_REQUIRED
                if error.code in {"REAUTHORIZATION_REQUIRED", "CREDENTIAL_UNAVAILABLE", "APP_NOT_CONFIGURED"}
                else TokenState.REFRESH_FAILED
            )
            self.accounts.mark_token_problem(account, state, error.message)
            self.audit.record(
                "token.refresh_failed",
                resource_type="social_account",
                resource_id=account.id,
                outcome="failure",
                metadata={"code": error.code, "rotation_id": rotation.id},
            )
            raise
        except Exception as error:  # unexpected: still leave a usable journal
            self._fail(rotation, "REFRESH_ERROR", str(error)[:300])
            self.accounts.mark_token_problem(account, TokenState.REFRESH_FAILED, str(error)[:300])
            raise SocialError("REFRESH_ERROR", f"the refresh failed unexpectedly: {str(error)[:200]}") from error
        finally:
            if journal_ref is not None and rotation.state in {RotationState.COMPLETED, RotationState.FAILED}:
                try:
                    await self.secrets.revoke(journal_ref, reason="rotation finished")
                except (SocialError, VaultError):
                    pass

    # -- lock + journal ----------------------------------------------------
    def _begin(self, account: SocialAccount) -> CredentialRotation | None:
        stale_before = utcnow() - STALE_LOCK
        open_rows = list(
            self.session.execute(
                sa.select(CredentialRotation).where(
                    CredentialRotation.account_id == account.id,
                    CredentialRotation.state.in_([RotationState.PENDING, RotationState.IN_PROGRESS]),
                )
            ).scalars()
        )
        for row in open_rows:
            if _aware(row.started_at) < stale_before:
                row.state = RotationState.ABANDONED
                row.finished_at = utcnow()
                row.error_code = "STALE_LOCK"
                self.audit.record(
                    "token.rotation_abandoned",
                    resource_type="credential_rotation",
                    resource_id=row.id,
                    outcome="failure",
                    metadata={"account_id": account.id, "reason": "stale lock"},
                )
            else:
                self.session.flush()
                return None
        self.session.flush()

        rotation = CredentialRotation(
            account_id=account.id,
            provider=account.platform,
            kind="refresh",
            state=RotationState.PENDING,
            lock_token=secrets.token_hex(16),
            started_at=utcnow(),
            old_ref_id=account.refresh_ref_id,
            meta_json={"access_ref": account.token_ref_id},
        )
        self.session.add(rotation)
        self.session.flush()
        # Only one writer can move pending -> in_progress.
        result = self.session.execute(
            sa.update(CredentialRotation)
            .where(CredentialRotation.id == rotation.id, CredentialRotation.state == RotationState.PENDING)
            .values(state=RotationState.IN_PROGRESS, attempts=CredentialRotation.attempts + 1)
        )
        self.session.flush()
        if result.rowcount != 1:  # pragma: no cover - single-process app, defensive
            return None
        self.session.refresh(rotation)
        return rotation

    async def _journal(self, account: SocialAccount, rotation: CredentialRotation) -> SecretRef | None:
        payload = json.dumps(
            {
                "account_id": account.id,
                "platform": account.platform.value,
                "rotation_id": rotation.id,
                "old_ref_id": rotation.old_ref_id,
                "started_at": rotation.started_at.isoformat(),
                "lock_token": rotation.lock_token,
            }
        )
        try:
            stored = await self.secrets.create(
                value=payload,
                purpose="rotation_journal",
                provider=account.platform.value,
                alias=f"rotation:{rotation.id}",
            )
        except SocialError:
            # A refresh without a journal is exactly the failure mode this design exists to avoid.
            raise SocialError(
                "VAULT_UNAVAILABLE",
                "the refresh was not started because its journal could not be written",
                http_status=503,
            )
        rotation.journal_ref_id = stored.ref.id
        self.session.flush()
        return stored.ref

    def _fail(self, rotation: CredentialRotation, code: str, message: str) -> None:
        rotation.state = RotationState.FAILED
        rotation.finished_at = utcnow()
        rotation.error_code = code[:64]
        rotation.error_detail = message[:2000]
        self.session.flush()

    # -- recovery ----------------------------------------------------------
    def resolve_interrupted(self, account: SocialAccount) -> dict:
        """Decide what a crashed refresh left behind, from stored facts only."""
        rows = list(
            self.session.execute(
                sa.select(CredentialRotation).where(
                    CredentialRotation.account_id == account.id,
                    CredentialRotation.state == RotationState.IN_PROGRESS,
                )
            ).scalars()
        )
        resolved = []
        for row in rows:
            status = compute_status(account)
            if status.can_use:
                row.state = RotationState.ABANDONED
                row.error_code = "INTERRUPTED_BUT_USABLE"
                outcome = "the account still has a usable credential; the interrupted rotation was abandoned"
                if status.state == TokenState.VALID:
                    account.token_state = TokenState.VALID
            else:
                row.state = RotationState.FAILED
                row.error_code = "INTERRUPTED_NO_CREDENTIAL"
                self.accounts.mark_token_problem(
                    account,
                    TokenState.REAUTHORIZATION_REQUIRED,
                    "a refresh was interrupted and no usable credential remained; reconnect the account",
                )
                outcome = "no usable credential remained; the account needs reauthorization"
            row.finished_at = utcnow()
            row.error_detail = outcome
            self.session.flush()
            resolved.append({"rotation_id": row.id, "outcome": outcome})
        return {"account_id": account.id, "resolved": resolved}

    def pending_rotations(self) -> list[CredentialRotation]:
        return list(
            self.session.execute(
                sa.select(CredentialRotation)
                .where(CredentialRotation.state.in_([RotationState.PENDING, RotationState.IN_PROGRESS]))
                .order_by(CredentialRotation.started_at)
            ).scalars()
        )


def _aware(value):
    if value is None:
        return None
    from datetime import UTC

    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _default_route(account: SocialAccount):
    from ...db.enums import OAuthRoute, Platform

    if account.platform == Platform.FACEBOOK:
        return OAuthRoute.FACEBOOK_LOGIN
    if account.platform == Platform.TIKTOK:
        return OAuthRoute.TIKTOK_LOGIN
    return OAuthRoute.INSTAGRAM_LOGIN


def _adapter(account: SocialAccount):
    from ...integrations.social.base import AdapterRegistry

    return AdapterRegistry().get(account.platform, account.route or _default_route(account))
