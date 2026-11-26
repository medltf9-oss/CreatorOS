"""Instagram adapter: two login paths, two metric types, product gating, no fake zeros.

Fixtures are hand-written from the documented payload shapes. They prove the adapter's decisions,
not that a live professional account returns them; that is the blocked category D/F item.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from social_fixtures import ProviderMock, run

from creatoros.db.enums import AnalyticsObjectType, MetricAvailability, MetricPeriod, MetricValueKind
from creatoros.integrations.analytics.base import CollectRequest
from creatoros.integrations.analytics.catalog import catalogue_by_key
from creatoros.integrations.analytics.instagram import (
    IG_HOST_FACEBOOK_LOGIN,
    IG_HOST_INSTAGRAM_LOGIN,
    LOGIN_FACEBOOK,
    LOGIN_INSTAGRAM,
    InstagramInsightsAdapter,
)

API_VERSION = "v23.0"
IG_USER = "17841400000000000"
MEDIA_ID = "17900000000000000"


def specs(*keys: str):
    by_key = catalogue_by_key()
    return tuple(by_key[key] for key in keys)


def ig_mock(body: dict | None = None, *, status: int = 200) -> ProviderMock:
    mock = ProviderMock()
    mock.json(r"/insights", status, body or {"data": []})
    return mock


def adapter(mock: ProviderMock, login_path: str = LOGIN_INSTAGRAM) -> InstagramInsightsAdapter:
    return InstagramInsightsAdapter(transport=mock.transport("instagram"), login_path=login_path)


def window() -> tuple[datetime, datetime]:
    return datetime(2026, 11, 19, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC)


def series(name: str, value: float, end: str = "2026-11-19T08:00:00+0000") -> dict:
    return {"name": name, "period": "day", "values": [{"value": value, "end_time": end}]}


# ------------------------------------------------------------------ hosts


def test_host_follows_the_login_path():
    since, until = window()
    for login_path, host in (
        (LOGIN_INSTAGRAM, IG_HOST_INSTAGRAM_LOGIN),
        (LOGIN_FACEBOOK, IG_HOST_FACEBOOK_LOGIN),
    ):
        mock = ig_mock({"data": [series("views", 10.0)]})
        result = run(
            adapter(mock, login_path).collect(
                CollectRequest(
                    object_id=IG_USER,
                    object_type=AnalyticsObjectType.ACCOUNT,
                    specs=specs("ig.account.views"),
                    since=since,
                    until=until,
                ),
                token="ig-token",
                api_version=API_VERSION,
            )
        )
        assert result.drafts[0].value_num == 10.0
        assert mock.calls[0].url.startswith(host)
        assert mock.calls[0].url.endswith(f"/{API_VERSION}/{IG_USER}/insights")


def test_unknown_login_path_is_rejected():
    with pytest.raises(ValueError):
        InstagramInsightsAdapter(login_path="graph_api_guess")


# ------------------------------------------------------------------ metric types


def test_account_daily_metrics_use_time_series_with_since_until():
    mock = ig_mock({"data": [series("reach", 321.0)]})
    since, until = window()
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=IG_USER,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("ig.account.reach"),
                since=since,
                until=until,
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    call = mock.calls[0]
    assert call.params["metric_type"] == "time_series"
    assert call.params["period"] == "day"
    assert call.params["since"] == str(int(since.timestamp()))
    assert call.params["until"] == str(int(until.timestamp()))
    draft = result.drafts[0]
    assert draft.period_kind is MetricPeriod.DAY
    assert draft.period_start == datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
    assert draft.period_end == datetime(2026, 11, 19, 8, 0, tzinfo=UTC)


def test_lifetime_metrics_use_total_value_and_no_window():
    mock = ig_mock({
        "data": [{
            "name": "follows_and_unfollows",
            "period": "lifetime",
            "total_value": {
                "breakdowns": [{
                    "dimension_keys": ["follow_type"],
                    "results": [
                        {"dimension_values": ["FOLLOWER"], "value": 12},
                        {"dimension_values": ["NON_FOLLOWER"], "value": 5},
                    ],
                }]
            },
        }]
    })
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=IG_USER,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("ig.account.follows_unfollows"),
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    call = mock.calls[0]
    assert call.params["metric_type"] == "total_value"
    assert "since" not in call.params and "until" not in call.params
    draft = result.drafts[0]
    assert draft.value_kind is MetricValueKind.BREAKDOWN
    assert draft.value_json == {"FOLLOWER": 12.0, "NON_FOLLOWER": 5.0}
    assert draft.period_kind is MetricPeriod.LIFETIME


def test_mixed_account_request_splits_into_two_calls():
    """Daily metrics and lifetime metrics cannot share one request: two calls, not one wrong one."""
    mock = ProviderMock()
    mock.json(r"metric_type=time_series", 200, {"data": [series("reach", 100.0)]})
    mock.json(r"metric_type=total_value", 200, {
        "data": [{"name": "profile_links_taps", "period": "lifetime", "total_value": {"value": 3}}]
    })
    since, until = window()
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=IG_USER,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("ig.account.reach", "ig.account.profile_link_taps"),
                since=since,
                until=until,
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert len(mock.calls) == 2
    observed = {draft.metric_key: draft for draft in result.observed()}
    assert observed["ig.account.reach"].value_num == 100.0
    # A breakdown metric answered with a single number keeps the number under `total`, because
    # inventing dimension keys would be a fabricated breakdown.
    assert observed["ig.account.profile_link_taps"].value_json == {"total": 3.0}


def test_user_node_fields_are_instant_and_not_requested_from_insights():
    mock = ProviderMock()
    mock.json(rf"/{API_VERSION}/{IG_USER}\?", 200, {
        "id": IG_USER,
        "followers_count": 8400,
        "follows_count": 210,
        "media_count": 96,
    })
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=IG_USER,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("ig.account.followers", "ig.account.follows_count", "ig.account.media_count"),
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert {draft.metric_key: draft.value_num for draft in result.drafts} == {
        "ig.account.followers": 8400.0,
        "ig.account.follows_count": 210.0,
        "ig.account.media_count": 96.0,
    }
    assert all(draft.period_kind is MetricPeriod.INSTANT for draft in result.drafts)
    assert not any("/insights" in url for url in mock.urls())


# ------------------------------------------------------------------ media and product types


def test_media_metrics_use_total_value():
    mock = ig_mock({
        "data": [
            {"name": "views", "period": "lifetime", "total_value": {"value": 5200}},
            {"name": "saved", "period": "lifetime", "total_value": {"value": 41}},
        ]
    })
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("ig.media.views", "ig.media.saved"),
                product_type="FEED",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert mock.calls[0].params["metric_type"] == "total_value"
    assert {draft.metric_key: draft.value_num for draft in result.drafts} == {
        "ig.media.views": 5200.0,
        "ig.media.saved": 41.0,
    }


def test_reels_only_metric_is_refused_for_a_feed_post_without_a_request():
    mock = ig_mock({"data": [{"name": "views", "period": "lifetime", "total_value": {"value": 10}}]})
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("ig.media.views", "ig.media.avg_watch_time"),
                product_type="FEED",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["ig.media.avg_watch_time"].availability is MetricAvailability.NOT_SUPPORTED
    assert "REELS" in by_key["ig.media.avg_watch_time"].note
    assert mock.calls[0].params["metric"] == "views"


def test_reels_metric_is_collected_for_a_reel():
    mock = ig_mock({"data": [{"name": "ig_reels_avg_watch_time", "period": "lifetime", "total_value": {"value": 8400}}]})
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.REEL,
                specs=specs("ig.media.avg_watch_time"),
                product_type="REELS",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].value_num == 8400.0
    assert result.drafts[0].unit == "ms"


def test_story_metric_is_refused_for_a_reel():
    mock = ig_mock({"data": []})
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.STORY,
                specs=specs("ig.media.link_clicks"),
                product_type="REELS",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].availability is MetricAvailability.NOT_SUPPORTED
    assert mock.calls == []


def test_total_metrics_require_facebook_login_and_say_so():
    since_calls: list[str] = []

    # Instagram Login: refused with the scope path named, no request attempted for that metric.
    mock = ig_mock({"data": [{"name": "views", "period": "lifetime", "total_value": {"value": 5}}]})
    result = run(
        adapter(mock, LOGIN_INSTAGRAM).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("ig.media.views", "ig.media.total_views"),
                product_type="FEED",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["ig.media.total_views"].availability is MetricAvailability.NOT_AUTHORIZED
    assert "Facebook Login" in by_key["ig.media.total_views"].note
    assert mock.calls[0].params["metric"] == "views"

    # Facebook Login: the same metric is requested.
    mock2 = ig_mock({
        "data": [
            {"name": "views", "period": "lifetime", "total_value": {"value": 5}},
            {"name": "total_views", "period": "lifetime", "total_value": {"value": 77}},
        ]
    })
    result2 = run(
        adapter(mock2, LOGIN_FACEBOOK).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("ig.media.views", "ig.media.total_views"),
                product_type="FEED",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert "total_views" in mock2.calls[0].params["metric"]
    assert {draft.metric_key: draft.value_num for draft in result2.observed()}["ig.media.total_views"] == 77.0
    assert since_calls == []


# ------------------------------------------------------------------ availability


def test_empty_data_set_becomes_missing_not_zero():
    mock = ig_mock({"data": []})
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("ig.media.reach"),
                product_type="FEED",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    draft = result.drafts[0]
    assert draft.availability is MetricAvailability.MISSING
    assert draft.value_num is None
    assert "empty data set" in draft.note


def test_a_present_zero_is_observed_zero():
    mock = ig_mock({"data": [{"name": "reposts", "period": "lifetime", "total_value": {"value": 0}}]})
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("ig.media.reposts"),
                product_type="FEED",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].availability is MetricAvailability.OBSERVED
    assert result.drafts[0].value_num == 0.0


def test_not_enough_viewers_is_below_threshold():
    mock = ig_mock({"error": {"message": "Not enough viewers to return insights", "code": 100}}, status=400)
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.STORY,
                specs=specs("ig.media.replies"),
                product_type="STORIES",
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].availability is MetricAvailability.BELOW_THRESHOLD
    assert result.drafts[0].note.startswith("INSUFFICIENT_VIEWERS")


def test_permission_error_names_the_scope():
    mock = ig_mock(
        {"error": {"message": "The user does not have permission for this action", "code": 10}},
        status=403,
    )
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=IG_USER,
                object_type=AnalyticsObjectType.ACCOUNT,
                specs=specs("ig.account.reach"),
                since=datetime(2026, 11, 19, tzinfo=UTC),
                until=datetime(2026, 11, 20, tzinfo=UTC),
            ),
            token="ig-token",
            api_version=API_VERSION,
        )
    )
    draft = result.drafts[0]
    assert draft.availability is MetricAvailability.NOT_AUTHORIZED
    assert "SCOPE_NOT_GRANTED" in draft.note


def test_metric_above_the_configured_version_is_not_requested():
    mock = ig_mock({"data": []})
    result = run(
        adapter(mock).collect(
            CollectRequest(
                object_id=MEDIA_ID,
                object_type=AnalyticsObjectType.POST,
                specs=specs("ig.media.impressions", "ig.media.views"),
                product_type="FEED",
            ),
            token="ig-token",
            api_version="v22.0",
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["ig.media.impressions"].availability is MetricAvailability.NOT_SUPPORTED
    assert mock.calls[0].params["metric"] == "views"
