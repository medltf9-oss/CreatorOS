"""The metric registry service: seed the catalogue, then plan what may be requested.

Two things live here and nothing else:

* the *seed*, which is deterministic and idempotent, so `creatoros db seed` can run twice without
  duplicating a metric or losing the ``docs_checked_at`` of a row a human verified;
* the *plan*, which answers "for this account, this object type and these granted scopes, which
  metrics may I request, and for the ones I may not, why exactly?".

The second is the guardrail behind requirement 6: a metric is skipped with a named reason
(``not_authorized``, ``below_threshold``, ``version_above``, ``not_supported``) instead of being
requested, failing, and being recorded as ``0``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import AnalyticsObjectType, MetricAvailability, Platform
from ...db.models import AnalyticsMetricDefinition
from ...integrations.analytics import CATALOG_VERSION, METRIC_CATALOG, MetricSpec, catalogue_by_key
from . import version_monitor

#: Why a metric will or will not be requested. Stable codes, shown verbatim in the UI.
REASON_OK = "ok"
REASON_NOT_AUTHORIZED = "not_authorized"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_VERSION_ABOVE = "version_above"
REASON_VERSION_BELOW = "version_below"
REASON_DEPRECATED = "deprecated"
REASON_NOT_SUPPORTED = "not_supported"
REASON_THRESHOLD_UNKNOWN = "threshold_unknown"

_REASON_AVAILABILITY: dict[str, MetricAvailability] = {
    REASON_NOT_AUTHORIZED: MetricAvailability.NOT_AUTHORIZED,
    REASON_BELOW_THRESHOLD: MetricAvailability.BELOW_THRESHOLD,
    REASON_VERSION_ABOVE: MetricAvailability.NOT_SUPPORTED,
    REASON_VERSION_BELOW: MetricAvailability.NOT_SUPPORTED,
    REASON_NOT_SUPPORTED: MetricAvailability.NOT_SUPPORTED,
    REASON_DEPRECATED: MetricAvailability.NOT_SUPPORTED,
}


@dataclass(frozen=True, slots=True)
class PlannedMetric:
    metric_key: str
    allowed: bool
    reason: str
    availability_if_skipped: MetricAvailability | None
    detail: str

    def as_dict(self) -> dict:
        return {
            "metric_key": self.metric_key,
            "allowed": self.allowed,
            "reason": self.reason,
            "availability_if_skipped": (
                self.availability_if_skipped.value if self.availability_if_skipped else None
            ),
            "detail": self.detail,
        }


def _permissions_json(value: tuple[str, ...] | dict) -> list | dict:
    """A flat list means "all of these"; ``{"any_of": [...]}`` means "one of these routes"."""
    if isinstance(value, dict):
        return {"any_of": [list(alt) for alt in value.get("any_of") or []]}
    return list(value)


def missing_permissions(value: list | dict, granted: set[str]) -> list[str]:
    """What is missing, phrased so the UI can ask for the *smallest* set that would work."""
    if isinstance(value, dict):
        alternatives = [set(alt) for alt in value.get("any_of") or []]
        if not alternatives:
            return []
        if any(alt <= granted for alt in alternatives):
            return []
        # Report the gap of the closest alternative, not the union of every gap.
        best = min(alternatives, key=lambda alt: len(alt - granted))
        return sorted(best - granted)
    required = {str(scope) for scope in value or []}
    return sorted(required - granted)


def _values(spec: MetricSpec, *, catalog_version: str) -> dict:
    return {
        "platform": spec.platform,
        "object_type": spec.object_type,
        "provider_metric_name": spec.provider_metric_name,
        "provider_object": spec.provider_object,
        "source_endpoint": spec.source_endpoint,
        "source_doc_url": spec.source_doc_url,
        "unit": spec.unit,
        "value_kind": spec.value_kind,
        "metric_class": spec.metric_class,
        "period_kinds_json": [period.value for period in spec.period_kinds],
        "definition_text": spec.definition,
        "is_estimate": spec.is_estimate,
        "is_cumulative": spec.metric_class.value == "cumulative",
        "is_deprecated": spec.is_deprecated,
        "deprecated_note": spec.deprecated_note,
        "supersedes_key": spec.supersedes,
        "api_version_min": spec.api_version_min,
        "api_version_max": spec.api_version_max,
        "required_permissions_json": _permissions_json(spec.required_permissions),
        "account_threshold_json": dict(spec.account_threshold),
        "availability_default": spec.availability_default,
        "preferred_for": spec.preferred_for,
        "catalog_version": catalog_version,
    }


def ensure_metric_catalog(
    session: Session,
    *,
    catalog_version: str = CATALOG_VERSION,
    now: datetime | None = None,
) -> dict:
    """Upsert every catalogue metric. Returns counts so the seed command can report them.

    Rows are never deleted: a metric that leaves the code stays in the registry with its history so
    old observations remain explainable. ``superseded_by_key`` is back-filled from ``supersedes``.
    """
    existing = {row.metric_key: row for row in session.scalars(sa.select(AnalyticsMetricDefinition))}
    created = 0
    updated = 0
    touched_at = now or datetime.now(UTC)
    for spec in METRIC_CATALOG:
        values = _values(spec, catalog_version=catalog_version)
        row = existing.get(spec.key)
        if row is None:
            session.add(
                AnalyticsMetricDefinition(metric_key=spec.key, docs_checked_at=touched_at, **values)
            )
            created += 1
            continue
        changed = False
        for field, value in values.items():
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
        if changed:
            # `docs_checked_at` tracks the last real change, so re-seeding an unchanged catalogue is
            # a no-op (the seed runs on every `db seed`, not only after a catalogue edit).
            row.docs_checked_at = touched_at
            updated += 1

    # A replacement metric points back at what it replaces, so the UI can say "views replaced
    # impressions" without guessing from names.
    session.flush()
    for spec in METRIC_CATALOG:
        if not spec.supersedes:
            continue
        replaced = session.get(AnalyticsMetricDefinition, spec.supersedes)
        if replaced is not None and replaced.superseded_by_key != spec.key:
            replaced.superseded_by_key = spec.key
    session.flush()
    return {
        "catalog_version": catalog_version,
        "created": created,
        "updated": updated,
        "total": len(METRIC_CATALOG),
        "stranded": len(set(existing) - set(catalogue_by_key())),
    }


def get_definition(session: Session, metric_key: str) -> AnalyticsMetricDefinition | None:
    return session.get(AnalyticsMetricDefinition, metric_key)


def definitions_for(
    session: Session,
    platform: Platform,
    object_type: AnalyticsObjectType | None = None,
) -> list[AnalyticsMetricDefinition]:
    stmt = sa.select(AnalyticsMetricDefinition).where(AnalyticsMetricDefinition.platform == platform)
    if object_type is not None:
        stmt = stmt.where(AnalyticsMetricDefinition.object_type == object_type)
    return list(session.scalars(stmt.order_by(AnalyticsMetricDefinition.metric_key)))


def resolve_key(
    session: Session,
    platform: Platform,
    object_type: AnalyticsObjectType,
    provider_metric_name: str,
) -> str | None:
    """Map a provider payload field back to our key.

    Two providers can use the same field name for different things (``likes`` on an Instagram
    account is not a TikTok ``like_count``), so the lookup is always qualified by platform +
    object type.
    """
    return session.scalar(
        sa.select(AnalyticsMetricDefinition.metric_key).where(
            AnalyticsMetricDefinition.platform == platform,
            AnalyticsMetricDefinition.object_type == object_type,
            AnalyticsMetricDefinition.provider_metric_name == provider_metric_name,
        )
    )


def _threshold_reason(spec: MetricSpec, signals: dict | None) -> tuple[str, str] | None:
    if not spec.account_threshold or signals is None:
        return None
    wanted = spec.account_threshold
    for signal_name, minimum in wanted.items():
        observed = signals.get(signal_name)
        if observed is None:
            return (REASON_THRESHOLD_UNKNOWN, f"{signal_name} not known yet; the provider may withhold this metric")
        try:
            if float(observed) < float(minimum):
                return (
                    REASON_BELOW_THRESHOLD,
                    f"needs {signal_name} >= {minimum}, account has {observed}",
                )
        except (TypeError, ValueError):
            return (REASON_THRESHOLD_UNKNOWN, f"{signal_name} is not numeric")
    return None


def plan(
    session: Session,
    *,
    platform: Platform,
    object_type: AnalyticsObjectType,
    granted_scopes: set[str] | frozenset[str] | tuple[str, ...] = (),
    api_version: str | None = None,
    account_signals: dict | None = None,
) -> list[PlannedMetric]:
    """Which metrics to request, and why the others are skipped.

    ``granted_scopes`` are the scopes actually on the token (intersected with what the app asked
    for), not what the developer app declares. ``account_signals`` may carry ``min_followers``
    style facts (``followers``, ``page_likes``) when known.
    """
    version = api_version or version_monitor.configured_version(session, platform)
    scopes = {str(scope) for scope in granted_scopes}
    planned: list[PlannedMetric] = []

    for definition in definitions_for(session, platform, object_type):
        key = definition.metric_key
        if definition.availability_default in {
            MetricAvailability.NOT_SUPPORTED,
            MetricAvailability.REDACTED_BY_POLICY,
        }:
            planned.append(
                PlannedMetric(
                    key,
                    False,
                    REASON_NOT_SUPPORTED,
                    definition.availability_default,
                    "not exposed by any approved API" if definition.availability_default is MetricAvailability.NOT_SUPPORTED else "withheld deliberately",
                )
            )
            continue
        if not version_monitor.metric_allowed_on_version(definition, version):
            above = bool(
                definition.api_version_max
                and not version_monitor.version_at_most(version, definition.api_version_max)
            )
            planned.append(
                PlannedMetric(
                    key,
                    False,
                    REASON_VERSION_ABOVE if above else REASON_VERSION_BELOW,
                    MetricAvailability.NOT_SUPPORTED,
                    f"documented for {definition.api_version_min or 'any'}..{definition.api_version_max or 'any'}, configured {version}",
                )
            )
            continue
        spec = catalogue_by_key().get(key)
        missing = missing_permissions(definition.required_permissions_json or [], scopes)
        if spec is not None and missing:
            planned.append(
                PlannedMetric(
                    key,
                    False,
                    REASON_NOT_AUTHORIZED,
                    MetricAvailability.NOT_AUTHORIZED,
                    "missing scope(s): " + ", ".join(missing),
                )
            )
            continue
        threshold = _threshold_reason(spec, account_signals) if spec is not None else None
        if threshold is not None:
            reason, detail = threshold
            planned.append(
                PlannedMetric(key, False, reason, _REASON_AVAILABILITY.get(reason), detail)
            )
            continue
        if definition.is_deprecated:
            planned.append(
                PlannedMetric(
                    key,
                    True,
                    REASON_DEPRECATED,
                    None,
                    definition.deprecated_note or "deprecated; collected only for historical comparison",
                )
            )
            continue
        planned.append(PlannedMetric(key, True, REASON_OK, None, ""))
    return planned


def summary(planned: list[PlannedMetric]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in planned:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return counts
