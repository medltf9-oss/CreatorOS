"""TikTok adapter - Login Kit with PKCE and rotating refresh tokens (ADR-12).

Facts this adapter is built around:

* TikTok requires PKCE (S256) for desktop and web apps. A request without a code challenge is
  invalid, so the engine always supplies one.
* The refresh token **rotates**: every refresh returns a new refresh token and invalidates the
  old one. Losing the new token means the user must sign in again, which is why the refresh path
  takes an account-scoped lock and writes a journal before it calls TikTok.
* `video.publish` (Direct Post) is a separate scope that requires the Direct Post add-on. Login
  succeeding says nothing about it, so the capability is evaluated from the granted scopes and
  the creator-info probe, never from "the token works".
* This adapter issues no publish, upload or post request. The creator-info query is a read-only
  metadata call, and it is the only POST here.
"""
from __future__ import annotations

import hashlib
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
    parse_epoch,
)
from .capability_rules import evaluate_platform
from .errors import SocialError

#: Fields granted by `user.info.basic`: identity only.
BASIC_USER_FIELDS = "open_id,union_id,avatar_url,display_name,username,profile_deep_link,is_verified"
#: Fields granted by `user.info.stats`. Asking for these without the grant fails the whole
#: `user/info` request, so they are appended only when the scope is on the token.
STATS_USER_FIELDS = "follower_count,following_count,likes_count,video_count"
#: The full set. Kept for callers that know the grant is present.
USER_FIELDS = f"{BASIC_USER_FIELDS},{STATS_USER_FIELDS}"


def user_fields(*, include_stats: bool) -> str:
    """`user/info` fields for the scopes actually granted.

    TikTok splits identity (`user.info.basic`) from counters (`user.info.stats`). Requesting a stats
    field without the grant is an error for the whole request, which would also hide the display
    name — so the stats fields are opt-in.
    """
    return USER_FIELDS if include_stats else BASIC_USER_FIELDS


def _has_stats_scope(credentials: ProviderCredentials) -> bool:
    """`user.info.stats` is an extra scope: it is granted only when the token carries it."""
    scopes = {str(scope) for scope in (getattr(credentials, "scopes", None) or [])}
    return "user.info.stats" in scopes


def pkce_challenge(verifier: str) -> str:
    """S256 challenge: base64url(sha256(verifier)) without padding (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64url(digest)


def base64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class TikTokAdapter(SocialAdapter):
    provider = Platform.TIKTOK

    def __init__(self, transport=None) -> None:
        super().__init__(transport)
        self.route = OAuthRoute.TIKTOK_LOGIN

    def _version(self, credentials: ProviderCredentials) -> str:
        return credentials.api_version or config.TIKTOK_API_VERSION

    def _api(self, credentials: ProviderCredentials) -> str:
        return config.TIKTOK_API_URL.format(version=self._version(credentials))

    def authorization_url(
        self,
        credentials: ProviderCredentials,
        *,
        state: str,
        code_challenge: str | None = None,
        scopes: list[str] | None = None,
    ) -> str:
        if not code_challenge:
            raise SocialError(
                "PKCE_REQUIRED",
                "TikTok requires a PKCE code challenge; refusing to build an authorization URL without one",
            )
        params = {
            "client_key": credentials.client_id,
            "response_type": "code",
            "scope": ",".join(scopes or credentials.scopes),
            "redirect_uri": credentials.redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{config.TIKTOK_AUTHORIZE_URL.format(version=self._version(credentials))}?{urlencode(params)}"

    async def exchange_code(
        self,
        credentials: ProviderCredentials,
        *,
        code: str,
        code_verifier: str | None = None,
    ) -> TokenBundle:
        if not credentials.client_secret:
            raise SocialError("APP_NOT_CONFIGURED", "the TikTok client secret is not in the vault")
        if not code_verifier:
            raise SocialError("PKCE_REQUIRED", "the PKCE verifier is missing; the code cannot be exchanged")
        body = await self._call(
            "POST",
            config.TIKTOK_TOKEN_URL.format(version=self._version(credentials)),
            operation="exchange_code",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
            data={
                "client_key": credentials.client_id,
                "client_secret": credentials.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": credentials.redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        token = str(body.get("access_token") or "")
        if not token:
            raise SocialError("PROVIDER_ERROR", "TikTok returned no access token", detail={"keys": sorted(body)})
        scopes = [scope.strip() for scope in str(body.get("scope") or "").split(",") if scope.strip()]
        return TokenBundle(
            access_token=token,
            token_type=str(body.get("token_type") or "Bearer"),
            expires_at=expires_in(body.get("expires_in")),
            scopes=scopes or list(credentials.scopes),
            refresh_token=body.get("refresh_token"),
            refresh_expires_at=expires_in(body.get("refresh_expires_in")),
            provider_account_id=str(body.get("open_id")) if body.get("open_id") else None,
            meta={
                "flow": "authorization_code",
                "open_id": body.get("open_id"),
                "scope_source": "token_response" if scopes else "consent_request",
                "rotation": "refresh_token_rotates",
            },
        )

    async def validate_token(self, credentials: ProviderCredentials, token: str) -> TokenValidation:
        body = await self._call(
            "GET",
            f"{self._api(credentials)}/user/info/",
            operation="validate_token",
            params={"fields": "open_id,display_name"},
            headers={"Authorization": f"Bearer {token}"},
        )
        user = (body.get("data") or {}).get("user") or {}
        return TokenValidation(
            valid=bool(user.get("open_id")),
            scopes=list(credentials.scopes),
            expires_at=None,
            user_id=str(user.get("open_id")) if user.get("open_id") else None,
            raw_flags={
                "display_name": user.get("display_name"),
                "validation": "user info read",
                "scope_source": "consent_request",
            },
        )

    async def refresh(self, credentials: ProviderCredentials, refresh_token: str) -> TokenBundle:
        if not credentials.client_secret:
            raise SocialError("APP_NOT_CONFIGURED", "the TikTok client secret is not in the vault")
        body = await self._call(
            "POST",
            config.TIKTOK_TOKEN_URL.format(version=self._version(credentials)),
            operation="refresh",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
            data={
                "client_key": credentials.client_id,
                "client_secret": credentials.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        token = str(body.get("access_token") or "")
        new_refresh = body.get("refresh_token")
        if not token:
            raise SocialError("REAUTHORIZATION_REQUIRED", "TikTok did not return an access token on refresh")
        scopes = [scope.strip() for scope in str(body.get("scope") or "").split(",") if scope.strip()]
        return TokenBundle(
            access_token=token,
            token_type=str(body.get("token_type") or "Bearer"),
            expires_at=expires_in(body.get("expires_in")),
            scopes=scopes or list(credentials.scopes),
            refresh_token=str(new_refresh) if new_refresh else None,
            refresh_expires_at=expires_in(body.get("refresh_expires_in")),
            provider_account_id=str(body.get("open_id")) if body.get("open_id") else None,
            meta={
                "flow": "refresh_token",
                "rotation": "rotated" if new_refresh else "not_rotated",
                "open_id": body.get("open_id"),
            },
        )

    async def revoke(self, credentials: ProviderCredentials, token: str) -> RevocationResult:
        if not credentials.client_secret:
            raise SocialError("APP_NOT_CONFIGURED", "the TikTok client secret is not in the vault")
        body = await self._call(
            "POST",
            config.TIKTOK_REVOKE_URL.format(version=self._version(credentials)),
            operation="revoke",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"client_key": credentials.client_id, "client_secret": credentials.client_secret, "token": token},
        )
        return RevocationResult(
            revoked=True,
            detail=f"TikTok accepted the revocation (status {body.get('status', 'ok')})",
        )

    async def discover_accounts(self, credentials: ProviderCredentials, token: str) -> list[DiscoveredAccount]:
        body = await self._call(
            "GET",
            f"{self._api(credentials)}/user/info/",
            operation="discover_accounts",
            params={"fields": user_fields(include_stats=_has_stats_scope(credentials))},
            headers={"Authorization": f"Bearer {token}"},
        )
        user = (body.get("data") or {}).get("user") or {}
        open_id = str(user.get("open_id") or "")
        if not open_id:
            raise SocialError("PROVIDER_ERROR", "TikTok returned no open_id for this credential")
        return [
            DiscoveredAccount(
                provider_account_id=open_id,
                account_type=AccountType.TIKTOK_CREATOR,
                display_name=str(user.get("display_name") or "TikTok account"),
                username=user.get("username"),
                avatar_url=user.get("avatar_url"),
                access_token=token,
                expires_at=None,
                scopes=list(credentials.scopes),
                meta={
                    "union_id": user.get("union_id"),
                    "is_verified": user.get("is_verified"),
                    "follower_count": user.get("follower_count"),
                    "video_count": user.get("video_count"),
                    "discovered_via": "tiktok_login",
                },
            )
        ]

    async def fetch_profile(
        self, credentials: ProviderCredentials, token: str, provider_account_id: str
    ) -> AccountProfile:
        body = await self._call(
            "GET",
            f"{self._api(credentials)}/user/info/",
            operation="fetch_profile",
            params={"fields": user_fields(include_stats=_has_stats_scope(credentials))},
            headers={"Authorization": f"Bearer {token}"},
        )
        user = (body.get("data") or {}).get("user") or {}
        followers = user.get("follower_count")
        return AccountProfile(
            provider_account_id=str(user.get("open_id") or provider_account_id),
            display_name=str(user.get("display_name") or "TikTok account"),
            username=user.get("username"),
            avatar_url=user.get("avatar_url"),
            account_type=AccountType.TIKTOK_CREATOR,
            followers=int(followers) if isinstance(followers, (int, float)) else None,
            meta={
                "is_verified": user.get("is_verified"),
                "video_count": user.get("video_count"),
                "profile_deep_link": user.get("profile_deep_link"),
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
            source="token_response",
        )

    async def creator_info(self, credentials: ProviderCredentials, token: str) -> dict:
        """Read-only Direct Post eligibility probe. Publishes nothing."""
        body = await self._call(
            "POST",
            config.TIKTOK_CREATOR_INFO_URL.format(version=self._version(credentials)),
            operation="creator_info",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
            json={},
        )
        data = body.get("data") or {}
        return {
            "creator_username": data.get("creator_username"),
            "privacy_level_options": data.get("privacy_level_options") or [],
            "comment_disabled": data.get("comment_disabled"),
            "duet_disabled": data.get("duet_disabled"),
            "stitch_disabled": data.get("stitch_disabled"),
            "max_video_post_duration_sec": data.get("max_video_post_duration_sec"),
        }

    async def diagnostics(self, credentials: ProviderCredentials, token: str | None) -> dict:
        report: dict = {
            "provider": "tiktok",
            "route": self.route.value,
            "api_version": self._version(credentials),
            "api_host": "open.tiktokapis.com",
            "client_key_suffix": credentials.client_id[-4:] if credentials.client_id else None,
            "has_client_secret": bool(credentials.client_secret),
            "app_mode": credentials.app_mode,
            "supports_pkce": True,
            "requires_pkce": True,
            "refresh_token_rotates": True,
            "token": None,
            "checks": [],
        }
        if not token:
            report["checks"].append({"name": "token", "ok": False, "detail": "no stored credential"})
            return report
        validation = await self.validate_token(credentials, token)
        report["token"] = {"valid": validation.valid, "open_id": validation.user_id}
        report["checks"].append(
            {"name": "user_info", "ok": validation.valid, "detail": validation.error or "user info is readable"}
        )
        if "video.publish" in (credentials.scopes or []):
            try:
                info = await self.creator_info(credentials, token)
                report["creator_info"] = info
                report["checks"].append(
                    {
                        "name": "creator_info",
                        "ok": True,
                        "detail": "Direct Post metadata is readable",
                    }
                )
            except SocialError as error:
                report["checks"].append(
                    {"name": "creator_info", "ok": False, "detail": f"{error.code}: {error.message}"}
                )
        else:
            report["checks"].append(
                {
                    "name": "creator_info",
                    "ok": False,
                    "detail": "not requested: video.publish was not part of this connection",
                }
            )
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
            Platform.TIKTOK,
            self.route,
            account_type=account_type,
            granted_scopes=granted_scopes,
            token_state=TokenState(token_state),
            app_mode=str((extra or {}).get("app_mode") or "development"),
            extra=extra,
            page_tasks=tuple((extra or {}).get("page_tasks")) if (extra or {}).get("page_tasks") is not None else None,
        )


def token_expiry_from(body: dict) -> object:
    """Exposed for tests: TikTok mixes epoch and relative expiries."""
    return parse_epoch(body.get("expires_at"))
