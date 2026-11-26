"""Instagram insights adapter — both login paths, one object model.

Instagram is the provider where "it depends" is the whole API:

* **Two hosts.** Instagram Login reads from ``graph.instagram.com``; Facebook Login reads the same
  professional account through ``graph.facebook.com``. Which host was used is recorded on the
  observation (via the app config and route), never averaged into the number.
* **Two metric types.** Account-level `period=day` metrics require ``metric_type=time_series`` and
  answer with ``values: [{value, end_time}]``; lifetime metrics answer with
  ``total_value: {value|breakdowns}``. Media insights default to ``total_value``.
* **Product types.** ``ig_reels_avg_watch_time`` is a Reels metric; asking for it on a FEED post is
  a provider error, so it is refused here with ``not_supported`` before the request is built.
* **Facebook-Login-only metrics.** ``total_likes``/``total_comments``/``total_views`` exist only on
  the Facebook Login path. On the Instagram Login path they are ``not_authorized``, not zero.
* **Empty is not zero.** Instagram documents that it returns an empty data set instead of 0. An
  absent metric becomes ``missing``; a present ``0`` stays ``0``.

Data is documented as delayed by up to 48 hours; a metric that has not arrived yet is ``missing``
with a note saying so, never a fabricated zero.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ...db.enums import AnalyticsObjectType, MetricAvailability, MetricPeriod, MetricValueKind
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

IG_HOST_INSTAGRAM_LOGIN = "https://graph.instagram.com"
IG_HOST_FACEBOOK_LOGIN = "https://graph.facebook.com"

LOGIN_INSTAGRAM = "instagram_login"
LOGIN_FACEBOOK = "facebook_login"

#: Product types the media endpoint documents.
PRODUCT_REELS = "REELS"
PRODUCT_FEED = "FEED"
PRODUCT_STORY = "STORIES"


class InstagramInsightsAdapter(AnalyticsAdapter):
    platform_name = "instagram"

    def __init__(
        self,
        transport=None,
        *,
        login_path: str = LOGIN_INSTAGRAM,
        base_url: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(transport, **kwargs)
        if login_path not in (LOGIN_INSTAGRAM, LOGIN_FACEBOOK):
            raise ValueError(f"unknown instagram login path: {login_path!r}")
        self.login_path = login_path
        self.base_url = (base_url or _host_for(login_path)).rstrip("/")

    def _url(self, api_version: str, path: str) -> str:
        return f"{self.base_url}/{api_version}{path}"

    @property
    def uses_facebook_login(self) -> bool:
        return self.login_path == LOGIN_FACEBOOK

    # ------------------------------------------------------------------ dispatch
    async def _collect(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        if request.object_type is AnalyticsObjectType.ACCOUNT:
            return await self.collect_account(request, token=token, api_version=api_version)
        return await self.collect_media(request, token=token, api_version=api_version)

    # ------------------------------------------------------------------ account
    async def collect_account(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        result = CollectResult()
        node_specs, insight_specs = _split_by_source(request.specs)

        if node_specs:
            result.extend(await self._user_node(request.object_id, node_specs, token=token, api_version=api_version))

        time_series = [spec for spec in insight_specs if MetricPeriod.DAY in spec.period_kinds]
        totals = [spec for spec in insight_specs if spec not in time_series]

        if time_series:
            since, until = request.since, request.until
            if since is None or until is None:
                until = _floor_day(self.now())
                since = until
            for window_since, window_until in self.split_window(since, until):
                result.extend(
                    await self._insights(
                        path=f"/{request.object_id}/insights",
                        specs=time_series,
                        metric_type="time_series",
                        period="day",
                        since=window_since,
                        until=window_until,
                        token=token,
                        api_version=api_version,
                    )
                )
        if totals:
            result.extend(
                await self._insights(
                    path=f"/{request.object_id}/insights",
                    specs=totals,
                    metric_type="total_value",
                    period="lifetime",
                    since=None,
                    until=None,
                    token=token,
                    api_version=api_version,
                )
            )
        return result

    async def _user_node(
        self, user_id: str, specs: list[MetricSpec], *, token: str, api_version: str
    ) -> CollectResult:
        fields = ",".join(sorted({spec.provider_metric_name for spec in specs}))
        response = await self._get(self._url(api_version, f"/{user_id}"), params={"fields": fields}, token=token)
        body = response.body if isinstance(response.body, dict) else {}
        result = CollectResult(calls=1)
        if body.get("error"):
            failure = classify_metric_failure(provider="instagram", status=response.status, body=body)
            for spec in specs:
                result.drafts.append(
                    from_failure(spec, failure, MetricPeriod.INSTANT)
                    if failure
                    else skipped(spec, MetricAvailability.MISSING, "instagram user node returned an error")
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

    # ------------------------------------------------------------------ media
    async def collect_media(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        product_type = (request.product_type or "").upper()
        allowed: list[MetricSpec] = []
        result = CollectResult()
        for spec in request.specs:
            gate = _product_gate(spec, product_type)
            if gate is not None:
                result.drafts.append(skipped(spec, MetricAvailability.NOT_SUPPORTED, gate))
            elif _needs_facebook_login(spec) and not self.uses_facebook_login:
                result.drafts.append(
                    skipped(
                        spec,
                        MetricAvailability.NOT_AUTHORIZED,
                        "this metric is only available through Facebook Login on a Page-linked account",
                    )
                )
            else:
                allowed.append(spec)
        if not allowed:
            return result

        result.extend(
            await self._insights(
                path=f"/{request.object_id}/insights",
                specs=allowed,
                metric_type="total_value",
                period=None,
                since=None,
                until=None,
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
        metric_type: str,
        period: str | None,
        since: datetime | None,
        until: datetime | None,
        token: str,
        api_version: str,
    ) -> CollectResult:
        params: dict[str, Any] = {
            "metric": ",".join(spec.provider_metric_name for spec in specs),
            "metric_type": metric_type,
        }
        if period is not None:
            params["period"] = period
        if metric_type == "time_series":
            epoch_since = self.epoch(since)
            epoch_until = self.epoch(until)
            if epoch_since is not None:
                params["since"] = epoch_since
            if epoch_until is not None:
                params["until"] = epoch_until

        try:
            response = await self._get(self._url(api_version, path), params=params, token=token)
        except AnalyticsError as error:
            if error.code in RUN_LEVEL_CODES:
                raise
            failure = classify_metric_failure(
                provider="instagram",
                status=error.http_status,
                body=error.detail.get("body") or {"error": {"message": error.message}},
            )
            if failure is None:
                raise
            return CollectResult(
                drafts=[from_failure(spec, failure) for spec in specs],
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
                result.drafts.append(
                    skipped(
                        spec,
                        MetricAvailability.MISSING,
                        f"{spec.provider_metric_name} was requested but not returned "
                        "(instagram returns an empty data set rather than a zero)",
                    )
                )
                continue
            result.drafts.extend(_entry_drafts(spec, entry, metric_type))
        return result


def _host_for(login_path: str) -> str:
    return IG_HOST_FACEBOOK_LOGIN if login_path == LOGIN_FACEBOOK else IG_HOST_INSTAGRAM_LOGIN


def _needs_facebook_login(spec: MetricSpec) -> bool:
    permissions = spec.required_permissions
    if not isinstance(permissions, dict):
        return False
    alternatives = [set(alt) for alt in permissions.get("any_of") or []]
    if not alternatives:
        return False
    # A metric that accepts the business-login route is available on both paths.
    return all("instagram_business_basic" not in alt for alt in alternatives)


def _product_gate(spec: MetricSpec, product_type: str) -> str | None:
    """Reels-only and Story-only metrics are refused on the wrong product type."""
    if spec.object_type is AnalyticsObjectType.REEL and product_type != PRODUCT_REELS:
        return f"documented for {PRODUCT_REELS} media only, this object is {product_type or 'unknown'}"
    if spec.object_type is AnalyticsObjectType.STORY and product_type != PRODUCT_STORY:
        return f"documented for {PRODUCT_STORY} media only, this object is {product_type or 'unknown'}"
    if spec.object_type is AnalyticsObjectType.POST and product_type == PRODUCT_STORY:
        return f"post metrics are not available for {PRODUCT_STORY} media"
    return None


def _split_by_source(specs: tuple[MetricSpec, ...] | list[MetricSpec]) -> tuple[list[MetricSpec], list[MetricSpec]]:
    node: list[MetricSpec] = []
    insights: list[MetricSpec] = []
    for spec in specs:
        (insights if "/insights" in spec.source_endpoint else node).append(spec)
    return node, insights


def _entry_drafts(spec: MetricSpec, entry: dict, metric_type: str) -> list[ObservationDraft]:
    if metric_type == "time_series" or isinstance(entry.get("values"), list):
        values = entry.get("values") or []
        drafts: list[ObservationDraft] = []
        for bucket in values:
            if not isinstance(bucket, dict):
                continue
            end = _as_dt(bucket.get("end_time"))
            start, end = period_bounds(end, MetricPeriod.DAY)
            drafts.append(
                graph_value(
                    spec=spec,
                    metric_key=spec.key,
                    raw=bucket.get("value"),
                    period_kind=MetricPeriod.DAY,
                    period_start=start,
                    period_end=end,
                )
            )
        if not drafts:
            return [
                skipped(spec, MetricAvailability.MISSING, "instagram returned an empty time series for this metric")
            ]
        return drafts

    total = entry.get("total_value")
    if not isinstance(total, dict):
        total = {"value": entry.get("value")}
    payload = _total_value_payload(total)
    if spec.value_kind is not MetricValueKind.BREAKDOWN and isinstance(payload, dict):
        # A breakdown-typed metric answered with a single number: keep the number, do not invent
        # breakdown keys for it.
        payload = payload.get("total", payload)
    elif spec.value_kind is MetricValueKind.BREAKDOWN and payload is not None and not isinstance(payload, dict):
        # The provider returned one number for a breakdown metric (no dimensions available). Record
        # it under the only honest key there is rather than dropping a real measurement.
        payload = {"total": payload}
    return [
        graph_value(
            spec=spec,
            metric_key=spec.key,
            raw=payload,
            period_kind=MetricPeriod.LIFETIME,
            period_start=None,
            period_end=None,
        )
    ]


def _total_value_payload(total: dict) -> Any:
    """`total_value` carries either a scalar `value` or `breakdowns`, never both."""
    breakdowns = total.get("breakdowns")
    if isinstance(breakdowns, list) and breakdowns:
        payload: dict[str, Any] = {}
        for block in breakdowns:
            if not isinstance(block, dict):
                continue
            for result in block.get("results") or []:
                if not isinstance(result, dict):
                    continue
                dimension = result.get("dimension_values") or []
                key = ".".join(str(part) for part in dimension) if dimension else str(
                    block.get("dimension_keys") or "total"
                )
                payload[key] = result.get("value")
        return payload or None
    if "value" in total:
        return total["value"]
    return None


def _floor_day(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _as_dt(value: Any) -> datetime | None:
    from .base import _as_datetime

    return _as_datetime(value)


def metric_spec(key: str) -> MetricSpec:
    spec = catalogue_by_key().get(key)
    if spec is None:
        raise KeyError(key)
    return spec
