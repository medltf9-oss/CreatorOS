"""The Python half of the credential vault (ADR-22).

The backend cannot encrypt or decrypt anything: it has no key. It talks to the vault broker that
the Electron main process exposes on loopback, authenticated by a per-launch token that is passed
to this child process only.

Two implementations exist:

* `BrokerVault` - the real one, used whenever ``CREATOROS_VAULT_URL`` and ``CREATOROS_VAULT_TOKEN``
  are set (the desktop app always sets them).
* `UnavailableVault` - refuses every operation with a clear code. This is what a headless backend
  gets, and it is the honest answer: without the desktop shell there is nowhere safe to put a
  secret, so nothing is stored.

There is deliberately no file-backed fallback and no "encrypted" local scheme. A tests-only
`InMemoryVault` lives in the test suite, not in shipped code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT_S = 10.0
PURPOSES = frozenset(
    {
        "provider_app_secret",
        "access_token",
        "refresh_token",
        "oauth_pkce_verifier",
        "rotation_journal",
        "token_metadata",
        "delivery_credentials",
        "provider_upload_session",
    }
)


class VaultError(RuntimeError):
    """A vault failure with the broker's code, never with the value."""

    def __init__(self, code: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(slots=True)
class VaultRecord:
    id: str
    alg: str
    created_at: str | None
    rotated_at: str | None
    bytes: int
    fingerprint: str
    purpose: str | None
    provider: str | None
    alias: str | None
    version: int = 1


class Vault:
    """Interface. Kept tiny on purpose: put, get, delete, verify, health."""

    available = False
    backend = "unavailable"

    async def put(self, *, secret_id: str, value: str, purpose: str, provider: str | None, alias: str | None) -> VaultRecord:
        raise NotImplementedError

    async def get(self, secret_id: str, purpose: str) -> str:
        raise NotImplementedError

    async def rotate(self, secret_id: str, value: str) -> VaultRecord:
        raise NotImplementedError

    async def delete(self, secret_id: str) -> bool:
        raise NotImplementedError

    async def verify(self, secret_id: str) -> dict:
        raise NotImplementedError

    async def health(self) -> dict:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class UnavailableVault(Vault):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _fail(self) -> VaultError:
        return VaultError(
            "VAULT_UNAVAILABLE",
            f"the credential vault is not available: {self.reason}",
        )

    async def put(self, **_kwargs) -> VaultRecord:  # type: ignore[override]
        raise self._fail()

    async def get(self, secret_id: str, purpose: str) -> str:  # type: ignore[override]
        raise self._fail()

    async def rotate(self, secret_id: str, value: str) -> VaultRecord:  # type: ignore[override]
        raise self._fail()

    async def delete(self, secret_id: str) -> bool:  # type: ignore[override]
        raise self._fail()

    async def verify(self, secret_id: str) -> dict:  # type: ignore[override]
        return {"id": secret_id, "present": False, "ok": False, "code": "VAULT_UNAVAILABLE"}

    async def health(self) -> dict:  # type: ignore[override]
        return {
            "available": False,
            "backend": "unavailable",
            "reason": self.reason,
            "protection": "no credential can be stored while the vault is unavailable",
        }


class BrokerVault(Vault):
    available = True

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self.backend = "broker"

    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _post(self, operation: str, payload: dict) -> dict:
        client = self._ensure()
        headers = {
            "x-creatoros-vault": self._token,
            "x-creatoros-vault-proto": str(PROTOCOL_VERSION),
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(
                f"{self.base_url}/vault/{operation}", json=payload, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise VaultError(
                "VAULT_UNREACHABLE",
                "the credential vault did not answer; the desktop shell may have stopped",
                detail={"operation": operation, "error": str(exc)[:200]},
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or not body.get("ok"):
            raise VaultError(
                str(body.get("code") or "VAULT_ERROR"),
                str(body.get("message") or f"vault operation {operation} failed")[:300],
                detail={"operation": operation, "status": response.status_code},
            )
        return body

    async def put(
        self, *, secret_id: str, value: str, purpose: str, provider: str | None = None, alias: str | None = None
    ) -> VaultRecord:
        if purpose not in PURPOSES:
            raise VaultError("VAULT_INVALID_PURPOSE", f"{purpose!r} is not a vault purpose")
        body = await self._post(
            "put",
            {
                "id": secret_id,
                "secret": value,
                "purpose": purpose,
                "provider": provider,
                "alias": alias,
            },
        )
        return _record(body.get("record") or {})

    async def get(self, secret_id: str, purpose: str) -> str:
        body = await self._post("get", {"id": secret_id, "purpose": purpose})
        value = body.get("value")
        if not isinstance(value, str) or not value:
            raise VaultError("VAULT_DECRYPT_FAILED", "the vault returned no value for this secret")
        return value

    async def rotate(self, secret_id: str, value: str) -> VaultRecord:
        body = await self._post("rotate", {"id": secret_id, "secret": value})
        return _record(body.get("record") or {})

    async def delete(self, secret_id: str) -> bool:
        body = await self._post("delete", {"id": secret_id})
        return bool((body or {}).get("removed"))

    async def verify(self, secret_id: str) -> dict:
        body = await self._post("verify", {"id": secret_id})
        return dict(body.get("result") or {})

    async def health(self) -> dict:
        body = await self._post("health", {})
        return dict(body.get("health") or {})

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _record(payload: dict) -> VaultRecord:
    return VaultRecord(
        id=str(payload.get("id") or ""),
        alg=str(payload.get("alg") or "unknown"),
        created_at=payload.get("created_at"),
        rotated_at=payload.get("rotated_at"),
        bytes=int(payload.get("bytes") or 0),
        fingerprint=str(payload.get("fingerprint") or ""),
        purpose=payload.get("purpose"),
        provider=payload.get("provider"),
        alias=payload.get("alias"),
        version=int(payload.get("version") or 1),
    )


def vault_from_env(environ: dict[str, str] | None = None) -> Vault:
    env = environ if environ is not None else os.environ
    url = (env.get("CREATOROS_VAULT_URL") or "").strip()
    token = (env.get("CREATOROS_VAULT_TOKEN") or "").strip()
    if url and token:
        return BrokerVault(url, token)
    if url and not token:
        return UnavailableVault("a vault URL was provided without a broker token")
    return UnavailableVault(
        "the backend was started outside the desktop shell (no broker URL or token was provided)"
    )
