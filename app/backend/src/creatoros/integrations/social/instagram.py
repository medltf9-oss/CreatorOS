"""Instagram adapter - two login paths, never merged (ADR-11).

Path A `instagram_login`: Instagram Business Login. Its own client, its own token endpoint, its
own graph host (`graph.instagram.com`) and `instagram_business_*` scopes. Works without a
Facebook Page.

Path B `facebook_login`: the Instagram professional account linked to a Facebook Page, reached
with a Page token from Facebook Login and `instagram_basic` / `instagram_content_publish`.

Both paths can produce a row for the same human. That is intentional: the provider account ids
differ, the credentials differ and the capabilities differ, so merging them by username would
destroy evidence. `meta.linked_by_username` records the observation without acting on it.
"""
from __future__ import annotations

from urllib.parse import urlencode

from ...db.enums import AccountType, OAuthRoute, Platform, TokenState
from . import config
from .base import (
    AccountProfile,
    CapabilityVerdict,
    DiscoveredAccount,
    ProviderCredentials,
    RevocationResult,
    ScopeReport,
    SocialAdapter,
    TokenBundle,
    TokenValidation,
    expires_in,
)
from .capability_rules import evaluate_platform
from .errors import SocialError
from .facebook import FacebookAdapter

IG_PROFILE_FIELDS = "user_id,username,name,account_type,profile_picture_url,followers_count,media_count"


class InstagramAdapter(SocialAdapter):
    provider = Platform.INSTAGRAM

    def __init__(self, transport=None) -> None:
        super().__init__(transport)
        self.route = OAuthRoute.INSTAGRAM_LOGIN
        self._facebook: FacebookAdapter | None = None

    # -- helpers -----------------------------------------------------------
    @property
    def via_facebook(self) -> bool:
        return self.route == OAuthRoute.FACEBOOK_LOGIN

    def _fb(self) -> FacebookAdapter:
        if self._facebook is None:
            self._facebook = FacebookAdapter(transport=self.transport)
        self._facebook.route = self.route
        return self._facebook

    def _version(self, credentials: ProviderCredentials) -> str:
        return credentials.api_version or config.DEFAULT_IG_VERSION

    def _graph(self, credentials: ProviderCredentials) -> str:
        return config.INSTAGRAM_GRAPH_URL.format(version=self._version(credentials))

    def authorization_url(
        self,
        credentials: ProviderCredentials,
        *,
        state: str,
        code_challenge: str | None = None,
        scopes: list[str] | None = None,
    ) -> str:
        if self.via_facebook:
            return self._fb().authorization_url(credentials, state=state, scopes=scopes)
        params = {
            "client_id": credentials.client_id,
            "redirect_uri": credentials.redirect_uri,
            "response_type": "code",
            "scope": ",".join(scopes or credentials.scopes),
            "state": state,
        }
        return f"{config.INSTAGRAM_AUTHORIZE_URL}?{urlencode(params)}"

    # -- tokens ------------------------------------------------------------
    async def exchange_code(
        self,
        credentials: ProviderCredentials,
        *,
        code: str,
        code_verifier: str | None = None,
    ) -> TokenBundle:
        if self.via_facebook:
            bundle = await self._fb().exchange_code(credentials, code=code, code_verifier=code_verifier)
            bundle.meta["flow"] = f"facebook_login:{bundle.meta.get('flow')}"
            return bundle
        if not credentials.client_secret:
            raise SocialError("APP_NOT_CONFIGURED", "the Instagram app secret is not in the vault")
        body = await self._call(
            "POST",
            config.INSTAGRAM_TOKEN_URL,
            operation="exchange_code",
            data={
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "grant_type": "authorization_code",
                "redirect_uri": credentials.redirect_uri,
                "code": code,
            },
        )
        token = str(body.get("access_token") or "")
        if not token:
            raise SocialError("PROVIDER_ERROR", "Instagram returned no access token", detail={"keys": sorted(body)})
        permissions = [str(scope) for scope in (body.get("permissions") or [])]
        bundle = TokenBundle(
            access_token=token,
            token_type="bearer",
            expires_at=expires_in(body.get("expires_in") or 3600),
            # Short-lived Instagram tokens last an hour; the long-lived exchange is what we want.
            scopes=permissions or list(credentials.scopes),
            provider_account_id=str(body.get("user_id")) if body.get("user_id") else None,
            meta={
                "flow": "authorization_code",
                "permissions_returned": bool(permissions),
                "scope_source": "token_response" if permissions else "consent_request",
            },
        )
        try:
            long_lived = await self._call(
                "GET",
                config.INSTAGRAM_LONG_LIVED_URL,
                operation="extend_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": credentials.client_secret,
                    "access_token": token,
                },
            )
            extended = str(long_lived.get("access_token") or "")
            if extended:
                bundle.access_token = extended
                bundle.expires_at = expires_in(long_lived.get("expires_in") or 60 * 24 * 3600)
                bundle.meta["flow"] = "ig_exchange_token"
                bundle.meta["long_lived"] = True
        except SocialError as error:
            bundle.meta["long_lived"] = False
            bundle.meta["long_lived_error"] = error.code
        return bundle

    async def validate_token(self, credentials: ProviderCredentials, token: str) -> TokenValidation:
        if self.via_facebook:
            return await self._fb().validate_token(credentials, token)
        # Instagram Login has no debug_token endpoint: the only honest check is a real read.
        body = await self._call(
            "GET",
            f"{self._graph(credentials)}/me",
            operation="validate_token",
            params={"fields": "user_id,username,account_type", "access_token": token},
        )
        return TokenValidation(
            valid=bool(body.get("user_id") or body.get("id")),
            scopes=list(credentials.scopes),
            expires_at=None,
            user_id=str(body.get("user_id") or body.get("id") or "") or None,
            raw_flags={
                "account_type": body.get("account_type"),
                "scope_source": "consent_request",
                "validation": "profile read",
            },
        )

    async def refresh(self, credentials: ProviderCredentials, refresh_token: str) -> TokenBundle:
        if self.via_facebook:
            bundle = await self._fb().refresh(credentials, refresh_token)
            bundle.meta["flow"] = "facebook_login:fb_exchange_token"
            return bundle
        # Instagram Login: long-lived tokens are refreshed with the token itself, no refresh token.
        body = await self._call(
            "GET",
            config.INSTAGRAM_REFRESH_URL,
            operation="refresh",
            params={"grant_type": "ig_refresh_token", "access_token": refresh_token},
        )
        token = str(body.get("access_token") or "")
        if not token:
            raise SocialError("REAUTHORIZATION_REQUIRED", "Instagram would not refresh this token; sign in again")
        return TokenBundle(
            access_token=token,
            token_type="bearer",
            expires_at=expires_in(body.get("expires_in") or 60 * 24 * 3600),
            scopes=list(credentials.scopes),
            refresh_token=None,
            meta={"flow": "ig_refresh_token", "rotation": "none"},
        )

    async def revoke(self, credentials: ProviderCredentials, token: str) -> RevocationResult:
        if self.via_facebook:
            return await self._fb().revoke(credentials, token)
        # There is no documented revoke endpoint for Instagram Business Login. Saying so is better
        # than pretending; the local credential is deleted and the user can remove the app access
        # in Instagram settings.
        return RevocationResult(
            revoked=False,
            detail=(
                "Instagram Business Login documents no token revocation endpoint. The credential was "
                "removed from this machine; remove CreatorOS from Instagram > Apps and websites to "
                "invalidate it at the provider."
            ),
        )

    # -- discovery ---------------------------------------------------------
    async def discover_accounts(self, credentials: ProviderCredentials, token: str) -> list[DiscoveredAccount]:
        if self.via_facebook:
            pages = await self._fb().discover_accounts(credentials, token)
            found: list[DiscoveredAccount] = []
            for page in pages:
                ig_id = (page.meta or {}).get("instagram_business_account")
                if not ig_id:
                    continue
                found.append(
                    DiscoveredAccount(
                        provider_account_id=str(ig_id),
                        account_type=AccountType.INSTAGRAM_BUSINESS,
                        display_name=page.display_name,
                        username=None,
                        avatar_url=page.avatar_url,
                        access_token=page.access_token,
                        expires_at=page.expires_at,
                        scopes=page.scopes,
                        parent_page_id=page.provider_account_id,
                        tasks=page.tasks,
                        meta={
                            "discovered_via": "facebook_login",
                            "linked_page_id": page.provider_account_id,
                            "linked_page_name": page.display_name,
                            "needs_profile_refresh": True,
                        },
                    )
                )
            return found

        body = await self._call(
            "GET",
            f"{self._graph(credentials)}/me",
            operation="discover_accounts",
            params={"fields": IG_PROFILE_FIELDS, "access_token": token},
        )
        account_type = _account_type(body.get("account_type"))
        picture = body.get("profile_picture_url")
        return [
            DiscoveredAccount(
                provider_account_id=str(body.get("user_id") or body.get("id") or ""),
                account_type=account_type,
                display_name=str(body.get("name") or body.get("username") or "Instagram account"),
                username=body.get("username"),
                avatar_url=picture,
                access_token=token,
                expires_at=None,
                scopes=list(credentials.scopes),
                meta={
                    "discovered_via": "instagram_login",
                    "followers_count": body.get("followers_count"),
                    "media_count": body.get("media_count"),
                    "raw_account_type": body.get("account_type"),
                },
            )
        ]

    async def fetch_profile(
        self, credentials: ProviderCredentials, token: str, provider_account_id: str
    ) -> AccountProfile:
        if self.via_facebook:
            body = await self._call(
                "GET",
                f"https://graph.facebook.com/{self._version(credentials)}/{provider_account_id}",
                operation="fetch_profile",
                params={
                    "fields": "username,account_type,profile_picture_url,followers_count,media_count,name",
                    "access_token": token,
                },
            )
        else:
            body = await self._call(
                "GET",
                f"{self._graph(credentials)}/{provider_account_id}",
                operation="fetch_profile",
                params={"fields": IG_PROFILE_FIELDS, "access_token": token},
            )
        return AccountProfile(
            provider_account_id=str(body.get("id") or body.get("user_id") or provider_account_id),
            display_name=str(body.get("name") or body.get("username") or f"Instagram {provider_account_id}"),
            username=body.get("username"),
            avatar_url=body.get("profile_picture_url"),
            account_type=_account_type(body.get("account_type")),
            followers=int(body["followers_count"]) if isinstance(body.get("followers_count"), (int, float)) else None,
            meta={
                "media_count": body.get("media_count"),
                "raw_account_type": body.get("account_type"),
                "route": self.route.value,
            },
        )

    def inspect_scopes(self, credentials: ProviderCredentials, granted: list[str]) -> ScopeReport:
        requested = self._dedupe(list(credentials.scopes))
        granted_clean = self._dedupe(granted)
        missing = [scope for scope in requested if scope not in granted_clean]
        return ScopeReport(
            requested=requested,
            granted=granted_clean,
            declined=missing,
            missing=missing,
            source="debug_token" if self.via_facebook else "consent_request",
        )

    async def diagnostics(self, credentials: ProviderCredentials, token: str | None) -> dict:
        report: dict = {
            "provider": "instagram",
            "route": self.route.value,
            "api_version": self._version(credentials),
            "graph_host": "graph.facebook.com" if self.via_facebook else "graph.instagram.com",
            "app_client_id_suffix": credentials.client_id[-4:] if credentials.client_id else None,
            "has_app_secret": bool(credentials.client_secret),
            "app_mode": credentials.app_mode,
            "supports_pkce": False,
            "token": None,
            "checks": [],
        }
        if not token:
            report["checks"].append({"name": "token", "ok": False, "detail": "no stored credential"})
            return report
        validation = await self.validate_token(credentials, token)
        report["token"] = {
            "valid": validation.valid,
            "user_id": validation.user_id,
            "scopes": validation.scopes,
            "scope_source": validation.raw_flags.get("scope_source"),
        }
        report["checks"].append(
            {"name": "profile_read", "ok": validation.valid, "detail": validation.error or "profile is readable"}
        )
        try:
            accounts = await self.discover_accounts(credentials, token)
            report["checks"].append(
                {"name": "discovery", "ok": True, "detail": f"{len(accounts)} professional account(s) found"}
            )
        except SocialError as error:
            report["checks"].append({"name": "discovery", "ok": False, "detail": f"{error.code}: {error.message}"})
        return report

    def evaluate_capabilities(
        self,
        *,
        account_type: AccountType,
        granted_scopes: list[str],
        token_state: str,
        extra: dict | None = None,
    ) -> list[CapabilityVerdict]:
        return evaluate_platform(
            Platform.INSTAGRAM,
            self.route,
            account_type=account_type,
            granted_scopes=granted_scopes,
            token_state=TokenState(token_state),
            app_mode=str((extra or {}).get("app_mode") or "development"),
            extra=extra,
            page_tasks=tuple((extra or {}).get("page_tasks")) if (extra or {}).get("page_tasks") is not None else None,
        )


def _account_type(raw: str | None) -> AccountType:
    text = str(raw or "").upper()
    if text in {"MEDIA_CREATOR", "CREATOR"}:
        return AccountType.INSTAGRAM_CREATOR
    if text == "BUSINESS":
        return AccountType.INSTAGRAM_BUSINESS
    # Instagram Login can return a personal account; it is reported, not silently upgraded.
    return AccountType.PERSONAL_UNSUPPORTED
