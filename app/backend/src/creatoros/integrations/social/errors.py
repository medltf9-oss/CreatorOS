"""Provider error taxonomy.

Every adapter maps raw provider failures onto these codes so the API, the UI and Phase 7 can
branch on a stable vocabulary instead of parsing prose. The mapping is deliberately
conservative: an unrecognised body becomes `PROVIDER_ERROR` with the raw code kept in `detail`,
because guessing "it was probably rate limiting" is how retry storms start.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Codes that mean "the user must act" rather than "try again".
USER_ACTION_CODES = frozenset(
    {
        "AUTHORIZATION_DENIED",
        "STATE_INVALID",
        "STATE_EXPIRED",
        "STATE_CONSUMED",
        "STATE_ROUTE_MISMATCH",
        "TOKEN_EXPIRED",
        "TOKEN_REVOKED",
        "APP_REVIEW_REQUIRED",
        "SCOPE_NOT_GRANTED",
        "ACCOUNT_TYPE_UNSUPPORTED",
        "REAUTHORIZATION_REQUIRED",
        "VAULT_UNAVAILABLE",
        "CREDENTIAL_UNAVAILABLE",
        "APP_NOT_CONFIGURED",
    }
)
# Codes where a bounded retry with backoff is legitimate.
TRANSIENT_CODES = frozenset(
    {"RATE_LIMITED", "PROVIDER_UNAVAILABLE", "NETWORK_ERROR", "PROVIDER_TIMEOUT"}
)


class SocialError(Exception):
    """A failure with a stable code, a human explanation and no secret material."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        detail: dict | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}
        self.retry_after_s = retry_after_s

    @property
    def user_action_required(self) -> bool:
        return self.code in USER_ACTION_CODES

    @property
    def transient(self) -> bool:
        return self.code in TRANSIENT_CODES

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        if self.retry_after_s is not None:
            payload["retry_after_s"] = self.retry_after_s
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SocialError({self.code!r}, http_status={self.http_status})"


@dataclass(slots=True)
class ProviderFailure:
    """Normalized provider response for classification."""

    provider: str
    operation: str
    status: int
    body: dict = field(default_factory=dict)
    retry_after_s: float | None = None


FACEBOOK_ERROR_MAP: dict[int, str] = {
    4: "RATE_LIMITED",
    17: "RATE_LIMITED",
    32: "RATE_LIMITED",
    613: "RATE_LIMITED",
    10: "SCOPE_NOT_GRANTED",
    200: "SCOPE_NOT_GRANTED",
    190: "TOKEN_EXPIRED",
    102: "TOKEN_EXPIRED",
    463: "TOKEN_EXPIRED",
    467: "TOKEN_EXPIRED",
    458: "APP_REVIEW_REQUIRED",
    459: "APP_REVIEW_REQUIRED",
    460: "TOKEN_EXPIRED",
    100: "PROVIDER_ERROR",
    2: "PROVIDER_UNAVAILABLE",
    1: "PROVIDER_UNAVAILABLE",
}

TIKTOK_ERROR_MAP: dict[str, str] = {
    "access_token_invalid": "TOKEN_EXPIRED",
    "access_token_expired": "TOKEN_EXPIRED",
    "refresh_token_invalid": "REAUTHORIZATION_REQUIRED",
    "refresh_token_expired": "REAUTHORIZATION_REQUIRED",
    "scope_not_authorized": "SCOPE_NOT_GRANTED",
    "scope_permission_missed": "SCOPE_NOT_GRANTED",
    "rate_limit_exceeded": "RATE_LIMITED",
    "internal_error": "PROVIDER_UNAVAILABLE",
    "invalid_request": "PROVIDER_ERROR",
    "invalid_grant": "REAUTHORIZATION_REQUIRED",
    "invalid_client": "APP_NOT_CONFIGURED",
}

INSTAGRAM_ERROR_MAP: dict[int, str] = {
    4: "RATE_LIMITED",
    17: "RATE_LIMITED",
    32: "RATE_LIMITED",
    613: "RATE_LIMITED",
    190: "TOKEN_EXPIRED",
    102: "TOKEN_EXPIRED",
    10: "SCOPE_NOT_GRANTED",
    200: "SCOPE_NOT_GRANTED",
    100: "PROVIDER_ERROR",
    803: "ACCOUNT_TYPE_UNSUPPORTED",
}


def classify(failure: ProviderFailure) -> SocialError:
    provider = failure.provider
    body = failure.body if isinstance(failure.body, dict) else {}
    nested = body.get("error") if isinstance(body.get("error"), dict) else {}
    message = str(
        nested.get("message")
        or body.get("error_description")
        or body.get("error_message")
        or body.get("message")
        or (body.get("error") if isinstance(body.get("error"), str) else "")
        or f"{provider} returned HTTP {failure.status}"
    )[:300]

    if failure.status == 429:
        return SocialError("RATE_LIMITED", message, http_status=429, retry_after_s=failure.retry_after_s, detail={"status": 429})
    if failure.status >= 500:
        return SocialError(
            "PROVIDER_UNAVAILABLE",
            f"{provider} is unavailable (HTTP {failure.status})",
            http_status=502,
            retry_after_s=failure.retry_after_s,
            detail={"status": failure.status},
        )

    if provider == "tiktok":
        error_block = body.get("error") if isinstance(body.get("error"), dict) else {}
        raw = str(error_block.get("code") or body.get("error") or body.get("code") or "").lower()
        code = TIKTOK_ERROR_MAP.get(raw, "PROVIDER_ERROR")
        return SocialError(code, message or raw or "tiktok error", http_status=400 if code != "RATE_LIMITED" else 429, detail={"provider_code": raw, "status": failure.status})

    error_block = body.get("error") if isinstance(body.get("error"), dict) else {}
    table = INSTAGRAM_ERROR_MAP if provider == "instagram" else FACEBOOK_ERROR_MAP
    try:
        numeric = int(error_block.get("code") or body.get("code") or 0)
    except (TypeError, ValueError):
        numeric = 0
    subcode = error_block.get("error_subcode")
    code = table.get(numeric, "PROVIDER_ERROR")
    if numeric == 190 and subcode in (458, 459, 460, 463, 464, 467):
        # A 190 with these subcodes is not a plain expiry: the app or the user changed.
        code = "APP_REVIEW_REQUIRED" if subcode in (458, 459) else "TOKEN_EXPIRED"
    if numeric == 200 and subcode == 460:
        code = "APP_REVIEW_REQUIRED"
    return SocialError(
        code,
        message,
        http_status=400,
        detail={"provider_code": numeric or None, "provider_subcode": subcode, "status": failure.status},
    )


def from_generic_exception(provider: str, operation: str, error: Exception) -> SocialError:
    """Turn a transport-level failure into a code without inventing a provider answer."""
    text = str(error)
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return SocialError("PROVIDER_TIMEOUT", f"{provider} did not answer in time", http_status=504, detail={"operation": operation})
    return SocialError("NETWORK_ERROR", f"could not reach {provider}: {text[:200]}", http_status=502, detail={"operation": operation})


def oauth_callback_error(params: dict[str, str]) -> SocialError:
    """Classify an OAuth redirect that carries `error` instead of `code`."""
    raw = str(params.get("error") or "").strip()
    description = str(params.get("error_description") or params.get("error_message") or "").strip()
    reason = str(params.get("error_reason") or "").strip()
    lowered = raw.lower()
    if lowered in {"access_denied", "user_cancelled", "cancelled", "denied"} or reason == "user_denied":
        return SocialError(
            "AUTHORIZATION_DENIED",
            description or "the authorization request was declined",
            http_status=400,
            detail={"provider_error": raw, "reason": reason},
        )
    if lowered in {"invalid_scope", "insufficient_scope"}:
        return SocialError("SCOPE_NOT_GRANTED", description or f"scope rejected: {raw}", http_status=400, detail={"provider_error": raw})
    return SocialError(
        "PROVIDER_ERROR",
        description or f"the provider returned {raw or 'an error'}",
        http_status=400,
        detail={"provider_error": raw},
    )
