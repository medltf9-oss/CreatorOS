"""Facebook insights adapter: request shape, window splitting, isolation and deprecation.

All responses are hand-written fixtures that mirror the documented payload shape. They are not
recorded provider traffic and the tests say so: nothing here proves a live Page still answers this
way, which is exactly why the live path is a separate, blocked item in the evidence table.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from social_fixtures import ProviderMock, run

from creatoros.db.enums import AnalyticsObjectType, MetricAvailability, MetricPeriod, MetricValueKind
from creatoros.integrations.analytics.base import (
    AnalyticsAdapter,
    CollectRequest,
    ObservationDraft,
    period_bounds,
    version_within,
)
from creatoros.integrations.analytics.catalog import catalogue_by_key, metrics_for
from creatoros.integrations.analytics.errors import AnalyticsError, classify_metric_failure
from creatoros.integrations.analytics.facebook import FacebookInsightsAdapter

API_VERSION = "v23.0"
PAGE_ID = "1234567890"


def page_mock(body: dict | None = None, *, status: int = 200) -> ProviderMock:
    mock = ProviderMock()
    mock.json(r"/insights", status, body or {"data": []})
    return mock


def adapter_for(mock: ProviderMock) -> FacebookInsightsAdapter:
    return FacebookInsightsAdapter(transport=mock.transport("facebook"))


def specs(*keys: str):
    by_key = catalogue_by_key()
    return tuple(by_key[key] for key in keys)


def yesterday_window(days: int = 1) -> tuple[datetime, datetime]:
    until = datetime(2026, 11, 20, tzinfo=UTC)
    return until - timedelta(days=days), until


def daily(values: list[tuple[str, float]], *, name: str) -> dict:
    return {
        "name": name,
        "period": "day",
        "title": name,
        "values": [{"value": value, "end_time": end} for end, value in values],
    }


# ------------------------------------------------------------------ request shape


def test_page_insights_request_uses_documented_parameters():
    mock = page_mock(
        {"data": [daily([("2026-11-19T07:00:00+0000", 12.0)], name="page_media_view")]}
    )
    since, until = yesterday_window()
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.media_views"),
                since=since,
                until=until,
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    call = mock.call(r"/v23\.0/1234567890/insights")
    assert call.params["metric"] == "page_media_view"
    assert call.params["period"] == "day"
    assert call.params["since"] == str(int(since.timestamp()))
    assert call.params["until"] == str(int(until.timestamp()))
    assert call.headers["authorization"] == "Bearer page-token"
    assert result.calls == 1
    draft = result.drafts[0]
    assert draft.availability is MetricAvailability.OBSERVED
    assert draft.value_num == 12.0
    assert draft.period_kind is MetricPeriod.DAY
    assert draft.period_start == datetime(2026, 11, 18, 7, 0, tzinfo=UTC)
    assert draft.period_end == datetime(2026, 11, 19, 7, 0, tzinfo=UTC)


def test_page_node_fields_are_read_from_the_node_not_insights():
    mock = ProviderMock()
    mock.json(r"/v23\.0/1234567890\?", 200, {"id": PAGE_ID, "followers_count": 4321, "fan_count": 4200})
    mock.json(r"/insights", 500, {"error": {"message": "must not be called"}})
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.followers_total", "fb.page.likes_total"),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    assert {draft.metric_key: draft.value_num for draft in result.drafts} == {
        "fb.page.followers_total": 4321.0,
        "fb.page.likes_total": 4200.0,
    }
    assert all(draft.period_kind is MetricPeriod.INSTANT for draft in result.drafts)
    assert not any("/insights" in url for url in mock.urls())


def test_zero_is_a_real_observation_not_a_missing_one():
    """A day with no engagement is 0; the ledger must be able to say so."""
    mock = page_mock({"data": [daily([("2026-11-19T07:00:00+0000", 0.0)], name="page_post_engagements")]})
    since, until = yesterday_window()
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.engagements"),
                since=since,
                until=until,
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].availability is MetricAvailability.OBSERVED
    assert result.drafts[0].value_num == 0.0


# ------------------------------------------------------------------ windows


def test_long_backfill_is_split_into_documented_90_day_windows():
    mock = page_mock({"data": []})
    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime(2026, 10, 1, tzinfo=UTC)  # 273 days
    run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.media_views"),
                since=since,
                until=until,
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    calls = [call for call in mock.calls if "/insights" in call.url]
    assert len(calls) == 4
    for call in calls:
        span = int(call.params["until"]) - int(call.params["since"])
        assert 0 < span <= 90 * 86400
    # Windows are contiguous and cover the request exactly, with no gap and no overlap.
    bounds = [(int(call.params["since"]), int(call.params["until"])) for call in calls]
    assert bounds[0][0] == int(since.timestamp())
    assert bounds[-1][1] == int(until.timestamp())
    assert all(left[1] == right[0] for left, right in itertools.pairwise(bounds))


def test_split_window_rejects_an_inverted_range():
    with pytest.raises(ValueError):
        AnalyticsAdapter.split_window(
            datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)
        )


def test_period_bounds_never_leave_a_gap_for_lifetime():
    assert period_bounds(None, MetricPeriod.LIFETIME) == (None, None)
    assert period_bounds(datetime(2026, 1, 2, tzinfo=UTC), MetricPeriod.DAYS_28) == (
        datetime(2025, 12, 5, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )


# ------------------------------------------------------------------ isolation


def test_metric_omitted_from_the_response_is_missing_not_zero():
    mock = page_mock({"data": [daily([("2026-11-19T07:00:00+0000", 9.0)], name="page_media_view")]})
    since, until = yesterday_window()
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.media_views", "fb.page.video_views"),
                since=since,
                until=until,
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["fb.page.media_views"].value_num == 9.0
    assert by_key["fb.page.video_views"].availability is MetricAvailability.MISSING
    assert by_key["fb.page.video_views"].value_num is None
    assert "not returned" in by_key["fb.page.video_views"].note


def test_empty_object_is_missing_with_a_reason():
    mock = page_mock({"data": [{"name": "page_media_view", "period": "day", "values": [{"value": {}, "end_time": "2026-11-19T07:00:00+0000"}]}]})
    since, until = yesterday_window()
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.media_views"),
                since=since,
                until=until,
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    draft = result.drafts[0]
    assert draft.availability is MetricAvailability.MISSING
    assert "empty object" in draft.note


def test_small_audience_failure_is_below_threshold_for_every_metric_in_the_batch():
    mock = page_mock(
        {"error": {"message": "(#100) Not enough viewers to return this metric", "code": 100}},
        status=400,
    )
    since, until = yesterday_window()
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.media_views", "fb.page.engagements"),
                since=since,
                until=until,
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    assert {draft.availability for draft in result.drafts} == {MetricAvailability.BELOW_THRESHOLD}
    assert all(draft.value_num is None for draft in result.drafts)
    assert result.warnings


def test_permission_failure_is_not_authorized_and_names_the_metric():
    mock = page_mock(
        {"error": {"message": "(#10) Application does not have permission for this action", "code": 10}},
        status=403,
    )
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.video_views"),
                since=datetime(2026, 11, 19, tzinfo=UTC),
                until=datetime(2026, 11, 20, tzinfo=UTC),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].availability is MetricAvailability.NOT_AUTHORIZED
    assert "SCOPE_NOT_GRANTED" in result.drafts[0].note
    assert result.drafts[0].value_num is None


def test_authentication_failure_stops_the_run_instead_of_writing_missing_rows():
    mock = page_mock({"error": {"message": "Invalid OAuth access token", "code": 190}}, status=401)
    with pytest.raises(AnalyticsError) as failed:
        run(
            adapter_for(mock).collect(
                CollectRequest(
                    object_id=PAGE_ID,
                    object_type=AnalyticsObjectType.ACCOUNT,
                    specs=specs("fb.page.media_views"),
                    since=datetime(2026, 11, 19, tzinfo=UTC),
                    until=datetime(2026, 11, 20, tzinfo=UTC),
                ),
                token="expired",
                api_version=API_VERSION,
            )
        )
    assert failed.value.code == "TOKEN_EXPIRED"


def test_rate_limit_is_retried_after_the_provider_retry_after():
    sleeps: list[float] = []

    def handler(request):
        import httpx

        if not sleeps:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            json={"data": [daily([("2026-11-19T07:00:00+0000", 5.0)], name="page_media_view")]},
        )

    mock = ProviderMock()
    mock.on(r"/insights", handler)
    adapter = FacebookInsightsAdapter(transport=mock.transport("facebook"), sleep=_record_sleeps(sleeps))
    result = run(
        adapter.collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("fb.page.media_views"),
                since=datetime(2026, 11, 19, tzinfo=UTC),
                until=datetime(2026, 11, 20, tzinfo=UTC),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    assert sleeps == [7.0]
    assert result.drafts[0].value_num == 5.0
    assert len([call for call in mock.calls if "/insights" in call.url]) == 2


def test_retry_is_bounded_and_never_hammers():
    sleeps: list[float] = []
    mock = ProviderMock()
    mock.json(r"/insights", 429, {"error": {"message": "rate limited"}})
    adapter = FacebookInsightsAdapter(transport=mock.transport("facebook"), sleep=_record_sleeps(sleeps))
    with pytest.raises(AnalyticsError) as failed:
        run(
            adapter.collect(
                CollectRequest(
                    object_id=PAGE_ID,
                    object_type=AnalyticsObjectType.ACCOUNT,
                    specs=specs("fb.page.media_views"),
                    since=datetime(2026, 11, 19, tzinfo=UTC),
                    until=datetime(2026, 11, 20, tzinfo=UTC),
                ),
                token="page-token",
                api_version=API_VERSION,
            )
        )
    assert failed.value.code == "RATE_LIMITED"
    assert len([call for call in mock.calls if "/insights" in call.url]) == 3
    assert len(sleeps) == 2


# ------------------------------------------------------------------ deprecation


def test_metric_above_the_configured_version_is_never_requested():
    mock = page_mock({"data": []})
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("fb.post.impressions", "fb.post.media_views"),
            ),
            token="page-token",
            api_version="v23.0",
        )
    )
    requested = mock.call(r"/insights").params["metric"]
    assert requested == "post_media_view"
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["fb.post.impressions"].availability is MetricAvailability.NOT_SUPPORTED
    assert "v21.0" in by_key["fb.post.impressions"].note


def test_every_metric_is_refused_when_the_version_is_above_all_of_them():
    mock = page_mock({"data": []})
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id=PAGE_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("fb.post.impressions"),
            ),
            token="page-token",
            api_version="v24.0",
        )
    )
    assert result.drafts[0].availability is MetricAvailability.NOT_SUPPORTED
    assert mock.calls == []


def test_version_window_helper_is_exact():
    assert version_within(None, "v24.0", "v23.0") is True
    assert version_within(None, "v24.0", "v25.0") is False
    assert version_within("v22.0", None, "v21.0") is False
    assert version_within(None, None, "not-a-version") is False


# ------------------------------------------------------------------ invariants


def test_drafts_cannot_claim_a_value_they_do_not_have():
    with pytest.raises(ValueError):
        ObservationDraft(
            metric_key="fb.page.media_views",
            provider_metric_name="page_media_view",
            availability=MetricAvailability.OBSERVED,
        )
    with pytest.raises(ValueError):
        ObservationDraft(
            metric_key="fb.page.media_views",
            provider_metric_name="page_media_view",
            availability=MetricAvailability.MISSING,
            value_num=0.0,
        )
    with pytest.raises(ValueError):
        ObservationDraft(
            metric_key="fb.page.retention",
            provider_metric_name="curve",
            availability=MetricAvailability.OBSERVED,
            value_kind=MetricValueKind.SERIES,
            value_num=1.0,
        )


def test_structured_metric_keeps_its_shape():
    """A 40-point retention curve is stored whole, never averaged into a float."""
    curve = [50 - index for index in range(40)]
    mock = page_mock({"data": [{"name": "post_video_retention_graph", "period": "lifetime", "values": [{"value": curve}]}]})
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id="999_888",
                object_type=AnalyticsObjectType.POST,
                specs=specs("fb.post.video_retention_curve"),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    draft = result.drafts[0]
    assert draft.availability is MetricAvailability.OBSERVED
    assert draft.value_kind is MetricValueKind.SERIES
    assert draft.value_num is None
    assert draft.value_json["points"] == curve
    assert draft.value_json["bucket_count"] == 40
    assert draft.value_json["unit"] == "ratio"
    assert "incomplete" not in draft.value_json


def test_preferred_metric_selection_is_not_deprecated():
    for spec in metrics_for(__import__("creatoros.db.enums", fromlist=["Platform"]).Platform.FACEBOOK):
        if spec.preferred_for:
            assert not spec.is_deprecated, spec.key


def test_adapter_modules_do_not_import_sqlalchemy():
    import ast
    from pathlib import Path

    import creatoros

    root = Path(creatoros.__file__).parent / "integrations" / "analytics"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: {alias.name}" for alias in node.names if alias.name.startswith("sqlalchemy")]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
                offenders.append(f"{path.name}: {node.module}")
    assert offenders == []


def test_classification_of_provider_failures_is_conservative():
    # An unrecognised body is not "probably rate limiting".
    assert classify_metric_failure(provider="facebook", status=400, body={"error": {"message": "Unknown"}}) is None
    assert (
        classify_metric_failure(provider="facebook", status=400, body={"error": {"message": "#100 not enough data"}}).availability
        is MetricAvailability.BELOW_THRESHOLD
    )
    assert (
        classify_metric_failure(provider="tiktok", status=429, body={"error": {"code": "rate_limit_exceeded"}}).availability
        is MetricAvailability.TEMPORARILY_UNAVAILABLE
    )


def _record_sleeps(sink: list[float]):
    async def _sleep(seconds: float) -> None:
        sink.append(seconds)

    return _sleep


# ------------------------------------------------------------------ post, video, reels (P8.6)


def test_post_engagement_comes_from_the_node_and_insights_from_insights():
    """`shares`/`reactions`/`comments` are Post-node fields; impressions are insights."""
    mock = ProviderMock()
    mock.json(r"/v23\.0/123_456(\?|$)", 200, {
        "id": "123_456",
        "shares": {"count": 3},
        "reactions": {"summary": {"total_count": 41}},
        "comments": {"summary": {"total_count": 7}},
    })
    mock.json(r"/v23\.0/123_456/insights", 200, {
        "data": [
            {"name": "post_media_view", "period": "lifetime", "values": [{"value": 900}]},
            {"name": "post_clicks_by_type", "period": "lifetime", "values": [{"value": {"link clicks": 12, "other clicks": 3}}]},
        ]
    })
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id="123_456",
                object_type=AnalyticsObjectType.POST,
                specs=specs(
                    "fb.post.shares",
                    "fb.post.reactions_total",
                    "fb.post.comments",
                    "fb.post.media_views",
                    "fb.post.clicks_by_type",
                ),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["fb.post.shares"].value_num == 3.0
    assert by_key["fb.post.reactions_total"].value_num == 41.0
    assert by_key["fb.post.comments"].value_num == 7.0
    assert by_key["fb.post.media_views"].value_num == 900.0
    assert by_key["fb.post.clicks_by_type"].value_json == {"link clicks": 12.0, "other clicks": 3.0}
    node_call = mock.call(r"/v23\.0/123_456$")
    assert "shares" in node_call.params["fields"]
    assert "reactions.summary(true).limit(0)" in node_call.params["fields"]
    # The node request must not ask for insights metrics, and vice versa.
    assert "post_media_view" not in node_call.params["fields"]


def test_reshared_video_metric_that_returns_zero_is_recorded_as_zero():
    """Facebook documents 0 for `post_video_avg_time_watched` on reshared videos: a real zero."""
    mock = ProviderMock()
    mock.json(r"/video_insights", 200, {
        "data": [{"name": "post_video_avg_time_watched", "period": "lifetime", "values": [{"value": 0}]}]
    })
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id="video-1",
                object_type=AnalyticsObjectType.VIDEO,
                specs=specs("fb.reel.avg_watch_time"),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    draft = result.drafts[0]
    assert draft.availability is MetricAvailability.OBSERVED
    assert draft.value_num == 0.0
    assert draft.unit == "ms"


def test_reels_metrics_are_routed_to_the_reel_object_type():
    mock = ProviderMock()
    mock.json(r"/video_insights", 200, {
        "data": [
            {"name": "blue_reels_play_count", "period": "lifetime", "values": [{"value": 1200}]},
            {"name": "fb_reels_replay_count", "period": "lifetime", "values": [{"value": 340}]},
        ]
    })
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id="reel-1",
                object_type=AnalyticsObjectType.REEL,
                specs=specs("fb.reel.plays", "fb.reel.replays"),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    keys = {draft.metric_key for draft in result.drafts}
    assert keys == {"fb.reel.plays", "fb.reel.replays"}
    assert all(catalogue_by_key()[key].object_type is AnalyticsObjectType.REEL for key in keys)
    assert mock.call(r"/video_insights").params["metric"] == "blue_reels_play_count,fb_reels_replay_count"


def test_video_insights_use_the_documented_endpoint_and_lifetime_period():
    mock = ProviderMock()
    mock.json(r"/video_insights", 200, {
        "data": [{"name": "total_video_view_total_time", "period": "lifetime", "values": [{"value": 540000}]}]
    })
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id="video-9",
                object_type=AnalyticsObjectType.VIDEO,
                specs=specs("fb.video.view_total_time"),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    call = mock.call(r"/video_insights")
    assert call.url.endswith("/v23.0/video-9/video_insights")
    assert call.params["period"] == "lifetime"
    assert "since" not in call.params
    assert result.drafts[0].period_kind is MetricPeriod.LIFETIME
    assert result.drafts[0].period_start is None


def test_reactions_breakdown_keeps_each_reaction_type():
    mock = ProviderMock()
    mock.json(r"/video_insights", 200, {
        "data": [{
            "name": "post_video_likes_by_reaction_type",
            "period": "lifetime",
            "values": [{"value": {"like": 30, "love": 12, "wow": 2}}],
        }]
    })
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id="reel-2",
                object_type=AnalyticsObjectType.REEL,
                specs=specs("fb.reel.likes"),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    draft = result.drafts[0]
    assert draft.value_kind is MetricValueKind.BREAKDOWN
    assert draft.value_num is None
    assert draft.value_json == {"like": 30.0, "love": 12.0, "wow": 2.0}


def test_short_retention_curve_is_kept_and_flagged_not_padded():
    short = [40, 30, 20]
    mock = ProviderMock()
    mock.json(r"/video_insights", 200, {
        "data": [{"name": "post_video_retention_graph", "period": "lifetime", "values": [{"value": short}]}]
    })
    result = run(
        adapter_for(mock).collect(
            CollectRequest(
                object_id="video-3",
                object_type=AnalyticsObjectType.VIDEO,
                specs=specs("fb.reel.retention_curve"),
            ),
            token="page-token",
            api_version=API_VERSION,
        )
    )
    draft = result.drafts[0]
    assert draft.availability is MetricAvailability.OBSERVED
    assert draft.value_json["points"] == [40.0, 30.0, 20.0]
    assert draft.value_json["bucket_count"] == 3
    assert draft.value_json["expected_buckets"] == 40
    assert draft.value_json["incomplete"] is True
    assert "3 of 40" in draft.note


def test_normalisation_never_turns_a_boolean_or_text_into_a_number():
    from creatoros.integrations.analytics.normalize import as_number

    assert as_number(True) is None
    assert as_number("41") == 41.0
    assert as_number("many") is None
    assert as_number({"value": 7}) == 7.0
    assert as_number(None) is None


def test_series_map_with_numeric_keys_is_ordered_by_value_not_by_text():
    from creatoros.integrations.analytics.normalize import normalize_series

    payload = normalize_series({"10": 5, "2": 9, "100": 1})
    assert payload["points"] == [9.0, 5.0, 1.0]
    assert payload["bucket_count"] == 3
