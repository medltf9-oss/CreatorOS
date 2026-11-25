"""Facebook Page insights adapter (Graph API).

Shapes this module has to live with, all documented and all different:

* **Page metrics** come from `GET /{version}/{page-id}/insights?metric=a,b&period=day&since=&until=`
  and return ``data: [{name, period, values: [{value, end_time}]}]`` — one value per bucket.
* **Post/video/reel lifetime metrics** use ``period=lifetime`` and return a single ``values`` entry
  with no ``end_time``.
* **Post engagement** (``shares``, ``reactions``, ``comments``) is not an insight at all: it lives on
  the Post node and is read with ``?fields=shares,reactions.summary(true).limit(0),...``.
* **Reels** are videos with a Reels-specific metric set; the same endpoint answers both, so the
  object type decides which metrics are requested, not a different URL.

Everything is one GET per window per period group. A metric the provider refuses (too small an
audience, missing permission) is isolated: the other metrics in the same response are kept.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ...db.enums import AnalyticsObjectType, MetricAvailability, MetricPeriod
from .base import (
    AnalyticsAdapter,
    CollectRequest,
    CollectResult,
    ObservationDraft,
    from_failure,
    graph_value,
    period_bounds,
    skipped,
)
from .catalog import MetricSpec, catalogue_by_key
from .errors import RUN_LEVEL_CODES, AnalyticsError, classify_metric_failure

#: Page insights are documented as approximately 24 hours behind and retained for two years.
RETENTION_DAYS = 730
#: `period` values Graph accepts for the Page metrics in the catalogue.
PERIOD_CODES: dict[MetricPeriod, str] = {
    MetricPeriod.DAY: "day",
    MetricPeriod.WEEK: "week",
    MetricPeriod.DAYS_28: "days_28",
    MetricPeriod.MONTH: "month",
    MetricPeriod.LIFETIME: "lifetime",
    MetricPeriod.TOTAL_OVER_RANGE: "total_over_range",
}


class FacebookInsightsAdapter(AnalyticsAdapter):
    platform_name = "facebook"

    def __init__(self, transport=None, *, base_url: str = "https://graph.facebook.com", **kwargs) -> None:
        super().__init__(transport, **kwargs)
        self.base_url = base_url.rstrip("/")

    def _url(self, api_version: str, path: str) -> str:
        return f"{self.base_url}/{api_version}{path}"

    # ------------------------------------------------------------------ dispatch
    async def _collect(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        if request.object_type is AnalyticsObjectType.ACCOUNT:
            return await self.collect_account(request, token=token, api_version=api_version)
        if request.object_type is AnalyticsObjectType.POST:
            return await self.collect_post(request, token=token, api_version=api_version)
        if request.object_type in (AnalyticsObjectType.VIDEO, AnalyticsObjectType.REEL):
            return await self.collect_video(request, token=token, api_version=api_version)
        raise AnalyticsError(
            "METRIC_UNAVAILABLE",
            f"facebook has no analytics surface for {request.object_type.value}",
            detail={"object_type": request.object_type.value},
        )

    # ------------------------------------------------------------------ account
    async def collect_account(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        result = CollectResult()
        node_specs, insight_specs = _split_by_source(request.specs)

        if node_specs:
            node = await self._page_node(request.object_id, node_specs, token=token, api_version=api_version)
            result.extend(node)

        if insight_specs:
            since, until = request.since, request.until
            if since is None or until is None:
                # A daily metric without a window is meaningless: ask for the last complete day.
                until = _floor_day(self.now())
                since = until - _days(1)
            for window_since, window_until in self.split_window(since, until):
                for period_kind, group in _group_by_period(insight_specs):
                    result.extend(
                        await self._insights(
                            path=f"/{request.object_id}/insights",
                            specs=group,
                            period_kind=period_kind,
                            since=window_since,
                            until=window_until,
                            token=token,
                            api_version=api_version,
                        )
                    )
        return result

    async def _page_node(
        self, page_id: str, specs: list[MetricSpec], *, token: str, api_version: str
    ) -> CollectResult:
        fields = ",".join(sorted({spec.provider_metric_name for spec in specs}))
        response = await self._get(
            self._url(api_version, f"/{page_id}"),
            params={"fields": fields},
            token=token,
        )
        result = CollectResult(calls=1)
        body = response.body if isinstance(response.body, dict) else {}
        if body.get("error"):
            failure = classify_metric_failure(provider="facebook", status=response.status, body=body)
            for spec in specs:
                result.drafts.append(
                    from_failure(spec, failure, MetricPeriod.INSTANT)
                    if failure
                    else skipped(spec, MetricAvailability.MISSING, "page node returned an error")
                )
            return result
        for spec in specs:
            result.drafts.append(
                graph_value(
                    spec=spec,
                    metric_key=spec.key,
                    raw=body.get(spec.provider_metric_name),
                    period_kind=MetricPeriod.INSTANT,
                    period_end=self.now(),
                )
            )
        return result

    # ------------------------------------------------------------------ post
    async def collect_post(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        result = CollectResult()
        node_specs, insight_specs = _split_by_source(request.specs)

        if node_specs:
            result.extend(await self._post_node(request.object_id, node_specs, token=token, api_version=api_version))
        if insight_specs:
            for period_kind, group in _group_by_period(insight_specs):
                result.extend(
                    await self._insights(
                        path=f"/{request.object_id}/insights",
                        specs=group,
                        period_kind=period_kind,
                        since=request.since,
                        until=request.until,
                        token=token,
                        api_version=api_version,
                    )
                )
        return result

    async def _post_node(
        self, post_id: str, specs: list[MetricSpec], *, token: str, api_version: str
    ) -> CollectResult:
        """`shares`, `reactions.summary` and `comments.summary` are node fields, not insights."""
        field_map = {
            "shares": "shares",
            "reactions": "reactions.summary(true).limit(0)",
            "comments": "comments.summary(true).limit(0)",
        }
        requested = sorted({field_map.get(spec.provider_metric_name, spec.provider_metric_name) for spec in specs})
        response = await self._get(
            self._url(api_version, f"/{post_id}"),
            params={"fields": ",".join(requested)},
            token=token,
        )
        body = response.body if isinstance(response.body, dict) else {}
        result = CollectResult(calls=1)
        if body.get("error"):
            failure = classify_metric_failure(provider="facebook", status=response.status, body=body)
            for spec in specs:
                result.drafts.append(
                    from_failure(spec, failure) if failure else skipped(spec, MetricAvailability.MISSING, "post node returned an error")
                )
            return result

        for spec in specs:
            name = spec.provider_metric_name
            if name in ("shares", "reactions", "comments"):
                raw = _summary_total(body.get(name))
            else:
                raw = body.get(name)
            result.drafts.append(
                graph_value(
                    spec=spec,
                    metric_key=spec.key,
                    raw=raw,
                    period_kind=MetricPeriod.LIFETIME,
                    period_end=None,
                )
            )
        return result

    # ------------------------------------------------------------------ video / reel
    async def collect_video(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        """Videos use `/video_insights`; reels use the same endpoint with a reels metric set."""
        result = CollectResult()
        for period_kind, group in _group_by_period(list(request.specs)):
            result.extend(
                await self._insights(
                    path=f"/{request.object_id}/video_insights",
                    specs=group,
                    period_kind=period_kind,
                    since=request.since,
                    until=request.until,
                    token=token,
                    api_version=api_version,
                )
            )
        return result

    # ------------------------------------------------------------------ insights
    async def _insights(
        self,
        *,
        path: str,
        specs: list[MetricSpec],
        period_kind: MetricPeriod,
        since: datetime | None,
        until: datetime | None,
        token: str,
        api_version: str,
    ) -> CollectResult:
        params: dict[str, Any] = {
            "metric": ",".join(spec.provider_metric_name for spec in specs),
            "period": PERIOD_CODES.get(period_kind, "lifetime"),
        }
        if period_kind is not MetricPeriod.LIFETIME:
            epoch_since = self.epoch(since)
            epoch_until = self.epoch(until)
            if epoch_since is not None:
                params["since"] = epoch_since
            if epoch_until is not None:
                params["until"] = epoch_until

        try:
            response = await self._get(self._url(api_version, path), params=params, token=token)
        except AnalyticsError as error:
            # A run-level failure is only run-level if it cannot be pinned on one metric, and a
            # throttle or an expired token is never one metric's fault: those abort the run so the
            # scheduler retries it, instead of writing "temporarily unavailable" on every row.
            if error.code in RUN_LEVEL_CODES:
                raise
            failure = classify_metric_failure(
                provider="facebook",
                status=error.http_status,
                body=error.detail.get("body") or {"error": {"message": error.message}},
            )
            if failure is None:
                raise
            return CollectResult(
                drafts=[from_failure(spec, failure, period_kind) for spec in specs],
                warnings=[f"{error.code}: {error.message}"],
                calls=1,
            )

        result = CollectResult(calls=1)
        body = response.body if isinstance(response.body, dict) else {}
        returned: dict[str, dict] = {}
        for entry in body.get("data") or []:
            if isinstance(entry, dict) and entry.get("name"):
                returned[str(entry["name"])] = entry

        for spec in specs:
            entry = returned.get(spec.provider_metric_name)
            if entry is None:
                # The metric was requested and silently omitted. That is `missing`, with the reason
                # kept, because "not returned" is not "zero" and not "not supported" either.
                result.drafts.append(
                    skipped(
                        spec,
                        MetricAvailability.MISSING,
                        f"{spec.provider_metric_name} was requested but not returned by the provider",
                    )
                )
                continue
            result.drafts.extend(_entry_drafts(spec, entry, period_kind))
        return result


# ---------------------------------------------------------------------- parsing


def _split_by_source(specs: tuple[MetricSpec, ...] | list[MetricSpec]) -> tuple[list[MetricSpec], list[MetricSpec]]:
    """Node fields (`followers_count`, `shares`) vs `/insights` metrics."""
    node: list[MetricSpec] = []
    insights: list[MetricSpec] = []
    for spec in specs:
        (node if "/insights" not in spec.source_endpoint and "/video_insights" not in spec.source_endpoint else insights).append(spec)
    return node, insights


def _group_by_period(specs: list[MetricSpec]) -> list[tuple[MetricPeriod, list[MetricSpec]]]:
    """Graph answers one `period` per request, so group and prefer the finest period requested."""
    groups: dict[MetricPeriod, list[MetricSpec]] = {}
    for spec in specs:
        period_kind = spec.period_kinds[0]
        groups.setdefault(period_kind, []).append(spec)
    order = [MetricPeriod.DAY, MetricPeriod.WEEK, MetricPeriod.DAYS_28, MetricPeriod.LIFETIME]
    return sorted(groups.items(), key=lambda item: order.index(item[0]) if item[0] in order else len(order))


def _entry_drafts(spec: MetricSpec, entry: dict, period_kind: MetricPeriod) -> list[ObservationDraft]:
    """One entry can carry 1 bucket (lifetime) or up to N daily buckets.

    Each bucket becomes its own observation, so the ledger is a time series and a later re-collection
    of the same day cannot rewrite history for a different day.
    """
    values = entry.get("values")
    if not isinstance(values, list):
        # Some metrics answer with a bare `value` instead of a `values` list.
        return [
            graph_value(
                spec=spec,
                metric_key=spec.key,
                raw=entry.get("value"),
                period_kind=period_kind,
                period_end=None if period_kind is MetricPeriod.LIFETIME else _parse_time(entry.get("end_time")),
            )
        ]

    drafts: list[ObservationDraft] = []
    for bucket in values:
        if not isinstance(bucket, dict):
            continue
        end = _parse_time(bucket.get("end_time"))
        start, end = period_bounds(end, period_kind)
        drafts.append(
            graph_value(
                spec=spec,
                metric_key=spec.key,
                raw=bucket.get("value"),
                period_kind=period_kind,
                period_start=start,
                period_end=end,
            )
        )
    if not drafts:
        drafts.append(
            skipped(spec, MetricAvailability.MISSING, "provider returned an empty values list for this metric")
        )
    return drafts


def _summary_total(raw: Any) -> Any:
    """`{"total_count": 4, "summary": {"total_count": 4}}` -> ``4`` without losing the real shape."""
    if not isinstance(raw, dict):
        return raw
    if "summary" in raw and isinstance(raw["summary"], dict):
        return raw["summary"].get("total_count")
    return raw.get("total_count", raw)


def _parse_time(value: Any) -> datetime | None:
    from .base import _as_datetime

    return _as_datetime(value)


def _floor_day(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)


def metric_spec(key: str) -> MetricSpec:
    spec = catalogue_by_key().get(key)
    if spec is None:
        raise KeyError(key)
    return spec
