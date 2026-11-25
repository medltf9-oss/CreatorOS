"""Deterministic statistics for comparisons — with explicit sample gates.

This module exists so that no agent, and no chart, ever has to invent a confidence statement. Every
function here is pure, seeded and reproducible: the same inputs produce byte-identical output, which
is what lets a recommendation store its own numbers and be re-checked later.

Three deliberate limitations:

* **Small samples produce ``inconclusive``, not a verdict.** The gates are explicit constants; below
  them the answer is "not enough data", because a 3-post comparison is a story, not a finding.
* **Bootstrap resampling is seeded.** A confidence interval that changes every refresh would make
  the UI look unstable and the recommendation unreproducible.
* **Proportions use Wilson intervals**, which behave sensibly at small n and near 0/1 where the
  normal approximation does not.
"""
from __future__ import annotations

import math
import random
import statistics as stdlib_statistics
from dataclasses import dataclass, field
from typing import Any

#: Default two-sided 95% interval.
Z_95 = 1.959963984540054

#: Sample gates. Below these, a comparison is reported as `inconclusive` with the gate named.
MIN_PUBLICATIONS_PER_GROUP = 8
MIN_OBSERVATIONS_PERCENTILE = 10
MIN_OBSERVATIONS_BOOTSTRAP = 5
#: Resamples for the bootstrap interval. Big enough to be stable, small enough to stay local.
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261124

VERDICT_A_HIGHER = "a_higher"
VERDICT_B_HIGHER = "b_higher"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_INSUFFICIENT = "insufficient_data"

CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float
    method: str
    level: float = 0.95

    @property
    def width(self) -> float:
        return self.high - self.low

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

    def overlaps(self, other: Interval) -> bool:
        return self.low <= other.high and other.low <= self.high

    def as_dict(self) -> dict:
        return {"low": self.low, "high": self.high, "method": self.method, "level": self.level}


@dataclass(frozen=True, slots=True)
class Verdict:
    """The result of a comparison: a direction, or an honest refusal to name one."""

    verdict: str
    reason: str
    a: Interval | None = None
    b: Interval | None = None
    sample_a: int = 0
    sample_b: int = 0
    confidence: str = CONFIDENCE_LOW
    notes: tuple[str, ...] = ()

    @property
    def conclusive(self) -> bool:
        return self.verdict in {VERDICT_A_HIGHER, VERDICT_B_HIGHER}

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "a": self.a.as_dict() if self.a else None,
            "b": self.b.as_dict() if self.b else None,
            "sample_a": self.sample_a,
            "sample_b": self.sample_b,
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class Description:
    """Distribution facts for one group of observed values."""

    n: int
    median: float | None = None
    mean: float | None = None
    p25: float | None = None
    p75: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    insufficient: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "median": self.median,
            "mean": self.mean,
            "p25": self.p25,
            "p75": self.p75,
            "min": self.minimum,
            "max": self.maximum,
            "insufficient": self.insufficient,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------- intervals


def wilson_interval(successes: int, total: int, *, z: float = Z_95) -> Interval:
    """Wilson score interval for a proportion. Never returns a nonsensical bound."""
    if total < 0 or successes < 0:
        raise ValueError("counts must be non-negative")
    if successes > total:
        raise ValueError("successes cannot exceed total")
    if total == 0:
        return Interval(low=0.0, high=1.0, method="wilson", level=_level(z))
    p = successes / total
    denominator = 1 + (z * z) / total
    centre = (p + (z * z) / (2 * total)) / denominator
    margin = (z * math.sqrt(p * (1 - p) / total + (z * z) / (4 * total * total))) / denominator
    return Interval(
        low=max(0.0, centre - margin),
        high=min(1.0, centre + margin),
        method="wilson",
        level=_level(z),
    )


def bootstrap_interval(
    values: list[float],
    *,
    statistic: str = "median",
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    level: float = 0.95,
) -> Interval | None:
    """A seeded percentile bootstrap. Returns ``None`` below the sample gate.

    The RNG is seeded from `seed`, so the same inputs always produce the same interval. Ties and
    ordering do not matter, which is why the values are sorted first.
    """
    if len(values) < MIN_OBSERVATIONS_BOOTSTRAP:
        return None
    ordered = sorted(float(value) for value in values)
    rng = random.Random(f"{seed}:{len(ordered)}:{int(sum(ordered))}")
    reducer = stdlib_statistics.median if statistic == "median" else stdlib_statistics.fmean
    draws: list[float] = []
    for _ in range(max(200, resamples)):
        sample = [ordered[rng.randrange(len(ordered))] for _ in range(len(ordered))]
        draws.append(reducer(sample))
    draws.sort()
    alpha = (1 - level) / 2
    low = draws[int(alpha * (len(draws) - 1))]
    high = draws[int((1 - alpha) * (len(draws) - 1))]
    return Interval(low=low, high=high, method=f"bootstrap_{statistic}", level=level)


# ---------------------------------------------------------------------- description


def describe(values: list[float]) -> Description:
    """Median and quartiles, with the sample gate stated rather than hidden."""
    numbers = [float(value) for value in values]
    if not numbers:
        return Description(n=0, insufficient=True, notes=("no observed values",))
    ordered = sorted(numbers)
    if len(ordered) < MIN_OBSERVATIONS_PERCENTILE:
        return Description(
            n=len(ordered),
            median=float(stdlib_statistics.median(ordered)),
            mean=float(stdlib_statistics.fmean(ordered)),
            minimum=ordered[0],
            maximum=ordered[-1],
            insufficient=True,
            notes=(f"{len(ordered)} observations is below the {MIN_OBSERVATIONS_PERCENTILE} needed for quartiles",),
        )
    quartiles = stdlib_statistics.quantiles(ordered, n=4, method="inclusive")
    return Description(
        n=len(ordered),
        median=float(stdlib_statistics.median(ordered)),
        mean=float(stdlib_statistics.fmean(ordered)),
        p25=float(quartiles[0]),
        p75=float(quartiles[2]),
        minimum=ordered[0],
        maximum=ordered[-1],
    )


# ---------------------------------------------------------------------- comparisons


def compare_proportions(
    *,
    successes_a: int,
    total_a: int,
    successes_b: int,
    total_b: int,
    label: str = "rate",
    min_total: int = MIN_PUBLICATIONS_PER_GROUP,
) -> Verdict:
    """Compare two rates with Wilson intervals; say "inconclusive" when they overlap."""
    if total_a < min_total or total_b < min_total:
        return Verdict(
            VERDICT_INSUFFICIENT,
            f"{label}: {total_a} and {total_b} observations are below the {min_total} required",
            sample_a=total_a,
            sample_b=total_b,
        )
    rate_a = successes_a / total_a
    rate_b = successes_b / total_b
    interval_a = wilson_interval(successes_a, total_a)
    interval_b = wilson_interval(successes_b, total_b)
    if interval_a.overlaps(interval_b):
        return Verdict(
            VERDICT_INCONCLUSIVE,
            f"{label}: the 95% intervals overlap, so the difference is not distinguishable from noise",
            a=interval_a,
            b=interval_b,
            sample_a=total_a,
            sample_b=total_b,
            confidence=CONFIDENCE_LOW,
            notes=(f"{rate_a:.3f} vs {rate_b:.3f}",),
        )
    winner = VERDICT_A_HIGHER if rate_a > rate_b else VERDICT_B_HIGHER
    return Verdict(
        winner,
        f"{label}: disjoint 95% intervals",
        a=interval_a,
        b=interval_b,
        sample_a=total_a,
        sample_b=total_b,
        confidence=_confidence_for(total_a + total_b),
        notes=(f"{rate_a:.3f} vs {rate_b:.3f}",),
    )


def compare_medians(
    a: list[float],
    b: list[float],
    *,
    label: str = "median",
    min_per_group: int = MIN_PUBLICATIONS_PER_GROUP,
) -> Verdict:
    """Compare two groups of observations. Overlapping bootstrap intervals mean inconclusive."""
    if len(a) < min_per_group or len(b) < min_per_group:
        return Verdict(
            VERDICT_INSUFFICIENT,
            f"{label}: {len(a)} and {len(b)} observations are below the {min_per_group} required",
            sample_a=len(a),
            sample_b=len(b),
        )
    interval_a = bootstrap_interval(a)
    interval_b = bootstrap_interval(b)
    if interval_a is None or interval_b is None:  # pragma: no cover - gates above guarantee this
        return Verdict(VERDICT_INSUFFICIENT, f"{label}: not enough observations to resample")
    if interval_a.overlaps(interval_b):
        return Verdict(
            VERDICT_INCONCLUSIVE,
            f"{label}: the bootstrap intervals overlap",
            a=interval_a,
            b=interval_b,
            sample_a=len(a),
            sample_b=len(b),
            notes=(f"medians {stdlib_statistics.median(a):.2f} vs {stdlib_statistics.median(b):.2f}",),
        )
    winner = VERDICT_A_HIGHER if stdlib_statistics.median(a) > stdlib_statistics.median(b) else VERDICT_B_HIGHER
    return Verdict(
        winner,
        f"{label}: disjoint bootstrap intervals",
        a=interval_a,
        b=interval_b,
        sample_a=len(a),
        sample_b=len(b),
        confidence=_confidence_for(len(a) + len(b)),
    )


def confidence_band(sample_size: int) -> str:
    """A plain-language confidence band, derived from the sample only."""
    return _confidence_for(sample_size)


def _confidence_for(sample_size: int) -> str:
    if sample_size < MIN_PUBLICATIONS_PER_GROUP:
        return CONFIDENCE_LOW
    if sample_size < 30:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_HIGH


def _level(z: float) -> float:
    return 0.95 if abs(z - Z_95) < 1e-6 else round(2 * 0.5 * (1 + math.erf(z / math.sqrt(2))) - 1, 4)


def summary_for_evidence(values: list[float], *, label: str) -> dict[str, Any]:
    """The block a recommendation stores as evidence: no verdict, just the measurements."""
    description = describe(values)
    interval = bootstrap_interval(values)
    return {
        "label": label,
        "n": description.n,
        "median": description.median,
        "min": description.minimum,
        "max": description.maximum,
        "interval": interval.as_dict() if interval else None,
        "insufficient": description.insufficient,
        "notes": list(description.notes),
    }
