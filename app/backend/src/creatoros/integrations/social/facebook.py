"""Facebook Pages adapter (Graph API, Pages only - ADR-10).

What this adapter deliberately does *not* do: publish, upload, message, delete Page content, or
touch a personal profile. It reads Pages the user administers, validates the token with
`debug_token`, and extends short-lived tokens to long-lived ones.
"""
from __future__ import annotations

from urllib.parse import urlencode

from ...db.enums import AccountType, OAuthRoute, Platform
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
    appsecret_proof,
    expires_in,
    parse_epoch,
)
from .capability_rules import evaluate_platform
from .errors import SocialError

PROFILE_FIELDS = "id,name,username,link,picture{url},category,followers_count,fan_count"
IG_FIELDS = "instagram_business_account{id,username,account_type,profile_picture_url,followers_count},"
PAGE_FIELDS = f"id,name,category,tasks,access_token,picture{{url}},{IG_FIELDS}".rstrip(",")
MAX_PAGES = 5


class FacebookAdapter(SocialAdapter):
    provider = Platform.FACEBOOK

    def __init__(self, transport=None) -> None:
        super().__init__(transport)
        self.route = OAuthRoute.FACEBOOK_LOGIN

    # -- urls --------------------------------------------------------------
    def _version(self, credentials: ProviderCredentials) -> str:
        return credentials.api_version or config.DEFAULT_GRAPH_VERSION

    def _graph(self, credentials: ProviderCredentials) -> str:
        return config.FACEBOOK_GRAPH_URL.format(version=self._version(credentials))

    def authorization_url(
        self,
        credentials: ProviderCredentials,
        *,
        state: str,
        code_challenge: str | None = None,
        scopes: list[str] | None = None,
    ) -> str:
        params = {
            "client_id": credentials.client_id,
            "redirect_uri": credentials.redirect_uri,
            "state": state,
            "response_type": "code",
            "scope": ",".join(scopes or credentials.scopes),
        }
        return f"{config.FACEBOOK_AUTHORIZE_URL.format(version=self._version(credentials))}?{urlencode(params)}"

    # -- tokens ------------------------------------------------------------
    async def exchange_code(
        self,
        credentials: ProviderCredentials,
        *,
        code: str,
        code_verifier: str | None = None,
    ) -> TokenBundle:
        if not credentials.client_secret:
            raise SocialError(
                "APP_NOT_CONFIGURED",
                "the Facebook app secret is not in the vault, so the code cannot be exchanged",
                detail={"provider": "facebook"},
            )
        short = await self._call(
            "GET",
            config.FACEBOOK_TOKEN_URL.format(version=self._version(credentials)),
            operation="exchange_code",
            params={
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "redirect_uri": credentials.redirect_uri,
                "code": code,
            },
        )
        access_token = str(short.get("access_token") or "")
        if not access_token:
            raise SocialError("PROVIDER_ERROR", "Facebook returned no access token", detail={"keys": sorted(short)})
        bundle = TokenBundle(
            access_token=access_token,
            token_type=str(short.get("token_type") or "bearer"),
            expires_at=expires_in(short.get("expires_in")),
            scopes=list(credentials.scopes),
            meta={"flow": "authorization_code", "short_lived_expires_in": short.get("expires_in")},
        )
        # A short-lived user token lives ~2 hours; the long-lived one lives ~60 days and is what
        # produces non-expiring Page tokens.
        try:
            long_lived = await self._call(
                "GET",
                config.FACEBOOK_TOKEN_URL.format(version=self._version(credentials)),
                operation="extend_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "fb_exchange_token": access_token,
                },
            )
            extended = str(long_lived.get("access_token") or "")
            if extended:
                bundle.access_token = extended
                bundle.expires_at = expires_in(long_lived.get("expires_in"))
                bundle.meta["flow"] = "fb_exchange_token"
                bundle.meta["long_lived"] = True
        except SocialError as error:
            # Not fatal: a page token still works. Recorded, never hidden.
            bundle.meta["long_lived"] = False
            bundle.meta["long_lived_error"] = error.code
        return bundle

    async def validate_token(self, credentials: ProviderCredentials, token: str) -> TokenValidation:
        if not credentials.client_secret:
            raise SocialError("APP_NOT_CONFIGURED", "debug_token needs the app secret, which is not in the vault")
        app_token = f"{credentials.client_id}|{credentials.client_secret}"
        body = await self._call(
            "GET",
            config.FACEBOOK_DEBUG_URL.format(version=self._version(credentials)),
            operation="debug_token",
            params={"input_token": token, "access_token": app_token},
        )
        data = body.get("data") or {}
        granted = [str(scope) for scope in (data.get("scopes") or [])]
        is_valid = bool(data.get("is_valid"))
        expires_at = parse_epoch(data.get("expires_at"))
        error_block = data.get("error") or {}
        return TokenValidation(
            valid=is_valid,
            scopes=granted,
            expires_at=expires_at,
            app_id=str(data.get("app_id")) if data.get("app_id") else None,
            user_id=str(data.get("user_id")) if data.get("user_id") else None,
            data_access_expires_at=parse_epoch(data.get("data_access_expires_at")),
            error=str(error_block.get("message")) if isinstance(error_block, dict) and error_block else None,
            raw_flags={
                "type": data.get("type"),
                "application": (data.get("application") or {}).get("name"),
                "issued_at": data.get("issued_at"),
                "scopes_present": bool(granted),
            },
        )

    async def refresh(self, credentials: ProviderCredentials, refresh_token: str) -> TokenBundle:
        """Facebook has no refresh token; the long-lived user token is extended instead."""
        if not credentials.client_secret:
            raise SocialError("APP_NOT_CONFIGURED", "cannot extend a token without the app secret")
        body = await self._call(
            "GET",
            config.FACEBOOK_TOKEN_URL.format(version=self._version(credentials)),
            operation="refresh",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "fb_exchange_token": refresh_token,
            },
        )
        token = str(body.get("access_token") or "")
        if not token:
            raise SocialError("REAUTHORIZATION_REQUIRED", "Facebook would not extend this token; sign in again")
        return TokenBundle(
            access_token=token,
            token_type=str(body.get("token_type") or "bearer"),
            expires_at=expires_in(body.get("expires_in")),
            scopes=list(credentials.scopes),
            refresh_token=None,
            meta={"flow": "fb_exchange_token", "rotation": "none"},
        )

    async def revoke(self, credentials: ProviderCredentials, token: str) -> RevocationResult:
        try:
            body = await self._call(
                "DELETE",
                f"{self._graph(credentials)}/me/permissions",
                operation="revoke",
                params={"access_token": token},
            )
        except SocialError as error:
            if error.code in {"TOKEN_EXPIRED", "REAUTHORIZATION_REQUIRED"}:
                return RevocationResult(revoked=False, detail="the credential was already invalid", already_revoked=True)
            raise
        success = bool(body.get("success"))
        return RevocationResult(
            revoked=success,
            detail="Facebook confirmed the permission removal" if success else "Facebook did not confirm the removal",
        )

    # -- discovery ---------------------------------------------------------
    async def discover_accounts(self, credentials: ProviderCredentials, token: str) -> list[DiscoveredAccount]:
        accounts: list[DiscoveredAccount] = []
        url = f"{self._graph(credentials)}/me/accounts"
        params: dict | None = {
            "fields": PAGE_FIELDS,
            "limit": 100,
            "access_token": token,
        }
        if credentials.client_secret:
            params["appsecret_proof"] = appsecret_proof(token, credentials.client_secret)
        for _page in range(MAX_PAGES):
            body = await self._call("GET", url, operation="discover_accounts", params=params)
            for row in body.get("data") or []:
                page_id = str(row.get("id") or "")
                if not page_id:
                    continue
                tasks = [str(task) for task in (row.get("tasks") or [])]
                picture = ((row.get("picture") or {}).get("data") or {}).get("url")
                accounts.append(
                    DiscoveredAccount(
                        provider_account_id=page_id,
                        account_type=AccountType.FACEBOOK_PAGE,
                        display_name=str(row.get("name") or f"Page {page_id}"),
                        username=row.get("username"),
                        avatar_url=picture,
                        access_token=row.get("access_token"),
                        # Page tokens minted from a long-lived user token do not expire.
                        expires_at=None,
                        scopes=list(await self._granted_scopes(credentials, token)),
                        tasks=tasks,
                        meta={
                            "category": row.get("category"),
                            "can_post": "CREATE_CONTENT" in tasks,
                            "instagram_business_account": (
                                (row.get("instagram_business_account") or {}).get("id")
                            ),
                        },
                    )
                )
            paging = body.get("paging") or {}
            next_url = paging.get("next")
            if not next_url:
                break
            url, params = next_url, None
        return accounts

    async def _granted_scopes(self, credentials: ProviderCredentials, token: str) -> list[str]:
        """Scopes belong to the user token, not the Page token, so ask once and reuse."""
        cached = getattr(self, "_scope_cache", None)
        if cached is not None:
            return cached
        validation = await self.validate_token(credentials, token)
        self._scope_cache = validation.scopes
        return validation.scopes

    async def fetch_profile(
        self, credentials: ProviderCredentials, token: str, provider_account_id: str
    ) -> AccountProfile:
        body = await self._call(
            "GET",
            f"{self._graph(credentials)}/{provider_account_id}",
            operation="fetch_profile",
            params={"fields": PROFILE_FIELDS, "access_token": token},
        )
        picture = ((body.get("picture") or {}).get("data") or {}).get("url")
        followers = body.get("followers_count")
        if followers is None:
            followers = body.get("fan_count")
        return AccountProfile(
            provider_account_id=str(body.get("id") or provider_account_id),
            display_name=str(body.get("name") or f"Page {provider_account_id}"),
            username=body.get("username"),
            avatar_url=picture,
            account_type=AccountType.FACEBOOK_PAGE,
            followers=int(followers) if isinstance(followers, (int, float)) else None,
            meta={"category": body.get("category"), "link": body.get("link")},
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
            source="debug_token",
        )

    async def diagnostics(self, credentials: ProviderCredentials, token: str | None) -> dict:
        report: dict = {
            "provider": "facebook",
            "route": self.route.value,
            "api_version": self._version(credentials),
            "graph_host": "graph.facebook.com",
            "app_client_id_suffix": credentials.client_id[-4:] if credentials.client_id else None,
            "has_app_secret": bool(credentials.client_secret),
            "app_mode": credentials.app_mode,
            "supports_pkce": False,
            "personal_profiles_supported": False,
            "token": None,
            "checks": [],
        }
        if not token:
            report["checks"].append({"name": "token", "ok": False, "detail": "no stored credential"})
            return report
        validation = await self.validate_token(credentials, token)
        report["token"] = {
            "valid": validation.valid,
            "expires_at": validation.expires_at.isoformat() if validation.expires_at else None,
            "scopes": validation.scopes,
            "app_id_matches": validation.app_id == credentials.client_id if validation.app_id else None,
            "type": validation.raw_flags.get("type"),
        }
        report["checks"].append(
            {
                "name": "debug_token",
                "ok": validation.valid,
                "detail": validation.error or "token is valid",
            }
        )
        try:
            pages = await self.discover_accounts(credentials, token)
            report["checks"].append({"name": "pages", "ok": True, "detail": f"{len(pages)} Page(s) reachable"})
        except SocialError as error:
            report["checks"].append({"name": "pages", "ok": False, "detail": f"{error.code}: {error.message}"})
        return report

    def evaluate_capabilities(
        self,
        *,
        account_type: AccountType,
        granted_scopes: list[str],
        token_state: str,
        extra: dict | None = None,
    ) -> list[CapabilityVerdict]:
        from ...db.enums import TokenState

        raw_tasks = (extra or {}).get("page_tasks")
        return evaluate_platform(
            Platform.FACEBOOK,
            self.route,
            account_type=account_type,
            granted_scopes=granted_scopes,
            token_state=TokenState(token_state),
            app_mode=str((extra or {}).get("app_mode") or "development"),
            extra=extra,
            page_tasks=tuple(raw_tasks) if raw_tasks is not None else None,
        )
