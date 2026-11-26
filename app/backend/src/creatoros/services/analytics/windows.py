"""Performance windows: which observation represents "+24 h", and how far off it may be.

Providers do not answer at the moment you ask. Instagram documents up to 48 hours of delay, Meta's
Page insights update roughly daily, and a queue can be backed up. So a window is a *target* plus a
documented tolerance, and the row that represents it is the observation closest to the target that
falls inside that tolerance.

If nothing falls inside the tolerance the window is ``missing`` with the gap recorded — never the
nearest available number. "This is the +24 h figure, measured at +31 h" is a legitimate statement;
"this is the +24 h figure" when it is really +31 h is not.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import AnalyticsWindowKind, MetricAvailability, MetricPeriod
from ...db.models import AnalyticsSnapshot

#: Target offset and tolerance, in minutes, per window. Documented provider delay sets the tolerance.
WINDOW_SPECS: dict[AnalyticsWindowKind, tuple[int, int]] = {
    AnalyticsWindowKind.INITIAL: (60, 60),
    AnalyticsWindowKind.H24: (24 * 60, 180),
    AnalyticsWindowKind.H48: (48 * 60, 360),
    AnalyticsWindowKind.D7: (24 * 60 * 7, 18 * 60),
    AnalyticsWindowKind.D28: (24 * 60 * 28, 3 * 24 * 60),
}

#: The order windows are evaluated in; also the order a publication matures through.
WINDOW_ORDER: tuple[AnalyticsWindowKind, ...] = (
    AnalyticsWindowKind.INITIAL,
    AnalyticsWindowKind.H24,
    AnalyticsWindowKind.H48,
    AnalyticsWindowKind.D7,
    AnalyticsWindowKind.D28,
)


@dataclass(frozen=True, slots=True)
class WindowSpec:
    kind: AnalyticsWindowKind
    hours: int
    tolerance_minutes: int

    def target_for(self, published_at: datetime) -> datetime:
        return published_at + timedelta(hours=self.hours)

    def bounds_for(self, published_at: datetime) -> tuple[datetime, datetime]:
        target = self.target_for(published_at)
        return (target - timedelta(minutes=self.tolerance_minutes), target + timedelta(minutes=self.tolerance_minutes))


def spec_for(window_kind: AnalyticsWindowKind) -> WindowSpec:
    if window_kind not in WINDOW_SPECS:
        raise ValueError(f"no documented tolerance for window {window_kind.value!r}")
    hours = WINDOW_SPECS[window_kind][0] // 60
    return WindowSpec(kind=window_kind, hours=hours, tolerance_minutes=WINDOW_SPECS[window_kind][1])


@dataclass(frozen=True, slots=True)
class WindowSelection:
    """The observation that represents a window, with the truth about when it was taken."""

    window_kind: AnalyticsWindowKind
    metric_key: str
    availability: MetricAvailability
    snapshot_id: str | None = None
    value_num: float | None = None
    value_json: dict | list | None = None
    collected_at: datetime | None = None
    gap_minutes: int | None = None
    note: str | None = None

    @property
    def observed(self) -> bool:
        return self.availability is MetricAvailability.OBSERVED


def select_window(
    session: Session,
    *,
    account_id: str,
    provider_object_id: str,
    metric_key: str,
    window_kind: AnalyticsWindowKind,
    published_at: datetime | None,
) -> WindowSelection:
    """The closest in-tolerance observation for one metric at one window."""
    spec = spec_for(window_kind)
    if published_at is None:
        return WindowSelection(
            window_kind=window_kind,
            metric_key=metric_key,
            availability=MetricAvailability.MISSING,
            note="the publication has no provider publish time, so no window can be anchored",
        )
    target = spec.target_for(published_at)
    lower, upper = spec.bounds_for(published_at)
    rows = list(
        session.scalars(
            sa.select(AnalyticsSnapshot).where(
                AnalyticsSnapshot.account_id == account_id,
                AnalyticsSnapshot.provider_object_id == provider_object_id,
                AnalyticsSnapshot.metric_key == metric_key,
                AnalyticsSnapshot.collected_at >= lower,
                AnalyticsSnapshot.collected_at <= upper,
                AnalyticsSnapshot.availability == MetricAvailability.OBSERVED,
            )
        )
    )
    if not rows:
        return WindowSelection(
            window_kind=window_kind,
            metric_key=metric_key,
            availability=MetricAvailability.MISSING,
            note=(
                f"no observation within ±{spec.tolerance_minutes} min of +{spec.hours} h; "
                "the provider had not published the value yet"
            ),
        )
    best = min(rows, key=lambda row: abs((row.collected_at - target).total_seconds()))
    gap = int((best.collected_at - target).total_seconds() // 60)
    return WindowSelection(
        window_kind=window_kind,
        metric_key=metric_key,
        availability=MetricAvailability.OBSERVED,
        snapshot_id=best.id,
        value_num=best.value_num,
        value_json=best.value_json,
        collected_at=best.collected_at,
        gap_minutes=gap,
        note=f"measured at +{spec.hours * 60 + gap} min, target +{spec.hours * 60} min",
    )


def window_for_age(published_at: datetime, *, now: datetime | None = None) -> AnalyticsWindowKind | None:
    """The newest window whose target has passed: what a publication is currently "at"."""
    moment = now or datetime.now(UTC)
    chosen: AnalyticsWindowKind | None = None
    for window_kind in WINDOW_ORDER:
        if spec_for(window_kind).target_for(published_at) <= moment:
            chosen = window_kind
    return chosen


def assess_comparability_of_windows(
    left: AnalyticsWindowKind,
    right: AnalyticsWindowKind,
) -> tuple[bool, str]:
    """Two windows of different length are not a like-for-like comparison."""
    if left is right:
        return (True, "")
    left_hours = spec_for(left).hours
    right_hours = spec_for(right).hours
    if left_hours == right_hours:
        return (True, "")
    return (
        False,
        f"+{left_hours} h and +{right_hours} h are different windows: compare the same window on both objects",
    )


def period_length_days(period_kind: MetricPeriod) -> int | None:
    """How long a provider aggregation window is, so two of them can be checked for equality."""
    return {
        MetricPeriod.DAY: 1,
        MetricPeriod.WEEK: 7,
        MetricPeriod.DAYS_28: 28,
        MetricPeriod.MONTH: 30,
    }.get(period_kind)


def overlaps(a_start: datetime | None, a_end: datetime | None, b_start: datetime | None, b_end: datetime | None) -> bool:
    """Do two period rows cover any of the same time? Summing those would double count."""
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start < b_end and b_start < a_end


def summarise(selections: list[WindowSelection]) -> dict:
    """Availability summary for a set of windows, weakest state wins."""
    counts: dict[str, int] = {}
    for selection in selections:
        counts[selection.availability.value] = counts.get(selection.availability.value, 0) + 1
    return {"counts": counts, "worst": weakest(selection.availability for selection in selections).value}


#: Weakness ranking: `observed` is strongest, and any refusal outranks a gap.
_WEAKNESS = {
    MetricAvailability.OBSERVED: 0,
    MetricAvailability.EXPIRED: 1,
    MetricAvailability.NOT_COLLECTED: 2,
    MetricAvailability.MISSING: 3,
    MetricAvailability.TEMPORARILY_UNAVAILABLE: 4,
    MetricAvailability.BELOW_THRESHOLD: 5,
    MetricAvailability.NOT_AUTHORIZED: 6,
    MetricAvailability.NOT_SUPPORTED: 7,
    MetricAvailability.REDACTED_BY_POLICY: 8,
    MetricAvailability.UNKNOWN: 9,
}


def weakest(states) -> MetricAvailability:
    """The availability a combined figure must carry: never better than its worst input."""
    return max(states, key=lambda state: _WEAKNESS.get(state, 9), default=MetricAvailability.UNKNOWN)


