"""OAuth engine: start a connection, receive the callback, claim the accounts.

State handling is the centre of gravity. The value handed to the provider is 256 bits of
randomness; the database stores only its SHA-256, so a database leak cannot forge a callback. The
session is bound to a provider, a route and an app, it expires in ten minutes, and it is consumed
by a conditional UPDATE so a replayed redirect loses the race and is reported as such.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import OAuthRoute, Platform, TokenState
from ...db.models.platform import OAuthSession, ProviderAppConfig, SecretRef, SocialAccount
from ...integrations.social.account_discovery import plan_discovery
from ...integrations.social.base import AdapterRegistry, SocialAdapter
from ...integrations.social.config import route_spec
from ...integrations.social.errors import SocialError, oauth_callback_error
from ...integrations.social.oauth import (
    challenge_for,
    check_state_shape,
    is_expired,
    new_state,
    new_verifier,
    state_hash,
)
from ...integrations.social.oauth import (
    redirect_uri as build_redirect_uri,
)
from ..backup import AuditService
from .accounts import AccountService
from .apps import ProviderAppService, validate_redirect_uri
from .callback_server import get_listener
from .capabilities import CapabilityService
from .secrets import SecretStore
from .tokens import compute_status
from .vault import Vault

SESSION_STATUSES = {"pending", "succeeded", "failed", "expired", "cancelled"}


@dataclass(slots=True)
class CallbackOutcome:
    ok: bool
    code: str
    message: str
    session_id: str | None = None
    accounts: list[str] | None = None


class OAuthEngine:
    def __init__(
        self,
        session: Session,
        vault: Vault,
        *,
        callback_port: int = 47821,
        state_ttl_s: int = 600,
        adapter_factory=None,
    ) -> None:
        self.session = session
        self.vault = vault
        self.callback_port = callback_port
        self.state_ttl = timedelta(seconds=state_ttl_s)
        self._adapter_factory = adapter_factory or (lambda: AdapterRegistry())
        self.apps = ProviderAppService(session, vault)
        self.accounts = AccountService(session, vault)
        self.capabilities = CapabilityService(session)
        self.secrets = SecretStore(session, vault)
        self.audit = AuditService(session)

    # -- helpers -----------------------------------------------------------
    def _adapter(self, platform: Platform, route: OAuthRoute) -> SocialAdapter:
        return self._adapter_factory().get(platform, route)

    def redirect_uri_for(self, route: OAuthRoute) -> str:
        return build_redirect_uri(self.callback_port, route)

    # -- start -------------------------------------------------------------
    async def start(
        self,
        platform: Platform,
        route: OAuthRoute,
        *,
        scopes: list[str] | None = None,
        return_route: str | None = None,
        account_id: str | None = None,
        purpose: str = "account_connect",
        context: dict | None = None,
    ) -> dict:
        """Begin a connection, or an account-bound permission upgrade (P8.20).

        When ``account_id`` is given the transaction is tied to that existing account: the callback
        can only adopt the credential for the same provider identity, and a mismatch is refused
        instead of silently re-pointing the account. The upgrade context (existing scopes, required
        scopes, missing scopes) is recorded on the transaction so the callback can verify it.
        """
        spec = route_spec(platform, route)
        config, credentials = await self.apps.resolve_credentials(platform, route)
        if not credentials.client_secret:
            raise SocialError(
                "APP_NOT_CONFIGURED",
                "the app secret is missing from the vault, so the authorization code could not be exchanged",
                http_status=409,
            )
        requested = list(dict.fromkeys(scopes or credentials.scopes or list(spec.scopes)))
        from .apps import validate_scopes

        requested = validate_scopes(route, platform, requested)

        account = None
        if account_id:
            account = self.session.get(SocialAccount, account_id)
            if account is None:
                raise SocialError("ACCOUNT_NOT_FOUND", "the account to upgrade does not exist", http_status=404)
            if account.platform != platform:
                raise SocialError(
                    "PLATFORM_MISMATCH",
                    "the account belongs to a different platform than the requested login path",
                    http_status=409,
                )
            if account.route is not None and account.route != route:
                raise SocialError(
                    "ROUTE_MISMATCH",
                    "the account was connected through a different login path; upgrade through the same one",
                    http_status=409,
                )

        expected_redirect = self.redirect_uri_for(route)
        if config.redirect_uri != expected_redirect:
            raise SocialError(
                "REDIRECT_MISMATCH",
                "the registered redirect URI does not match this app's callback listener",
                http_status=409,
                detail={
                    "registered": config.redirect_uri,
                    "listener": expected_redirect,
                    "hint": "update the developer app and the CreatorOS app settings so both match exactly",
                },
            )
        validate_redirect_uri(config.redirect_uri, route)
        listener = get_listener()
        if listener is None or not listener.running:
            raise SocialError(
                "CALLBACK_NOT_READY",
                "the loopback callback listener is not running, so the authorization could not be completed",
                http_status=503,
            )

        state = new_state()
        verifier = new_verifier() if spec.pkce_supported else None
        verifier_ref: SecretRef | None = None
        if verifier is not None:
            stored = await self.secrets.create(
                value=verifier,
                purpose="oauth_pkce_verifier",
                provider=platform.value,
                alias=f"pkce:{platform.value}:{route.value}",
            )
            verifier_ref = stored.ref

        row = OAuthSession(
            platform=platform,
            route=route,
            state_hash=state_hash(state),
            code_verifier_ref_id=verifier_ref.id if verifier_ref else None,
            redirect_uri=config.redirect_uri,
            expires_at=utcnow() + self.state_ttl,
            app_config_id=config.id,
            scopes_json=requested,
            pkce_method="S256" if verifier else None,
            return_route=(return_route or "")[:200] or None,
            status="pending",
            account_id=account.id if account is not None else None,
            result_json=(
                {
                    "purpose": purpose,
                    "upgrade": {
                        "account_id": account.id,
                        "provider_account_id": account.provider_account_id,
                        "account_type": account.account_type.value,
                        "login_path": route.value,
                        "existing_credential_ref": account.token_ref_id,
                        "refresh_credential_ref": account.refresh_ref_id,
                        "existing_scopes": list(account.scopes_json or []),
                        **(context or {}),
                        "requested_scopes": requested,
                    },
                }
                if account is not None
                else {"purpose": purpose}
            ),
        )
        self.session.add(row)
        self.session.flush()

        adapter = self._adapter(platform, route)
        try:
            url = adapter.authorization_url(
                credentials,
                state=state,
                code_challenge=challenge_for(verifier) if verifier else None,
                scopes=requested,
            )
        finally:
            await adapter.aclose()

        self.audit.record(
            "oauth.started",
            resource_type="oauth_session",
            resource_id=row.id,
            metadata={
                "platform": platform.value,
                "route": route.value,
                "scopes": requested,
                "pkce": bool(verifier),
                "app_mode": config.app_mode,
                "purpose": purpose,
                "account_id": account.id if account is not None else None,
            },
        )
        return {
            "session_id": row.id,
            "authorization_url": url,
            "redirect_uri": config.redirect_uri,
            "expires_at": row.expires_at.isoformat(),
            "scopes_requested": requested,
            "platform": platform.value,
            "route": route.value,
            "pkce": "S256" if verifier else None,
            "label": spec.label,
            "notes": spec.notes,
            "app_mode": config.app_mode,
            "purpose": purpose,
            "account_id": account.id if account is not None else None,
            "poll_url": f"/social/oauth/transactions/{row.id}",
        }

    # -- callback ----------------------------------------------------------
    async def handle_callback(self, route_value: str, params: dict[str, str]) -> dict:
        """Called by the loopback listener. Returns a page-friendly outcome."""
        try:
            route = OAuthRoute(route_value)
        except ValueError:
            return {"ok": False, "code": "UNKNOWN_ROUTE", "message": "This callback path is not one of ours."}

        state = params.get("state")
        try:
            check_state_shape(state)
        except SocialError as error:
            self.audit.record(
                "oauth.callback_rejected",
                resource_type="oauth_session",
                outcome="failure",
                metadata={"code": error.code, "route": route_value},
            )
            return {"ok": False, "code": error.code, "message": error.message}

        row = self.session.execute(
            sa.select(OAuthSession).where(OAuthSession.state_hash == state_hash(str(state)))
        ).scalar_one_or_none()
        if row is None:
            self.audit.record(
                "oauth.callback_rejected",
                resource_type="oauth_session",
                outcome="failure",
                metadata={"code": "STATE_INVALID", "route": route_value},
            )
            return {
                "ok": False,
                "code": "STATE_INVALID",
                "message": "This authorization response is not recognised. Start the connection again from CreatorOS.",
            }
        if row.route is not None and row.route != route:
            return {
                "ok": False,
                "code": "STATE_ROUTE_MISMATCH",
                "message": "This response belongs to a different connection attempt.",
                "session_id": row.id,
            }
        if row.consumed_at is not None:
            self.audit.record(
                "oauth.callback_rejected",
                resource_type="oauth_session",
                resource_id=row.id,
                outcome="failure",
                metadata={"code": "STATE_CONSUMED"},
            )
            return {
                "ok": False,
                "code": "STATE_CONSUMED",
                "message": "This connection was already completed. Nothing was changed twice.",
                "session_id": row.id,
            }
        if is_expired(row.expires_at):
            row.status = "expired"
            row.error_code = "STATE_EXPIRED"
            row.error_message = "the authorization was not completed within 10 minutes"
            self.session.flush()
            return {
                "ok": False,
                "code": "STATE_EXPIRED",
                "message": "This connection took too long, so it expired. Start again from CreatorOS.",
                "session_id": row.id,
            }

        # Consume before exchanging: a replay loses the race and never reaches the provider.
        consumed = self.session.execute(
            sa.update(OAuthSession)
            .where(OAuthSession.id == row.id, OAuthSession.consumed_at.is_(None))
            .values(consumed_at=utcnow(), callback_received_at=utcnow(), attempts=OAuthSession.attempts + 1)
        )
        self.session.flush()
        if consumed.rowcount != 1:
            return {
                "ok": False,
                "code": "STATE_CONSUMED",
                "message": "This connection was already completed.",
                "session_id": row.id,
            }
        self.session.refresh(row)

        if params.get("error"):
            error = oauth_callback_error(params)
            self._fail(row, error.code, error.message)
            self._audit_consent_outcome(row, error.code)
            return {"ok": False, "code": error.code, "message": error.message, "session_id": row.id}

        adapter = None
        try:
            config = self.session.get(ProviderAppConfig, row.app_config_id) if row.app_config_id else None
            if config is None:
                raise SocialError("APP_NOT_CONFIGURED", "the provider app registration disappeared mid-flow")
            _config, credentials = await self.apps.resolve_credentials(row.platform, row.route or route)
            adapter = self._adapter(row.platform, row.route or route)
            code = adapter.validate_callback(params)
            verifier = None
            if row.code_verifier_ref_id:
                verifier_row = self.session.get(SecretRef, row.code_verifier_ref_id)
                if verifier_row is None:
                    raise SocialError("STATE_INVALID", "the PKCE verifier is missing; start the connection again")
                try:
                    verifier = await self.secrets.read(verifier_row, "oauth_pkce_verifier")
                except SocialError as error:
                    raise SocialError("STATE_INVALID", f"the PKCE verifier could not be read: {error.code}") from error

            bundle = await adapter.exchange_code(credentials, code=code, code_verifier=verifier)
            granted = list(bundle.scopes or [])
            discovered = await adapter.discover_accounts(credentials, bundle.access_token)
            upgrade = (row.result_json or {}).get("upgrade")
            if row.account_id and isinstance(upgrade, dict):
                # Account-bound permission upgrade: adopt the credential for the *same* identity only.
                return await self._complete_upgrade(
                    row, upgrade, config, credentials, adapter, bundle, discovered
                )
            plan = plan_discovery(
                row.platform,
                row.route or route,
                discovered,
                existing=self.accounts.routes_by_provider_id(row.platform),
            )
            if not plan.usable:
                detail = {
                    "unsupported": [account.provider_account_id for account in plan.unsupported],
                    "observations": plan.observations,
                }
                raise SocialError(
                    "NO_SUPPORTED_ACCOUNT",
                    self._no_account_message(row.platform, plan),
                    http_status=409,
                    detail=detail,
                )

            claimed: list[str] = []
            states: dict[str, str] = {}
            for found in plan.usable:
                token_state = TokenState.VALID
                try:
                    profile = await adapter.fetch_profile(credentials, found.access_token or "", found.provider_account_id)
                except SocialError:
                    profile = None
                if profile is not None:
                    found.display_name = profile.display_name or found.display_name
                    found.username = profile.username or found.username
                    found.avatar_url = profile.avatar_url or found.avatar_url
                    found.account_type = profile.account_type or found.account_type
                    found.meta = {**(found.meta or {}), "profile": profile.meta}
                result = await self.accounts.claim(
                    platform=row.platform,
                    route=row.route or route,
                    discovered=found,
                    bundle=bundle,
                    app_config=config,
                    granted_scopes=granted,
                    token_state=token_state,
                    expires_at=bundle.expires_at if not found.expires_at else found.expires_at,
                    refresh_expires_at=bundle.refresh_expires_at,
                    scope_source=str(bundle.meta.get("scope_source") or ("token_response" if granted else "consent_request")),
                    api_version=credentials.api_version,
                )
                status = compute_status(result.account)
                verdicts = adapter.evaluate_capabilities(
                    account_type=result.account.account_type,
                    granted_scopes=granted,
                    token_state=status.state.value,
                    extra={
                        "app_mode": config.app_mode,
                        "api_version": credentials.api_version,
                        "scope_source": result.account.scope_source,
                        "page_tasks": list(found.tasks or []),
                    },
                )
                report = self.capabilities.record(
                    result.account,
                    verdicts,
                    trigger="oauth_connect",
                    api_version=credentials.api_version,
                )
                claimed.append(result.account.id)
                states[result.account.id] = report.overall.value

            row.status = "succeeded"
            row.completed_at = utcnow()
            row.account_id = claimed[0] if claimed else None
            row.scopes_json = granted or row.scopes_json
            row.result_json = {
                "accounts": claimed,
                "overall_states": states,
                "summary": plan.summary(),
                "unsupported": [account.provider_account_id for account in plan.unsupported],
            }
            self.session.flush()
            self.audit.record(
                "oauth.completed",
                resource_type="oauth_session",
                resource_id=row.id,
                metadata={
                    "platform": row.platform.value,
                    "route": (row.route.value if row.route else route.value),
                    "accounts": len(claimed),
                    "scopes": granted,
                },
            )
            title = "Connected" if len(claimed) == 1 else f"Connected {len(claimed)} accounts"
            return {
                "ok": True,
                "code": "CONNECTED",
                "title": title,
                "message": (
                    "You can close this window and return to CreatorOS Studio."
                    if not plan.unsupported and not plan.duplicates
                    else self._partial_message(plan)
                ),
                "session_id": row.id,
                "accounts": claimed,
            }
        except SocialError as error:
            self._fail(row, error.code, error.message)
            self._audit_consent_outcome(row, error.code)
            return {"ok": False, "code": error.code, "message": error.message, "session_id": row.id}
        except Exception as error:  # noqa: BLE001 - defensive; still recorded honestly, never hidden
            self._fail(row, "CALLBACK_FAILED", str(error)[:300])
            self._audit_consent_outcome(row, "CALLBACK_FAILED")
            return {
                "ok": False,
                "code": "CALLBACK_FAILED",
                "message": "The connection failed unexpectedly. CreatorOS recorded the reason.",
                "session_id": row.id,
            }
        finally:
            if adapter is not None:
                await adapter.aclose()

    # -- permission upgrade ------------------------------------------------
    async def _complete_upgrade(
        self, row: OAuthSession, upgrade: dict, config, credentials, adapter, bundle, discovered
    ) -> dict:
        """Adopt a re-consent credential for an existing account, or refuse without touching it.

        Order matters and is the whole safety argument: the provider identity is checked first, the
        new credential is written and the account row updated by `AccountService.claim`, and only
        then is the previous credential revoked. Any failure before that leaves the old (working)
        credential in place, and a mismatch is reported rather than re-pointing the account.
        """
        from ...db.enums import CapabilityState, normalize_capability_state
        from ...integrations.social.config import analytics_capability

        account = self.session.get(SocialAccount, row.account_id) if row.account_id else None
        if account is None:
            self._fail(row, "ACCOUNT_REMOVED", "the account being upgraded no longer exists")
            return {
                "ok": False,
                "code": "ACCOUNT_REMOVED",
                "message": "That account is no longer connected, so nothing was changed.",
                "session_id": row.id,
            }

        expected_id = str(upgrade.get("provider_account_id") or account.provider_account_id)
        found = next((item for item in discovered if item.provider_account_id == expected_id), None)
        if found is None:
            self._fail(
                row,
                "ACCOUNT_IDENTITY_MISMATCH",
                "the provider returned a different account than the one being upgraded",
            )
            return {
                "ok": False,
                "code": "ACCOUNT_IDENTITY_MISMATCH",
                "message": "The provider did not return the same account, so its credential was left untouched.",
                "session_id": row.id,
            }

        # Never forget an existing grant: the stored scopes, the provider's answer and the account's
        # own reported scopes are unioned, in a deterministic order.
        granted = list(
            dict.fromkeys(
                [*(account.scopes_json or []), *(bundle.scopes or []), *(found.scopes or [])]
            )
        )
        try:
            result = await self.accounts.claim(
                platform=row.platform,
                route=row.route or account.route or OAuthRoute.FACEBOOK_LOGIN,
                discovered=found,
                bundle=bundle,
                app_config=config,
                granted_scopes=granted,
                token_state=TokenState.VALID,
                expires_at=bundle.expires_at if not found.expires_at else found.expires_at,
                refresh_expires_at=bundle.refresh_expires_at,
                scope_source=str(
                    bundle.meta.get("scope_source") or ("token_response" if bundle.scopes else "consent_request")
                ),
                api_version=credentials.api_version,
            )
        except SocialError as error:
            # `claim` stores the new secret before revoking the old one, so the existing credential is
            # still valid and the user can try again.
            self._fail(row, error.code, error.message)
            self.audit.record(
                "analytics.consent.failed",
                resource_type="social_account",
                resource_id=account.id,
                outcome="failure",
                metadata={"code": error.code, "preserved_credential": True},
            )
            return {"ok": False, "code": error.code, "message": error.message, "session_id": row.id}

        status = compute_status(result.account)
        verdicts = adapter.evaluate_capabilities(
            account_type=result.account.account_type,
            granted_scopes=granted,
            token_state=status.state.value,
            extra={
                "app_mode": config.app_mode,
                "api_version": credentials.api_version,
                "scope_source": result.account.scope_source,
                "page_tasks": list(found.tasks or []),
            },
        )
        report = self.capabilities.record(
            result.account,
            verdicts,
            trigger="analytics_consent",
            api_version=credentials.api_version,
            evidence_ref=f"oauth_session:{row.id}",
        )
        capability = analytics_capability(row.platform)
        verdict = next((item for item in verdicts if item.capability == (capability.key if capability else "")), None)
        available = verdict is not None and normalize_capability_state(verdict.state) == CapabilityState.AVAILABLE
        outcome = "capability_available" if available else "capability_still_blocked"

        row.status = "succeeded"
        row.completed_at = utcnow()
        row.scopes_json = granted
        row.result_json = {
            **(row.result_json or {}),
            "purpose": "analytics_consent",
            "accounts": [result.account.id],
            "overall_states": {result.account.id: report.overall.value},
            "upgrade": {
                **upgrade,
                "outcome": outcome,
                "granted_scopes": granted,
                "still_missing": list(verdict.missing) if verdict is not None else [],
                "capability": capability.key if capability else None,
                "capability_state": verdict.state if verdict is not None else None,
            },
        }
        self.session.flush()
        self.audit.record(
            "analytics.consent.completed",
            resource_type="social_account",
            resource_id=result.account.id,
            metadata={
                "outcome": outcome,
                "capability": capability.key if capability else None,
                "granted_scopes": granted,
                "missing": list(verdict.missing) if verdict is not None else [],
            },
        )
        return {
            "ok": available,
            "code": outcome.upper(),
            "title": "Analytics access granted" if available else "Consent recorded",
            "message": (
                "You can close this window and return to CreatorOS Studio."
                if available
                else "The permission was recorded, but analytics is still blocked. The account page explains why."
            ),
            "session_id": row.id,
            "accounts": [result.account.id],
        }

    # -- state transitions -------------------------------------------------
    def _audit_consent_outcome(self, row: OAuthSession, code: str) -> None:
        """Record a permission-upgrade outcome on the *account*, for a denial or a failure.

        The account is untouched (denial never disconnects), so the only durable trace is the audit
        record; without it the user would see "failed" with no reason attached to the account.
        """
        if row.account_id is None:
            return
        purpose = (row.result_json or {}).get("purpose")
        if purpose != "analytics_consent":
            return
        denied = code in {"AUTHORIZATION_DENIED", "ACCESS_DENIED"}
        self.audit.record(
            "analytics.consent.denied" if denied else "analytics.consent.failed",
            resource_type="social_account",
            resource_id=row.account_id,
            outcome="failure",
            metadata={"code": code, "session_id": row.id, "credential_preserved": True},
        )

    def _fail(self, row: OAuthSession, code: str, message: str) -> None:
        row.status = "failed"
        row.error_code = code[:64]
        row.error_message = message[:300]
        row.completed_at = utcnow()
        self.session.flush()
        self.audit.record(
            "oauth.failed",
            resource_type="oauth_session",
            resource_id=row.id,
            outcome="failure",
            metadata={"code": code, "platform": row.platform.value},
        )

    def cancel(self, session_id: str) -> dict:
        row = self.session.get(OAuthSession, session_id)
        if row is None:
            raise SocialError("NOT_FOUND", "no such connection attempt", http_status=404)
        if row.status != "pending":
            return self.transaction(session_id)
        row.status = "cancelled"
        row.completed_at = utcnow()
        row.error_code = "CANCELLED_BY_USER"
        row.error_message = "the connection attempt was cancelled in CreatorOS"
        self.session.flush()
        self.audit.record(
            "oauth.cancelled",
            resource_type="oauth_session",
            resource_id=row.id,
            metadata={"platform": row.platform.value},
        )
        return self.transaction(session_id)

    def transaction(self, session_id: str) -> dict:
        row = self.session.get(OAuthSession, session_id)
        if row is None:
            raise SocialError("NOT_FOUND", "no such connection attempt", http_status=404)
        expired = row.status == "pending" and is_expired(row.expires_at)
        accounts = []
        for account_id in (row.result_json or {}).get("accounts", []):
            account = self.session.get(SocialAccount, account_id)
            if account is not None:
                accounts.append(
                    {
                        "id": account.id,
                        "label": account.internal_label or account.display_name,
                        "platform": account.platform.value,
                        "route": account.route.value if account.route else None,
                        "account_type": account.account_type.value,
                        "connection_status": account.connection_status.value,
                        "capabilities": account.capabilities_json,
                    }
                )
        return {
            "session_id": row.id,
            "platform": row.platform.value,
            "route": row.route.value if row.route else None,
            "status": "expired" if expired else row.status,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "consumed_at": row.consumed_at.isoformat() if row.consumed_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "accounts": accounts,
            "scopes": list(row.scopes_json or []),
            "summary": (row.result_json or {}).get("summary", {}),
            "unsupported": (row.result_json or {}).get("unsupported", []),
        }

    def recent(self, *, limit: int = 20) -> list[dict]:
        rows = list(
            self.session.execute(
                sa.select(OAuthSession).order_by(OAuthSession.created_at.desc()).limit(limit)
            ).scalars()
        )
        keys = (
            "session_id",
            "platform",
            "route",
            "status",
            "created_at",
            "expires_at",
            "completed_at",
            "error_code",
            "error_message",
        )
        return [{key: item[key] for key in keys} for item in (self.transaction(row.id) for row in rows)]

    # -- messages ----------------------------------------------------------
    def _no_account_message(self, platform: Platform, plan) -> str:
        if platform == Platform.FACEBOOK:
            return (
                "Sign-in worked, but this account does not administer a Facebook Page. CreatorOS "
                "publishes to Pages only (a personal profile cannot be connected through the Graph "
                "API), so there is nothing to connect."
            )
        if platform == Platform.INSTAGRAM:
            if plan.unsupported:
                return (
                    "This Instagram account is personal, not professional. Convert it to a business "
                    "or creator account, then connect again."
                )
            return (
                "Instagram returned no professional account that CreatorOS can use. If the account "
                "is linked to a Facebook Page, try the Facebook Login path instead."
            )
        if plan.claimed:
            return "The account was found but no usable credential came back, so nothing was connected."
        return "The provider returned no account that CreatorOS can use."

    def _partial_message(self, plan) -> str:
        parts = ["You can close this window and return to CreatorOS Studio."]
        if plan.unsupported:
            parts.append(
                f"{len(plan.unsupported)} account(s) were skipped because this platform does not support that account type."
            )
        if plan.duplicates:
            parts.append(f"{len(plan.duplicates)} account(s) were already connected through another login path.")
        return " ".join(parts)
