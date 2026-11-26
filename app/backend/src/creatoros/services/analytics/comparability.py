"""Comparability: the guard between two numbers and a sentence about them.

Every comparison in Phase 8 — post against post, week against week, platform against platform — goes
through `assert_comparable()` or one of the typed helpers here, and gets back either a value with its
caveats or a **typed refusal**. Nothing in this module returns a best guess.

The rules, and why each one exists:

* **Same metric.** `fb.post.media_views` and `fb.post.impressions` are not the same thing; the second
  was retired and counting them as equal is how a chart lies.
* **Same class** (approval requirement 5). A cumulative counter and a 28-day period value cannot be
  subtracted; a point-in-time snapshot is not a period total.
* **Same window length.** A 7-day value against a 28-day value is not a comparison, it is arithmetic.
* **No overlapping periods.** Two rows covering the same days would double count when summed.
* **Both observed.** If either side is `below_threshold`, `not_authorized` or `missing`, the
  comparison is refused with that state rather than computed from the one number that exists.
* **Same unit.** Milliseconds against counts produces a number and no meaning.
* **Cross-platform only by documented concept.** Facebook "view" is 3 seconds, Instagram "view" is a
  play, TikTok "view" is a play after impression. They are comparable *as a concept* only where the
  catalogue says so, and never as an exact number.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ...db.enums import AnalyticsWindowKind, MetricAvailability, MetricClass, MetricPeriod, Platform
from ...db.models import AnalyticsMetricDefinition, AnalyticsSnapshot
from .windows import period_length_days, weakest

#: Stable refusal codes, shown verbatim in the UI and stored on recommendations.
REFUSED_DIFFERENT_METRIC = "different_metric"
REFUSED_DIFFERENT_CLASS = "different_metric_class"
REFUSED_DIFFERENT_WINDOW = "different_window_length"
REFUSED_OVERLAP = "overlapping_periods"
REFUSED_NOT_OBSERVED = "not_observed"
REFUSED_DIFFERENT_UNIT = "different_unit"
REFUSED_DIFFERENT_OBJECT = "different_object"
REFUSED_DIFFERENT_PLATFORM = "different_platform"
REFUSED_ZERO_DENOMINATOR = "zero_denominator"
REFUSED_MISSING_INPUT = "missing_input"
REFUSED_UNKNOWN_METRIC = "unknown_metric"
REFUSED_CROSS_PLATFORM = "cross_platform_not_comparable"

#: The only cross-platform equivalences Phase 8 is willing to state, and what each one hides.
CROSS_PLATFORM_CONCEPTS: dict[str, dict[Platform, str]] = {
    "views": {
        Platform.FACEBOOK: "a play of at least 3 seconds (or nearly the whole video, if shorter)",
        Platform.INSTAGRAM: "a play, with replays counted separately by Meta",
        Platform.TIKTOK: "a play counted after the video starts",
    },
    "followers": {
        Platform.FACEBOOK: "Page followers, which Meta counts separately from Page likes",
        Platform.INSTAGRAM: "accounts following the professional account",
        Platform.TIKTOK: "accounts following the TikTok profile",
    },
}

#: Concepts whose numbers are directionally useful but not numerically identical.
CROSS_PLATFORM_NOT_NUMERIC = frozenset({"views", "reach"})


@dataclass(frozen=True, slots=True)
class Comparable:
    """The answer: either a comparison with caveats, or a refusal with a code."""

    ok: bool
    code: str = ""
    reason: str = ""
    value: float | None = None
    availability: MetricAvailability = MetricAvailability.OBSERVED
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "value": self.value,
            "availability": self.availability.value,
            "notes": list(self.notes),
        }


def refuse(code: str, reason: str, *, availability: MetricAvailability = MetricAvailability.UNKNOWN) -> Comparable:
    return Comparable(ok=False, code=code, reason=reason, availability=availability)


def _definition(session, metric_key: str) -> AnalyticsMetricDefinition | None:
    return session.get(AnalyticsMetricDefinition, metric_key)


def assert_comparable(
    session,
    left: AnalyticsSnapshot,
    right: AnalyticsSnapshot,
) -> Comparable:
    """Can these two observations be put in the same sentence?"""
    if left.metric_key != right.metric_key:
        return refuse(
            REFUSED_DIFFERENT_METRIC,
            f"{left.metric_key} and {right.metric_key} are different metrics",
        )
    definition = _definition(session, left.metric_key)
    if definition is None:
        return refuse(REFUSED_UNKNOWN_METRIC, f"{left.metric_key} is not in the metric registry")

    if not (left.availability is MetricAvailability.OBSERVED and right.availability is MetricAvailability.OBSERVED):
        worst = weakest([left.availability, right.availability])
        return refuse(
            REFUSED_NOT_OBSERVED,
            "one side was not observed, so there is nothing to compare",
            availability=worst,
        )
    if left.unit != right.unit:
        return refuse(REFUSED_DIFFERENT_UNIT, f"units differ: {left.unit} vs {right.unit}")
    if definition.metric_class is MetricClass.PERIOD:
        left_days = period_length_days(left.period_kind)
        right_days = period_length_days(right.period_kind)
        if left_days != right_days:
            return refuse(
                REFUSED_DIFFERENT_WINDOW,
                f"period lengths differ: {left.period_kind.value} vs {right.period_kind.value}",
            )
        if left.period_start and right.period_start and left.period_start < right.period_end and right.period_start < left.period_end:  # type: ignore[operator]
            return refuse(
                REFUSED_OVERLAP,
                "the two periods overlap, so summing or averaging them would double count",
            )
    if definition.metric_class is MetricClass.SERIES:
        left_buckets = (left.value_json or {}).get("bucket_count") if isinstance(left.value_json, dict) else None
        right_buckets = (right.value_json or {}).get("bucket_count") if isinstance(right.value_json, dict) else None
        if left_buckets != right_buckets:
            return refuse(
                REFUSED_DIFFERENT_WINDOW,
                f"series lengths differ: {left_buckets} vs {right_buckets} buckets",
            )
    return Comparable(
        ok=True,
        availability=weakest([left.availability, right.availability]),
        notes=(f"metric class: {definition.metric_class.value}",),
    )


def delta(
    session,
    earlier: AnalyticsSnapshot,
    later: AnalyticsSnapshot,
) -> Comparable:
    """A change between two observations, or a refusal.

    A delta is only meaningful for the *same object*, in the same class, with both sides observed.
    For a cumulative counter the delta is the growth between two collections; for a point-in-time
    value it is a change over the elapsed time and is labelled as such; for a period value the two
    windows must be the same length and must not overlap.
    """
    comparable = assert_comparable(session, earlier, later)
    if not comparable.ok:
        return comparable
    if earlier.provider_object_id != later.provider_object_id:
        return refuse(
            REFUSED_DIFFERENT_OBJECT,
            "a change is only defined for the same object across time",
        )
    definition = _definition(session, earlier.metric_key)
    if definition is None:  # pragma: no cover - assert_comparable already refused
        return refuse(REFUSED_UNKNOWN_METRIC, earlier.metric_key)
    if definition.metric_class is MetricClass.SERIES:
        return refuse(REFUSED_DIFFERENT_WINDOW, "a series has no single delta; compare curves point by point")
    if earlier.value_num is None or later.value_num is None:
        return refuse(REFUSED_MISSING_INPUT, "a delta needs two numbers")
    if later.collected_at < earlier.collected_at:
        return refuse(REFUSED_DIFFERENT_OBJECT, "the 'later' observation was collected before the earlier one")
    value = float(later.value_num) - float(earlier.value_num)
    if definition.metric_class is MetricClass.CUMULATIVE and value < 0:
        # A cumulative counter that goes down means the object was edited, deleted or recounted.
        return refuse(
            REFUSED_DIFFERENT_METRIC,
            "a cumulative counter decreased, so a difference is not a growth",
        )
    notes = [f"{definition.metric_class.value} change between two collections"]
    if definition.metric_class is MetricClass.POINT_IN_TIME:
        notes.append("point-in-time change: the 'value' is a level, not a total for the interval")
    return Comparable(ok=True, value=value, availability=comparable.availability, notes=tuple(notes))


def rate(
    numerator: AnalyticsSnapshot | None,
    denominator: AnalyticsSnapshot | None,
    *,
    label: str = "ratio",
    scale: float = 1.0,
) -> Comparable:
    """A derived rate, refused whenever either input is missing or the denominator is zero."""
    if numerator is None or denominator is None:
        return refuse(REFUSED_MISSING_INPUT, f"{label}: one of the inputs is not available", availability=MetricAvailability.MISSING)
    if numerator.availability is not MetricAvailability.OBSERVED or denominator.availability is not MetricAvailability.OBSERVED:
        return refuse(
            REFUSED_NOT_OBSERVED,
            f"{label}: both inputs must be observed",
            availability=weakest([numerator.availability, denominator.availability]),
        )
    if numerator.value_num is None or denominator.value_num is None:
        return refuse(REFUSED_MISSING_INPUT, f"{label}: a rate needs two numbers")
    if denominator.value_num == 0:
        return refuse(REFUSED_ZERO_DENOMINATOR, f"{label}: the denominator is zero, so no rate exists")
    return Comparable(
        ok=True,
        value=float(numerator.value_num) / float(denominator.value_num) * scale,
        availability=MetricAvailability.OBSERVED,
        notes=(f"{label}: derived from {numerator.metric_key} / {denominator.metric_key}",),
    )


def cross_platform(
    *,
    left_platform: Platform,
    left_metric: str,
    right_platform: Platform,
    right_metric: str,
    left_value: float | None,
    right_value: float | None,
    concept: str | None = None,
) -> Comparable:
    """Comparing platforms is allowed as a *concept*, with the differences stated.

    Requirement 6 is explicit: no misleading cross-platform comparison. So a numeric league table is
    refused, and the caller gets the documented meaning of each number instead.
    """
    if left_platform is right_platform:
        return Comparable(ok=True, value=left_value, notes=("same platform",))
    if concept is None or concept not in CROSS_PLATFORM_CONCEPTS:
        return refuse(
            REFUSED_CROSS_PLATFORM,
            f"{left_metric} on {left_platform.value} and {right_metric} on {right_platform.value} "
            "are not the same measurement",
        )
    meanings = CROSS_PLATFORM_CONCEPTS[concept]
    if left_platform not in meanings or right_platform not in meanings:
        return refuse(REFUSED_CROSS_PLATFORM, f"{concept} is not documented for both platforms")
    if left_value is None or right_value is None:
        return refuse(REFUSED_MISSING_INPUT, "both sides need a value to be compared")
    notes = (
        f"{left_platform.value}: {meanings[left_platform]}",
        f"{right_platform.value}: {meanings[right_platform]}",
    )
    if concept in CROSS_PLATFORM_NOT_NUMERIC:
        return refuse(
            REFUSED_CROSS_PLATFORM,
            f"{concept} means something different on each platform; showing one number would imply "
            "an equivalence that does not exist",
        )
    return Comparable(ok=True, value=left_value - right_value, notes=notes)


def cells_are_comparable(left: MetricClass, right: MetricClass) -> bool:
    """Only the same metric class may be put side by side (approval requirement 5)."""
    return left is right


def same_window_or_refuse(left: AnalyticsWindowKind, right: AnalyticsWindowKind) -> Comparable:
    from .windows import assess_comparability_of_windows

    ok, reason = assess_comparability_of_windows(left, right)
    return Comparable(ok=ok) if ok else refuse(REFUSED_DIFFERENT_WINDOW, reason)


def period_pair_is_legal(
    left_kind: MetricPeriod,
    left_start: datetime | None,
    left_end: datetime | None,
    right_kind: MetricPeriod,
    right_start: datetime | None,
    right_end: datetime | None,
) -> Comparable:
    from .windows import overlaps

    if period_length_days(left_kind) != period_length_days(right_kind):
        return refuse(REFUSED_DIFFERENT_WINDOW, f"{left_kind.value} vs {right_kind.value}")
    if overlaps(left_start, left_end, right_start, right_end):
        return refuse(REFUSED_OVERLAP, "the periods overlap")
    return Comparable(ok=True)
