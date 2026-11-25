"""Signed access to media files for HTML media elements.

``<img>`` and ``<video>`` cannot send an ``Authorization`` header, so the library hands out
short-lived HMAC-signed URLs instead of weakening the API. The signature binds the asset id
and an expiry to the per-launch session token, so:

* a URL cannot be guessed or reused for a different asset;
* it stops working after ten minutes;
* it never contains a filesystem path, so it cannot be used to read arbitrary files;
* the renderer never sees the signing key (the backend derives it from the session token,
  which the shell already holds).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from fastapi import HTTPException, Request, status

SIGNED_URL_TTL_S = 600


def _secret(request: Request) -> bytes:
    token = getattr(request.app.state, "session_token", "") or ""
    return hashlib.sha256(f"media-url:{token}".encode()).digest()


def sign_media_url(request: Request, asset_id: str, *, resource: str, extra: str = "") -> str:
    expires = int(time.time()) + SIGNED_URL_TTL_S
    payload = f"{asset_id}:{resource}:{extra}:{expires}".encode()
    signature = hmac.new(_secret(request), payload, hashlib.sha256).hexdigest()[:32]
    query = urlencode({"exp": expires, "sig": signature, **({"i": extra} if extra else {})})
    path = {
        "thumbnail": f"/library/assets/{asset_id}/thumbnail",
        "poster": f"/library/assets/{asset_id}/poster",
        "stream": f"/library/assets/{asset_id}/stream",
        "derivative": f"/library/assets/{asset_id}/derivative",
    }[resource]
    return f"{path}?{query}"


def verify_media_url(
    request: Request, asset_id: str, *, resource: str, exp: str | None, sig: str | None, extra: str = ""
) -> None:
    if not exp or not sig:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MEDIA_URL_UNSIGNED: a signed media URL is required")
    try:
        expires = int(exp)
    except ValueError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MEDIA_URL_INVALID: malformed expiry") from exc
    if expires < int(time.time()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MEDIA_URL_EXPIRED: refresh the library view")
    payload = f"{asset_id}:{resource}:{extra}:{expires}".encode()
    expected = hmac.new(_secret(request), payload, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MEDIA_URL_INVALID: signature does not match")


__all__ = ["SIGNED_URL_TTL_S", "sign_media_url", "verify_media_url"]
