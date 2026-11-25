"""Payload normalisation: keep the provider's shape, drop the provider's accidents.

"Normalised" here does **not** mean "turned into a number". It means one documented shape per
`value_kind`, with the provider's structure intact:

* ``scalar`` → ``value_num`` (a float, because Graph mixes ints and strings), and a *missing* answer
  stays missing. ``0`` is only produced when the provider actually said zero;
* ``breakdown`` → a dict with the provider's own keys, numeric values, sorted for determinism;
* ``series`` → a dict ``{"points": [...], "bucket_count": n, "unit": "..."}``; a retention curve is
  never averaged, truncated or padded. If it does not have the documented bucket count, the row is
  stored with a note saying so rather than silently becoming a 39-point curve.

Nothing in this module invents a value. When a payload cannot be normalised the result says which
shape was expected and which arrived, so the ingestion layer can store ``missing`` with evidence
instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...db.enums import MetricValueKind
from .catalog import MetricSpec

#: The Facebook retention curve is documented as 40 equally spaced intervals.
RETENTION_BUCKETS = 40


@dataclass(frozen=True, slots=True)
class NormalizedPayload:
    value_kind: MetricValueKind
    value_num: float | None = None
    value_json: Any | None = None
    note: str | None = None

    @property
    def usable(self) -> bool:
        if self.value_kind is MetricValueKind.SCALAR:
            return self.value_num is not None
        return self.value_json is not None


def as_number(raw: Any) -> float | None:
    """A provider number, without inventing one.

    Graph answers with ints, floats and (occasionally) numeric strings; TikTok answers with ints.
    Booleans are not numbers and ``True`` must never become ``1``.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    if isinstance(raw, dict):
        for key in ("value", "total", "total_value", "count"):
            if key in raw:
                return as_number(raw[key])
    return None


def normalize_breakdown(raw: Any) -> dict | None:
    """A breakdown keeps its keys; nested summaries are unwrapped, not flattened away."""
    if not isinstance(raw, dict) or not raw:
        return None
    normalized: dict[str, Any] = {}
    for key in sorted(raw, key=str):
        value = raw[key]
        if isinstance(value, dict):
            number = as_number(value)
            normalized[str(key)] = number if number is not None else normalize_breakdown(value)
        else:
            number = as_number(value)
            normalized[str(key)] = number if number is not None else value
    return normalized or None


def normalize_series(raw: Any, *, expected_points: int | None = None, unit: str = "ratio") -> dict | None:
    """A curve or time series, validated for length but never repaired.

    Accepted shapes: a list of points, ``{"points": [...]}``, or Graph's ``{"0": n, "1": n, …}``
    object map. The result always records ``bucket_count`` so a consumer can refuse to compare a
    39-point curve with a 40-point one.
    """
    points: list[Any] | None = None
    if isinstance(raw, list):
        points = raw
    elif isinstance(raw, dict):
        if isinstance(raw.get("points"), list):
            points = raw["points"]
        elif raw and all(str(key).lstrip("-").replace(".", "", 1).isdigit() for key in raw):
            points = [raw[key] for key in sorted(raw, key=lambda key: float(str(key)))]
    if points is None:
        return None

    numbers: list[float] = []
    for point in points:
        number = as_number(point)
        if number is None:
            # A non-numeric point means the payload is not a series. Say so instead of dropping it.
            return None
        numbers.append(number)
    if not numbers:
        return None
    payload: dict[str, Any] = {
        "points": numbers,
        "bucket_count": len(numbers),
        "unit": unit,
    }
    if expected_points is not None and len(numbers) != expected_points:
        payload["expected_buckets"] = expected_points
        payload["incomplete"] = True
    return payload


def normalize_payload(spec: MetricSpec, raw: Any) -> NormalizedPayload:
    """Shape one provider value according to the metric's declared `value_kind`."""
    if spec.value_kind is MetricValueKind.SCALAR:
        number = as_number(raw)
        return NormalizedPayload(
            value_kind=MetricValueKind.SCALAR,
            value_num=number,
            note=None if number is not None else f"expected a number, provider returned {type(raw).__name__}",
        )
    if spec.value_kind is MetricValueKind.OBJECT:
        payload = raw if isinstance(raw, dict) else {"value": raw}
        return NormalizedPayload(
            value_kind=MetricValueKind.OBJECT,
            value_json=payload,
            note=None if payload else "expected an object, provider returned nothing",
        )
    if spec.value_kind is MetricValueKind.BREAKDOWN:
        payload = normalize_breakdown(raw)
        return NormalizedPayload(
            value_kind=MetricValueKind.BREAKDOWN,
            value_json=payload,
            note=None if payload is not None else "expected a breakdown object, provider returned another shape",
        )
    expected = RETENTION_BUCKETS if "retention" in spec.key else None
    payload = normalize_series(raw, expected_points=expected, unit=spec.unit)
    note = None
    if payload is None:
        note = "expected a series, provider returned another shape"
    elif payload.get("incomplete"):
        note = f"provider returned {payload['bucket_count']} of {expected} documented intervals"
    return NormalizedPayload(
        value_kind=MetricValueKind.SERIES,
        value_json=payload,
        note=note,
    )
