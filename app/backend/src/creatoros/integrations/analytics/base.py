"""The analytics adapter contract.

Adapters are HTTP and parsing only: no SQLAlchemy, no session, no token storage. They receive an
access token and an API version for the duration of one call, and they return *drafts* — values
already bound to a catalogue metric key and to an availability state — which the ingestion service
persists under a run id.

Two rules are enforced here rather than left to discipline:

* **A draft cannot claim a value it does not have.** ``ObservationDraft`` refuses ``observed``
  without a value and refuses a value without ``observed``, and it refuses a value whose shape does
  not match the metric's ``value_kind``. That is approval requirement 4 at the boundary.
* **One metric failing does not fail the batch.** A Graph insights request can return per-metric
  errors; those become drafts with ``below_threshold``/``not_authorized``/``temporarily_unavailable``
  while the rest of the batch is kept.

Window splitting is here too: Graph refuses insights windows longer than 90 days, so a backfill is
chunked by the adapter, never by the caller remembering to.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ...db.enums import AnalyticsObjectType, MetricAvailability, MetricPeriod, MetricValueKind
from ..social.base import HttpResponse, HttpxTransport, Transport
from ..social.errors import SocialError
from .catalog import MetricSpec
from .errors import AnalyticsError, MetricFailure, as_error
from .normalize import normalize_payload

#: Meta documents a maximum insights window of 90 days per request.
MAX_WINDOW_DAYS = 90
#: A batch this large is a bug, not a plan: Graph accepts ~20 metrics per request in practice.
MAX_METRICS_PER_REQUEST = 25
#: Retries are for rate limits and 5xx, never for a 400 that a retry cannot fix.
MAX_ATTEMPTS = 3

#: Metrics the catalogue already knows are unavailable. They are recorded, never requested.
_NEVER_REQUESTABLE = frozenset({MetricAvailability.NOT_SUPPORTED, MetricAvailability.REDACTED_BY_POLICY})

_VERSION_RE = __import__("re").compile(r"^v(\d+)(?:\.(\d+))?$")


def parse_api_version(version: str) -> tuple[int, int]:
    """``v23.0`` -> ``(23, 0)``, ``v2`` -> ``(2, 0)``. Raises rather than guessing."""
    match = _VERSION_RE.match(str(version).strip())
    if match is None:
        raise ValueError(f"unrecognised provider version: {version!r}")
    return int(match.group(1)), int(match.group(2) or 0)


def version_within(min_version: str | None, max_version: str | None, version: str) -> bool:
    """Is ``version`` inside the metric's documented window? Unparseable input is never a yes."""
    try:
        current = parse_api_version(version)
        if min_version and current < parse_api_version(min_version):
            return False
        if max_version and current > parse_api_version(max_version):
            return False
    except ValueError:
        return False
    return True


@dataclass(slots=True)
class CollectRequest:
    """What to collect for one object in one run."""

    object_id: str
    object_type: AnalyticsObjectType
    specs: tuple[MetricSpec, ...]
    since: datetime | None = None
    until: datetime | None = None
    period_kind: MetricPeriod | None = None
    product_type: str | None = None
    #: The owning Page, when the object is a Page-owned post/video (Instagram and Facebook alike).
    page_id: str | None = None
    #: Scopes actually granted on the token, not the ones the app asked for. TikTok needs this to
    #: keep `user.info.stats` and `video.list` independent; the others use it for the same reason.
    granted_scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if len(self.specs) > MAX_METRICS_PER_REQUEST:
            raise ValueError(
                f"{len(self.specs)} metrics in one request exceeds the documented batch size; split the plan"
            )


@dataclass(slots=True)
class ObservationDraft:
    """One measurement, or one documented reason there is no measurement."""

    metric_key: str
    provider_metric_name: str
    availability: MetricAvailability
    value_kind: MetricValueKind = MetricValueKind.SCALAR
    value_num: float | None = None
    value_json: Any | None = None
    period_kind: MetricPeriod = MetricPeriod.LIFETIME
    period_start: datetime | None = None
    period_end: datetime | None = None
    unit: str = "count"
    note: str | None = None
    provider_payload: dict | None = None

    def __post_init__(self) -> None:
        has_number = self.value_num is not None
        has_structured = self.value_json is not None
        if self.availability is MetricAvailability.OBSERVED:
            if self.value_kind is MetricValueKind.SCALAR:
                if not has_number:
                    raise ValueError(f"{self.metric_key}: observed scalar without a value")
                if has_structured:
                    raise ValueError(f"{self.metric_key}: scalar observation carries value_json")
            elif not has_structured:
                raise ValueError(f"{self.metric_key}: observed {self.value_kind.value} without value_json")
            elif has_number:
                raise ValueError(f"{self.metric_key}: {self.value_kind.value} observation carries value_num")
        elif has_number or has_structured:
            raise ValueError(
                f"{self.metric_key}: availability {self.availability.value} must not carry a value"
            )

    @property
    def observed(self) -> bool:
        return self.availability is MetricAvailability.OBSERVED

    def as_dict(self) -> dict:
        return {
            "metric_key": self.metric_key,
            "provider_metric_name": self.provider_metric_name,
            "availability": self.availability.value,
            "value_kind": self.value_kind.value,
            "value_num": self.value_num,
            "value_json": self.value_json,
            "period_kind": self.period_kind.value,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "note": self.note,
        }


@dataclass(slots=True)
class CollectResult:
    drafts: list[ObservationDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calls: int = 0

    def extend(self, other: CollectResult) -> None:
        self.drafts.extend(other.drafts)
        self.warnings.extend(other.warnings)
        self.calls += other.calls

    def observed(self) -> list[ObservationDraft]:
        return [draft for draft in self.drafts if draft.observed]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for draft in self.drafts:
            counts[draft.availability.value] = counts.get(draft.availability.value, 0) + 1
        return counts


def skipped(spec: MetricSpec, availability: MetricAvailability, note: str) -> ObservationDraft:
    """A draft for a metric that was deliberately not requested, or that the provider withheld."""
    return ObservationDraft(
        metric_key=spec.key,
        provider_metric_name=spec.provider_metric_name,
        availability=availability,
        value_kind=spec.value_kind,
        period_kind=spec.period_kinds[0],
        unit=spec.unit,
        note=note[:300],
    )


def from_failure(spec: MetricSpec, failure: MetricFailure, period_kind: MetricPeriod | None = None) -> ObservationDraft:
    return ObservationDraft(
        metric_key=spec.key,
        provider_metric_name=spec.provider_metric_name,
        availability=failure.availability,
        value_kind=spec.value_kind,
        period_kind=period_kind or spec.period_kinds[0],
        unit=spec.unit,
        note=f"{failure.code}: {failure.detail}"[:300],
    )


def _as_datetime(value: Any) -> datetime | None:
    """Provider timestamps arrive as epoch seconds or ISO-8601; both become aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    return parsed.replace(tzinfo=UTC)


class AnalyticsAdapter(ABC):
    """Shared transport, retry and window behaviour for the three providers."""

    platform_name: str = ""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_sleep_s: float = 30.0,
    ) -> None:
        self.transport = transport or HttpxTransport(self.platform_name)
        self.max_attempts = max_attempts
        self._sleep = sleep
        self.max_sleep_s = max_sleep_s

    # ------------------------------------------------------------------ contract
    async def collect(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        """Request what the configured version still serves, and say why the rest was skipped.

        The version filter lives here rather than in each provider so a retired metric can never be
        requested by accident: it becomes a ``not_supported`` draft before any URL is built.
        """
        allowed: list[MetricSpec] = []
        result = CollectResult()
        for spec in request.specs:
            if spec.availability_default in _NEVER_REQUESTABLE:
                result.drafts.append(
                    skipped(
                        spec,
                        spec.availability_default,
                        "not exposed by any approved API"
                        if spec.availability_default is MetricAvailability.NOT_SUPPORTED
                        else "withheld deliberately (policy)",
                    )
                )
                continue
            if version_within(spec.api_version_min, spec.api_version_max, api_version):
                allowed.append(spec)
                continue
            result.drafts.append(
                skipped(
                    spec,
                    MetricAvailability.NOT_SUPPORTED,
                    f"documented for {spec.api_version_min or 'any'}..{spec.api_version_max or 'any'}; configured {api_version}",
                )
            )
        if not allowed:
            return result
        permitted = CollectRequest(
            object_id=request.object_id,
            object_type=request.object_type,
            specs=tuple(allowed),
            since=request.since,
            until=request.until,
            period_kind=request.period_kind,
            product_type=request.product_type,
            page_id=request.page_id,
            granted_scopes=request.granted_scopes,
        )
        result.extend(await self._collect(permitted, token=token, api_version=api_version))
        return result

    @abstractmethod
    async def _collect(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        """Fetch, parse and return drafts. Never persists anything."""

    # ------------------------------------------------------------------ helpers
    async def _get(
        self,
        url: str,
        *,
        params: dict,
        token: str,
        timeout: float = 30.0,
    ) -> HttpResponse:
        """One GET with bounded retries.

        A 429 or 5xx is retried after the provider's own `retry-after` (capped), because the correct
        behaviour is to wait, not to hammer and not to give up the run. A 4xx that a retry cannot fix
        is raised immediately.
        """
        last: SocialError | None = None
        for attempt in range(1, self.max_attempts + 1):
            headers = {"Authorization": f"Bearer {token}"}
            response = await self.transport.request("GET", url, params=params, headers=headers, timeout=timeout)
            if response.status < 400:
                return response
            error = as_error(
                self.platform_name,
                response.status,
                response.body,
                retry_after_s=_retry_after(response),
            )
            last = error
            retryable = response.status == 429 or response.status >= 500 or error.code == "RATE_LIMITED"
            if not retryable or attempt == self.max_attempts:
                raise error
            await self._sleep(self._delay(error, attempt))
        raise last or AnalyticsError("PROVIDER_ERROR", "request failed without a classified error")

    def _delay(self, error: SocialError, attempt: int) -> float:
        if error.retry_after_s is not None:
            return max(0.0, min(float(error.retry_after_s), self.max_sleep_s))
        return min(2.0 ** (attempt - 1), self.max_sleep_s)

    @staticmethod
    def split_window(
        since: datetime | None,
        until: datetime | None,
        *,
        max_days: int = MAX_WINDOW_DAYS,
    ) -> list[tuple[datetime | None, datetime | None]]:
        """Chunk a backfill into provider-legal windows, oldest first."""
        if since is None or until is None:
            return [(since, until)]
        if until <= since:
            raise ValueError("until must be after since")
        windows: list[tuple[datetime | None, datetime | None]] = []
        cursor = since
        step = timedelta(days=max_days)
        while cursor < until:
            end = min(cursor + step, until)
            windows.append((cursor, end))
            cursor = end
        return windows

    @staticmethod
    def epoch(value: datetime | None) -> int | None:
        if value is None:
            return None
        return int((value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp())

    def now(self) -> datetime:
        return datetime.now(UTC)

    def elapsed_ms(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)


def graph_value(
    *,
    spec: MetricSpec,
    metric_key: str,
    raw: Any,
    period_kind: MetricPeriod,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> ObservationDraft:
    """Turn one provider value into a draft without losing its shape.

    Graph returns three shapes: a number, an object (breakdown) and an array (curve). A number is
    stored as ``value_num``; everything else is stored whole in ``value_json`` by
    `normalize.normalize_payload`, which also refuses to pad a short curve. An empty result is
    ``missing`` — never ``0``.
    """
    provider_name = spec.provider_metric_name
    base = {
        "metric_key": metric_key,
        "provider_metric_name": provider_name,
        "value_kind": spec.value_kind,
        "period_kind": period_kind,
        "period_start": period_start,
        "period_end": period_end,
        "unit": spec.unit,
    }
    if raw is None:
        return ObservationDraft(availability=MetricAvailability.MISSING, note="provider returned no value for this metric", **base)
    if isinstance(raw, (dict, list)) and not raw:
        return ObservationDraft(
            availability=MetricAvailability.MISSING,
            note="provider returned an empty object (usually a volume or permission condition)",
            **base,
        )

    normalized = normalize_payload(spec, raw)
    if not normalized.usable:
        return ObservationDraft(
            availability=MetricAvailability.MISSING,
            note=(normalized.note or "provider returned an unusable payload")[:300],
            **base,
        )
    return ObservationDraft(
        availability=MetricAvailability.OBSERVED,
        value_num=normalized.value_num,
        value_json=normalized.value_json,
        note=normalized.note[:300] if normalized.note else None,
        **base,
    )


def _retry_after(response: HttpResponse) -> float | None:
    """`retry-after` seconds, or the BUC estimate Meta puts in `x-business-use-case-usage`."""
    response_retry = response.retry_after()
    if response_retry is not None:
        return response_retry
    raw = response.headers.get("x-business-use-case-usage")
    if not raw:
        return None
    try:
        import json

        usage = json.loads(raw)
    except (TypeError, ValueError):
        return None
    seconds: list[float] = []
    for entries in usage.values() if isinstance(usage, dict) else []:
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("estimated_time_to_regain_access"):
                try:
                    seconds.append(float(entry["estimated_time_to_regain_access"]) * 60.0)
                except (TypeError, ValueError):
                    continue
    return max(seconds) if seconds else None


#: How long a provider aggregation window is, in days. `lifetime`/`instant` have no start.
PERIOD_DAYS: dict[MetricPeriod, int | None] = {
    MetricPeriod.DAY: 1,
    MetricPeriod.WEEK: 7,
    MetricPeriod.DAYS_28: 28,
    MetricPeriod.MONTH: 30,
    MetricPeriod.LIFETIME: None,
    MetricPeriod.TOTAL_OVER_RANGE: None,
    MetricPeriod.INSTANT: None,
}


def period_bounds(end: datetime | None, period_kind: MetricPeriod) -> tuple[datetime | None, datetime | None]:
    """The start of the provider window that ends at ``end``, so buckets never overlap by accident."""
    days = PERIOD_DAYS.get(period_kind)
    if end is None or days is None:
        return (None, end)
    return (end - timedelta(days=days), end)
