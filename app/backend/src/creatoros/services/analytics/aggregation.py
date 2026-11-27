"""Deterministic aggregation: publication windows, account totals, campaign rollups.

Every number this module writes is reproducible: the input snapshot ids, the method, the aggregation
version and the availability are stored next to the value. A later version of the algorithm writes new
rows rather than mutating the old ones, so a recommendation that quoted the old number still has a
row to point at.

What is deliberately **not** aggregated:

* a *unique* metric (reach, unique viewers) across objects — summing unique audiences is arithmetic
  that produces a number nobody can defend;
* an estimated metric, unless the caller asks for an approximation knowingly;
* anything whose inputs are not all observed: the result carries the weakest input's availability and
  no value, because a total built from "not authorized" is not a total.
"""
from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import (
    AnalyticsAggregationMethod,
    AnalyticsAggregationScope,
    AnalyticsWindowKind,
    MetricAvailability,
    MetricClass,
    MetricPeriod,
    Platform,
)
from ...db.models import (
    AnalyticsAggregation,
    AnalyticsMetricDefinition,
    AnalyticsSnapshot,
    PublishJob,
    SocialAccount,
)
from . import windows
from .windows import weakest

AGGREGATION_VERSION = 1

#: Metrics whose values are counts of *people*, which cannot be summed across objects.
NEVER_SUMMABLE_UNITS = frozenset({"people", "unique"})


class NotAggregatable(ValueError):
    code = "NOT_AGGREGATABLE"


@dataclass(slots=True)
class AggregationInput:
    metric_key: str
    availability: MetricAvailability
    value_num: float | None = None
    value_json: dict | list | None = None
    snapshot_ids: list[str] = field(default_factory=list)
    unit: str = "count"
    window_kind: AnalyticsWindowKind = AnalyticsWindowKind.LIFETIME


@dataclass(slots=True)
class AggregationResult:
    scope: AnalyticsAggregationScope
    metric_key: str
    window_kind: AnalyticsWindowKind
    method: AnalyticsAggregationMethod
    availability: MetricAvailability
    value_num: float | None = None
    value_json: dict | list | None = None
    unit: str = "count"
    sample_size: int | None = None
    input_snapshot_ids: list[str] = field(default_factory=list)
    input_metric_keys: list[str] = field(default_factory=list)
    confidence: dict = field(default_factory=dict)
    note: str | None = None


def _definition(session: Session, metric_key: str) -> AnalyticsMetricDefinition | None:
    return session.get(AnalyticsMetricDefinition, metric_key)


def publication_window_aggregation(
    session: Session,
    *,
    account: SocialAccount,
    provider_object_id: str,
    metric_key: str,
    window_kind: AnalyticsWindowKind,
    published_at: datetime | None,
) -> AggregationResult:
    """One metric at one performance window, chosen by `windows.select_window`."""
    selection = windows.select_window(
        session,
        account_id=account.id,
        provider_object_id=provider_object_id,
        metric_key=metric_key,
        window_kind=window_kind,
        published_at=published_at,
    )
    spec = windows.spec_for(window_kind)
    return AggregationResult(
        scope=AnalyticsAggregationScope.PUBLICATION,
        metric_key=metric_key,
        window_kind=window_kind,
        method=AnalyticsAggregationMethod.DIRECT,
        availability=selection.availability,
        value_num=selection.value_num,
        value_json=selection.value_json,
        input_snapshot_ids=[selection.snapshot_id] if selection.snapshot_id else [],
        input_metric_keys=[metric_key],
        confidence={
            "window_hours": spec.hours,
            "tolerance_minutes": spec.tolerance_minutes,
            "gap_minutes": selection.gap_minutes,
        },
        note=selection.note,
    )


def sum_across_objects(
    session: Session,
    *,
    account: SocialAccount,
    metric_key: str,
    snapshots: list[AnalyticsSnapshot],
    scope: AnalyticsAggregationScope = AnalyticsAggregationScope.ACCOUNT,
    window_kind: AnalyticsWindowKind = AnalyticsWindowKind.LIFETIME,
) -> AggregationResult:
    """A total across publications — refused for anything that counts people or is estimated."""
    definition = _definition(session, metric_key)
    if definition is None:
        raise NotAggregatable(f"{metric_key} is not in the metric registry")
    if definition.is_estimate:
        raise NotAggregatable(
            f"{metric_key} is an estimate per the provider; summing estimates compounds the error"
        )
    if definition.unit in NEVER_SUMMABLE_UNITS:
        raise NotAggregatable(f"{metric_key} counts unique people and cannot be summed across objects")
    if definition.metric_class is MetricClass.SERIES:
        raise NotAggregatable(f"{metric_key} is a series; there is no single total to add up")
    if definition.metric_class is MetricClass.RATIO:
        raise NotAggregatable(f"{metric_key} is a ratio; sum the numerator and denominator instead")

    if not snapshots:
        return AggregationResult(
            scope=scope,
            metric_key=metric_key,
            window_kind=window_kind,
            method=AnalyticsAggregationMethod.SUM,
            availability=MetricAvailability.NOT_COLLECTED,
            note="no observations to sum",
        )
    if any(snapshot.availability is not MetricAvailability.OBSERVED for snapshot in snapshots):
        worst = weakest(snapshot.availability for snapshot in snapshots)
        return AggregationResult(
            scope=scope,
            metric_key=metric_key,
            window_kind=window_kind,
            method=AnalyticsAggregationMethod.SUM,
            availability=worst,
            sample_size=len(snapshots),
            input_snapshot_ids=[snapshot.id for snapshot in snapshots],
            input_metric_keys=[metric_key],
            note=f"a total needs every input observed; the worst input was {worst.value}",
        )
    total = sum(float(snapshot.value_num or 0.0) for snapshot in snapshots)
    return AggregationResult(
        scope=scope,
        metric_key=metric_key,
        window_kind=window_kind,
        method=AnalyticsAggregationMethod.SUM,
        availability=MetricAvailability.OBSERVED,
        value_num=total,
        unit=definition.unit,
        sample_size=len(snapshots),
        input_snapshot_ids=[snapshot.id for snapshot in snapshots],
        input_metric_keys=[metric_key],
        confidence={"objects": len(snapshots)},
        note=f"sum of {len(snapshots)} publication(s)",
    )


def median_across_objects(
    session: Session,
    *,
    metric_key: str,
    snapshots: list[AnalyticsSnapshot],
) -> AggregationResult:
    """A typical value across objects. Median, never mean: one viral post skews a mean permanently."""
    observed = [
        snapshot for snapshot in snapshots if snapshot.availability is MetricAvailability.OBSERVED and snapshot.value_num is not None
    ]
    if not observed:
        return AggregationResult(
            scope=AnalyticsAggregationScope.CONTENT_GROUP,
            metric_key=metric_key,
            window_kind=AnalyticsWindowKind.LIFETIME,
            method=AnalyticsAggregationMethod.MEDIAN,
            availability=MetricAvailability.NOT_COLLECTED,
            note="no observed values to summarise",
        )
    values = [float(snapshot.value_num) for snapshot in observed]
    return AggregationResult(
        scope=AnalyticsAggregationScope.CONTENT_GROUP,
        metric_key=metric_key,
        window_kind=AnalyticsWindowKind.LIFETIME,
        method=AnalyticsAggregationMethod.MEDIAN,
        availability=MetricAvailability.OBSERVED,
        value_num=float(statistics.median(values)),
        sample_size=len(values),
        input_snapshot_ids=[snapshot.id for snapshot in observed],
        input_metric_keys=[metric_key],
        confidence={"n": len(values), "min": min(values), "max": max(values)},
        note=f"median of {len(values)} observed publication(s)",
    )


def delta_between_windows(
    session: Session,
    *,
    account: SocialAccount,
    provider_object_id: str,
    metric_key: str,
    earlier: AnalyticsWindowKind,
    later: AnalyticsWindowKind,
    published_at: datetime | None,
) -> AggregationResult:
    """Growth between two windows *of the same object*.

    Different window lengths are the whole point here (that is how maturation is measured), so the
    "same window length" rule from `comparability` does not apply. What does apply is that the later
    window must genuinely be later, and that both windows must be observed for the same object.
    """
    if earlier is later:
        return AggregationResult(
            scope=AnalyticsAggregationScope.PUBLICATION,
            metric_key=metric_key,
            window_kind=later,
            method=AnalyticsAggregationMethod.DELTA,
            availability=MetricAvailability.UNKNOWN,
            note="a delta needs two different windows",
        )
    if windows.spec_for(later).hours < windows.spec_for(earlier).hours:
        return AggregationResult(
            scope=AnalyticsAggregationScope.PUBLICATION,
            metric_key=metric_key,
            window_kind=later,
            method=AnalyticsAggregationMethod.DELTA,
            availability=MetricAvailability.UNKNOWN,
            note=f"+{windows.spec_for(later).hours} h comes before +{windows.spec_for(earlier).hours} h",
        )
    first = windows.select_window(
        session,
        account_id=account.id,
        provider_object_id=provider_object_id,
        metric_key=metric_key,
        window_kind=earlier,
        published_at=published_at,
    )
    second = windows.select_window(
        session,
        account_id=account.id,
        provider_object_id=provider_object_id,
        metric_key=metric_key,
        window_kind=later,
        published_at=published_at,
    )
    if not (first.observed and second.observed):
        worst = weakest([first.availability, second.availability])
        return AggregationResult(
            scope=AnalyticsAggregationScope.PUBLICATION,
            metric_key=metric_key,
            window_kind=later,
            method=AnalyticsAggregationMethod.DELTA,
            availability=worst,
            input_snapshot_ids=[sid for sid in (first.snapshot_id, second.snapshot_id) if sid],
            note=f"both windows must be observed; worst was {worst.value}",
        )
    if first.value_num is None or second.value_num is None:
        return AggregationResult(
            scope=AnalyticsAggregationScope.PUBLICATION,
            metric_key=metric_key,
            window_kind=later,
            method=AnalyticsAggregationMethod.DELTA,
            availability=MetricAvailability.MISSING,
            note="a delta needs two numbers",
        )
    return AggregationResult(
        scope=AnalyticsAggregationScope.PUBLICATION,
        metric_key=metric_key,
        window_kind=later,
        method=AnalyticsAggregationMethod.DELTA,
        availability=MetricAvailability.OBSERVED,
        value_num=float(second.value_num) - float(first.value_num),
        input_snapshot_ids=[sid for sid in (first.snapshot_id, second.snapshot_id) if sid],
        input_metric_keys=[metric_key],
        confidence={"from": earlier.value, "to": later.value},
        note=f"change from +{windows.spec_for(earlier).hours} h to +{windows.spec_for(later).hours} h",
    )


def store(
    session: Session,
    *,
    account: SocialAccount,
    result: AggregationResult,
    publication_id: str | None = None,
    campaign_id: str | None = None,
    content_project_id: str | None = None,
    now: datetime | None = None,
    version: int = AGGREGATION_VERSION,
) -> AnalyticsAggregation:
    """Upsert the aggregation for this key and version, keeping other versions intact."""
    platform = account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform))
    publication_key = publication_id or ""
    campaign_key = campaign_id or ""
    row = session.scalar(
        sa.select(AnalyticsAggregation).where(
            AnalyticsAggregation.account_id == account.id,
            AnalyticsAggregation.scope == result.scope,
            AnalyticsAggregation.publication_key == publication_key,
            AnalyticsAggregation.campaign_key == campaign_key,
            AnalyticsAggregation.metric_key == result.metric_key,
            AnalyticsAggregation.window_kind == result.window_kind,
            AnalyticsAggregation.aggregation_version == version,
        )
    )
    spec = windows.spec_for(result.window_kind) if result.window_kind in windows.WINDOW_SPECS else None
    values = {
        "platform": platform,
        "publication_id": publication_id,
        "publication_key": publication_key,
        "campaign_key": campaign_key,
        "content_project_id": content_project_id,
        "campaign_id": campaign_id,
        "window_hours": spec.hours if spec else None,
        "window_tolerance_minutes": spec.tolerance_minutes if spec else None,
        "value_num": result.value_num,
        "value_json": result.value_json,
        "unit": result.unit,
        "sample_size": result.sample_size,
        "availability": result.availability,
        "method": result.method,
        "input_snapshot_ids_json": list(result.input_snapshot_ids),
        "input_metric_keys_json": list(result.input_metric_keys),
        "confidence_json": dict(result.confidence),
        "computed_at": now or datetime.now(UTC),
    }
    if row is None:
        row = AnalyticsAggregation(
            account_id=account.id,
            scope=result.scope,
            metric_key=result.metric_key,
            window_kind=result.window_kind,
            aggregation_version=version,
            **values,
        )
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    session.flush()
    return row


def aggregations_for(
    session: Session,
    *,
    account: SocialAccount,
    metric_key: str | None = None,
    window_kind: AnalyticsWindowKind | None = None,
    version: int = AGGREGATION_VERSION,
) -> list[AnalyticsAggregation]:
    stmt = sa.select(AnalyticsAggregation).where(
        AnalyticsAggregation.account_id == account.id,
        AnalyticsAggregation.aggregation_version == version,
    )
    if metric_key:
        stmt = stmt.where(AnalyticsAggregation.metric_key == metric_key)
    if window_kind:
        stmt = stmt.where(AnalyticsAggregation.window_kind == window_kind)
    return list(session.scalars(stmt.order_by(AnalyticsAggregation.metric_key)))


def recompute_account(
    session: Session,
    *,
    account: SocialAccount,
    now: datetime | None = None,
    version: int = AGGREGATION_VERSION,
) -> dict[str, int]:
    """Rebuild publication-window aggregations for every published object of one account.

    Recomputing under a new `version` writes new rows and leaves the old ones: a recommendation that
    quoted version 1 still resolves.
    """
    from ...db.models import PlatformVariant

    written = 0
    skipped = 0
    jobs = session.scalars(
        sa.select(PublishJob).where(
            PublishJob.account_id == account.id,
            PublishJob.remote_post_id.is_not(None),
        )
    )
    for job in jobs:
        variant = session.get(PlatformVariant, job.variant_id)
        project_id = variant.project_id if variant is not None else None
        published_at = job.provider_published_at
        if published_at is None:
            skipped += 1
            continue
        observed_metrics = {
            row[0]
            for row in session.execute(
                sa.select(AnalyticsSnapshot.metric_key)
                .where(
                    AnalyticsSnapshot.account_id == account.id,
                    AnalyticsSnapshot.provider_object_id == job.remote_post_id,
                )
                .distinct()
            )
        }
        if not observed_metrics:
            skipped += 1
            continue
        for metric_key in observed_metrics:
            for window_kind in windows.WINDOW_ORDER:
                result = publication_window_aggregation(
                    session,
                    account=account,
                    provider_object_id=str(job.remote_post_id),
                    metric_key=metric_key,
                    window_kind=window_kind,
                    published_at=published_at,
                )
                if result.availability is MetricAvailability.MISSING and not result.input_snapshot_ids:
                    continue
                store(
                    session,
                    account=account,
                    result=result,
                    publication_id=job.id,
                    content_project_id=project_id,
                    now=now,
                    version=version,
                )
                written += 1
    return {"written": written, "skipped": skipped, "version": version}


def delete_version(session: Session, *, account: SocialAccount, version: int) -> int:
    """Drop one aggregation version so it can be rebuilt (the documented rollback)."""
    rows = session.execute(
        sa.delete(AnalyticsAggregation).where(
            AnalyticsAggregation.account_id == account.id,
            AnalyticsAggregation.aggregation_version == version,
        )
    )
    return int(rows.rowcount or 0)


def period_bucket_aggregate(
    snapshots: list[AnalyticsSnapshot],
    *,
    metric_key: str,
    period_kind: MetricPeriod,
) -> AggregationResult:
    """Total one metric across non-overlapping period rows of the same length."""
    from .comparability import period_pair_is_legal

    relevant = [snapshot for snapshot in snapshots if snapshot.metric_key == metric_key]
    for left, right in itertools.pairwise(relevant):
        legal = period_pair_is_legal(
            left.period_kind,
            left.period_start,
            left.period_end,
            right.period_kind,
            right.period_start,
            right.period_end,
        )
        if not legal.ok:
            return AggregationResult(
                scope=AnalyticsAggregationScope.ACCOUNT,
                metric_key=metric_key,
                window_kind=AnalyticsWindowKind.CUSTOM,
                method=AnalyticsAggregationMethod.SUM,
                availability=MetricAvailability.UNKNOWN,
                note=legal.reason,
            )
    observed = [snapshot for snapshot in relevant if snapshot.availability is MetricAvailability.OBSERVED]
    if len(observed) != len(relevant) or not relevant:
        return AggregationResult(
            scope=AnalyticsAggregationScope.ACCOUNT,
            metric_key=metric_key,
            window_kind=AnalyticsWindowKind.CUSTOM,
            method=AnalyticsAggregationMethod.SUM,
            availability=weakest([snapshot.availability for snapshot in relevant] or [MetricAvailability.MISSING]),
            note="every period row must be observed to total them",
        )
    return AggregationResult(
        scope=AnalyticsAggregationScope.ACCOUNT,
        metric_key=metric_key,
        window_kind=AnalyticsWindowKind.CUSTOM,
        method=AnalyticsAggregationMethod.SUM,
        availability=MetricAvailability.OBSERVED,
        value_num=sum(float(snapshot.value_num or 0.0) for snapshot in observed),
        sample_size=len(observed),
        input_snapshot_ids=[snapshot.id for snapshot in observed],
        input_metric_keys=[metric_key],
        confidence={"period": period_kind.value, "rows": len(observed)},
        note=f"sum of {len(observed)} non-overlapping {period_kind.value} rows",
    )
