"""Aggregation: windows, totals and the refusals that keep them honest.

Requirement 5 says a change may only be computed between comparable observations, and requirement 6
says an incomparable pairing must be refused rather than rendered. These tests are mostly about the
refusals, because the arithmetic is trivial and the guardrails are not.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from creatoros.db.enums import (
    AnalyticsAggregationMethod,
    AnalyticsWindowKind,
    ConnectionStatus,
    MetricAvailability,
    MetricPeriod,
    Platform,
)
from creatoros.db.models import AnalyticsAggregation, AnalyticsSnapshot
from creatoros.integrations.analytics.base import ObservationDraft
from creatoros.services.analytics import aggregation, comparability, registry, runs, windows
from creatoros.services.analytics.snapshots import SnapshotWriter, attribute

PUBLISHED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def make_account(session, platform: Platform = Platform.TIKTOK):
    from uuid import uuid4

    from creatoros.db.models import SocialAccount

    account = SocialAccount(
        platform=platform,
        provider_account_id=f"agg-{uuid4().hex[:10]}",
        display_name="Aggregate account",
        account_type="tiktok_creator",
        scopes_json=["user.info.basic", "user.info.stats", "video.list"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2" if platform is Platform.TIKTOK else "v23.0",
        meta_json={},
    )
    session.add(account)
    session.flush()
    return account


def write_snapshot(
    session,
    account,
    *,
    metric_key: str,
    value: float | None,
    collected_at: datetime,
    object_id: str = "vid-1",
    availability: MetricAvailability = MetricAvailability.OBSERVED,
    period_kind: MetricPeriod = MetricPeriod.LIFETIME,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
):
    spec = __import__(
        "creatoros.integrations.analytics.catalog", fromlist=["catalogue_by_key"]
    ).catalogue_by_key()[metric_key]
    draft = ObservationDraft(
        metric_key=metric_key,
        provider_metric_name=spec.provider_metric_name,
        availability=availability,
        value_kind=spec.value_kind,
        value_num=value if availability is MetricAvailability.OBSERVED else None,
        period_kind=period_kind,
        period_start=period_start,
        period_end=period_end,
        unit=spec.unit,
        note=None if availability is MetricAvailability.OBSERVED else "not observed",
    )
    writer = SnapshotWriter(session)
    context = runs.open_run(
        session,
        account=account,
        target_kind=__import__(
            "creatoros.db.enums", fromlist=["AnalyticsRunTarget"]
        ).AnalyticsRunTarget.METRICS,
        api_version="v2",
        now=collected_at,
    )
    writer.write(
        run_id=context.run_id,
        account_id=account.id,
        platform=account.platform,
        object_type=__import__(
            "creatoros.db.enums", fromlist=["AnalyticsObjectType"]
        ).AnalyticsObjectType.VIDEO,
        provider_object_id=object_id,
        drafts=[draft],
        api_version="v2",
        api_host="open.tiktokapis.com",
        source_ref="video",
        attribution=attribute(session, platform=account.platform, provider_object_id=object_id, account_id=account.id),
        collected_at=collected_at,
    )
    session.flush()


def snapshot(session, account, object_id="vid-1", metric="tt.video.views"):
    return session.scalar(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot)
        .where(
            AnalyticsSnapshot.account_id == account.id,
            AnalyticsSnapshot.provider_object_id == object_id,
            AnalyticsSnapshot.metric_key == metric,
        )
        .order_by(AnalyticsSnapshot.collected_at)
    )


# ------------------------------------------------------------------ windows


def test_window_specs_carry_documented_tolerances():
    initial = windows.spec_for(AnalyticsWindowKind.INITIAL)
    assert (initial.hours, initial.tolerance_minutes) == (1, 60)
    day = windows.spec_for(AnalyticsWindowKind.H24)
    assert (day.hours, day.tolerance_minutes) == (24, 180)
    month = windows.spec_for(AnalyticsWindowKind.D28)
    assert month.hours == 672 and month.tolerance_minutes == 3 * 24 * 60
    with pytest.raises(ValueError):
        windows.spec_for(AnalyticsWindowKind.CUSTOM)


def test_selection_prefers_the_closest_observation_inside_tolerance(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    target = PUBLISHED + timedelta(hours=24)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=target - timedelta(hours=4))
    write_snapshot(session, account, metric_key="tt.video.views", value=120, collected_at=target + timedelta(minutes=40))
    session.commit()

    selection = windows.select_window(
        session,
        account_id=account.id,
        provider_object_id="vid-1",
        metric_key="tt.video.views",
        window_kind=AnalyticsWindowKind.H24,
        published_at=PUBLISHED,
    )
    assert selection.availability is MetricAvailability.OBSERVED
    assert selection.value_num == 120.0
    assert selection.gap_minutes == 40
    assert "+24 h" in (selection.note or "") or "1440" in (selection.note or "")


def test_selection_refuses_instead_of_using_the_nearest_unrelated_number(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    # 9 hours away from the +24 h target: outside the 3 hour tolerance.
    write_snapshot(
        session,
        account,
        metric_key="tt.video.views",
        value=999,
        collected_at=PUBLISHED + timedelta(hours=33),
    )
    session.commit()
    selection = windows.select_window(
        session,
        account_id=account.id,
        provider_object_id="vid-1",
        metric_key="tt.video.views",
        window_kind=AnalyticsWindowKind.H24,
        published_at=PUBLISHED,
    )
    assert selection.availability is MetricAvailability.MISSING
    assert selection.value_num is None
    assert "tolerance" in (selection.note or "") or "not published" in (selection.note or "")


def test_a_publication_without_a_publish_time_has_no_windows(session):
    account = make_account(session)
    selection = windows.select_window(
        session,
        account_id=account.id,
        provider_object_id="vid-1",
        metric_key="tt.video.views",
        window_kind=AnalyticsWindowKind.H24,
        published_at=None,
    )
    assert selection.availability is MetricAvailability.MISSING
    assert "publish time" in (selection.note or "")


def test_window_for_age_walks_forward():
    assert windows.window_for_age(PUBLISHED, now=PUBLISHED + timedelta(minutes=30)) is None
    assert windows.window_for_age(PUBLISHED, now=PUBLISHED + timedelta(hours=2)) is AnalyticsWindowKind.INITIAL
    assert windows.window_for_age(PUBLISHED, now=PUBLISHED + timedelta(hours=30)) is AnalyticsWindowKind.H24
    assert windows.window_for_age(PUBLISHED, now=PUBLISHED + timedelta(days=8)) is AnalyticsWindowKind.D7


# ------------------------------------------------------------------ class rules


def test_a_cumulative_counter_and_a_period_value_are_not_subtractable(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=PUBLISHED)
    session.commit()
    views = snapshot(session, account)

    definition = session.get(
        __import__("creatoros.db.models", fromlist=["AnalyticsMetricDefinition"]).AnalyticsMetricDefinition,
        "fb.page.media_views",
    )
    assert definition.metric_class.value == "period"

    # A period metric of a different key is refused before class even matters, and the reason names it.
    result = comparability.assert_comparable(session, views, views)
    assert result.ok is True
    other = comparability.assert_comparable(
        session,
        views,
        _synthetic(session, account, metric_key="tt.video.likes", value=5, collected_at=PUBLISHED),
    )
    assert other.ok is False
    assert other.code == comparability.REFUSED_DIFFERENT_METRIC


def _synthetic(session, account, *, metric_key: str, value: float, collected_at: datetime):
    write_snapshot(session, account, metric_key=metric_key, value=value, collected_at=collected_at)
    session.flush()
    return snapshot(session, account, metric=metric_key)


def test_delta_is_positive_growth_for_a_cumulative_counter(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=PUBLISHED)
    write_snapshot(session, account, metric_key="tt.video.views", value=180, collected_at=PUBLISHED + timedelta(hours=24))
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot)
        .where(AnalyticsSnapshot.metric_key == "tt.video.views")
        .order_by(AnalyticsSnapshot.collected_at)
    ).all()
    result = comparability.delta(session, rows[0], rows[1])
    assert result.ok is True
    assert result.value == 80.0
    assert result.availability is MetricAvailability.OBSERVED


def test_a_cumulative_counter_that_goes_down_is_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=200, collected_at=PUBLISHED)
    write_snapshot(session, account, metric_key="tt.video.views", value=150, collected_at=PUBLISHED + timedelta(hours=24))
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot)
        .where(AnalyticsSnapshot.metric_key == "tt.video.views")
        .order_by(AnalyticsSnapshot.collected_at)
    ).all()
    result = comparability.delta(session, rows[0], rows[1])
    assert result.ok is False
    assert "decreased" in result.reason


def test_a_delta_across_two_objects_is_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=PUBLISHED, object_id="vid-1")
    write_snapshot(
        session, account, metric_key="tt.video.views", value=180, collected_at=PUBLISHED + timedelta(hours=1), object_id="vid-2"
    )
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot)
        .where(AnalyticsSnapshot.metric_key == "tt.video.views")
        .order_by(AnalyticsSnapshot.collected_at)
    ).all()
    result = comparability.delta(session, rows[0], rows[1])
    assert result.ok is False
    assert result.code == comparability.REFUSED_DIFFERENT_OBJECT


def test_periods_of_different_length_are_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    day_start = datetime(2026, 11, 1, tzinfo=UTC)
    write_snapshot(
        session,
        account,
        metric_key="fb.page.media_views",
        value=100,
        collected_at=day_start + timedelta(days=1),
        availability=MetricAvailability.OBSERVED,
        period_kind=MetricPeriod.DAY,
        period_start=day_start,
        period_end=day_start + timedelta(days=1),
    )
    week_start = datetime(2026, 11, 2, tzinfo=UTC)
    write_snapshot(
        session,
        account,
        metric_key="fb.page.media_views",
        value=700,
        collected_at=week_start + timedelta(days=7),
        availability=MetricAvailability.OBSERVED,
        period_kind=MetricPeriod.WEEK,
        period_start=week_start,
        period_end=week_start + timedelta(days=7),
    )
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot)
        .where(AnalyticsSnapshot.metric_key == "fb.page.media_views")
        .order_by(AnalyticsSnapshot.collected_at)
    ).all()
    result = comparability.assert_comparable(session, rows[0], rows[1])
    assert result.ok is False
    assert result.code == comparability.REFUSED_DIFFERENT_WINDOW


def test_overlapping_periods_are_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    start = datetime(2026, 11, 1, tzinfo=UTC)
    write_snapshot(
        session,
        account,
        metric_key="fb.page.media_views",
        value=10,
        collected_at=start + timedelta(days=1),
        period_kind=MetricPeriod.DAYS_28,
        period_start=start,
        period_end=start + timedelta(days=28),
    )
    write_snapshot(
        session,
        account,
        metric_key="fb.page.media_views",
        value=11,
        collected_at=start + timedelta(days=2),
        period_kind=MetricPeriod.DAYS_28,
        period_start=start + timedelta(days=1),
        period_end=start + timedelta(days=29),
    )
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot)
        .where(AnalyticsSnapshot.metric_key == "fb.page.media_views")
        .order_by(AnalyticsSnapshot.collected_at)
    ).all()
    result = comparability.assert_comparable(session, rows[0], rows[1])
    assert result.ok is False
    assert result.code == comparability.REFUSED_OVERLAP


def test_a_missing_side_propagates_its_availability(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=PUBLISHED)
    # A different day, so this is a genuinely separate run rather than a resume of the first.
    write_snapshot(
        session,
        account,
        metric_key="tt.video.views",
        value=None,
        collected_at=PUBLISHED + timedelta(days=1),
        availability=MetricAvailability.BELOW_THRESHOLD,
    )
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot)
        .where(AnalyticsSnapshot.metric_key == "tt.video.views")
        .order_by(AnalyticsSnapshot.collected_at)
    ).all()
    result = comparability.assert_comparable(session, rows[0], rows[1])
    assert result.ok is False
    assert result.code == comparability.REFUSED_NOT_OBSERVED
    assert result.availability is MetricAvailability.BELOW_THRESHOLD


def test_a_rate_with_a_zero_denominator_is_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=0, collected_at=PUBLISHED)
    write_snapshot(session, account, metric_key="tt.video.likes", value=4, collected_at=PUBLISHED)
    session.commit()
    views = snapshot(session, account, metric="tt.video.views")
    likes = snapshot(session, account, metric="tt.video.likes")
    result = comparability.rate(likes, views, label="engagement rate")
    assert result.ok is False
    assert result.code == comparability.REFUSED_ZERO_DENOMINATOR


def test_a_rate_is_computed_when_both_inputs_are_observed(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=200, collected_at=PUBLISHED)
    write_snapshot(session, account, metric_key="tt.video.likes", value=50, collected_at=PUBLISHED)
    session.commit()
    result = comparability.rate(
        snapshot(session, account, metric="tt.video.likes"),
        snapshot(session, account, metric="tt.video.views"),
        label="like rate",
        scale=100.0,
    )
    assert result.ok is True
    assert result.value == pytest.approx(25.0)


# ------------------------------------------------------------------ cross-platform


def test_cross_platform_numbers_are_refused_when_the_concept_differs():
    result = comparability.cross_platform(
        left_platform=Platform.FACEBOOK,
        left_metric="fb.post.media_views",
        right_platform=Platform.TIKTOK,
        right_metric="tt.video.views",
        left_value=1000,
        right_value=2000,
        concept=None,
    )
    assert result.ok is False
    assert result.code == comparability.REFUSED_CROSS_PLATFORM


def test_cross_platform_views_are_described_not_ranked():
    """Even the documented concept is not turned into a league table."""
    result = comparability.cross_platform(
        left_platform=Platform.FACEBOOK,
        left_metric="fb.post.media_views",
        right_platform=Platform.INSTAGRAM,
        right_metric="ig.media.views",
        left_value=1000,
        right_value=2000,
        concept="views",
    )
    assert result.ok is False
    assert result.code == comparability.REFUSED_CROSS_PLATFORM
    assert "means something different" in result.reason


def test_cross_platform_follower_totals_are_comparable_with_notes():
    result = comparability.cross_platform(
        left_platform=Platform.FACEBOOK,
        left_metric="fb.page.followers_total",
        right_platform=Platform.TIKTOK,
        right_metric="tt.account.followers",
        left_value=1000,
        right_value=2000,
        concept="followers",
    )
    assert result.ok is True
    assert result.value == -1000.0
    assert any("Page followers" in note for note in result.notes)


# ------------------------------------------------------------------ aggregation storage


def test_publication_aggregation_is_stored_with_its_inputs(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(
        session,
        account,
        metric_key="tt.video.views",
        value=500,
        collected_at=PUBLISHED + timedelta(hours=24),
    )
    session.commit()
    result = aggregation.publication_window_aggregation(
        session,
        account=account,
        provider_object_id="vid-1",
        metric_key="tt.video.views",
        window_kind=AnalyticsWindowKind.H24,
        published_at=PUBLISHED,
    )
    row = aggregation.store(session, account=account, result=result, now=PUBLISHED + timedelta(hours=24))
    session.commit()
    assert row.value_num == 500.0
    assert row.availability is MetricAvailability.OBSERVED
    assert row.method is AnalyticsAggregationMethod.DIRECT
    assert row.input_snapshot_ids_json
    assert row.window_hours == 24
    assert row.aggregation_version == aggregation.AGGREGATION_VERSION


def test_summing_unique_reach_is_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=10, collected_at=PUBLISHED)
    session.commit()
    with pytest.raises(aggregation.NotAggregatable) as refused:
        aggregation.sum_across_objects(
            session,
            account=account,
            metric_key="ig.account.reach",
            snapshots=[snapshot(session, account)],
        )
    assert "unique" in str(refused.value) or "estimate" in str(refused.value)


def test_summing_a_series_is_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    with pytest.raises(aggregation.NotAggregatable):
        aggregation.sum_across_objects(
            session, account=account, metric_key="fb.post.video_retention_curve", snapshots=[]
        )


def test_a_sum_with_one_unobserved_input_carries_the_weakest_state(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=10, collected_at=PUBLISHED, object_id="a")
    write_snapshot(
        session,
        account,
        metric_key="tt.video.views",
        value=None,
        collected_at=PUBLISHED,
        object_id="b",
        availability=MetricAvailability.NOT_AUTHORIZED,
    )
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.metric_key == "tt.video.views"
        )
    ).all()
    result = aggregation.sum_across_objects(
        session, account=account, metric_key="tt.video.views", snapshots=list(rows)
    )
    assert result.availability is MetricAvailability.NOT_AUTHORIZED
    assert result.value_num is None


def test_median_is_used_for_a_typical_value(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    for object_id, value in (("a", 10), ("b", 12), ("c", 5000)):
        write_snapshot(session, account, metric_key="tt.video.views", value=value, collected_at=PUBLISHED, object_id=object_id)
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.metric_key == "tt.video.views"
        )
    ).all()
    result = aggregation.median_across_objects(
        session, metric_key="tt.video.views", snapshots=list(rows)
    )
    assert result.value_num == 12.0
    assert result.confidence["n"] == 3


def test_a_new_aggregation_version_keeps_the_old_rows(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=PUBLISHED + timedelta(hours=1))
    session.commit()
    result = aggregation.publication_window_aggregation(
        session,
        account=account,
        provider_object_id="vid-1",
        metric_key="tt.video.views",
        window_kind=AnalyticsWindowKind.INITIAL,
        published_at=PUBLISHED,
    )
    aggregation.store(session, account=account, result=result, version=1)
    aggregation.store(session, account=account, result=result, version=2)
    session.commit()
    import sqlalchemy as sa

    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsAggregation)) == 2
    assert aggregation.delete_version(session, account=account, version=1) == 1


def test_recompute_writes_publication_windows_for_published_objects(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session)
    fixture = make_published_job(
        session, platform=Platform.TIKTOK, account=account, published_at=PUBLISHED
    )
    write_snapshot(
        session,
        account,
        metric_key="tt.video.views",
        value=300,
        collected_at=PUBLISHED + timedelta(hours=1, minutes=5),
        object_id=fixture.remote_post_id,
    )
    session.commit()
    counts = aggregation.recompute_account(session, account=account)
    session.commit()
    assert counts["written"] >= 1
    rows = aggregation.aggregations_for(session, account=account, metric_key="tt.video.views")
    assert rows
    assert rows[0].publication_key == fixture.job_id


def test_delta_between_windows_uses_the_selected_observations(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=PUBLISHED + timedelta(hours=1))
    write_snapshot(session, account, metric_key="tt.video.views", value=900, collected_at=PUBLISHED + timedelta(hours=24))
    session.commit()
    result = aggregation.delta_between_windows(
        session,
        account=account,
        provider_object_id="vid-1",
        metric_key="tt.video.views",
        earlier=AnalyticsWindowKind.INITIAL,
        later=AnalyticsWindowKind.H24,
        published_at=PUBLISHED,
    )
    assert result.availability is MetricAvailability.OBSERVED
    assert result.value_num == 800.0
    assert result.method is AnalyticsAggregationMethod.DELTA


def test_delta_with_an_uncollected_window_is_refused(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    write_snapshot(session, account, metric_key="tt.video.views", value=100, collected_at=PUBLISHED + timedelta(hours=1))
    session.commit()
    result = aggregation.delta_between_windows(
        session,
        account=account,
        provider_object_id="vid-1",
        metric_key="tt.video.views",
        earlier=AnalyticsWindowKind.INITIAL,
        later=AnalyticsWindowKind.D28,
        published_at=PUBLISHED,
    )
    assert result.availability is MetricAvailability.MISSING
    assert result.value_num is None


def test_period_total_refuses_overlapping_rows(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session)
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for offset in (0, 1):
        write_snapshot(
            session,
            account,
            metric_key="fb.page.media_views",
            value=10,
            collected_at=start + timedelta(days=2 + offset),
            period_kind=MetricPeriod.DAY,
            period_start=start + timedelta(days=offset),
            period_end=start + timedelta(days=offset + 1),
        )
    session.commit()
    rows = session.scalars(
        __import__("sqlalchemy", fromlist=["select"]).select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.metric_key == "fb.page.media_views"
        )
    ).all()
    legal = aggregation.period_bucket_aggregate(list(rows), metric_key="fb.page.media_views", period_kind=MetricPeriod.DAY)
    assert legal.value_num == 20.0
    assert legal.sample_size == 2
