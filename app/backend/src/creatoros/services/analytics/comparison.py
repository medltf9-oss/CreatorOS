"""Comparison: put several groups of publications side by side without lying about them.

A comparison here is a *report*, not a ranking. It returns, for each group, the distribution of one
metric at one window; the pairwise verdicts (with Wilson/bootstrap intervals); the comparability
refusals that stopped a pair from being compared at all; and the limitations a reader needs.

Caps are deliberate. Above them the answer is "too many groups" rather than a table nobody can read,
and each group keeps the ids of the rows it was built from so any cell can be traced back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ...db.enums import AnalyticsWindowKind, MetricAvailability, MetricClass
from ...db.models import AnalyticsMetricDefinition, AnalyticsSnapshot, SocialAccount
from . import comparability, statistics, windows
from .windows import weakest

#: A comparison wider than this is a data export, not a chart.
MAX_GROUPS = 5
#: Items per group, newest first. Beyond this the median stops describing the group anyway.
MAX_ITEMS_PER_GROUP = 50


class ComparisonTooLarge(ValueError):
    code = "COMPARISON_TOO_LARGE"


@dataclass(slots=True)
class GroupComparison:
    label: str
    n: int
    availability: MetricAvailability
    description: statistics.Description
    snapshot_ids: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "n": self.n,
            "availability": self.availability.value,
            "description": self.description.as_dict(),
            "snapshot_ids": self.snapshot_ids,
            "refusals": self.refusals,
        }


@dataclass(slots=True)
class Comparison:
    metric_key: str
    window_kind: AnalyticsWindowKind
    groups: list[GroupComparison]
    verdicts: list[dict]
    limitations: list[str]
    comparability: dict
    sufficient: bool

    def as_dict(self) -> dict:
        return {
            "metric_key": self.metric_key,
            "window_kind": self.window_kind.value,
            "groups": [group.as_dict() for group in self.groups],
            "verdicts": self.verdicts,
            "limitations": self.limitations,
            "comparability": self.comparability,
            "sufficient": self.sufficient,
        }


def group_observations(
    snapshots: list[AnalyticsSnapshot],
    *,
    key: str = "provider_object_id",
) -> dict[str, list[AnalyticsSnapshot]]:
    """One value per object: the latest observed row wins, so a re-collection does not double count."""
    grouped: dict[str, dict[str, AnalyticsSnapshot]] = {}
    for snapshot in snapshots:
        name = str(getattr(snapshot, key, "") or "")
        if not name:
            continue
        seen = grouped.setdefault(name, {})
        current = seen.get(snapshot.metric_key)
        if current is None or snapshot.collected_at >= current.collected_at:
            seen[snapshot.metric_key] = snapshot
    return {
        name: sorted(rows.values(), key=lambda row: row.metric_key)
        for name, rows in grouped.items()
    }


def compare_groups(
    session,
    *,
    metric_key: str,
    window_kind: AnalyticsWindowKind,
    groups: dict[str, list[AnalyticsSnapshot]],
    label: str | None = None,
) -> Comparison:
    """Compare the same metric at the same window across up to `MAX_GROUPS` groups."""
    if len(groups) > MAX_GROUPS:
        raise ComparisonTooLarge(
            f"{len(groups)} groups exceeds the {MAX_GROUPS} this comparison supports; split the request"
        )
    definition: AnalyticsMetricDefinition | None = session.get(AnalyticsMetricDefinition, metric_key)
    if definition is None:
        raise ValueError(f"{metric_key} is not in the metric registry")
    if definition.value_kind.value != "scalar":
        raise ValueError(
            f"{metric_key} is a {definition.value_kind.value} metric; compare it with a series comparison"
        )

    prepared: list[GroupComparison] = []
    values_by_label: dict[str, list[float]] = {}
    refusals: list[str] = []
    for name, snapshots in groups.items():
        selected = snapshots[:MAX_ITEMS_PER_GROUP]
        observed = [
            snapshot
            for snapshot in selected
            if snapshot.metric_key == metric_key and snapshot.availability is MetricAvailability.OBSERVED
        ]
        availability = weakest([snapshot.availability for snapshot in selected] or [MetricAvailability.MISSING])
        numbers = [float(snapshot.value_num) for snapshot in observed if snapshot.value_num is not None]
        values_by_label[name] = numbers
        for left, right in _pairs(selected):
            verdict = comparability.assert_comparable(session, left, right)
            if not verdict.ok and verdict.code not in refusals:
                refusals.append(verdict.code)
        prepared.append(
            GroupComparison(
                label=name,
                n=len(numbers),
                availability=availability,
                description=statistics.describe(numbers),
                snapshot_ids=[snapshot.id for snapshot in observed],
                refusals=[],
            )
        )

    verdicts: list[dict] = []
    labels = list(values_by_label)
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            if definition.metric_class is MetricClass.POINT_IN_TIME:
                verdicts.append(
                    {
                        "a": left,
                        "b": right,
                        "verdict": statistics.VERDICT_INCONCLUSIVE,
                        "reason": "a point-in-time value is a level, not a sample: no interval applies",
                    }
                )
                continue
            verdict = statistics.compare_medians(values_by_label[left], values_by_label[right], label=metric_key)
            verdicts.append({"a": left, "b": right, **verdict.as_dict()})

    limitations = _limitations(definition, prepared, window_kind)
    return Comparison(
        metric_key=metric_key,
        window_kind=window_kind,
        groups=prepared,
        verdicts=verdicts,
        limitations=limitations,
        comparability={
            "refusals": refusals,
            "checked_pairs": sum(1 for _ in _pairs(next(iter(groups.values())))) if groups else 0,
        },
        sufficient=all(group.description.n >= statistics.MIN_PUBLICATIONS_PER_GROUP for group in prepared),
    )


def compare_windows(
    session,
    *,
    account: SocialAccount,
    provider_object_id: str,
    metric_key: str,
    published_at: datetime | None,
    window_kinds: list[AnalyticsWindowKind] | None = None,
    label: str | None = None,
) -> dict:
    """How one publication matured across windows — a trend, never a "performance score"."""
    chosen = window_kinds or list(windows.WINDOW_ORDER[:4])
    series = []
    for window_kind in chosen:
        selection = windows.select_window(
            session,
            account_id=account.id,
            provider_object_id=provider_object_id,
            metric_key=metric_key,
            window_kind=window_kind,
            published_at=published_at,
        )
        series.append(
            {
                "window": window_kind.value,
                "hours": windows.spec_for(window_kind).hours,
                "availability": selection.availability.value,
                "value": selection.value_num,
                "gap_minutes": selection.gap_minutes,
            }
        )
    return {
        "metric_key": metric_key,
        "label": label or provider_object_id,
        "series": series,
        "observed_windows": sum(1 for point in series if point["availability"] == "observed"),
        "limitations": [
            "windows of different length are not a like-for-like comparison",
            "a missing window means the provider had not published the value inside the tolerance",
        ],
    }


def _pairs(items: list[AnalyticsSnapshot]):
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            yield (left, right)


def _limitations(
    definition: AnalyticsMetricDefinition,
    groups: list[GroupComparison],
    window_kind: AnalyticsWindowKind,
) -> list[str]:
    notes = ["correlation is not causation: these groups differ in more than the metric being compared"]
    if window_kind in windows.WINDOW_SPECS:
        spec = windows.spec_for(window_kind)
        notes.insert(0, f"window: +{spec.hours} h with a ±{spec.tolerance_minutes} min tolerance")
    if definition.is_estimate:
        notes.append("the provider marks this metric as an estimate")
    if definition.is_deprecated:
        notes.append("this metric is deprecated by the provider and will stop being collected")
    short = [group.label for group in groups if group.n < statistics.MIN_PUBLICATIONS_PER_GROUP]
    if short:
        notes.append(
            f"below the {statistics.MIN_PUBLICATIONS_PER_GROUP}-publication gate: {', '.join(short)}"
        )
    unavailable = [
        group.label for group in groups if group.availability is not MetricAvailability.OBSERVED
    ]
    if unavailable:
        notes.append(f"not fully observed: {', '.join(unavailable)}")
    return notes
