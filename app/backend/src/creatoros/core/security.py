"""Local API security: session token, origin allowlist, headers, body limits.

INV-8: the service binds to loopback only, requires a per-launch token on every
route except liveness, and refuses requests whose Origin is not a known
CreatorOS renderer origin.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

ALLOWED_ORIGINS = frozenset(
    {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "app://creatoros",
        "null",  # packaged build loads from file://; Chromium reports "null"
    }
)

MAX_BODY_BYTES = 16 * 1024 * 1024  # metadata/JSON only; media uses the streaming importer

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-site",
    "Cache-Control": "no-store",
}


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def mask_token(token: str | None) -> str:
    if not token:
        return "<none>"
    return f"{token[:4]}…{token[-2:]} ({len(token)} chars)"


def _bearer_from(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def require_token(request: Request) -> None:
    """FastAPI dependency: constant-time token comparison."""
    expected: str = getattr(request.app.state, "session_token", "") or ""
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SESSION_TOKEN_UNSET")
    supplied = _bearer_from(request)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "AUTH_REQUIRED")


def check_origin(request: Request) -> None:
    """Reject cross-site requests from anything but our own renderer."""
    origin = request.headers.get("origin")
    if origin is None:
        # Same-origin fetches from the renderer may omit Origin; the token still gates access.
        return
    if origin not in ALLOWED_ORIGINS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "ORIGIN_NOT_ALLOWED")


async def security_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
):
    started = time.perf_counter()
    try:
        check_origin(request)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "PERMISSION_DENIED", "message": exc.detail, "correlation_id": None},
        )

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"code": "PAYLOAD_TOO_LARGE", "message": "request body exceeds 16 MB", "correlation_id": None},
        )

    correlation_id = request.headers.get("x-correlation-id") or secrets.token_hex(8)
    request.state.correlation_id = correlation_id

    response = await call_next(request)

    response.headers["x-correlation-id"] = correlation_id
    response.headers["x-duration-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response
