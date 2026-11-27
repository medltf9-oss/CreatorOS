"""TikTok Display API adapter — two grants, two endpoints, no invented metrics.

The Display API exposes far less than Meta, and the boundary matters more than the data:

* ``user.info.stats`` gives four account counters (``follower_count``, ``following_count``,
  ``likes_count``, ``video_count``). Without that grant they are ``not_authorized`` with the scope
  named — never ``0``, and never silently dropped.
* ``video.list`` gives per-video ``view_count``, ``like_count``, ``comment_count``, ``share_count``
  plus metadata. It is independent of the account grant: a user can grant one and refuse the other.
* Watch time, retention, replays, saves, reach and unique viewers are **not exposed by any approved
  TikTok API**. The catalogue marks them ``not_supported`` and the adapter never requests them. The
  TikTok Research API is not a substitute for creator analytics and is not used anywhere.

Every response carries ``error.code``; anything other than ``"ok"`` is classified before parsing, so
a scope problem can never be read as "the video has no views". ``video/list`` pages by cursor
(UTC milliseconds) with ``max_count`` ≤ 20, and the adapter counts its own requests so a runaway
pagination cannot burn the 600/minute budget.
"""
from __future__ import annotations

from typing import Any

from ...db.enums import AnalyticsObjectType, MetricAvailability, MetricPeriod
from .base import (
    AnalyticsAdapter,
    CollectRequest,
    CollectResult,
    from_failure,
    graph_value,
    skipped,
)
from .catalog import MetricSpec
from .errors import RUN_LEVEL_CODES, AnalyticsError, classify_metric_failure

TIKTOK_API = "https://open.tiktokapis.com"
SCOPE_STATS = "user.info.stats"
SCOPE_BASIC = "user.info.basic"
SCOPE_VIDEO_LIST = "video.list"

#: `max_count` is documented as 1..20 per page.
MAX_PAGE_SIZE = 20
#: A single collect for one video must not turn into a crawl.
MAX_REQUESTS_PER_COLLECT = 5

STATS_FIELDS = ("follower_count", "following_count", "likes_count", "video_count")
BASIC_FIELDS = ("open_id", "union_id", "avatar_url", "display_name", "username", "profile_deep_link", "is_verified")
VIDEO_FIELDS = (
    "id",
    "title",
    "video_description",
    "duration",
    "cover_image_url",
    "share_url",
    "create_time",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
)


class TikTokInsightsAdapter(AnalyticsAdapter):
    platform_name = "tiktok"

    def __init__(
        self,
        transport=None,
        *,
        base_url: str = TIKTOK_API,
        max_requests: int = MAX_REQUESTS_PER_COLLECT,
        **kwargs,
    ) -> None:
        super().__init__(transport, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.requests_made = 0
        self.max_requests = max_requests

    def _url(self, api_version: str, path: str) -> str:
        return f"{self.base_url}/{api_version}{path}"

    # ------------------------------------------------------------------ dispatch
    async def collect(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        """Reset the per-object request counter, then collect."""
        self.requests_made = 0
        return await super().collect(request, token=token, api_version=api_version)

    async def _collect(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        if request.object_type is AnalyticsObjectType.ACCOUNT:
            return await self.collect_account(request, token=token, api_version=api_version)
        if request.object_type is AnalyticsObjectType.VIDEO:
            return await self.collect_video(request, token=token, api_version=api_version)
        raise AnalyticsError(
            "METRIC_UNAVAILABLE",
            f"tiktok has no analytics surface for {request.object_type.value}",
            detail={"object_type": request.object_type.value},
        )

    # ------------------------------------------------------------------ account
    async def collect_account(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        granted = set(request.granted_scopes)
        stats_specs = [spec for spec in request.specs if SCOPE_STATS in _scopes(spec)]
        basic_specs = [spec for spec in request.specs if SCOPE_STATS not in _scopes(spec)]
        result = CollectResult()

        if stats_specs and SCOPE_STATS not in granted:
            for spec in stats_specs:
                result.drafts.append(
                    skipped(
                        spec,
                        MetricAvailability.NOT_AUTHORIZED,
                        "missing scope user.info.stats: the account statistics grant was not approved",
                    )
                )
            stats_specs = []

        if basic_specs and SCOPE_BASIC not in granted and not granted:
            for spec in basic_specs:
                result.drafts.append(
                    skipped(spec, MetricAvailability.NOT_AUTHORIZED, "missing scope user.info.basic")
                )
            basic_specs = []

        if not (stats_specs or basic_specs):
            return result

        wanted = [spec.provider_metric_name for spec in stats_specs + basic_specs]
        fields = ",".join(field for field in (*BASIC_FIELDS, *STATS_FIELDS) if field in wanted)
        if not fields:
            return result
        body = await self._request(
            "GET",
            self._url(api_version, "/user/info/"),
            token=token,
            params={"fields": fields},
            specs=stats_specs + basic_specs,
        )
        if isinstance(body, CollectResult):
            result.extend(body)
            return result
        user = (body.get("data") or {}).get("user") or {}
        for spec in stats_specs + basic_specs:
            result.drafts.append(
                graph_value(
                    spec=spec,
                    metric_key=spec.key,
                    raw=user.get(spec.provider_metric_name),
                    period_kind=MetricPeriod.INSTANT,
                    period_end=self.now(),
                )
            )
        return result

    # ------------------------------------------------------------------ video
    async def collect_video(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        granted = set(request.granted_scopes)
        if SCOPE_VIDEO_LIST not in granted:
            return CollectResult(
                drafts=[
                    skipped(spec, MetricAvailability.NOT_AUTHORIZED, "missing scope video.list")
                    for spec in request.specs
                ]
            )
        body = await self._request(
            "POST",
            self._url(api_version, "/video/query/"),
            token=token,
            params={"fields": ",".join(VIDEO_FIELDS)},
            payload={"filters": {"video_ids": [request.object_id]}, "max_count": 1},
            specs=list(request.specs),
        )
        if isinstance(body, CollectResult):
            return body
        videos = (body.get("data") or {}).get("videos") or []
        if not videos:
            # Asked for one video id and got nothing: the post is gone, private or not ours.
            return CollectResult(
                drafts=[
                    skipped(spec, MetricAvailability.MISSING, "tiktok returned no video for this id")
                    for spec in request.specs
                ],
                warnings=["tiktok video/query returned no video for the requested id"],
                calls=1,
            )
        return CollectResult(drafts=self._video_drafts(videos[0], request.specs), calls=1)

    async def list_videos(
        self,
        *,
        token: str,
        api_version: str,
        cursor: int | None = None,
        max_pages: int = 1,
        page_size: int = MAX_PAGE_SIZE,
    ) -> tuple[list[dict], int | None, bool]:
        """Reverse-chronological discovery, bounded on purpose.

        `max_count` is clamped to the documented 20 and `max_pages` bounds a single call, because a
        discovery sweep must not become an accidental full-account crawl.
        """
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        videos: list[dict] = []
        next_cursor = cursor
        has_more = False
        for _ in range(max(1, max_pages)):
            payload: dict[str, Any] = {"max_count": page_size}
            if next_cursor:
                payload["cursor"] = int(next_cursor)
            body = await self._request(
                "POST",
                self._url(api_version, "/video/list/"),
                token=token,
                params={"fields": ",".join(VIDEO_FIELDS)},
                payload=payload,
                specs=[],
            )
            if isinstance(body, CollectResult):  # pragma: no cover - discovery has no specs
                break
            data = body.get("data") or {}
            videos.extend(data.get("videos") or [])
            next_cursor = data.get("cursor")
            has_more = bool(data.get("has_more"))
            if not has_more:
                break
        return videos, next_cursor, has_more

    def _video_drafts(self, video: dict, specs: list[MetricSpec]) -> list:
        drafts = []
        for spec in specs:
            raw = video.get(spec.provider_metric_name)
            drafts.append(
                graph_value(
                    spec=spec,
                    metric_key=spec.key,
                    raw=raw,
                    period_kind=MetricPeriod.LIFETIME,
                    period_end=None,
                )
            )
        return drafts

    # ------------------------------------------------------------------ transport
    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict | None = None,
        payload: dict | None = None,
        specs: list[MetricSpec],
    ) -> dict | CollectResult:
        if self.requests_made >= self.max_requests:
            raise AnalyticsError(
                "RANGE_TOO_LARGE",
                f"tiktok collection exceeded {self.max_requests} requests for one object",
                http_status=429,
            )
        self.requests_made += 1
        try:
            response = await self._send(method, url, token=token, params=params, payload=payload)
        except AnalyticsError as error:
            if error.code in RUN_LEVEL_CODES:
                raise
            failure = classify_metric_failure(
                provider="tiktok",
                status=error.http_status,
                body=error.detail.get("body") or {"error": {"message": error.message}},
                metric_name=specs[0].key if specs else "",
            )
            if failure is None:
                raise
            return CollectResult(
                drafts=[from_failure(spec, failure) for spec in specs],
                warnings=[f"{error.code}: {error.message}"],
                calls=1,
            )
        body = response.body if isinstance(response.body, dict) else {}
        error_block = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = str(error_block.get("code") or "ok").lower()
        if code != "ok":
            failure = _classify_tiktok_error(code, error_block)
            if failure is None:
                raise AnalyticsError(
                    "PROVIDER_ERROR",
                    str(error_block.get("message") or f"tiktok error {code}")[:300],
                    detail={"provider_code": code},
                )
            return CollectResult(
                drafts=[from_failure(spec, failure) for spec in specs],
                warnings=[f"tiktok {code}: {error_block.get('message', '')}"[:300]],
                calls=1,
            )
        return body

    async def _send(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict | None,
        payload: dict | None,
    ):
        """One request with the same bounded retry policy as the base adapter."""
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            response = await self.transport.request(
                method, url, params=params, json=payload, headers=headers, timeout=30.0
            )
            if response.status < 400:
                return response
            error = _as_error(response)
            last = error
            if response.status == 429 or response.status >= 500:
                if attempt == self.max_attempts:
                    raise error
                await self._sleep(self._delay(error, attempt))
                continue
            raise error
        raise last or AnalyticsError("PROVIDER_ERROR", "tiktok request failed")


def _as_error(response):
    from .errors import as_error

    return as_error("tiktok", response.status, response.body)


def _classify_tiktok_error(code: str, error_block: dict):
    from .errors import MetricFailure

    if code in {"scope_not_authorized", "scope_permission_missed"}:
        return MetricFailure(MetricAvailability.NOT_AUTHORIZED, "SCOPE_NOT_GRANTED", "the token lacks this scope")
    if code in {"rate_limit_exceeded", "spam_risk_too_many_posts"}:
        return MetricFailure(
            MetricAvailability.TEMPORARILY_UNAVAILABLE, "RATE_LIMITED", "tiktok is throttling this app"
        )
    if code in {"video_not_found", "invalid_params"}:
        return MetricFailure(MetricAvailability.MISSING, "OBJECT_NOT_FOUND", "tiktok did not return this object")
    return None


def _scopes(spec: MetricSpec) -> tuple[str, ...]:
    permissions = spec.required_permissions
    if isinstance(permissions, dict):
        return tuple(scope for alternative in permissions.get("any_of") or [] for scope in alternative)
    return tuple(permissions or ())


