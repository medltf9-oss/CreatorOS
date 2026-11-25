"""OAuth primitives: state, PKCE, redirect validation and session lifecycle rules.

The rules enforced here are the ones that stop a redirect from being replayed, borrowed from
another provider, or accepted after it has expired:

* 32 random bytes of state, base64url, single use, bound to one provider and one route;
* only the SHA-256 of the state is stored, so a database read cannot forge a callback;
* the PKCE verifier (TikTok) never leaves the vault, and the challenge sent to the provider is
  S256 of it;
* a session expires 10 minutes after it is created and is dead after first use;
* the redirect URI is compared byte for byte with the registered one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ...db.enums import OAuthRoute
from ...integrations.social import config as social_config
from ...integrations.social.errors import SocialError

STATE_BYTES = 32
STATE_TTL = timedelta(minutes=10)
VERIFIER_BYTES = 64
STATE_MIN_LENGTH = 32


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_state() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(STATE_BYTES)).decode("ascii").rstrip("=")


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def new_verifier() -> str:
    # RFC 7636: 43-128 characters from the unreserved set. Hex keeps it trivially valid.
    return secrets.token_urlsafe(VERIFIER_BYTES)[:96]


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def redirect_uri(port: int, route: OAuthRoute) -> str:
    return f"http://127.0.0.1:{port}{social_config.CALLBACK_PATH.format(route=route.value)}"


def check_state_shape(state: str | None) -> str:
    if not state or len(state) < STATE_MIN_LENGTH or len(state) > 200:
        raise SocialError(
            "STATE_INVALID",
            "the authorization response did not carry a usable state value",
            http_status=400,
        )
    return state


def check_redirect_uri(expected: str, received: str | None) -> None:
    """Registered redirect URIs must match exactly (no prefix, no case folding)."""
    if received is None:
        return
    if not hmac.compare_digest(expected, received):
        raise SocialError(
            "REDIRECT_MISMATCH",
            "the redirect URI does not match the one registered for this app",
            http_status=400,
        )


@dataclass(slots=True)
class StartedAuthorization:
    session_id: str
    authorization_url: str
    redirect_uri: str
    expires_at: datetime
    state: str
    code_verifier: str | None
    scopes: list[str]
    pkce_method: str | None


def is_expired(expires_at: datetime, *, now: datetime | None = None) -> bool:
    moment = now or utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= moment


def session_status_for_callback(*, consumed_at: datetime | None, expires_at: datetime) -> str:
    """What a callback is allowed to do, given the stored session."""
    if consumed_at is not None:
        return "consumed"
    if is_expired(expires_at):
        return "expired"
    return "pending"
