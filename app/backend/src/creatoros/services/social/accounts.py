"""Account management: rows, labels, claims and disconnects.

One row per provider identity. Everything a capability decision needs is stored on the row:
the route, the granted scopes, the token references, the token state and the evidence that backs
them. Everything a human needs is there too: a label they chose, a group, and the last time the
account was actually verified - not "last time we tried".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import (
    ConnectionStatus,
    OAuthRoute,
    Platform,
    SecretStatus,
    TokenState,
)
from ...db.models.platform import ProviderAppConfig, SecretRef, SocialAccount
from ...integrations.social.base import DiscoveredAccount, TokenBundle
from ...integrations.social.errors import SocialError
from ..backup import AuditService
from .apps import ProviderAppService
from .secrets import SecretStore
from .vault import Vault


@dataclass(slots=True)
class ClaimResult:
    account: SocialAccount
    created: bool
    token_stored: bool
    refresh_stored: bool
    warnings: list[str]


def mask(value: str | None) -> str | None:
    """Never return a usable identifier fragment for a secret-like value."""
    if not value:
        return None
    text = str(value)
    if len(text) <= 4:
        return "…"
    return f"…{text[-2:]}"


class AccountService:
    def __init__(self, session: Session, vault: Vault) -> None:
        self.session = session
        self.vault = vault
        self.secrets = SecretStore(session, vault)
        self.audit = AuditService(session)

    # -- queries -----------------------------------------------------------
    def list(
        self,
        *,
        platform: Platform | None = None,
        status: ConnectionStatus | None = None,
        include_disconnected: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SocialAccount], int]:
        stmt = sa.select(SocialAccount)
        count_stmt = sa.select(sa.func.count()).select_from(SocialAccount)
        conditions = []
        if platform is not None:
            conditions.append(SocialAccount.platform == platform)
        if status is not None:
            conditions.append(SocialAccount.connection_status == status)
        if not include_disconnected:
            conditions.append(SocialAccount.connection_status != ConnectionStatus.DISCONNECTED)
        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        stmt = stmt.order_by(SocialAccount.platform, SocialAccount.display_name).limit(limit).offset(offset)
        rows = list(self.session.execute(stmt).scalars())
        total = int(self.session.execute(count_stmt).scalar_one())
        return rows, total

    def get(self, account_id: str) -> SocialAccount | None:
        return self.session.get(SocialAccount, account_id)

    def by_provider_id(self, platform: Platform, provider_account_id: str) -> SocialAccount | None:
        return self.session.execute(
            sa.select(SocialAccount).where(
                SocialAccount.platform == platform,
                SocialAccount.provider_account_id == provider_account_id,
            )
        ).scalar_one_or_none()

    def routes_by_provider_id(self, platform: Platform) -> dict[str, str]:
        rows = self.session.execute(
            sa.select(SocialAccount.provider_account_id, SocialAccount.route).where(SocialAccount.platform == platform)
        ).all()
        return {str(pid): (route.value if route else "unknown") for pid, route in rows}

    # -- claims ------------------------------------------------------------
    async def claim(
        self,
        *,
        platform: Platform,
        route: OAuthRoute,
        discovered: DiscoveredAccount,
        bundle: TokenBundle,
        app_config: ProviderAppConfig,
        granted_scopes: list[str],
        token_state: TokenState,
        expires_at: datetime | None,
        refresh_expires_at: datetime | None,
        scope_source: str,
        api_version: str | None,
    ) -> ClaimResult:
        """Store the credential for one discovered account and create/refresh its row."""
        warnings: list[str] = []
        existing = self.by_provider_id(platform, discovered.provider_account_id)
        access_token = discovered.access_token or bundle.access_token
        if not access_token:
            raise SocialError(
                "NO_ACCOUNT_TOKEN",
                f"{discovered.display_name} came back without a usable token, so it cannot be claimed",
                http_status=409,
            )

        previous_access_ref = existing.token_ref_id if existing else None
        previous_refresh_ref = existing.refresh_ref_id if existing else None

        stored_access = await self.secrets.create(
            value=access_token,
            purpose="access_token",
            provider=platform.value,
            alias=f"account:{platform.value}:{discovered.provider_account_id}",
        )
        refresh_stored = False
        stored_refresh = None
        if bundle.refresh_token:
            stored_refresh = await self.secrets.create(
                value=bundle.refresh_token,
                purpose="refresh_token",
                provider=platform.value,
                alias=f"refresh:{platform.value}:{discovered.provider_account_id}",
            )
            refresh_stored = True

        account = existing or SocialAccount(
            platform=platform,
            provider_account_id=discovered.provider_account_id,
            display_name=discovered.display_name,
        )
        account.display_name = discovered.display_name or account.display_name
        account.username = discovered.username
        account.avatar_url = discovered.avatar_url
        account.account_type = discovered.account_type
        account.parent_page_id = discovered.parent_page_id
        account.route = route
        account.app_config_id = app_config.id
        account.scopes_json = list(dict.fromkeys(granted_scopes))
        account.scope_source = scope_source[:190]
        account.token_ref_id = stored_access.ref.id
        account.refresh_ref_id = stored_refresh.ref.id if stored_refresh else account.refresh_ref_id
        account.token_expires_at = expires_at
        account.refresh_expires_at = refresh_expires_at
        account.token_state = token_state
        account.token_state_reason = None
        account.api_version = api_version
        account.disconnected_at = None
        account.connection_status = ConnectionStatus.CONNECTED
        meta = dict(account.meta_json or {})
        meta.update(discovered.meta or {})
        if discovered.tasks:
            # Persist the Page role list: a Page task (ANALYZE, CREATE_CONTENT) is not a scope, and
            # the analytics consent plan must be able to say "the Page ANALYZE task is missing".
            meta["page_tasks"] = [str(task) for task in discovered.tasks]
        meta["discovered_at"] = utcnow().isoformat()
        if existing is not None:
            observations = list(meta.get("observations") or [])
            observations.append(f"reconnected via {route.value} at {utcnow().isoformat()}")
            meta["observations"] = observations[-10:]
        account.meta_json = meta
        if existing is None:
            self.session.add(account)
        self.session.flush()

        # The old credential is revoked only after the new one is safely stored.
        for old_id in (previous_access_ref, previous_refresh_ref):
            if old_id and old_id != account.token_ref_id and old_id != account.refresh_ref_id:
                old_ref = self.session.get(SecretRef, old_id)
                if old_ref is not None and old_ref.status == SecretStatus.ACTIVE:
                    await self.secrets.revoke(old_ref, reason="replaced by a newer credential")
        self.audit.record(
            "account.claimed" if existing is None else "account.reconnected",
            resource_type="social_account",
            resource_id=account.id,
            metadata={
                "platform": platform.value,
                "route": route.value,
                "account_type": discovered.account_type.value,
                "scopes": account.scopes_json,
                "refresh_token_stored": refresh_stored,
            },
        )
        return ClaimResult(
            account=account,
            created=existing is None,
            token_stored=True,
            refresh_stored=refresh_stored,
            warnings=warnings,
        )

    # -- mutations ---------------------------------------------------------
    def rename(self, account: SocialAccount, *, label: str | None, group: str | None) -> SocialAccount:
        if label is not None:
            cleaned = label.strip()
            if len(cleaned) > 200:
                raise SocialError("VALIDATION_ERROR", "the label must be at most 200 characters")
            account.internal_label = cleaned or None
        if group is not None:
            cleaned = group.strip()
            if len(cleaned) > 120:
                raise SocialError("VALIDATION_ERROR", "the group must be at most 120 characters")
            account.group_label = cleaned or None
        self.session.flush()
        self.audit.record(
            "account.label_changed",
            resource_type="social_account",
            resource_id=account.id,
            metadata={"label": account.internal_label, "group": account.group_label},
        )
        return account

    async def disconnect(self, account: SocialAccount, *, revoke_at_provider: bool = True) -> dict:
        result = {"revoked_at_provider": False, "detail": ""}
        if revoke_at_provider and account.token_ref_id:
            ref = self.session.get(SecretRef, account.token_ref_id)
            if ref is not None and ref.status == SecretStatus.ACTIVE:
                try:
                    token = await self.secrets.read(ref, "access_token")
                except SocialError as error:
                    result["detail"] = f"the credential could not be read before revocation ({error.code})"
                    token = None
                if token:
                    try:
                        _config, credentials = await ProviderAppService(self.session, self.vault).resolve_credentials(
                            account.platform, account.route or OAuthRoute.FACEBOOK_LOGIN
                        )
                    except SocialError as error:
                        credentials = None
                        result["detail"] = f"the app registration is incomplete: {error.code}"
                    if credentials is not None:
                        adapter = self._adapter(account)
                        try:
                            revoked = await adapter.revoke(credentials, token)
                            result["revoked_at_provider"] = revoked.revoked
                            result["detail"] = revoked.detail
                        except SocialError as error:
                            result["detail"] = f"provider revocation failed: {error.code}: {error.message}"
                        finally:
                            await adapter.aclose()
        for ref_id in (account.token_ref_id, account.refresh_ref_id):
            if not ref_id:
                continue
            ref = self.session.get(SecretRef, ref_id)
            if ref is not None and ref.status == SecretStatus.ACTIVE:
                await self.secrets.revoke(ref, reason="account disconnected")
        account.connection_status = ConnectionStatus.DISCONNECTED
        account.token_state = TokenState.REVOKED
        account.token_state_reason = "disconnected in CreatorOS"
        account.disconnected_at = utcnow()
        self.session.flush()
        self.audit.record(
            "account.disconnected",
            resource_type="social_account",
            resource_id=account.id,
            metadata={
                "platform": account.platform.value,
                "revoked_at_provider": result["revoked_at_provider"],
                "detail": result["detail"][:300],
            },
        )
        return result

    def _adapter(self, account: SocialAccount):
        from ...integrations.social.base import AdapterRegistry

        registry = AdapterRegistry()
        route = account.route or (
            OAuthRoute.FACEBOOK_LOGIN if account.platform == Platform.FACEBOOK else OAuthRoute.TIKTOK_LOGIN
        )
        return registry.get(account.platform, route)

    def mark_token_problem(self, account: SocialAccount, state: TokenState, reason: str) -> SocialAccount:
        account.token_state = state
        account.token_state_reason = reason[:300]
        account.connection_status = {
            TokenState.EXPIRED: ConnectionStatus.TOKEN_EXPIRED,
            TokenState.REVOKED: ConnectionStatus.TOKEN_EXPIRED,
            TokenState.REAUTHORIZATION_REQUIRED: ConnectionStatus.TOKEN_EXPIRED,
            TokenState.CREDENTIAL_UNAVAILABLE: ConnectionStatus.UNKNOWN,
            TokenState.REFRESH_FAILED: ConnectionStatus.UNKNOWN,
        }.get(state, account.connection_status)
        self.session.flush()
        return account

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict:
        rows = self.session.execute(
            sa.select(SocialAccount.platform, SocialAccount.connection_status, sa.func.count()).group_by(
                SocialAccount.platform, SocialAccount.connection_status
            )
        ).all()
        by_platform: dict[str, dict[str, int]] = {}
        for platform, status, count in rows:
            key = platform.value if hasattr(platform, "value") else str(platform)
            bucket = by_platform.setdefault(key, {"total": 0})
            status_key = status.value if hasattr(status, "value") else str(status)
            bucket[status_key] = int(count)
            bucket["total"] += int(count)
        total = sum(bucket["total"] for bucket in by_platform.values())
        return {
            "total": total,
            "by_platform": by_platform,
            "needs_attention": sum(
                count
                for platform, status, count in rows
                if (status.value if hasattr(status, "value") else str(status))
                not in {"connected", "disconnected"}
            ),
            "disconnected": sum(
                count
                for _platform, status, count in rows
                if (status.value if hasattr(status, "value") else str(status)) == "disconnected"
            ),
        }


def account_label(account: SocialAccount) -> str:
    return account.internal_label or account.display_name
