"""TikTok Display API adapter: grant separation, cursor paging, no invented metrics.

The tests that matter here are the negative ones. A TikTok account that has authorized `video.list`
but not `user.info.stats` must still get its video numbers, must *not* get zeroed account counters,
and must be told which scope is missing. Watch time and retention do not exist in this API at all,
so they are `not_supported` no matter what is granted.
"""
from __future__ import annotations

import json

import pytest
from social_fixtures import ProviderMock, run

from creatoros.db.enums import AnalyticsObjectType, MetricAvailability, MetricValueKind, Platform
from creatoros.integrations.analytics.base import CollectRequest
from creatoros.integrations.analytics.catalog import catalogue_by_key, metrics_for
from creatoros.integrations.analytics.errors import AnalyticsError
from creatoros.integrations.analytics.tiktok import (
    MAX_PAGE_SIZE,
    TikTokInsightsAdapter,
)

API_VERSION = "v2"
VIDEO_ID = "7300000000000000000"
VIDEO = {
    "id": VIDEO_ID,
    "title": "How we shot this in one take",
    "video_description": "#bts",
    "duration": 34,
    "create_time": 1763500000,
    "share_url": "https://www.tiktok.com/@creator/video/7300000000000000000",
    "view_count": 18200,
    "like_count": 1400,
    "comment_count": 96,
    "share_count": 210,
}


def specs(*keys: str):
    by_key = catalogue_by_key()
    return tuple(by_key[key] for key in keys)


def ok(payload: dict) -> dict:
    return {"data": payload, "error": {"code": "ok", "message": "", "log_id": "test"}}


def tiktok_mock(body: dict | None = None, *, status: int = 200) -> ProviderMock:
    mock = ProviderMock()
    mock.json(r"/user/info/", status, body or ok({"user": {}}))
    mock.json(r"/video/query/", status, body or ok({"videos": [VIDEO], "has_more": False, "cursor": 0}))
    mock.json(r"/video/list/", status, body or ok({"videos": [VIDEO], "has_more": False, "cursor": 0}))
    return mock


def adapter(mock: ProviderMock) -> TikTokInsightsAdapter:
    return TikTokInsightsAdapter(transport=mock.transport("tiktok"))


def request_for(object_type: AnalyticsObjectType, keys: str, *, granted: set[str]) -> CollectRequest:
    return CollectRequest(
        object_id=VIDEO_ID if object_type is AnalyticsObjectType.VIDEO else "open-id-1",
        object_type=object_type,
        specs=specs(*keys.split()),
        granted_scopes=frozenset(granted),
    )


# ------------------------------------------------------------------ account scope split


def test_account_stats_are_not_requested_without_the_stats_scope():
    mock = tiktok_mock(ok({"user": {"open_id": "open-id-1", "display_name": "Creator"}}))
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.ACCOUNT,
                "tt.account.followers tt.account.likes_total tt.account.display_name",
                granted={"user.info.basic"},  # stats deliberately absent
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["tt.account.followers"].availability is MetricAvailability.NOT_AUTHORIZED
    assert by_key["tt.account.followers"].value_num is None
    assert "user.info.stats" in by_key["tt.account.followers"].note
    assert by_key["tt.account.likes_total"].availability is MetricAvailability.NOT_AUTHORIZED
    # The basic field is still collected, and the request must not have asked for counters.
    assert by_key["tt.account.display_name"].availability is MetricAvailability.OBSERVED
    fields = mock.call(r"/user/info/").params["fields"]
    assert "display_name" in fields
    assert "follower_count" not in fields
    assert "likes_count" not in fields


def test_account_stats_are_collected_when_the_stats_scope_is_granted():
    mock = tiktok_mock(
        ok({
            "user": {
                "open_id": "open-id-1",
                "follower_count": 52000,
                "following_count": 310,
                "likes_count": 900000,
                "video_count": 142,
                "display_name": "Creator",
            }
        })
    )
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.ACCOUNT,
                "tt.account.followers tt.account.following tt.account.likes_total tt.account.video_count",
                granted={"user.info.basic", "user.info.stats"},
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    assert {draft.metric_key: draft.value_num for draft in result.drafts} == {
        "tt.account.followers": 52000.0,
        "tt.account.following": 310.0,
        "tt.account.likes_total": 900000.0,
        "tt.account.video_count": 142.0,
    }
    fields = mock.call(r"/user/info/").params["fields"]
    assert set(fields.split(",")) == {"follower_count", "following_count", "likes_count", "video_count"}
    assert all(draft.period_kind.value == "instant" for draft in result.drafts)


def test_missing_counter_field_is_missing_not_zero():
    mock = tiktok_mock(ok({"user": {"open_id": "open-id-1", "follower_count": 10}}))
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.ACCOUNT,
                "tt.account.followers tt.account.video_count",
                granted={"user.info.basic", "user.info.stats"},
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["tt.account.followers"].value_num == 10.0
    assert by_key["tt.account.video_count"].availability is MetricAvailability.MISSING


# ------------------------------------------------------------------ video scope split


def test_video_metrics_are_blocked_without_video_list_and_no_request_is_made():
    mock = tiktok_mock()
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.VIDEO,
                "tt.video.views tt.video.likes",
                granted={"user.info.basic", "user.info.stats"},  # video.list absent
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    assert {draft.availability for draft in result.drafts} == {MetricAvailability.NOT_AUTHORIZED}
    assert all("video.list" in draft.note for draft in result.drafts)
    assert mock.calls == []


def test_video_counters_are_collected_independently_of_the_account_scope():
    mock = tiktok_mock()
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.VIDEO,
                "tt.video.views tt.video.likes tt.video.comments tt.video.shares",
                granted={"video.list"},  # no user.info.stats at all
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    assert {draft.metric_key: draft.value_num for draft in result.drafts} == {
        "tt.video.views": 18200.0,
        "tt.video.likes": 1400.0,
        "tt.video.comments": 96.0,
        "tt.video.shares": 210.0,
    }
    call = mock.call(r"/video/query/")
    assert call.params["fields"].startswith("id,title")
    assert (call.json or {})["filters"]["video_ids"] == [VIDEO_ID]


def test_video_query_targets_one_video_by_id():
    mock = tiktok_mock()
    run(
        adapter(mock).collect(
            request_for(AnalyticsObjectType.VIDEO, "tt.video.views", granted={"video.list"}),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    call = mock.call(r"/video/query/")
    assert call.method == "POST"
    assert "video_ids" in (call.json or {}).get("filters", {})
    assert (call.json or {})["filters"]["video_ids"] == [VIDEO_ID]


def test_unknown_video_is_missing_with_a_warning():
    mock = tiktok_mock(ok({"videos": [], "has_more": False, "cursor": 0}))
    result = run(
        adapter(mock).collect(
            request_for(AnalyticsObjectType.VIDEO, "tt.video.views", granted={"video.list"}),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].availability is MetricAvailability.MISSING
    assert result.warnings


# ------------------------------------------------------------------ impossible metrics


def test_watch_time_and_retention_are_never_requested():
    mock = tiktok_mock()
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.VIDEO,
                "tt.video.views tt.video.watch_time tt.video.retention_curve tt.video.reach",
                granted={"video.list"},
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    for key in ("tt.video.watch_time", "tt.video.retention_curve", "tt.video.reach"):
        assert by_key[key].availability is MetricAvailability.NOT_SUPPORTED
        assert by_key[key].value_num is None
        assert by_key[key].value_json is None
    assert by_key["tt.video.views"].value_num == 18200.0
    assert mock.call(r"/video/query/").params["fields"].count("watch") == 0


def test_catalogue_never_promises_tiktok_metrics_the_api_lacks():
    video_keys = {spec.key for spec in metrics_for(Platform.TIKTOK, AnalyticsObjectType.VIDEO)}
    assert "tt.video.watch_time" in video_keys  # named, so the UI can explain the gap
    assert {spec.key for spec in metrics_for(Platform.TIKTOK, AnalyticsObjectType.ACCOUNT)} == {
        "tt.account.followers",
        "tt.account.following",
        "tt.account.likes_total",
        "tt.account.video_count",
        "tt.account.display_name",
    }


# ------------------------------------------------------------------ pagination and limits


def test_video_list_paging_clamps_the_page_size_and_respects_has_more():
    pages = {"n": 0}

    def handler(request):
        import httpx

        pages["n"] += 1
        payload = json.loads(request.content or b"{}")
        assert payload["max_count"] <= MAX_PAGE_SIZE
        if pages["n"] == 1:
            return httpx.Response(
                200,
                json=ok({"videos": [VIDEO], "has_more": True, "cursor": 1763500000000}),
            )
        return httpx.Response(200, json=ok({"videos": [VIDEO], "has_more": False, "cursor": 0}))

    mock = ProviderMock()
    mock.on(r"/video/list/", handler)
    videos, cursor, has_more = run(
        adapter(mock).list_videos(token="tt-token", api_version=API_VERSION, page_size=200, max_pages=3)
    )
    assert len(videos) == 2
    assert has_more is False
    assert cursor == 0
    assert pages["n"] == 2
    assert len(mock.calls) == 2
    # The second page must carry the cursor from the first.
    assert (mock.calls[1].json or {}).get("cursor") == 1763500000000


def test_page_size_is_clamped_to_the_documented_maximum():
    mock = tiktok_mock()
    run(adapter(mock).list_videos(token="tt-token", api_version=API_VERSION, page_size=99))
    assert (mock.calls[0].json or {})["max_count"] == MAX_PAGE_SIZE


def test_discovery_is_bounded_by_max_pages():
    mock = ProviderMock()
    mock.json(r"/video/list/", 200, ok({"videos": [VIDEO], "has_more": True, "cursor": 123}))
    videos, _cursor, has_more = run(
        adapter(mock).list_videos(token="tt-token", api_version=API_VERSION, max_pages=2)
    )
    assert len(videos) == 2
    assert has_more is True
    assert len(mock.calls) == 2


def test_request_budget_is_enforced_for_one_object():
    mock = tiktok_mock()
    subject = TikTokInsightsAdapter(transport=mock.transport("tiktok"), max_requests=0)
    with pytest.raises(AnalyticsError) as failed:
        run(
            subject.collect(
                request_for(AnalyticsObjectType.VIDEO, "tt.video.views", granted={"video.list"}),
                token="tt-token",
                api_version=API_VERSION,
            )
        )
    assert failed.value.code == "RANGE_TOO_LARGE"
    assert mock.calls == []


# ------------------------------------------------------------------ error mapping


def test_rate_limit_is_retried_then_reported():
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    mock = ProviderMock()
    mock.json(r"/video/query/", 429, {"error": {"code": "rate_limit_exceeded", "message": "slow down"}})
    subject = TikTokInsightsAdapter(transport=mock.transport("tiktok"), sleep=_sleep)
    with pytest.raises(AnalyticsError) as failed:
        run(
            subject.collect(
                request_for(AnalyticsObjectType.VIDEO, "tt.video.views", granted={"video.list"}),
                token="tt-token",
                api_version=API_VERSION,
            )
        )
    assert failed.value.code == "RATE_LIMITED"
    assert len(mock.calls) == 3
    assert len(sleeps) == 2


def test_scope_error_inside_a_200_response_is_not_authorized():
    mock = tiktok_mock({"data": {}, "error": {"code": "scope_not_authorized", "message": "no stats scope"}})
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.ACCOUNT,
                "tt.account.followers",
                granted={"user.info.basic", "user.info.stats"},
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    assert result.drafts[0].availability is MetricAvailability.NOT_AUTHORIZED
    assert "SCOPE_NOT_GRANTED" in result.drafts[0].note


def test_unrecognised_error_code_stops_the_run_instead_of_writing_zeros():
    mock = tiktok_mock({"data": {}, "error": {"code": "internal_error", "message": "something broke"}})
    with pytest.raises(AnalyticsError):
        run(
            adapter(mock).collect(
                request_for(AnalyticsObjectType.VIDEO, "tt.video.views", granted={"video.list"}),
                token="tt-token",
                api_version=API_VERSION,
            )
        )


def test_video_metadata_keeps_native_types():
    mock = tiktok_mock()
    result = run(
        adapter(mock).collect(
            request_for(
                AnalyticsObjectType.VIDEO,
                "tt.video.views tt.video.duration tt.video.created_at",
                granted={"video.list"},
            ),
            token="tt-token",
            api_version=API_VERSION,
        )
    )
    by_key = {draft.metric_key: draft for draft in result.drafts}
    assert by_key["tt.video.duration"].value_num == 34.0
    assert by_key["tt.video.duration"].unit == "seconds"
    assert by_key["tt.video.created_at"].value_num == 1763500000.0
    assert by_key["tt.video.created_at"].unit == "epoch_s"
    assert by_key["tt.video.created_at"].value_kind is MetricValueKind.SCALAR


def test_account_object_type_is_rejected_for_reels():
    mock = tiktok_mock()
    with pytest.raises(AnalyticsError):
        run(
            adapter(mock).collect(
                request_for(AnalyticsObjectType.REEL, "tt.video.views", granted={"video.list"}),
                token="tt-token",
                api_version=API_VERSION,
            )
        )
