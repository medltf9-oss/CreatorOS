"""Test doubles for the social/vault layer.

Two rules keep these honest:

1. the vault double stores values in memory only - it never writes a file, so a leak scan of the
   data root is meaningful;
2. the provider double is an `httpx.MockTransport` that answers *real* HTTP shapes recorded from
   the published provider documentation. Nothing here claims a live account exists.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from creatoros.db.enums import OAuthRoute, Platform
from creatoros.integrations.social.base import HttpxTransport
from creatoros.services.social.vault import Vault, VaultError, VaultRecord


class InMemoryVault(Vault):
    """Tests only. No disk, no crypto, no pretence of either."""

    available = True
    backend = "memory-test"

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail_get: set[str] = set()
        self.fail_put = False

    async def put(self, *, secret_id, value, purpose, provider=None, alias=None) -> VaultRecord:
        if self.fail_put:
            raise VaultError("VAULT_UNAVAILABLE", "the OS refused to encrypt")
        if secret_id in self.records:
            raise VaultError("VAULT_EXISTS", "record exists")
        import hashlib

        digest = hashlib.sha256(value.encode()).hexdigest()
        self.records[secret_id] = {
            "value": value,
            "purpose": purpose,
            "provider": provider,
            "alias": alias,
            "fingerprint": digest[:32],
            "alg": "memory-test",
            "bytes": len(value),
            "rotated_at": None,
            "schema_version": 1,
        }
        self.calls.append(("put", secret_id, purpose))
        return self._record(secret_id)

    async def get(self, secret_id: str, purpose: str) -> str:
        self.calls.append(("get", secret_id, purpose))
        if secret_id in self.fail_get:
            raise VaultError("VAULT_DECRYPT_FAILED", "the vault could not decrypt this record")
        record = self.records.get(secret_id)
        if record is None:
            raise VaultError("VAULT_NOT_FOUND", "no such secret")
        if record["purpose"] != purpose:
            raise VaultError("VAULT_PURPOSE_MISMATCH", f"record is {record['purpose']}")
        return record["value"]

    async def rotate(self, secret_id: str, value: str) -> VaultRecord:
        record = self.records.get(secret_id)
        if record is None:
            raise VaultError("VAULT_NOT_FOUND", "no such secret")
        import hashlib

        record["value"] = value
        record["bytes"] = len(value)
        record["fingerprint"] = hashlib.sha256(value.encode()).hexdigest()[:32]
        record["rotated_at"] = "2026-11-23T00:00:00+00:00"
        self.calls.append(("rotate", secret_id, record["purpose"]))
        return self._record(secret_id)

    async def delete(self, secret_id: str) -> bool:
        self.calls.append(("delete", secret_id, None))
        return self.records.pop(secret_id, None) is not None

    async def verify(self, secret_id: str) -> dict:
        record = self.records.get(secret_id)
        if record is None:
            return {"id": secret_id, "present": False, "ok": False, "code": "VAULT_NOT_FOUND"}
        return {
            "id": secret_id,
            "present": True,
            "ok": True,
            "purpose": record["purpose"],
            "bytes": record["bytes"],
            "alg": record["alg"],
            "fingerprint": record["fingerprint"],
            "version": record["schema_version"],
        }

    async def health(self) -> dict:
        return {
            "available": True,
            "backend": "memory-test",
            "schema_version": 1,
            "alg": "memory-test",
            "entries": len(self.records),
            "corrupt": 0,
            "protection": "tests only: values live in process memory",
        }

    def _record(self, secret_id: str) -> VaultRecord:
        record = self.records[secret_id]
        return VaultRecord(
            id=secret_id,
            alg=record["alg"],
            created_at="2026-11-23T00:00:00+00:00",
            rotated_at=record["rotated_at"],
            bytes=record["bytes"],
            fingerprint=record["fingerprint"],
            purpose=record["purpose"],
            provider=record["provider"],
            alias=record["alias"],
            version=1,
        )


@dataclass
class RecordedCall:
    method: str
    url: str
    params: dict | None = None
    data: dict | None = None
    headers: dict | None = None
    json: dict | None = None


@dataclass
class ProviderMock:
    """An httpx transport that answers like a provider, and records what was asked."""

    routes: list[tuple[str, Callable[[httpx.Request], httpx.Response]]] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)
    default_status: int = 404

    def on(self, pattern: str, handler) -> ProviderMock:
        self.routes.append((pattern, handler))
        return self

    def json(self, pattern: str, status: int, body: dict | Callable[[httpx.Request], dict]) -> ProviderMock:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = body(request) if callable(body) else body
            return httpx.Response(status, json=payload)

        return self.on(pattern, handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            RecordedCall(
                method=request.method,
                url=str(request.url).split("?")[0],
                params=dict(request.url.params) or None,
                data=_form(request) if "form" in request.headers.get("content-type", "") else None,
                headers={key.lower(): value for key, value in request.headers.items()},
                json=_json_body(request),
            )
        )
        for pattern, handler in self.routes:
            if re.search(pattern, str(request.url)):
                return handler(request)
        return httpx.Response(self.default_status, json={"error": {"message": "unmocked"}})

    def transport(self, provider: str) -> HttpxTransport:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return HttpxTransport(provider, client=client)

    def urls(self, method: str | None = None) -> list[str]:
        return [call.url for call in self.calls if method is None or call.method == method.upper()]

    def call(self, pattern: str, method: str | None = None) -> RecordedCall:
        for call in self.calls:
            if re.search(pattern, call.url) and (method is None or call.method == method.upper()):
                return call
        raise AssertionError(f"no call matching {pattern!r}; saw {self.urls()}")


def _form(request: httpx.Request) -> dict:
    from urllib.parse import parse_qs

    return {key: values[-1] for key, values in parse_qs(request.content.decode()).items()}


def _json_body(request: httpx.Request) -> dict | None:
    if not request.content:
        return None
    try:
        return json.loads(request.content)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ helpers

GRAPH = "https://graph.facebook.com/v23.0"
IG_GRAPH = "https://graph.instagram.com/v23.0"
TIKTOK_API = "https://open.tiktokapis.com/v2"


def facebook_account_graph(
    *,
    page_id: str = "1234567890",
    page_token: str = "EAAB-page-token",
    ig_id: str | None = None,
    tasks: list[str] | None = None,
    scopes: list[str] | None = None,
) -> ProviderMock:
    tasks = tasks if tasks is not None else ["CREATE_CONTENT", "MANAGE"]
    scopes = scopes if scopes is not None else ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]
    mock = ProviderMock()
    mock.json(r"/oauth/access_token", 200, {"access_token": "EAAB-user-token", "token_type": "bearer", "expires_in": 5184000})
    mock.json(r"/debug_token", 200, {
        "data": {
            "app_id": "app-123",
            "is_valid": True,
            "scopes": scopes,
            "expires_at": 1900000000,
            "user_id": "user-1",
            "type": "USER",
        }
    })
    page_entry = {
        "id": page_id,
        "name": "Test Page",
        "category": "Creator",
        "tasks": tasks,
        "access_token": page_token,
        "picture": {"data": {"url": "https://cdn.example/avatar.png"}},
    }
    if ig_id:
        page_entry["instagram_business_account"] = {
            "id": ig_id,
            "username": "test.page.ig",
            "account_type": "BUSINESS",
        }
    mock.json(r"/me/accounts", 200, {"data": [page_entry]})
    mock.json(r"/me/permissions", 200, {"success": True})
    mock.json(rf"/{page_id}", 200, {
        "id": page_id,
        "name": "Test Page",
        "username": "testpage",
        "followers_count": 1234,
        "fan_count": 1200,
        "category": "Creator",
        "picture": {"data": {"url": "https://cdn.example/avatar.png"}},
    })
    mock.json(rf"/{ig_id or 'no-ig'}", 200, {
        "id": ig_id or "0",
        "username": "test.page.ig",
        "account_type": "BUSINESS",
        "profile_picture_url": "https://cdn.example/ig.png",
        "followers_count": 555,
    })
    mock.json(r"/me$", 200, {"id": "user-1", "name": "Person"})
    return mock


def instagram_login_graph(
    *,
    user_id: str = "17841400000000000",
    account_type: str = "BUSINESS",
    permissions: list[str] | None = None,
) -> ProviderMock:
    permissions = permissions if permissions is not None else [
        "instagram_business_basic",
        "instagram_business_content_publish",
    ]
    mock = ProviderMock()
    mock.json(r"api\.instagram\.com/oauth/access_token", 200, {
        "access_token": "IG-short-token",
        "user_id": user_id,
        "permissions": permissions,
    })
    mock.json(r"graph\.instagram\.com/access_token", 200, {
        "access_token": "IG-long-token",
        "token_type": "bearer",
        "expires_in": 5184000,
    })
    mock.json(r"graph\.instagram\.com/refresh_access_token", 200, {
        "access_token": "IG-refreshed-token",
        "token_type": "bearer",
        "expires_in": 5184000,
    })
    profile = {
        "user_id": user_id,
        "username": "test.ig",
        "name": "Test IG",
        "account_type": account_type,
        "profile_picture_url": "https://cdn.example/ig.png",
        "followers_count": 777,
        "media_count": 42,
    }
    mock.json(rf"{IG_GRAPH}/me", 200, profile)
    mock.json(rf"{IG_GRAPH}/{user_id}", 200, profile)
    return mock


def tiktok_graph(*, open_id: str = "tt-open-id-1", scope: str = "user.info.basic,video.list") -> ProviderMock:
    mock = ProviderMock()
    mock.json(r"open\.tiktokapis\.com/v2/oauth/token/", 200, {
        "access_token": "tt-access-1",
        "expires_in": 86400,
        "open_id": open_id,
        "refresh_expires_in": 31536000,
        "refresh_token": "tt-refresh-1",
        "scope": scope,
        "token_type": "Bearer",
    })
    mock.json(r"open\.tiktokapis\.com/v2/user/info/", 200, {
        "data": {
            "user": {
                "open_id": open_id,
                "union_id": "union-1",
                "display_name": "Test TikTok",
                "username": "testtiktok",
                "avatar_url": "https://cdn.example/tt.png",
                "follower_count": 999,
                "video_count": 12,
                "is_verified": False,
            }
        },
        "error": {"code": "ok", "message": ""},
    })
    mock.json(r"open\.tiktokapis\.com/v2/oauth/revoke/", 200, {"status": "ok"})
    mock.json(r"open\.tiktokapis\.com/v2/post/publish/creator_info/query/", 200, {
        "data": {
            "creator_username": "testtiktok",
            "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
            "max_video_post_duration_sec": 600,
        },
        "error": {"code": "ok", "message": ""},
    })
    return mock


def run(coro):
    """Run one coroutine from a synchronous test."""
    return asyncio.run(coro)


async def make_app_config(
    session,
    vault: Vault,
    *,
    platform: Platform = Platform.FACEBOOK,
    route: OAuthRoute = OAuthRoute.FACEBOOK_LOGIN,
    client_id: str = "app-123",
    client_secret: str | None = "app-secret-xyz",
    redirect_uri: str | None = None,
    app_mode: str = "development",
    scopes: list[str] | None = None,
    api_version: str = "v23.0",
):
    from creatoros.integrations.social.config import route_spec
    from creatoros.integrations.social.oauth import redirect_uri as build_redirect
    from creatoros.services.social.apps import AppRegistration, ProviderAppService

    spec = route_spec(platform, route)
    service = ProviderAppService(session, vault)
    return await service.upsert(
        AppRegistration(
            provider=platform,
            route=route,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri or build_redirect(47821, route),
            api_version=api_version,
            app_mode=app_mode,
            scopes=scopes or list(spec.scopes),
            login_config_id=None,
            notes="test",
        )
    )


def assert_no_secret_in(payload, secrets: list[str]) -> None:
    """Fail loudly if any secret value appears in a JSON-able payload."""
    text = json.dumps(payload, default=str)
    for secret in secrets:
        assert secret not in text, f"secret material leaked into a response: {secret[:6]}…"
