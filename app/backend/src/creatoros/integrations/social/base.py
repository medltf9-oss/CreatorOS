"""Adapter contract shared by every social provider.

Each adapter answers the same questions in the same shape, so the OAuth engine, the account
service, the capability engine and Phase 7 never branch on a provider name:

    authorization_url   -> where to send the user
    validate_callback   -> is this redirect a real answer to *our* request?
    exchange_code       -> tokens
    validate_token      -> what does the provider say about this token right now?
    discover_accounts   -> which accounts can this credential act for?
    fetch_profile       -> display facts about one account
    inspect_scopes      -> granted / declined / missing
    refresh             -> rotate tokens (and report the new refresh token)
    revoke              -> give the credential back
    diagnostics         -> a redacted report of what the provider answered
    evaluate_capabilities -> normalized states with evidence

Adapters never touch the database, never write a secret anywhere and never log a token. They
receive an already-resolved `ProviderCredentials` (client id/secret/redirect uri) and a
`TokenBundle`; the caller decides what to persist.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from ...db.enums import AccountType, OAuthRoute, Platform
from .errors import ProviderFailure, SocialError, classify, from_generic_exception

# --------------------------------------------------------------------------- transport


@dataclass(slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: dict
    text: str = ""

    def retry_after(self) -> float | None:
        raw = self.headers.get("retry-after") or self.headers.get("x-business-use-case-usage")
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


class Transport(Protocol):
    provider: str

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        timeout: float = 20.0,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        content: bytes | None = None,
    ) -> HttpResponse: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    """Thin httpx wrapper. The only place that knows about httpx."""

    def __init__(self, provider: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.provider = provider
        self._client = client
        self._owns_client = client is None

    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        timeout: float = 20.0,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        content: bytes | None = None,
    ) -> HttpResponse:
        client = self._ensure()
        try:
            if content is not None:
                # Raw body: TikTok's upload endpoint takes the bytes with a Content-Range header and
                # no form encoding at all.
                response = await client.request(
                    method, url, params=params, content=content, headers=headers, timeout=timeout
                )
            elif files:
                # Chunked video upload needs real multipart/form-data: the provider reads the part
                # named `video_file_chunk` as raw bytes, not as a urlencoded field. httpx sends the
                # `data` entries as ordinary fields alongside the file part.
                response = await client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    files=files,
                    headers=headers,
                    timeout=timeout,
                )
            else:
                response = await client.request(
                    method, url, params=params, data=data, json=json, headers=headers, timeout=timeout
                )
        except httpx.TimeoutException as exc:
            raise from_generic_exception(self.provider, method, exc) from exc
        except httpx.HTTPError as exc:
            raise from_generic_exception(self.provider, method, exc) from exc
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"data": body}
        return HttpResponse(
            status=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=body,
            text=response.text[:2000],
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------- value objects


@dataclass(slots=True)
class ProviderCredentials:
    """Non-secret app configuration resolved from `provider_app_config`."""

    provider: Platform
    route: OAuthRoute
    client_id: str
    client_secret: str | None
    redirect_uri: str
    api_version: str
    scopes: list[str] = field(default_factory=list)
    app_mode: str = "development"
    login_config_id: str | None = None

    def fingerprint(self) -> str:
        """Identifies the app without revealing the client secret."""
        digest = hashlib.sha256(f"{self.provider.value}:{self.route.value}:{self.client_id}".encode()).hexdigest()
        return digest[:32]


@dataclass(slots=True)
class TokenBundle:
    access_token: str
    token_type: str
    expires_at: datetime | None
    scopes: list[str]
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None
    provider_account_id: str | None = None
    # Non-secret provider facts only. Asserted scrubbed by the tests.
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class TokenValidation:
    valid: bool
    scopes: list[str]
    expires_at: datetime | None
    app_id: str | None = None
    user_id: str | None = None
    data_access_expires_at: datetime | None = None
    error: str | None = None
    raw_flags: dict = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveredAccount:
    provider_account_id: str
    account_type: AccountType
    display_name: str
    username: str | None
    avatar_url: str | None
    access_token: str | None
    expires_at: datetime | None
    scopes: list[str]
    parent_page_id: str | None = None
    tasks: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class AccountProfile:
    provider_account_id: str
    display_name: str
    username: str | None
    avatar_url: str | None
    account_type: AccountType
    followers: int | None = None
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class ScopeReport:
    requested: list[str]
    granted: list[str]
    declined: list[str]
    missing: list[str]
    source: str


@dataclass(slots=True)
class RevocationResult:
    revoked: bool
    detail: str
    already_revoked: bool = False


@dataclass(slots=True)
class CapabilityVerdict:
    capability: str
    state: str
    reason: str
    required: list[str]
    granted: list[str]
    missing: list[str]
    docs_reference: str
    evidence: dict = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(UTC)


def expires_in(seconds: float | None) -> datetime | None:
    if not seconds:
        return None
    return utcnow() + timedelta(seconds=int(seconds))


def parse_epoch(value: Any) -> datetime | None:
    """Provider timestamps arrive as epoch seconds, epoch milliseconds or ISO strings."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return parse_epoch(int(text))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def appsecret_proof(access_token: str, client_secret: str) -> str:
    """Meta requires this HMAC on server-side calls made with a user or page token."""
    return hmac.new(client_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- base adapter


class SocialAdapter(ABC):
    provider: Platform
    route: OAuthRoute

    def __init__(self, transport: Transport | None = None) -> None:
        self.transport: Transport = transport or HttpxTransport(self.provider.value)

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        await self.transport.aclose()

    # -- contract ----------------------------------------------------------
    @abstractmethod
    def authorization_url(
        self,
        credentials: ProviderCredentials,
        *,
        state: str,
        code_challenge: str | None = None,
        scopes: list[str] | None = None,
    ) -> str: ...

    @abstractmethod
    async def exchange_code(
        self,
        credentials: ProviderCredentials,
        *,
        code: str,
        code_verifier: str | None = None,
    ) -> TokenBundle: ...

    @abstractmethod
    async def validate_token(self, credentials: ProviderCredentials, token: str) -> TokenValidation: ...

    @abstractmethod
    async def discover_accounts(
        self, credentials: ProviderCredentials, token: str
    ) -> list[DiscoveredAccount]: ...

    @abstractmethod
    async def fetch_profile(
        self, credentials: ProviderCredentials, token: str, provider_account_id: str
    ) -> AccountProfile: ...

    @abstractmethod
    def inspect_scopes(self, credentials: ProviderCredentials, granted: list[str]) -> ScopeReport: ...

    @abstractmethod
    async def refresh(self, credentials: ProviderCredentials, refresh_token: str) -> TokenBundle: ...

    @abstractmethod
    async def revoke(self, credentials: ProviderCredentials, token: str) -> RevocationResult: ...

    @abstractmethod
    async def diagnostics(
        self, credentials: ProviderCredentials, token: str | None
    ) -> dict: ...

    @abstractmethod
    def evaluate_capabilities(
        self,
        *,
        account_type: AccountType,
        granted_scopes: list[str],
        token_state: str,
        extra: dict | None = None,
    ) -> list[CapabilityVerdict]: ...

    # -- helpers -----------------------------------------------------------
    def validate_callback(self, params: dict[str, str]) -> str:
        """Confirm the redirect contains a usable code. Raises `SocialError` otherwise."""
        from .errors import oauth_callback_error

        if params.get("error"):
            raise oauth_callback_error(params)
        code = (params.get("code") or "").strip()
        if not code:
            raise SocialError(
                "PROVIDER_ERROR",
                "the provider redirect did not include an authorization code",
                http_status=400,
                detail={"keys": sorted(params.keys())[:12]},
            )
        if len(code) > 4096:
            raise SocialError("PROVIDER_ERROR", "authorization code is implausibly long", http_status=400)
        return code

    async def _call(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        params: dict | None = None,
        data: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        timeout: float = 20.0,
    ) -> dict:
        response = await self.transport.request(
            method, url, params=params, data=data, json=json, headers=headers, timeout=timeout
        )
        if response.status >= 400:
            raise classify(
                ProviderFailure(
                    provider=self.provider.value,
                    operation=operation,
                    status=response.status,
                    body=response.body,
                    retry_after_s=response.retry_after(),
                )
            )
        body = response.body or {}
        # Meta and TikTok both report failures inside HTTP 200 bodies. TikTok answers success with
        # `{"error": {"code": "ok"}}`, so "an error object exists" is not the test - the code is.
        error_block = body.get("error")
        if isinstance(error_block, dict):
            code = error_block.get("code")
            if code not in (None, 0, "0", "", "ok", "OK", "success"):
                try:
                    status_hint = int(code)
                except (TypeError, ValueError):
                    status_hint = 400
                raise classify(
                    ProviderFailure(
                        provider=self.provider.value,
                        operation=operation,
                        status=status_hint,
                        body=body,
                    )
                )
        if isinstance(error_block, str) and error_block and error_block.lower() not in {"ok", "success"}:
            raise classify(
                ProviderFailure(provider=self.provider.value, operation=operation, status=400, body=body)
            )
        return body

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in seen:
                seen.append(text)
        return seen


class AdapterRegistry:
    """Builds the adapter for a (platform, route) pair. One instance per request scope."""

    def __init__(self, *, transport_factory=None) -> None:
        self._transport_factory = transport_factory

    def get(self, platform: Platform, route: OAuthRoute) -> SocialAdapter:
        from .facebook import FacebookAdapter
        from .instagram import InstagramAdapter
        from .tiktok import TikTokAdapter

        key = (platform, route)
        if key == (Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN):
            factory = FacebookAdapter
        elif key == (Platform.INSTAGRAM, OAuthRoute.INSTAGRAM_LOGIN) or key == (Platform.INSTAGRAM, OAuthRoute.FACEBOOK_LOGIN):
            factory = InstagramAdapter
        elif key == (Platform.TIKTOK, OAuthRoute.TIKTOK_LOGIN):
            factory = TikTokAdapter
        else:  # pragma: no cover - API validation rejects unknown pairs first
            raise SocialError("UNSUPPORTED_ROUTE", f"{platform.value}/{route.value} is not a supported login path")
        transport = self._transport_factory(platform.value) if self._transport_factory else None
        adapter = factory(transport=transport)  # type: ignore[call-arg]
        adapter.route = route
        return adapter


def monotonic() -> float:  # pragma: no cover - tiny indirection for tests
    return time.monotonic()
