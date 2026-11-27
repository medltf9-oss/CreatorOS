"""Statistics: correctness, determinism and the sample gates that produce "inconclusive".

A statistics module is easy to write and hard to trust, so these tests check the numbers against
values computed by hand, check that repeated calls are byte-identical, and check that the gates
refuse to produce a verdict from three data points.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from creatoros.db.enums import AnalyticsWindowKind, MetricAvailability, MetricPeriod, Platform
from creatoros.services.analytics import comparison, registry, statistics

NOW = datetime(2026, 11, 24, tzinfo=UTC)


# ------------------------------------------------------------------ Wilson


def test_wilson_interval_matches_a_hand_computed_value():
    # n=100, p=0.5, z=1.96 -> 0.4038 .. 0.5962 (the textbook Wilson example).
    interval = statistics.wilson_interval(50, 100)
    assert interval.low == pytest.approx(0.4038, abs=0.001)
    assert interval.high == pytest.approx(0.5962, abs=0.001)
    assert interval.method == "wilson"
    assert interval.level == 0.95


def test_wilson_interval_stays_inside_zero_and_one_at_the_edges():
    low = statistics.wilson_interval(0, 20)
    high = statistics.wilson_interval(20, 20)
    assert low.low == 0.0
    assert 0.0 < low.high < 1.0
    assert high.high == 1.0
    assert 0.0 < high.low < 1.0


def test_wilson_interval_widens_as_the_sample_shrinks():
    small = statistics.wilson_interval(5, 10)
    large = statistics.wilson_interval(500, 1000)
    assert small.width > large.width


def test_wilson_interval_rejects_impossible_counts():
    with pytest.raises(ValueError):
        statistics.wilson_interval(11, 10)
    with pytest.raises(ValueError):
        statistics.wilson_interval(-1, 10)


def test_an_empty_sample_is_not_a_zero_rate():
    interval = statistics.wilson_interval(0, 0)
    assert interval.low == 0.0
    assert interval.high == 1.0


# ------------------------------------------------------------------ bootstrap


def test_bootstrap_is_deterministic_across_calls():
    values = [10, 12, 9, 15, 11, 13, 14, 8, 12, 16]
    first = statistics.bootstrap_interval(values)
    second = statistics.bootstrap_interval(values)
    assert first is not None and second is not None
    assert (first.low, first.high) == (second.low, second.high)
    assert first.method == "bootstrap_median"


def test_bootstrap_brackets_the_median():
    values = sorted([5, 7, 8, 9, 10, 11, 12, 13, 15, 30])
    interval = statistics.bootstrap_interval(values)
    assert interval is not None
    assert interval.low <= statistics.describe(values).median <= interval.high


def test_bootstrap_returns_none_below_the_gate():
    assert statistics.bootstrap_interval([1, 2, 3]) is None


def test_bootstrap_order_does_not_change_the_result():
    values = [10, 12, 9, 15, 11, 13, 14, 8]
    shuffled = [13, 8, 15, 10, 14, 9, 12, 11]
    a = statistics.bootstrap_interval(values)
    b = statistics.bootstrap_interval(shuffled)
    assert (a.low, a.high) == (b.low, b.high)


# ------------------------------------------------------------------ description


def test_describe_reports_quartiles_only_above_the_gate():
    wide = statistics.describe([float(value) for value in range(1, 21)])
    assert wide.insufficient is False
    assert wide.n == 20
    assert wide.median == 10.5
    assert wide.p25 is not None and wide.p75 is not None
    assert wide.minimum == 1.0 and wide.maximum == 20.0

    narrow = statistics.describe([1.0, 2.0, 3.0])
    assert narrow.insufficient is True
    assert narrow.p25 is None
    assert narrow.notes


def test_describe_of_nothing_says_so():
    empty = statistics.describe([])
    assert empty.n == 0
    assert empty.insufficient is True
    assert empty.median is None


# ------------------------------------------------------------------ verdicts


def test_two_close_rates_are_inconclusive():
    verdict = statistics.compare_proportions(successes_a=50, total_a=100, successes_b=52, total_b=100)
    assert verdict.verdict == statistics.VERDICT_INCONCLUSIVE
    assert verdict.conclusive is False
    assert "overlap" in verdict.reason


def test_two_clearly_different_rates_produce_a_direction():
    verdict = statistics.compare_proportions(successes_a=90, total_a=100, successes_b=20, total_b=100)
    assert verdict.verdict == statistics.VERDICT_A_HIGHER
    assert verdict.conclusive is True
    assert verdict.confidence in {statistics.CONFIDENCE_MEDIUM, statistics.CONFIDENCE_HIGH}


def test_a_small_sample_is_insufficient_not_inconclusive():
    verdict = statistics.compare_proportions(successes_a=3, total_a=3, successes_b=0, total_b=3)
    assert verdict.verdict == statistics.VERDICT_INSUFFICIENT
    assert "below" in verdict.reason


def test_medians_with_overlapping_intervals_are_inconclusive():
    a = [100, 105, 102, 99, 101, 103, 98, 104]
    b = [102, 101, 99, 104, 100, 103, 97, 105]
    verdict = statistics.compare_medians(a, b)
    assert verdict.verdict == statistics.VERDICT_INCONCLUSIVE


def test_medians_with_disjoint_intervals_produce_a_direction():
    a = [10, 11, 12, 13, 14, 15, 16, 17]
    b = [100, 101, 102, 103, 104, 105, 106, 107]
    verdict = statistics.compare_medians(a, b)
    assert verdict.verdict == statistics.VERDICT_B_HIGHER
    assert verdict.confidence in {statistics.CONFIDENCE_MEDIUM, statistics.CONFIDENCE_HIGH}


def test_medians_below_the_gate_are_insufficient():
    verdict = statistics.compare_medians([1, 2, 3], [4, 5, 6])
    assert verdict.verdict == statistics.VERDICT_INSUFFICIENT


def test_confidence_band_is_monotonic():
    assert statistics.confidence_band(2) == statistics.CONFIDENCE_LOW
    assert statistics.confidence_band(12) == statistics.CONFIDENCE_MEDIUM
    assert statistics.confidence_band(60) == statistics.CONFIDENCE_HIGH


def test_evidence_block_carries_measurements_not_a_verdict():
    block = statistics.summary_for_evidence([1, 2, 3, 4, 5, 6, 7, 8], label="views at +24 h")
    assert block["label"] == "views at +24 h"
    assert block["n"] == 8
    assert block["median"] == 4.5
    assert block["interval"]["method"] == "bootstrap_median"
    assert "verdict" not in block


# ------------------------------------------------------------------ comparison


def make_snapshots(values: list[float], *, metric_key: str = "tt.video.views"):
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            id=f"snap-{index}",
            metric_key=metric_key,
            provider_object_id=f"vid-{index}",
            availability=MetricAvailability.OBSERVED,
            unit="count",
            period_kind=MetricPeriod.LIFETIME,
            period_start=None,
            period_end=None,
            value_num=float(value),
            value_json=None,
            collected_at=NOW,
        )
        for index, value in enumerate(values)
    ]


def test_comparison_between_two_groups_reports_a_verdict_and_limitations(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    low = make_snapshots([10, 11, 12, 13, 14, 15, 16, 17])
    high = make_snapshots([100, 101, 102, 103, 104, 105, 106, 107])
    result = comparison.compare_groups(
        session,
        metric_key="tt.video.views",
        window_kind=AnalyticsWindowKind.H24,
        groups={"short": low, "long": high},
    )
    payload = result.as_dict()
    assert payload["sufficient"] is True
    assert payload["verdicts"][0]["verdict"] == statistics.VERDICT_B_HIGHER
    assert any("correlation is not causation" in note for note in payload["limitations"])
    assert payload["groups"][0]["snapshot_ids"]


def test_a_comparison_below_the_gate_is_marked_insufficient(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    result = comparison.compare_groups(
        session,
        metric_key="tt.video.views",
        window_kind=AnalyticsWindowKind.H24,
        groups={"a": make_snapshots([10, 12, 11]), "b": make_snapshots([100, 101, 103])},
    )
    payload = result.as_dict()
    assert payload["sufficient"] is False
    assert payload["verdicts"][0]["verdict"] == statistics.VERDICT_INSUFFICIENT
    assert any("gate" in note for note in payload["limitations"])


def test_too_many_groups_is_refused(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    groups = {f"g{index}": make_snapshots([1, 2, 3]) for index in range(comparison.MAX_GROUPS + 1)}
    with pytest.raises(comparison.ComparisonTooLarge):
        comparison.compare_groups(
            session, metric_key="tt.video.views", window_kind=AnalyticsWindowKind.H24, groups=groups
        )


def test_group_items_are_capped(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    many = make_snapshots([float(index) for index in range(comparison.MAX_ITEMS_PER_GROUP + 20)])
    result = comparison.compare_groups(
        session, metric_key="tt.video.views", window_kind=AnalyticsWindowKind.H24, groups={"big": many}
    )
    assert result.groups[0].n <= comparison.MAX_ITEMS_PER_GROUP


def test_a_point_in_time_metric_gets_no_interval_verdict(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    result = comparison.compare_groups(
        session,
        metric_key="tt.account.followers",
        window_kind=AnalyticsWindowKind.H24,
        groups={"a": make_snapshots([100.0] * 10, metric_key="tt.account.followers"), "b": make_snapshots([200.0] * 10, metric_key="tt.account.followers")},
    )
    assert result.verdicts[0]["verdict"] == statistics.VERDICT_INCONCLUSIVE
    assert "level" in result.verdicts[0]["reason"]


def test_a_structured_metric_cannot_be_compared_as_a_scalar(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    with pytest.raises(ValueError):
        comparison.compare_groups(
            session,
            metric_key="fb.post.video_retention_curve",
            window_kind=AnalyticsWindowKind.H24,
            groups={"a": []},
        )


def test_grouping_keeps_the_latest_observation_per_object():
    rows = make_snapshots([10.0, 20.0])
    rows[1].provider_object_id = rows[0].provider_object_id
    rows[1].collected_at = NOW.replace(hour=20)
    grouped = comparison.group_observations(rows)
    assert len(grouped) == 1
    assert grouped[next(iter(grouped))][0].value_num == 20.0


def test_window_maturation_series_reports_gaps_not_estimates(session):
    from creatoros.db.enums import ConnectionStatus
    from creatoros.db.models import SocialAccount

    registry.ensure_metric_catalog(session)
    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="mature-1",
        display_name="Maturation",
        account_type="tiktok_creator",
        scopes_json=["video.list"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2",
        meta_json={},
    )
    session.add(account)
    session.flush()
    session.commit()
    payload = comparison.compare_windows(
        session,
        account=account,
        provider_object_id="vid-none",
        metric_key="tt.video.views",
        published_at=NOW,
    )
    assert payload["observed_windows"] == 0
    assert all(point["availability"] == "missing" for point in payload["series"])
    assert payload["limitations"]
