"""Analytics error vocabulary.

An analytics failure is not one thing. "I asked for reach and got nothing" can mean the Page is too
small, the token is missing a scope, the metric does not exist on this object, the version retired it
or the provider is throttling us — and each of those produces a *different* availability state, not
a zero. `classify_metric_failure()` is the single place that decides which.

Nothing here raises for a metric-level problem: a failed metric inside a batch is isolated and
recorded with its availability. `AnalyticsError` is reserved for failures that make the whole run
pointless (no token, retired version, provider down).
"""
from __future__ import annotations

from dataclasses import dataclass

from ...db.enums import MetricAvailability
from ..social.errors import ProviderFailure, SocialError, classify

#: Analytics-specific codes, added to the Phase 6/7 vocabulary.
ANALYTICS_CODES = frozenset(
    {
        "METRIC_UNAVAILABLE",
        "METRIC_NOT_RETURNED",
        "PRODUCT_TYPE_MISMATCH",
        "RANGE_TOO_LARGE",
        "SCOPE_NOT_GRANTED",
        "GRAPH_VERSION_UNSUPPORTED",
        "INSUFFICIENT_VIEWERS",
        "OBJECT_NOT_FOUND",
    }
)


#: Codes that describe the whole run (credential, provider, throttle), never one metric.
RUN_LEVEL_CODES = frozenset(
    {
        "RATE_LIMITED",
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_TIMEOUT",
        "NETWORK_ERROR",
        "TOKEN_EXPIRED",
        "TOKEN_REVOKED",
        "APP_REVIEW_REQUIRED",
        "CREDENTIAL_UNAVAILABLE",
        "VAULT_UNAVAILABLE",
        "GRAPH_VERSION_UNSUPPORTED",
    }
)


class AnalyticsError(SocialError):
    """A failure that stops collection for one account/target."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        detail: dict | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(code, message, http_status=http_status, detail=detail, retry_after_s=retry_after_s)


@dataclass(frozen=True, slots=True)
class MetricFailure:
    """A metric-level failure with the availability it implies."""

    availability: MetricAvailability
    code: str
    detail: str


#: Graph error codes/subcodes that describe a per-metric problem rather than a run problem.
_INSUFFICIENT = ("not enough viewers", "not enough data", "too small", "insufficient")
_PERMISSION = (
    "does not have permission",
    "insufficient permission",
    "requires permission",
    "permission denied",
    "unsupported get request",
)


def classify_metric_failure(
    *,
    provider: str,
    status: int,
    body: dict | None,
    metric_name: str = "",
) -> MetricFailure | None:
    """Return the availability a failed metric should carry, or ``None`` for a run-level failure.

    Returning ``None`` means "this is not a metric problem": the caller lets the classified
    `SocialError` propagate so the run fails honestly instead of writing nine missing rows.
    """
    body = body if isinstance(body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    message = str(error.get("message") or body.get("error_message") or "").lower()
    try:
        numeric = int(error.get("code") or body.get("code") or 0)
    except (TypeError, ValueError):
        numeric = 0
    subcode = error.get("error_subcode")

    # An expired or revoked credential is not a metric problem: every metric in the run is equally
    # dead, so the run must fail and be retried with a fresh token.
    if numeric in (190, 102) or subcode in (463, 467):
        return None

    if status == 429 or numeric in (4, 17, 32, 613) or "rate limit" in message:
        return MetricFailure(MetricAvailability.TEMPORARILY_UNAVAILABLE, "RATE_LIMITED", "provider is throttling this app")

    if any(token in message for token in _INSUFFICIENT):
        return MetricFailure(
            MetricAvailability.BELOW_THRESHOLD,
            "INSUFFICIENT_VIEWERS",
            "the provider withholds this metric because the audience is too small",
        )

    if numeric in (10, 200) or subcode in (458, 459, 460) or any(token in message for token in _PERMISSION):
        return MetricFailure(
            MetricAvailability.NOT_AUTHORIZED,
            "SCOPE_NOT_GRANTED",
            f"the token is not authorized for {metric_name or 'this metric'}",
        )

    if numeric == 100 and ("nonexisting" in message or "does not exist" in message or "unsupported" in message):
        return MetricFailure(
            MetricAvailability.NOT_SUPPORTED,
            "METRIC_UNAVAILABLE",
            "the provider does not expose this metric for this object",
        )

    if provider == "tiktok":
        raw = str((body.get("error") or {}).get("code") or body.get("error") or "").lower()
        if raw in {"rate_limit_exceeded", "spam_risk_too_many_posts"}:
            return MetricFailure(MetricAvailability.TEMPORARILY_UNAVAILABLE, "RATE_LIMITED", "tiktok is throttling us")
        if raw in {"scope_not_authorized", "scope_permission_missed"}:
            return MetricFailure(MetricAvailability.NOT_AUTHORIZED, "SCOPE_NOT_GRANTED", "the token lacks this scope")
        if raw == "access_token_invalid":
            return None  # the whole run is dead, not one metric
    return None


def as_error(provider: str, status: int, body: dict | None, *, retry_after_s: float | None = None) -> AnalyticsError:
    """Classify a run-level failure without inventing a provider answer."""
    classified = classify(
        ProviderFailure(
            provider=provider,
            operation="analytics.collect",
            status=status,
            body=body or {},
            retry_after_s=retry_after_s,
        )
    )
    return AnalyticsError(
        classified.code,
        classified.message,
        http_status=classified.http_status,
        detail=classified.detail,
        retry_after_s=classified.retry_after_s,
    )
