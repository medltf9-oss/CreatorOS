"""Evidence: the measurements a recommendation is allowed to talk about.

Requirement 7 says every AI recommendation must carry its source measurements, observation periods,
sample sizes, confidence and limitations. `evidence.py` is how that becomes checkable rather than
hopeful:

* an evidence item is built **from a stored row** (a snapshot or an aggregation), never from prose;
* `assert_resolvable()` re-reads every id, so a dangling reference fails before the recommendation is
  stored;
* `assert_numbers_traceable()` rejects a narrative that mentions a number no evidence item contains,
  which is the mechanical defence against a model inventing statistics.

Nothing here formats text or chooses words: presentation is not evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import MetricAvailability, MetricClass
from ...db.models import AnalyticsAggregation, AnalyticsMetricDefinition, AnalyticsSnapshot

#: Numbers a narrative may use without evidence: a year, an hour count in a window name, an ordinal.
_TRIVIAL = {"0", "1", "2", "3", "24", "48", "7", "28", "95", "100"}


class UntraceableEvidence(ValueError):
    """A referenced measurement does not exist, or a number is not supported by any of them."""

    code = "UNTRACEABLE_EVIDENCE"


@dataclass(slots=True)
class EvidenceItem:
    metric_key: str
    provider_metric_name: str
    unit: str
    period_kind: str
    availability: str
    collected_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    value: float | None = None
    value_json: Any | None = None
    snapshot_ids: list[str] = field(default_factory=list)
    aggregation_ids: list[str] = field(default_factory=list)
    group: str | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        payload = {
            "metric_key": self.metric_key,
            "provider_metric_name": self.provider_metric_name,
            "unit": self.unit,
            "period_kind": self.period_kind,
            "availability": self.availability,
            "snapshot_ids": list(self.snapshot_ids),
            "aggregation_ids": list(self.aggregation_ids),
        }
        if self.value is not None:
            payload["value"] = self.value
        if self.value_json is not None:
            payload["value_json"] = self.value_json
        if self.collected_at is not None:
            payload["collected_at"] = self.collected_at.isoformat()
        if self.period_start is not None:
            payload["period_start"] = self.period_start.isoformat()
        if self.period_end is not None:
            payload["period_end"] = self.period_end.isoformat()
        if self.group is not None:
            payload["group"] = self.group
        if self.note is not None:
            payload["note"] = self.note
        return payload


def _definition(session: Session, metric_key: str) -> AnalyticsMetricDefinition | None:
    return session.get(AnalyticsMetricDefinition, metric_key)


def from_snapshot(
    session: Session,
    snapshot: AnalyticsSnapshot,
    *,
    group: str | None = None,
) -> EvidenceItem:
    definition = _definition(session, snapshot.metric_key)
    return EvidenceItem(
        metric_key=snapshot.metric_key,
        provider_metric_name=definition.provider_metric_name if definition else snapshot.metric_key,
        unit=snapshot.unit,
        period_kind=snapshot.period_kind.value,
        availability=snapshot.availability.value,
        collected_at=snapshot.collected_at,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        value=snapshot.value_num,
        value_json=snapshot.value_json,
        snapshot_ids=[snapshot.id],
        group=group,
    )


def from_aggregation(
    session: Session,
    aggregation: AnalyticsAggregation,
    *,
    group: str | None = None,
) -> EvidenceItem:
    definition = _definition(session, aggregation.metric_key)
    return EvidenceItem(
        metric_key=aggregation.metric_key,
        provider_metric_name=definition.provider_metric_name if definition else aggregation.metric_key,
        unit=aggregation.unit,
        period_kind=aggregation.window_kind.value,
        availability=aggregation.availability.value,
        collected_at=aggregation.computed_at,
        value=aggregation.value_num,
        value_json=aggregation.value_json,
        snapshot_ids=list(aggregation.input_snapshot_ids_json or []),
        aggregation_ids=[aggregation.id],
        group=group,
    )


def assert_resolvable(session: Session, evidence: list[dict] | list[EvidenceItem]) -> None:
    """Every referenced id must exist. A recommendation may not cite a row that is gone."""
    if not evidence:
        raise UntraceableEvidence("a recommendation needs at least one measurement")
    for item in evidence:
        payload = item.as_dict() if isinstance(item, EvidenceItem) else dict(item)
        if not payload.get("metric_key"):
            raise UntraceableEvidence("an evidence item has no metric_key")
        ids = list(payload.get("snapshot_ids") or []) + list(payload.get("aggregation_ids") or [])
        if not ids:
            raise UntraceableEvidence(
                f"{payload['metric_key']}: an evidence item must reference a stored measurement"
            )
        for snapshot_id in payload.get("snapshot_ids") or []:
            if session.get(AnalyticsSnapshot, snapshot_id) is None:
                raise UntraceableEvidence(f"snapshot {snapshot_id} does not exist")
        for aggregation_id in payload.get("aggregation_ids") or []:
            if session.get(AnalyticsAggregation, aggregation_id) is None:
                raise UntraceableEvidence(f"aggregation {aggregation_id} does not exist")


def numbers_in(text: str) -> list[str]:
    """Numeric tokens in a narrative, with thousands separators and percentages normalised."""
    tokens = re.findall(r"-?\d[\d,]*(?:\.\d+)?%?", text or "")
    return [token.rstrip("%").replace(",", "") for token in tokens]


def _evidence_numbers(evidence: list[dict] | list[EvidenceItem]) -> set[str]:
    values: set[str] = set()
    for item in evidence:
        payload = item.as_dict() if isinstance(item, EvidenceItem) else dict(item)
        for candidate in (payload.get("value"), payload.get("sample_size")):
            if isinstance(candidate, (int, float)):
                values.add(_normalise(float(candidate)))
        if isinstance(payload.get("value_json"), dict):
            for chunk in payload["value_json"].get("points") or []:
                if isinstance(chunk, (int, float)):
                    values.add(_normalise(float(chunk)))
    return values


def _normalise(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _numbers_from_mapping(payload: object) -> set[str]:
    """Numbers inside a nested record field, e.g. the recommendation's own sample_size."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.add(_normalise(float(node)))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(payload)
    return found


def assert_numbers_traceable(
    text: str,
    evidence: list[dict] | list[EvidenceItem],
    *,
    allow: object | None = None,
) -> None:
    """Reject a narrative that states a number the evidence does not contain.

    Small integers are allowed because prose legitimately says "3 posts" or "the 24-hour window";
    anything larger must come from an evidence item, a sample size, or the record fields the reader
    also sees (`allow`, typically `sample_size_json`).
    """
    supported = _evidence_numbers(evidence)
    if allow is not None:
        supported |= _numbers_from_mapping(allow)
    for token in numbers_in(text):
        if token in _TRIVIAL or token in supported:
            continue
        raise UntraceableEvidence(f"{token} is not present in the evidence for this recommendation")


def summary(evidence: list[dict] | list[EvidenceItem]) -> dict:
    """Counts by metric and availability, so a reader can see how complete the evidence is."""
    per_metric: dict[str, int] = {}
    per_availability: dict[str, int] = {}
    for item in evidence:
        payload = item.as_dict() if isinstance(item, EvidenceItem) else dict(item)
        per_metric[payload["metric_key"]] = per_metric.get(payload["metric_key"], 0) + 1
        state = payload.get("availability", MetricAvailability.UNKNOWN.value)
        per_availability[state] = per_availability.get(state, 0) + 1
    return {"metrics": per_metric, "availability": per_availability, "items": len(evidence)}


def snapshot_ids(evidence: list[dict] | list[EvidenceItem]) -> list[str]:
    ids: list[str] = []
    for item in evidence:
        payload = item.as_dict() if isinstance(item, EvidenceItem) else dict(item)
        ids.extend(payload.get("snapshot_ids") or [])
    return list(dict.fromkeys(ids))


def metric_classes(session: Session, evidence: list[dict] | list[EvidenceItem]) -> dict[str, MetricClass]:
    """The class of each referenced metric, so a consumer can refuse a mixed comparison."""
    classes: dict[str, MetricClass] = {}
    for item in evidence:
        payload = item.as_dict() if isinstance(item, EvidenceItem) else dict(item)
        definition = _definition(session, payload["metric_key"])
        if definition is not None:
            classes[payload["metric_key"]] = definition.metric_class
    return classes


def resolve(session: Session, evidence: list[dict]) -> list[Any]:
    """Re-read the rows behind an evidence list (used by the API and by tests)."""
    rows: list[Any] = []
    for item in evidence:
        for snapshot_id in item.get("snapshot_ids") or []:
            row = session.get(AnalyticsSnapshot, snapshot_id)
            if row is not None:
                rows.append(row)
        for aggregation_id in item.get("aggregation_ids") or []:
            row = session.get(AnalyticsAggregation, aggregation_id)
            if row is not None:
                rows.append(row)
    return rows


def pending_metric_keys(session: Session, evidence: list[dict]) -> list[str]:
    """Evidence whose measurement was unavailable: named, so a limitation can say which."""
    return [
        item["metric_key"]
        for item in evidence
        if item.get("availability") and item["availability"] != MetricAvailability.OBSERVED.value
    ]


def count_snapshots(session: Session, ids: list[str]) -> int:
    if not ids:
        return 0
    return int(
        session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot).where(AnalyticsSnapshot.id.in_(ids)))
        or 0
    )
