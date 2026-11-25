"""Provider developer-app registration.

The client id is public: it is in every authorization URL. The client secret is not, so it lives
in the vault and this service only ever holds a `secret_ref` id. `resolve_credentials` is the one
function that pulls the secret out (briefly, in memory) for an adapter call.

Redirect URIs are validated, not trusted: they must be loopback on 127.0.0.1 with the exact
callback path, because a redirect to any other host would hand an authorization code to a third
party.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import OAuthRoute, Platform
from ...db.models.platform import ProviderAppConfig, SecretRef, SocialAccount
from ...integrations.social import config as social_config
from ...integrations.social.base import ProviderCredentials
from ...integrations.social.config import route_spec
from ...integrations.social.errors import SocialError
from ...integrations.social.oauth import redirect_uri as build_redirect_uri
from ..backup import AuditService
from .secrets import SecretStore
from .vault import Vault


@dataclass(slots=True)
class AppRegistration:
    provider: Platform
    route: OAuthRoute
    client_id: str
    client_secret: str | None
    redirect_uri: str | None
    api_version: str | None
    app_mode: str
    scopes: list[str]
    login_config_id: str | None
    notes: str | None


def validate_redirect_uri(uri: str, route: OAuthRoute) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "http":
        raise SocialError(
            "REDIRECT_INVALID",
            "the redirect URI must use http on the loopback interface",
            detail={"scheme": parsed.scheme},
        )
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SocialError(
            "REDIRECT_INVALID",
            "the redirect URI must point at 127.0.0.1, not at another host",
            detail={"host": parsed.hostname},
        )
    if parsed.hostname == "localhost":
        raise SocialError(
            "REDIRECT_INVALID",
            "use 127.0.0.1 rather than localhost: providers compare the string exactly",
        )
    if not parsed.port or not (1024 <= parsed.port <= 65535):
        raise SocialError("REDIRECT_INVALID", "the redirect URI needs an explicit loopback port")
    if parsed.path != social_config.CALLBACK_PATH.format(route=route.value):
        raise SocialError(
            "REDIRECT_INVALID",
            f"the redirect path must be {social_config.CALLBACK_PATH.format(route=route.value)}",
            detail={"path": parsed.path},
        )
    if parsed.query or parsed.fragment:
        raise SocialError("REDIRECT_INVALID", "the redirect URI must not contain a query or fragment")
    return uri


def validate_scopes(route: OAuthRoute, provider: Platform, scopes: list[str]) -> list[str]:
    spec = route_spec(provider, route)
    allowed = set(spec.scopes) | set(spec.extra_scopes)
    cleaned = [scope.strip() for scope in scopes if scope and scope.strip()]
    unknown = [scope for scope in cleaned if scope not in allowed]
    if unknown:
        raise SocialError(
            "SCOPE_UNKNOWN",
            f"these scopes are not part of the {spec.label} surface: {', '.join(unknown)}",
            detail={"allowed": sorted(allowed)},
        )
    return list(dict.fromkeys(cleaned))


class ProviderAppService:
    def __init__(self, session: Session, vault: Vault) -> None:
        self.session = session
        self.vault = vault
        self.secrets = SecretStore(session, vault)
        self.audit = AuditService(session)

    # -- read --------------------------------------------------------------
    def list(self) -> list[ProviderAppConfig]:
        return list(
            self.session.execute(
                sa.select(ProviderAppConfig).order_by(ProviderAppConfig.provider, ProviderAppConfig.route)
            ).scalars()
        )

    def get(self, provider: Platform, route: OAuthRoute) -> ProviderAppConfig | None:
        return self.session.execute(
            sa.select(ProviderAppConfig).where(
                ProviderAppConfig.provider == provider, ProviderAppConfig.route == route
            )
        ).scalar_one_or_none()

    def describe(self, config: ProviderAppConfig) -> dict:
        spec = route_spec(config.provider, config.route)
        secret_present = False
        secret_status = None
        if config.client_secret_ref_id:
            ref = self.session.get(SecretRef, config.client_secret_ref_id)
            secret_present = bool(ref and ref.status.value == "active")
            secret_status = ref.status.value if ref else "missing"
        return {
            "id": config.id,
            "provider": config.provider.value,
            "route": config.route.value,
            "label": spec.label,
            "client_id": config.client_id,
            "client_id_suffix": config.client_id[-4:] if config.client_id else None,
            "redirect_uri": config.redirect_uri,
            "scopes": list(config.scopes_json or []),
            "api_version": config.api_version,
            "app_mode": config.app_mode,
            "login_config_id": config.login_config_id,
            "has_client_secret": secret_present,
            "client_secret_status": secret_status,
            "last_verified_at": config.last_verified_at.isoformat() if config.last_verified_at else None,
            "notes": config.notes,
            "docs_url": spec.docs_url,
            "pkce_required": spec.pkce_required,
            "requires_app_review_for_live": config.app_mode != "live",
        }

    # -- write -------------------------------------------------------------
    async def upsert(self, registration: AppRegistration) -> ProviderAppConfig:
        spec = route_spec(registration.provider, registration.route)
        client_id = (registration.client_id or "").strip()
        if not client_id or len(client_id) > 190:
            raise SocialError("VALIDATION_ERROR", "a client id is required (max 190 characters)")
        redirect = registration.redirect_uri or build_redirect_uri(social_config.DEFAULT_CALLBACK_PORT, registration.route)
        validate_redirect_uri(redirect, registration.route)
        scopes = validate_scopes(registration.route, registration.provider, registration.scopes or list(spec.scopes))
        if registration.app_mode not in {"development", "live"}:
            raise SocialError("VALIDATION_ERROR", "app_mode must be 'development' or 'live'")

        config = self.get(registration.provider, registration.route)
        created = config is None
        if config is None:
            config = ProviderAppConfig(
                provider=registration.provider,
                route=registration.route,
                client_id=client_id,
                redirect_uri=redirect,
                scopes_json=scopes,
                app_mode=registration.app_mode,
            )
            self.session.add(config)
            self.session.flush()
        else:
            config.client_id = client_id
            config.redirect_uri = redirect
            config.scopes_json = scopes
            config.app_mode = registration.app_mode

        config.api_version = registration.api_version or config.api_version
        config.login_config_id = registration.login_config_id
        config.notes = registration.notes
        config.last_verified_at = utcnow()

        if registration.client_secret:
            if config.client_secret_ref_id:
                ref = self._secret_ref(config.client_secret_ref_id)
                if ref is not None:
                    await self.secrets.rotate(ref, registration.client_secret)
                else:
                    stored = await self.secrets.create(
                        value=registration.client_secret,
                        purpose="provider_app_secret",
                        provider=registration.provider.value,
                        alias=f"app:{registration.provider.value}:{registration.route.value}",
                    )
                    config.client_secret_ref_id = stored.ref.id
            else:
                stored = await self.secrets.create(
                    value=registration.client_secret,
                    purpose="provider_app_secret",
                    provider=registration.provider.value,
                    alias=f"app:{registration.provider.value}:{registration.route.value}",
                )
                config.client_secret_ref_id = stored.ref.id
        self.session.flush()
        self.audit.record(
            "provider_app.saved" if created else "provider_app.updated",
            resource_type="provider_app_config",
            resource_id=config.id,
            metadata={
                "provider": registration.provider.value,
                "route": registration.route.value,
                "client_id_suffix": client_id[-4:],
                "app_mode": config.app_mode,
                "secret_stored": bool(registration.client_secret),
            },
        )
        return config

    async def set_secret(self, provider: Platform, route: OAuthRoute, secret: str) -> ProviderAppConfig:
        config = self.get(provider, route)
        if config is None:
            raise SocialError("APP_NOT_CONFIGURED", "register the app before adding its secret", http_status=404)
        if config.client_secret_ref_id:
            ref = self._secret_ref(config.client_secret_ref_id)
            if ref is not None:
                await self.secrets.rotate(ref, secret)
            else:
                stored = await self.secrets.create(
                    value=secret,
                    purpose="provider_app_secret",
                    provider=provider.value,
                    alias=f"app:{provider.value}:{route.value}",
                )
                config.client_secret_ref_id = stored.ref.id
        else:
            stored = await self.secrets.create(
                value=secret,
                purpose="provider_app_secret",
                provider=provider.value,
                alias=f"app:{provider.value}:{route.value}",
            )
            config.client_secret_ref_id = stored.ref.id
        config.last_verified_at = utcnow()
        self.session.flush()
        self.audit.record(
            "provider_app.secret_saved",
            resource_type="provider_app_config",
            resource_id=config.id,
            metadata={"provider": provider.value, "route": route.value},
        )
        return config

    async def attach_secret_ref(
        self, provider: Platform, route: OAuthRoute, secret_id: str
    ) -> ProviderAppConfig:
        """Link an already-sealed vault record to this app registration.

        The previous credential is revoked only after the new one is linked, so a failure halfway
        through can never leave the registration pointing at nothing.
        """
        config = self.get(provider, route)
        if config is None:
            raise SocialError("APP_NOT_CONFIGURED", "register the app before attaching its secret", http_status=404)
        ref = self.session.get(SecretRef, secret_id)
        if ref is None:
            raise SocialError("VAULT_NOT_FOUND", "no adopted secret has that id", http_status=404)
        if ref.purpose != "provider_app_secret":
            raise SocialError(
                "VAULT_PURPOSE_MISMATCH",
                f"that secret is a {ref.purpose}, not a provider app secret",
                http_status=409,
            )
        if ref.status.value != "active":
            raise SocialError("CREDENTIAL_UNAVAILABLE", f"that secret is {ref.status.value}", http_status=409)
        previous = config.client_secret_ref_id
        config.client_secret_ref_id = ref.id
        config.last_verified_at = utcnow()
        self.session.flush()
        if previous and previous != ref.id:
            old = self._secret_ref(previous)
            if old is not None:
                await self.secrets.revoke(old, reason="replaced by a new app secret")
        self.audit.record(
            "provider_app.secret_linked",
            resource_type="provider_app_config",
            resource_id=config.id,
            metadata={"provider": provider.value, "route": route.value, "secret_id": ref.id},
        )
        return config

    async def delete(self, provider: Platform, route: OAuthRoute) -> bool:
        config = self.get(provider, route)
        if config is None:
            return False
        accounts = self.session.execute(
            sa.select(sa.func.count())
            .select_from(SocialAccount)
            .where(
                SocialAccount.app_config_id == config.id,
                SocialAccount.connection_status != "disconnected",
            )
        ).scalar_one()
        if accounts:
            raise SocialError(
                "APP_IN_USE",
                f"{accounts} connected account(s) still use this app; disconnect them first",
                http_status=409,
            )
        # The sealed secret is revoked first: deleting the row alone would leave an encrypted
        # credential in the vault with nothing pointing at it, which is exactly the kind of orphan
        # a user cannot see or clean up.
        secret_id = config.client_secret_ref_id
        if secret_id:
            ref = self._secret_ref(secret_id)
            if ref is not None:
                await self.secrets.revoke(ref, reason="provider app registration deleted")
        self.session.delete(config)
        self.session.flush()
        self.audit.record(
            "provider_app.deleted",
            resource_type="provider_app_config",
            resource_id=config.id,
            metadata={"provider": provider.value, "route": route.value},
        )
        return True

    async def verify(self, provider: Platform, route: OAuthRoute) -> dict:
        """Check what can be checked locally, and say plainly what cannot."""
        config = self.get(provider, route)
        if config is None:
            return {"configured": False, "problems": ["this provider has no registered app"], "checks": []}
        spec = route_spec(provider, route)
        checks: list[dict] = []
        problems: list[str] = []

        try:
            validate_redirect_uri(config.redirect_uri, route)
            checks.append({"name": "redirect_uri", "ok": True, "detail": config.redirect_uri})
        except SocialError as error:
            problems.append(error.message)
            checks.append({"name": "redirect_uri", "ok": False, "detail": error.message})

        missing_scopes = [scope for scope in spec.scopes if scope not in (config.scopes_json or [])]
        checks.append(
            {
                "name": "scopes",
                "ok": not missing_scopes,
                "detail": (
                    "every scope this login path needs is registered"
                    if not missing_scopes
                    else f"missing from the registration: {', '.join(missing_scopes)}"
                ),
            }
        )
        if missing_scopes:
            problems.append(f"scopes not registered: {', '.join(missing_scopes)}")

        if config.client_secret_ref_id:
            ref = self._secret_ref(config.client_secret_ref_id)
            verdict = await self.secrets.verify(ref) if ref is not None else {"ok": False, "code": "VAULT_NOT_FOUND"}
            checks.append(
                {
                    "name": "client_secret",
                    "ok": bool(verdict.get("ok")),
                    "detail": "the app secret is in the vault"
                    if verdict.get("ok")
                    else f"the app secret is not readable ({verdict.get('code')})",
                }
            )
            if not verdict.get("ok"):
                problems.append("the app secret is not readable from the vault")
        else:
            problems.append("no app secret has been stored")
            checks.append({"name": "client_secret", "ok": False, "detail": "no app secret stored"})

        checks.append(
            {
                "name": "app_mode",
                "ok": True,
                "detail": (
                    "development: only app testers can authorize, and most permissions need review"
                    if config.app_mode != "live"
                    else "live: any user can authorize; permissions still need approval"
                ),
            }
        )
        checks.append(
            {
                "name": "provider_reachability",
                "ok": None,
                "detail": (
                    "not tested: verifying the app against the provider needs a real authorization, "
                    "which only a person can give. Use Connect account to find out."
                ),
            }
        )
        return {
            "configured": not problems,
            "checks": checks,
            "problems": problems,
            "docs_url": spec.docs_url,
            "pkce_required": spec.pkce_required,
        }

    # -- adapters ----------------------------------------------------------
    async def resolve_credentials(self, provider: Platform, route: OAuthRoute) -> tuple[ProviderAppConfig, ProviderCredentials]:
        config = self.get(provider, route)
        if config is None:
            spec = route_spec(provider, route)
            raise SocialError(
                "APP_NOT_CONFIGURED",
                f"the {spec.label} app has not been registered yet",
                http_status=409,
                detail={"provider": provider.value, "route": route.value},
            )
        secret: str | None = None
        if config.client_secret_ref_id:
            ref = self._secret_ref(config.client_secret_ref_id)
            if ref is not None:
                secret = await self.secrets.read(ref, "provider_app_secret")
        api_version = config.api_version
        if not api_version:
            api_version = (
                social_config.DEFAULT_GRAPH_VERSION
                if provider in {Platform.FACEBOOK, Platform.INSTAGRAM} and route == OAuthRoute.FACEBOOK_LOGIN
                else social_config.TIKTOK_API_VERSION
                if provider == Platform.TIKTOK
                else social_config.DEFAULT_IG_VERSION
            )
        return config, ProviderCredentials(
            provider=provider,
            route=route,
            client_id=config.client_id,
            client_secret=secret,
            redirect_uri=config.redirect_uri,
            api_version=api_version,
            scopes=list(config.scopes_json or []),
            app_mode=config.app_mode,
            login_config_id=config.login_config_id,
        )

    def _secret_ref(self, ref_id: str) -> SecretRef | None:
        return self.session.get(SecretRef, ref_id)
