"""Comparability rules as a contract: every rule, every refusal code, both directions.

These tests treat a snapshot as a record of facts (metric, class, unit, window, availability) and
check that the guard refuses exactly the pairings that would produce a misleading number — and
accepts the ones that are legitimate, with notes that say what is being compared.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from creatoros.db.enums import MetricAvailability, MetricClass, MetricPeriod, Platform
from creatoros.services.analytics import comparability, registry
from creatoros.services.analytics.comparability import (
    REFUSED_CROSS_PLATFORM,
    REFUSED_DIFFERENT_CLASS,
    REFUSED_DIFFERENT_METRIC,
    REFUSED_DIFFERENT_OBJECT,
    REFUSED_DIFFERENT_UNIT,
    REFUSED_DIFFERENT_WINDOW,
    REFUSED_MISSING_INPUT,
    REFUSED_NOT_OBSERVED,
    REFUSED_OVERLAP,
    REFUSED_UNKNOWN_METRIC,
    REFUSED_ZERO_DENOMINATOR,
)

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def row(**overrides):
    base = {
        "id": "snap-1",
        "metric_key": "tt.video.views",
        "provider_object_id": "vid-1",
        "availability": MetricAvailability.OBSERVED,
        "unit": "count",
        "period_kind": MetricPeriod.LIFETIME,
        "period_start": None,
        "period_end": None,
        "value_num": 100.0,
        "value_json": None,
        "collected_at": T0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def seeded(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    return session


def test_same_metric_same_shape_is_comparable(seeded):
    result = comparability.assert_comparable(seeded, row(), row(value_num=120.0, collected_at=T0 + timedelta(days=1)))
    assert result.ok is True
    assert result.availability is MetricAvailability.OBSERVED
    assert any("metric class" in note for note in result.notes)


def test_an_unknown_metric_is_refused_not_assumed(seeded):
    result = comparability.assert_comparable(seeded, row(metric_key="tt.video.invented"), row(metric_key="tt.video.invented"))
    assert result.ok is False
    assert result.code == REFUSED_UNKNOWN_METRIC


def test_different_metrics_are_refused_even_when_similar(seeded):
    result = comparability.assert_comparable(
        seeded,
        row(metric_key="fb.post.media_views", unit="count"),
        row(metric_key="fb.post.video_views", unit="count"),
    )
    assert result.ok is False
    assert result.code == REFUSED_DIFFERENT_METRIC


def test_different_units_never_get_a_number(seeded):
    result = comparability.assert_comparable(
        seeded,
        row(metric_key="ig.media.avg_watch_time", unit="ms"),
        row(metric_key="ig.media.avg_watch_time", unit="seconds"),
    )
    assert result.ok is False
    assert result.code == REFUSED_DIFFERENT_UNIT


def test_period_metrics_of_different_lengths_are_refused(seeded):
    day = row(
        metric_key="fb.page.media_views",
        period_kind=MetricPeriod.DAY,
        period_start=T0,
        period_end=T0 + timedelta(days=1),
    )
    week = row(
        metric_key="fb.page.media_views",
        period_kind=MetricPeriod.WEEK,
        period_start=T0 + timedelta(days=1),
        period_end=T0 + timedelta(days=8),
    )
    result = comparability.assert_comparable(seeded, day, week)
    assert result.ok is False
    assert result.code == REFUSED_DIFFERENT_WINDOW


def test_overlapping_periods_are_refused_because_summing_double_counts(seeded):
    first = row(
        metric_key="fb.page.media_views",
        period_kind=MetricPeriod.DAYS_28,
        period_start=T0,
        period_end=T0 + timedelta(days=28),
    )
    second = row(
        metric_key="fb.page.media_views",
        period_kind=MetricPeriod.DAYS_28,
        period_start=T0 + timedelta(days=10),
        period_end=T0 + timedelta(days=38),
    )
    result = comparability.assert_comparable(seeded, first, second)
    assert result.ok is False
    assert result.code == REFUSED_OVERLAP


def test_adjacent_periods_are_comparable(seeded):
    first = row(
        metric_key="fb.page.media_views",
        period_kind=MetricPeriod.DAY,
        period_start=T0,
        period_end=T0 + timedelta(days=1),
    )
    second = row(
        metric_key="fb.page.media_views",
        period_kind=MetricPeriod.DAY,
        period_start=T0 + timedelta(days=1),
        period_end=T0 + timedelta(days=2),
        collected_at=T0 + timedelta(days=2),
    )
    assert comparability.assert_comparable(seeded, first, second).ok is True


@pytest.mark.parametrize(
    "state",
    [
        MetricAvailability.MISSING,
        MetricAvailability.BELOW_THRESHOLD,
        MetricAvailability.NOT_AUTHORIZED,
        MetricAvailability.NOT_SUPPORTED,
        MetricAvailability.TEMPORARILY_UNAVAILABLE,
        MetricAvailability.EXPIRED,
        MetricAvailability.NOT_COLLECTED,
    ],
)
def test_any_non_observed_side_is_refused_with_its_own_state(seeded, state):
    result = comparability.assert_comparable(seeded, row(), row(availability=state, value_num=None))
    assert result.ok is False
    assert result.code == REFUSED_NOT_OBSERVED
    assert result.availability is state


def test_series_of_different_lengths_are_refused(seeded):
    left = row(
        metric_key="fb.post.video_retention_curve",
        value_num=None,
        value_json={"points": [1.0] * 40, "bucket_count": 40},
        unit="ratio",
    )
    right = row(
        metric_key="fb.post.video_retention_curve",
        value_num=None,
        value_json={"points": [1.0] * 39, "bucket_count": 39},
        unit="ratio",
    )
    result = comparability.assert_comparable(seeded, left, right)
    assert result.ok is False
    assert result.code == REFUSED_DIFFERENT_WINDOW


def test_series_of_the_same_length_are_comparable(seeded):
    left = row(
        metric_key="fb.post.video_retention_curve",
        value_num=None,
        value_json={"points": [1.0] * 40, "bucket_count": 40},
        unit="ratio",
    )
    assert comparability.assert_comparable(seeded, left, left).ok is True


def test_a_delta_refuses_a_series(seeded):
    left = row(
        metric_key="fb.post.video_retention_curve",
        value_num=None,
        value_json={"points": [1.0] * 40, "bucket_count": 40},
        unit="ratio",
    )
    result = comparability.delta(seeded, left, left)
    assert result.ok is False
    assert result.code == REFUSED_DIFFERENT_WINDOW


def test_a_delta_refuses_two_objects(seeded):
    result = comparability.delta(seeded, row(), row(provider_object_id="vid-2", value_num=200.0))
    assert result.ok is False
    assert result.code == REFUSED_DIFFERENT_OBJECT


def test_a_delta_refuses_a_backwards_collection_time(seeded):
    result = comparability.delta(seeded, row(collected_at=T0 + timedelta(days=2)), row())
    assert result.ok is False
    assert result.code == REFUSED_DIFFERENT_OBJECT


def test_a_point_in_time_change_is_labelled_as_a_level_change(seeded):
    earlier = row(metric_key="tt.account.followers", value_num=1000.0, period_kind=MetricPeriod.INSTANT)
    later = row(
        metric_key="tt.account.followers",
        value_num=1100.0,
        period_kind=MetricPeriod.INSTANT,
        collected_at=T0 + timedelta(days=1),
    )
    result = comparability.delta(seeded, earlier, later)
    assert result.ok is True
    assert result.value == 100.0
    assert any("point-in-time" in note for note in result.notes)


def test_a_rate_needs_two_observed_numbers(seeded):
    numerator = row(metric_key="tt.video.likes", value_num=10.0)
    assert comparability.rate(numerator, None).code == REFUSED_MISSING_INPUT
    assert (
        comparability.rate(numerator, row(availability=MetricAvailability.MISSING, value_num=None)).code
        == REFUSED_NOT_OBSERVED
    )
    assert comparability.rate(numerator, row(value_num=0.0)).code == REFUSED_ZERO_DENOMINATOR


def test_cross_platform_without_a_documented_concept_is_refused():
    result = comparability.cross_platform(
        left_platform=Platform.FACEBOOK,
        left_metric="fb.page.media_views",
        right_platform=Platform.TIKTOK,
        right_metric="tt.video.views",
        left_value=1,
        right_value=2,
    )
    assert result.ok is False
    assert result.code == REFUSED_CROSS_PLATFORM


def test_cross_platform_views_are_refused_even_with_the_concept():
    result = comparability.cross_platform(
        left_platform=Platform.INSTAGRAM,
        left_metric="ig.media.views",
        right_platform=Platform.TIKTOK,
        right_metric="tt.video.views",
        left_value=1,
        right_value=2,
        concept="views",
    )
    assert result.ok is False
    assert "each platform" in result.reason or "means something different" in result.reason


def test_a_refusal_serialises_with_its_code(session):
    payload = comparability.refuse(REFUSED_DIFFERENT_METRIC, "nope").as_dict()
    assert payload["ok"] is False
    assert payload["code"] == REFUSED_DIFFERENT_METRIC
    assert payload["availability"] == "unknown"


def test_class_equality_is_the_only_route_to_a_comparison():
    assert comparability.cells_are_comparable(MetricClass.CUMULATIVE, MetricClass.CUMULATIVE) is True
    assert comparability.cells_are_comparable(MetricClass.CUMULATIVE, MetricClass.PERIOD) is False
    assert comparability.cells_are_comparable(MetricClass.POINT_IN_TIME, MetricClass.CUMULATIVE) is False
    # REFUSED_DIFFERENT_CLASS is reserved for callers that compare across metric keys of the same
    # name space (e.g. two "views" keys with different classes); the code must stay stable.
    assert REFUSED_DIFFERENT_CLASS == "different_metric_class"


def test_every_comparison_path_goes_through_the_guard():
    """A source-level invariant: no analytics service subtracts two snapshot values directly."""
    from pathlib import Path

    import creatoros

    root = Path(creatoros.__file__).parent / "services" / "analytics"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in {"aggregation.py", "comparability.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in ("value_num - ", "- row.value_num", "- snapshot.value_num"):
            if pattern in text:
                offenders.append(f"{path.name}: {pattern}")
    assert offenders == []
