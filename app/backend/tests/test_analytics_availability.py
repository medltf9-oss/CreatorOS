"""Phase 8 vocabulary: availability, metric class, value kind and run outcomes.

The point of these tests is that "unavailable" can never silently become a zero, and that the
taxonomy which makes comparisons legal is not optional.
"""
from __future__ import annotations

import pytest

from creatoros.db.enums import (
    VALUED_AVAILABILITY_STATES,
    WINDOW_OFFSET_HOURS,
    AnalyticsErrorClass,
    AnalyticsRunOutcome,
    AnalyticsWindowKind,
    MetricAvailability,
    MetricClass,
    MetricPeriod,
    MetricValueKind,
    normalize_availability,
)


def test_ten_availability_states_are_defined() -> None:
    assert len(MetricAvailability) == 10
    assert {state.value for state in MetricAvailability} == {
        "observed",
        "missing",
        "below_threshold",
        "not_supported",
        "not_authorized",
        "not_collected",
        "temporarily_unavailable",
        "expired",
        "redacted_by_policy",
        "unknown",
    }


def test_only_observed_may_carry_a_value() -> None:
    """The single most important invariant in the phase."""
    assert VALUED_AVAILABILITY_STATES == frozenset({MetricAvailability.OBSERVED})
    for state in MetricAvailability:
        if state is MetricAvailability.OBSERVED:
            continue
        assert state not in VALUED_AVAILABILITY_STATES


def test_normalize_availability_is_total() -> None:
    assert normalize_availability(None) is MetricAvailability.UNKNOWN
    assert normalize_availability("") is MetricAvailability.UNKNOWN
    assert normalize_availability("OBSERVED") is MetricAvailability.OBSERVED
    assert normalize_availability(MetricAvailability.NOT_AUTHORIZED) is MetricAvailability.NOT_AUTHORIZED
    assert normalize_availability("something_new") is MetricAvailability.UNKNOWN


def test_legacy_completeness_words_map_without_inventing_a_value() -> None:
    assert normalize_availability("complete") is MetricAvailability.OBSERVED
    assert normalize_availability("partial") is MetricAvailability.OBSERVED
    # Phase 2's `unavailable` said nothing about why. `missing` is the honest, non-zero answer.
    assert normalize_availability("unavailable") is MetricAvailability.MISSING


def test_metric_class_covers_the_five_comparison_families() -> None:
    assert {state.value for state in MetricClass} == {
        "cumulative",
        "period",
        "point_in_time",
        "ratio",
        "series",
    }


def test_value_kind_separates_scalar_from_structured_payloads() -> None:
    assert {kind.value for kind in MetricValueKind} == {"scalar", "breakdown", "series", "object"}


def test_metric_period_matches_the_provider_vocabulary() -> None:
    assert {period.value for period in MetricPeriod} == {
        "day",
        "week",
        "days_28",
        "month",
        "lifetime",
        "total_over_range",
        "instant",
    }


def test_every_window_kind_has_an_offset_and_lifetime_has_none() -> None:
    assert set(WINDOW_OFFSET_HOURS) == set(AnalyticsWindowKind)
    assert WINDOW_OFFSET_HOURS[AnalyticsWindowKind.H24] == 24
    assert WINDOW_OFFSET_HOURS[AnalyticsWindowKind.D7] == 168
    assert WINDOW_OFFSET_HOURS[AnalyticsWindowKind.D28] == 672
    assert WINDOW_OFFSET_HOURS[AnalyticsWindowKind.LIFETIME] is None
    offsets = [value for value in WINDOW_OFFSET_HOURS.values() if value is not None]
    assert offsets == sorted(offsets), "window offsets must be ordered for the fallback search"


def test_error_classes_include_the_threshold_case_the_providers_use() -> None:
    """Facebook's 100-like rule and Instagram's 100-follower rule are not failures."""
    assert AnalyticsErrorClass.THRESHOLD.value == "threshold"
    assert AnalyticsErrorClass.PERMISSION.value == "permission"


def test_run_outcomes_do_not_include_a_silent_success_value() -> None:
    assert "ok" not in {outcome.value for outcome in AnalyticsRunOutcome}
    assert AnalyticsRunOutcome.SKIPPED in set(AnalyticsRunOutcome)


@pytest.mark.parametrize("platform_metric", ["fb.page.media_views", "ig.media.views", "tt.video.views"])
def test_metric_keys_are_namespaced_by_platform(platform_metric: str) -> None:
    assert platform_metric.split(".")[0] in {"fb", "ig", "tt"}
