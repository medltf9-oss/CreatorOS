"""Proof that an import request came from the desktop shell, not from a web page.

The renderer must not be able to ask the backend to read an arbitrary path. The file picker
and drag-and-drop both live in the Electron main process, which is the only component that
sees real paths from the operating system. Main signs the path list with a per-launch
secret that is passed to the backend through its environment; the renderer never has it.

Calls through the local API without a configured secret (tests, headless tooling) are
allowed, because in that case no renderer exists at all. Once a secret is configured, a
missing or wrong signature is a hard 403 — fail closed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import HTTPException, Request, status

SECRET_ENV = "CREATOROS_IPC_SECRET"
HEADER = "x-creatoros-ipc"
CLOCK_SKEW_S = 300


def configured_secret() -> bytes | None:
    value = os.environ.get(SECRET_ENV, "").strip()
    return value.encode() if value else None


def sign_body(secret: bytes, body: bytes, *, timestamp: int | None = None) -> str:
    stamp = timestamp or int(time.time())
    digest = hmac.new(secret, f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"{stamp}.{digest}"


def require_ipc_signature(request: Request, body: bytes) -> None:
    secret = configured_secret()
    if secret is None:
        # No shell in front of this process (tests / CLI): nothing to authenticate against.
        return
    header = request.headers.get(HEADER, "")
    if not header:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "IPC_SIGNATURE_REQUIRED: media imports must be started from the desktop app",
        )
    stamp_text, _, signature = header.partition(".")
    try:
        stamp = int(stamp_text)
    except ValueError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "IPC_SIGNATURE_INVALID: malformed timestamp") from exc
    if abs(time.time() - stamp) > CLOCK_SKEW_S:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "IPC_SIGNATURE_EXPIRED: the request is too old")
    expected = sign_body(secret, body, timestamp=stamp).split(".", 1)[1]
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "IPC_SIGNATURE_INVALID: signature does not match")


def canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


__all__ = ["CLOCK_SKEW_S", "HEADER", "SECRET_ENV", "canonical", "configured_secret", "require_ipc_signature", "sign_body"]
