"""The recommendation engine: deterministic detection first, prose second (and never numbers).

Two stages, on purpose:

1. **Detection** — pure SQL and Python. Every candidate is a *measured* pattern with a sample gate, a
   Wilson or bootstrap interval, and the ids of the rows that produced it. If the gate is not met,
   nothing is emitted: "post more about X" from three posts is not a finding.
2. **Narration** — optional, and never allowed to introduce a number. The deterministic sentence is
   always available, which is why a model failure degrades quality instead of hiding a real pattern.

The eight mandated fields (requirement 7) are checked by `_validate()` before anything is stored, so a
recommendation without evidence cannot exist in the database. Acceptance creates an approval request
for *work*, never a publish job and never a publishing approval (requirement 8).
"""
from __future__ import annotations

import hashlib
import json
import statistics as stdlib_statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import (
    AnalyticsWindowKind,
    MetricAvailability,
    Platform,
    RecommendationKind,
    RecommendationStatus,
)
from ...db.models import (
    AnalyticsRecommendation,
    Campaign,
    ContentProject,
    PlatformVariant,
    PublishJob,
    SocialAccount,
)
from ...integrations.analytics.catalog import catalogue_by_key
from . import evidence, statistics, windows

#: Metrics the engine reads, by role. Missing metrics make a rule skip, never guess.
VIEWS_METRICS = ("tt.video.views", "ig.media.views", "fb.post.media_views", "fb.video.views")
INTERACTION_METRICS = {
    "tt.video.views": ("tt.video.likes", "tt.video.comments", "tt.video.shares"),
    "ig.media.views": ("ig.media.likes", "ig.media.comments", "ig.media.shares"),
    "fb.post.media_views": ("fb.post.reactions_total", "fb.post.comments", "fb.post.shares"),
    "fb.video.views": ("fb.post.reactions_total", "fb.post.comments", "fb.post.shares"),
}
CURVE_METRICS = ("fb.post.video_retention_curve", "fb.video.retention_curve", "fb.reel.retention_curve")

#: Rule gates, mirrored from plan §13.1 so they are testable constants rather than literals.
MIN_PUBLICATIONS = statistics.MIN_PUBLICATIONS_PER_GROUP
MIN_TOPIC_VIEWS = 1000
MIN_RETENTION_CASES = 3
MIN_HASHTAG_USES = 5
MIN_TIMING_PUBLICATIONS = 10
MIN_GROWTH_POINTS = 4
MIN_CAMPAIGN_PUBLICATIONS = 3
MAX_UNAVAILABLE_SHARE = 0.5

CONFIDENCE_WILSON = "wilson_95"
CONFIDENCE_BOOTSTRAP = "bootstrap_median_95"
CONFIDENCE_OVERLAP = "interval_overlap"


class RecommendationRejected(ValueError):
    """A recommendation that cannot be stored: it is missing a mandatory field."""

    code = "RECOMMENDATION_INCOMPLETE"


@dataclass(slots=True)
class PublicationPerformance:
    """One published object, as measured. Only observed values are carried."""

    publication_id: str
    provider_object_id: str
    published_at: datetime | None
    views: float | None = None
    interactions: float | None = None
    curve: list[float] | None = None
    topic: str | None = None
    format: str | None = None
    hashtags: tuple[str, ...] = ()
    campaign_id: str | None = None
    content_project_id: str | None = None
    snapshot_ids: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def engagement_rate(self) -> float | None:
        if self.views is None or self.interactions is None or self.views <= 0:
            return None
        return self.interactions / self.views


@dataclass(slots=True)
class Candidate:
    kind: RecommendationKind
    observation: str
    evidence: list[evidence.EvidenceItem]
    sample_size: dict
    confidence: float | None
    confidence_method: str
    limitations: list[str]
    proposed_experiment: dict
    expected_benefit: dict
    account_id: str | None = None
    platform: Platform | None = None
    campaign_id: str | None = None
    content_project_id: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    generated_by: str = "deterministic"

    def as_payload(self) -> dict:
        """Canonical form: what the approval hash is computed over."""
        return {
            "kind": self.kind.value,
            "observation": self.observation,
            "evidence": [item.as_dict() for item in self.evidence],
            "sample_size": self.sample_size,
            "confidence": self.confidence,
            "confidence_method": self.confidence_method,
            "limitations": self.limitations,
            "proposed_experiment": self.proposed_experiment,
            "expected_benefit": self.expected_benefit,
        }

    def payload_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_payload(), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()


# ---------------------------------------------------------------------- dataset


def load_dataset(
    session: Session,
    account: SocialAccount,
    *,
    window_kind: AnalyticsWindowKind = AnalyticsWindowKind.H24,
) -> list[PublicationPerformance]:
    """Published objects with their observed metrics at one window, missing values left missing."""
    from .aggregation import aggregations_for

    platform = account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform))
    views_key = next(
        (key for key in VIEWS_METRICS if catalogue_by_key()[key].platform is platform),
        None,
    )
    if views_key is None:
        return []

    jobs = list(
        session.scalars(
            sa.select(PublishJob).where(
                PublishJob.account_id == account.id,
                PublishJob.remote_post_id.is_not(None),
            )
        )
    )
    if not jobs:
        return []

    by_publication: dict[str, dict[str, Any]] = {}
    for row in aggregations_for(session, account=account, window_kind=window_kind):
        if row.availability is not MetricAvailability.OBSERVED:
            continue
        by_publication.setdefault(row.publication_key, {})[row.metric_key] = row

    performances: list[PublicationPerformance] = []
    for job in jobs:
        variant = session.get(PlatformVariant, job.variant_id)
        project = session.get(ContentProject, variant.project_id) if variant is not None else None
        # Topic/format are written by the composer on the per-destination variant; the project
        # metadata is the fallback for projects created before the composer existed.
        metadata = dict(getattr(variant, "metadata_json", None) or {})
        metadata = metadata or dict(getattr(project, "metadata_json", None) or {})
        rows = by_publication.get(job.id, {})
        views_row = rows.get(views_key)
        interactions = 0.0
        interaction_ids: list[str] = []
        unavailable: list[str] = []
        for key in INTERACTION_METRICS.get(views_key, ()):
            row = rows.get(key)
            if row is None:
                unavailable.append(key)
                continue
            interactions += float(row.value_num or 0.0)
            interaction_ids.extend(row.input_snapshot_ids_json or [])
        curve = None
        for key in CURVE_METRICS:
            row = rows.get(key)
            if row is not None and isinstance(row.value_json, dict):
                curve = [float(point) for point in row.value_json.get("points") or []]
                break
        performances.append(
            PublicationPerformance(
                publication_id=job.id,
                provider_object_id=str(job.remote_post_id),
                published_at=job.provider_published_at or job.created_at,
                views=float(views_row.value_num) if views_row is not None and views_row.value_num is not None else None,
                interactions=interactions if interaction_ids else None,
                curve=curve,
                topic=(metadata.get("topic") or metadata.get("pillar") or None),
                format=(metadata.get("format") or metadata.get("video_format") or None),
                hashtags=tuple(str(tag) for tag in (job.hashtags_json or [])),
                campaign_id=project.campaign_id if project is not None else None,
                content_project_id=project.id if project is not None else None,
                snapshot_ids=list(views_row.input_snapshot_ids_json or []) + interaction_ids if views_row else interaction_ids,
                unavailable=unavailable,
            )
        )
    return performances


# ---------------------------------------------------------------------- rules


def _group_by(performances: list[PublicationPerformance], attribute: str) -> dict[str, list[PublicationPerformance]]:
    groups: dict[str, list[PublicationPerformance]] = {}
    for performance in performances:
        key = getattr(performance, attribute, None)
        if not key:
            continue
        groups.setdefault(str(key), []).append(performance)
    return groups


def _rates(group: list[PublicationPerformance]) -> list[float]:
    return [performance.engagement_rate for performance in group if performance.engagement_rate is not None]


def _candidate_for_group(
    *,
    kind: RecommendationKind,
    label: str,
    attribute: str,
    group: list[PublicationPerformance],
    baseline: list[PublicationPerformance],
    direction_above: bool,
    account: SocialAccount,
    window_kind: AnalyticsWindowKind,
) -> Candidate | None:
    """Shared shape for the topic/format/hashtag rules: same gate, same statistics, same fields.

    The baseline is the *rest* of the account, never the group plus itself: comparing a group with a
    mixture that contains it guarantees overlapping intervals and hides real differences.
    """
    group_ids = {performance.publication_id for performance in group}
    baseline = [performance for performance in baseline if performance.publication_id not in group_ids]
    group_rates = _rates(group)
    baseline_rates = _rates(baseline)
    if len(group) < MIN_PUBLICATIONS or len(baseline_rates) < MIN_PUBLICATIONS:
        return None
    if sum(performance.views or 0.0 for performance in group) < MIN_TOPIC_VIEWS and kind is RecommendationKind.TOPIC:
        return None
    verdict = statistics.compare_medians(group_rates, baseline_rates, label=label)
    conclusive = verdict.verdict in {statistics.VERDICT_A_HIGHER, statistics.VERDICT_B_HIGHER}
    if not conclusive:
        return None
    if direction_above and verdict.verdict != statistics.VERDICT_A_HIGHER:
        return None
    if not direction_above and verdict.verdict != statistics.VERDICT_B_HIGHER:
        return None

    group_median = stdlib_statistics.median(group_rates)
    baseline_median = stdlib_statistics.median(baseline_rates)
    comparison_word = "above" if direction_above else "below"
    observation = (
        f"Publications with {attribute} '{label}' have a median engagement rate of "
        f"{group_median:.2%}, {comparison_word} the account median of {baseline_median:.2%} "
        f"across {len(group)} publications."
    )
    items: list[evidence.EvidenceItem] = []
    for performance in group[:20]:
        items.append(
            evidence.EvidenceItem(
                metric_key="engagement_rate",
                provider_metric_name="derived",
                unit="ratio",
                period_kind=window_kind.value,
                availability=MetricAvailability.OBSERVED.value,
                collected_at=performance.published_at,
                value=performance.engagement_rate,
                snapshot_ids=list(performance.snapshot_ids),
                group=label,
                note=f"{performance.views} views, {performance.interactions} interactions",
            )
        )
    limitations = [
        f"sample size: {len(group)} publications in the group, {len(baseline)} in the baseline",
        "observational comparison: topic, format, timing and audience overlap all differ between groups",
        f"engagement rate is interactions/views at the +{windows.spec_for(window_kind).hours} h window",
    ]
    missing = sorted({key for performance in group for key in performance.unavailable})
    if missing:
        limitations.append(f"unavailable interaction metrics: {', '.join(missing[:4])}")
    experiment = {
        "variable": attribute,
        "from": label if direction_above else f"not {label}",
        "to": f"not {label}" if direction_above else label,
        "success_metric": "engagement_rate",
        "window": window_kind.value,
        "required_sample": MIN_PUBLICATIONS,
        "stop_rule": f"stop after {MIN_PUBLICATIONS} publications or when the intervals separate",
    }
    return Candidate(
        kind=kind,
        observation=observation,
        evidence=items,
        sample_size={"group": len(group), "baseline": len(baseline), "observations": len(group_rates) + len(baseline_rates)},
        confidence=round(max(0.0, 1.0 - (verdict.a.width + verdict.b.width) / 2), 3) if verdict.a and verdict.b else None,
        confidence_method=CONFIDENCE_BOOTSTRAP,
        limitations=limitations,
        proposed_experiment=experiment,
        expected_benefit={
            "kind": "estimate",
            "range": [0.0, round(abs(group_median - baseline_median), 4)],
            "basis": "difference of medians at the same window, not a forecast",
        },
        account_id=account.id,
        platform=account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform)),
        content_project_id=group[0].content_project_id,
    )


def detect_topic(account: SocialAccount, performances: list[PublicationPerformance], window_kind) -> list[Candidate]:
    groups = _group_by(performances, "topic")
    candidates = []
    for label, group in groups.items():
        candidate = _candidate_for_group(
            kind=RecommendationKind.TOPIC,
            label=label,
            attribute="topic",
            group=group,
            baseline=performances,
            direction_above=True,
            account=account,
            window_kind=window_kind,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def detect_format(account: SocialAccount, performances: list[PublicationPerformance], window_kind) -> list[Candidate]:
    groups = _group_by(performances, "format")
    candidates = []
    for label, group in groups.items():
        candidate = _candidate_for_group(
            kind=RecommendationKind.FORMAT,
            label=label,
            attribute="format",
            group=group,
            baseline=performances,
            direction_above=False,
            account=account,
            window_kind=window_kind,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def detect_hashtag(account: SocialAccount, performances: list[PublicationPerformance], window_kind) -> list[Candidate]:
    tagged: dict[str, list[PublicationPerformance]] = {}
    untagged: list[PublicationPerformance] = []
    for performance in performances:
        if performance.hashtags:
            for tag in performance.hashtags:
                tagged.setdefault(tag, []).append(performance)
        else:
            untagged.append(performance)
    candidates = []
    for tag, group in tagged.items():
        if len(group) < MIN_HASHTAG_USES:
            continue
        candidate = _candidate_for_group(
            kind=RecommendationKind.HASHTAG,
            label=tag,
            attribute="hashtag",
            group=group,
            baseline=untagged or performances,
            direction_above=True,
            account=account,
            window_kind=window_kind,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def detect_timing(account: SocialAccount, performances: list[PublicationPerformance], window_kind) -> list[Candidate]:
    buckets: dict[str, list[PublicationPerformance]] = {}
    for performance in performances:
        if performance.published_at is None:
            continue
        moment = performance.published_at.astimezone(UTC)
        buckets.setdefault(f"{moment.strftime('%A')} {moment.hour:02d}:00 UTC", []).append(performance)
    candidates = []
    for label, group in buckets.items():
        if len(group) < MIN_TIMING_PUBLICATIONS:
            continue
        candidate = _candidate_for_group(
            kind=RecommendationKind.TIMING,
            label=label,
            attribute="publish time",
            group=group,
            baseline=performances,
            direction_above=True,
            account=account,
            window_kind=window_kind,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def detect_retention(account: SocialAccount, performances: list[PublicationPerformance]) -> list[Candidate]:
    """A cliff that keeps happening in the same bucket is worth one experiment."""
    curves = [performance for performance in performances if performance.curve and len(performance.curve) >= 4]
    if len(curves) < MIN_RETENTION_CASES:
        return []
    buckets: dict[int, int] = {}
    for performance in curves:
        points = performance.curve or []
        drops = [points[index] - points[index + 1] for index in range(len(points) - 1)]
        if not drops:
            continue
        worst = max(range(len(drops)), key=lambda index: drops[index])
        buckets[worst] = buckets.get(worst, 0) + 1
    if not buckets:
        return []
    bucket, count = max(buckets.items(), key=lambda item: item[1])
    if count < MIN_RETENTION_CASES:
        return []
    items = [
        evidence.EvidenceItem(
            metric_key="retention_curve",
            provider_metric_name="retention_graph",
            unit="ratio",
            period_kind=AnalyticsWindowKind.LIFETIME.value,
            availability=MetricAvailability.OBSERVED.value,
            collected_at=performance.published_at,
            value_json={"points": performance.curve},
            snapshot_ids=list(performance.snapshot_ids),
            group=f"bucket-{bucket}",
        )
        for performance in curves
    ]
    return [
        Candidate(
            kind=RecommendationKind.RETENTION,
            observation=(
                f"{count} of {len(curves)} measured videos lose the largest share of viewers in the same "
                f"segment of the retention curve, which suggests a structural edit rather than a one-off."
            ),
            evidence=items,
            sample_size={"publications_with_curve": len(curves), "same_cliff": count},
            confidence=round(count / len(curves), 3),
            confidence_method=CONFIDENCE_OVERLAP,
            limitations=[
                f"only {len(curves)} videos have a retention curve available",
                "the curve is provider-defined and cannot be aligned to a frame number",
                "correlation is not causation: a repeated cliff may reflect platform ranking too",
            ],
            proposed_experiment={
                "variable": "intro length",
                "change": "shorten the opening by two seconds",
                "success_metric": "retention at the cliff segment",
                "required_sample": MIN_RETENTION_CASES,
            },
            expected_benefit={"kind": "unmeasured", "range": [0.0, 0.0], "basis": "no counterfactual available"},
            account_id=account.id,
            platform=account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform)),
        )
    ]


def detect_growth(account: SocialAccount, session: Session, *, now: datetime | None = None) -> list[Candidate]:
    """A 28-day trend, only with enough observed daily points to see a slope at all."""
    from ...db.models import AnalyticsSnapshot

    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=28)
    rows = list(
        session.scalars(
            sa.select(AnalyticsSnapshot)
            .where(
                AnalyticsSnapshot.account_id == account.id,
                AnalyticsSnapshot.object_type == "account",
                AnalyticsSnapshot.availability == MetricAvailability.OBSERVED,
                AnalyticsSnapshot.collected_at >= since,
                AnalyticsSnapshot.metric_key.like("%.followers%"),
            )
            .order_by(AnalyticsSnapshot.collected_at)
        )
    )
    points = [(row.collected_at, float(row.value_num or 0.0)) for row in rows if row.value_num is not None]
    if len(points) < MIN_GROWTH_POINTS:
        return []
    first_day = min(point[0] for point in points)
    xs = [((point[0] - first_day).total_seconds() / 86400) for point in points]
    ys = [point[1] for point in points]
    slope = _slope(xs, ys)
    if slope is None or slope >= 0:
        return []
    items = [
        evidence.EvidenceItem(
            metric_key=row.metric_key,
            provider_metric_name=row.metric_key.split(".")[-1],
            unit=row.unit,
            period_kind=row.period_kind.value,
            availability=row.availability.value,
            collected_at=row.collected_at,
            value=row.value_num,
            snapshot_ids=[row.id],
            group="28-day trend",
        )
        for row in rows[:40]
    ]
    return [
        Candidate(
            kind=RecommendationKind.GROWTH,
            observation=(
                f"The follower count fell on average by {abs(slope):.1f} per day over "
                f"{len(points)} measured days in the last 28 days."
            ),
            evidence=items,
            sample_size={"observed_days": len(points)},
            confidence=None,
            confidence_method=CONFIDENCE_OVERLAP,
            limitations=[
                f"{len(points)} observations over 28 days; weekends and posting gaps are not controlled",
                "follower counts are point-in-time levels, so this is a trend, not a total",
            ],
            proposed_experiment={
                "variable": "posting cadence",
                "change": "hold cadence steady for two weeks to separate seasonality from the trend",
                "success_metric": "daily follower change",
                "required_sample": MIN_GROWTH_POINTS * 2,
            },
            expected_benefit={"kind": "estimate", "range": [0.0, abs(slope)], "basis": "observed slope, not a forecast"},
            account_id=account.id,
            platform=account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform)),
            period_start=first_day,
            period_end=points[-1][0],
        )
    ]


def detect_campaign_attention(account: SocialAccount, session: Session) -> list[Candidate]:
    """A campaign that is too small or too unmeasured to learn from: a process recommendation."""
    campaigns = list(session.scalars(sa.select(Campaign)))
    candidates: list[Candidate] = []
    for campaign in campaigns:
        jobs = list(
            session.scalars(
                sa.select(PublishJob).where(
                    PublishJob.account_id == account.id,
                    PublishJob.variant_id.in_(
                        sa.select(PlatformVariant.id).where(PlatformVariant.project_id.in_(
                            sa.select(ContentProject.id).where(ContentProject.campaign_id == campaign.id)
                        ))
                    ),
                )
            )
        )
        if not jobs:
            continue
        unavailable = 0
        for job in jobs:
            if job.remote_post_id is None:
                unavailable += 1
        share = unavailable / len(jobs)
        if len(jobs) >= MIN_CAMPAIGN_PUBLICATIONS and share <= MAX_UNAVAILABLE_SHARE:
            continue
        candidates.append(
            Candidate(
                kind=RecommendationKind.CAMPAIGN,
                observation=(
                    f"Campaign '{campaign.name}' has {len(jobs)} publication(s) for this account and "
                    f"{unavailable} without a provider id, which is not enough to measure."
                ),
                evidence=[
                    evidence.EvidenceItem(
                        metric_key="campaign_publications",
                        provider_metric_name="derived",
                        unit="count",
                        period_kind=AnalyticsWindowKind.LIFETIME.value,
                        availability=MetricAvailability.OBSERVED.value,
                        collected_at=jobs[0].created_at,
                        value=float(len(jobs)),
                        snapshot_ids=[],
                        group=campaign.id,
                        note="process measurement, not a provider metric",
                    )
                ],
                sample_size={"publications": len(jobs), "unmeasured": unavailable},
                confidence=None,
                confidence_method=CONFIDENCE_OVERLAP,
                limitations=[
                    "this is a process observation about measurement coverage, not a performance claim",
                    f"{unavailable} of {len(jobs)} publications have no provider object id",
                ],
                proposed_experiment={
                    "variable": "campaign size",
                    "change": "publish at least three measured objects before drawing conclusions",
                    "success_metric": "measured publications per campaign",
                    "required_sample": MIN_CAMPAIGN_PUBLICATIONS,
                },
                expected_benefit={"kind": "unmeasured", "range": [0.0, 0.0], "basis": "coverage, not performance"},
                account_id=account.id,
                platform=account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform)),
                campaign_id=campaign.id,
            )
        )
    return candidates


def _slope(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(set(xs)) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return numerator / denominator


# ---------------------------------------------------------------------- engine


class RecommendationEngine:
    def __init__(self, session: Session) -> None:
        self.session = session

    def detect(
        self,
        account: SocialAccount,
        *,
        window_kind: AnalyticsWindowKind = AnalyticsWindowKind.H24,
        now: datetime | None = None,
    ) -> list[Candidate]:
        performances = load_dataset(self.session, account, window_kind=window_kind)
        candidates: list[Candidate] = []
        candidates.extend(detect_topic(account, performances, window_kind))
        candidates.extend(detect_format(account, performances, window_kind))
        candidates.extend(detect_hashtag(account, performances, window_kind))
        candidates.extend(detect_timing(account, performances, window_kind))
        candidates.extend(detect_retention(account, performances))
        candidates.extend(detect_growth(account, self.session, now=now))
        candidates.extend(detect_campaign_attention(account, self.session))
        return candidates

    def store(self, candidate: Candidate, *, now: datetime | None = None) -> AnalyticsRecommendation:
        _validate(candidate)
        evidence.assert_resolvable(self.session, candidate.evidence)
        row = AnalyticsRecommendation(
            account_id=candidate.account_id,
            platform=candidate.platform,
            campaign_id=candidate.campaign_id,
            content_project_id=candidate.content_project_id,
            kind=candidate.kind,
            observation=candidate.observation,
            evidence_json=[item.as_dict() for item in candidate.evidence],
            sample_size_json=dict(candidate.sample_size),
            period_start=candidate.period_start,
            period_end=candidate.period_end,
            confidence=candidate.confidence,
            confidence_method=candidate.confidence_method,
            limitations_json=list(candidate.limitations),
            proposed_experiment_json=dict(candidate.proposed_experiment),
            expected_benefit_json=dict(candidate.expected_benefit),
            status=RecommendationStatus.DRAFT,
            generated_by=candidate.generated_by,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def generate(
        self,
        account: SocialAccount,
        *,
        window_kind: AnalyticsWindowKind = AnalyticsWindowKind.H24,
        now: datetime | None = None,
        limit: int = 10,
    ) -> list[AnalyticsRecommendation]:
        # A refresh must be idempotent: re-detecting the same pattern on the same account must not
        # pile up identical drafts. A decided recommendation is left alone too, so the history stays.
        existing = {
            (row.kind.value, row.observation)
            for row in self.session.scalars(
                sa.select(AnalyticsRecommendation).where(
                    AnalyticsRecommendation.account_id == account.id,
                    AnalyticsRecommendation.status.in_(
                        [RecommendationStatus.DRAFT, RecommendationStatus.REVIEWED]
                    ),
                )
            )
        }
        stored: list[AnalyticsRecommendation] = []
        candidates = [
            candidate
            for candidate in self.detect(account, window_kind=window_kind, now=now)
            if (candidate.kind.value, candidate.observation) not in existing
        ]
        for candidate in candidates[:limit]:
            try:
                stored.append(self.store(candidate, now=now))
            except (RecommendationRejected, evidence.UntraceableEvidence):
                # A candidate whose evidence cannot be resolved is dropped rather than stored weak.
                continue
        return stored


def _validate(candidate: Candidate) -> None:
    """Requirement 7: eight mandatory fields, checked before the row exists."""
    missing: list[str] = []
    if not candidate.observation.strip():
        missing.append("observation")
    if not candidate.evidence:
        missing.append("evidence")
    if not candidate.sample_size:
        missing.append("sample_size")
    if not candidate.limitations:
        missing.append("limitations")
    if not candidate.proposed_experiment:
        missing.append("proposed_experiment")
    if not candidate.expected_benefit:
        missing.append("expected_benefit")
    if candidate.confidence is not None and not candidate.confidence_method:
        missing.append("confidence_method")
    if candidate.confidence_method and candidate.confidence_method not in {
        CONFIDENCE_WILSON,
        CONFIDENCE_BOOTSTRAP,
        CONFIDENCE_OVERLAP,
    }:
        missing.append("confidence_method (unknown)")
    payload = candidate.as_payload()
    if "kind" not in payload:
        missing.append("kind")
    if missing:
        raise RecommendationRejected(f"missing mandatory field(s): {', '.join(missing)}")


# ---------------------------------------------------------------------- decisions


def accept(
    session: Session,
    recommendation: AnalyticsRecommendation,
    *,
    decided_by: str,
    note: str | None = None,
) -> AnalyticsRecommendation:
    """Record acceptance as a hash-bound approval request for *work*.

    Accepting never publishes and never creates a publish approval: it opens one approval request
    whose payload is exactly the recommendation that was read, so the human consented to those
    numbers and that experiment. A later publish still needs its own Phase 7 approval.
    """
    from ..approvals import ApprovalService

    payload = {
        "recommendation_id": recommendation.id,
        "kind": recommendation.kind.value,
        "observation": recommendation.observation,
        "evidence": recommendation.evidence_json,
        "sample_size": recommendation.sample_size_json,
        "confidence": recommendation.confidence,
        "confidence_method": recommendation.confidence_method,
        "limitations": recommendation.limitations_json,
        "proposed_experiment": recommendation.proposed_experiment_json,
        "expected_benefit": recommendation.expected_benefit_json,
    }
    request = ApprovalService(session).create(
        action="analytics.recommendation.accept",
        target_type="analytics_recommendation",
        target_id=recommendation.id,
        summary=f"Accept analytics recommendation: {recommendation.observation[:200]}",
        payload=payload,
        rationale=note,
        risks=list(recommendation.limitations_json or []),
        requested_by=decided_by,
    )
    recommendation.status = RecommendationStatus.ACCEPTED
    recommendation.approval_request_id = request.id
    recommendation.decided_at = datetime.now(UTC)
    recommendation.decision_note = note
    session.flush()
    return recommendation


def reject(
    session: Session,
    recommendation: AnalyticsRecommendation,
    *,
    decided_by: str,
    reason: str,
) -> AnalyticsRecommendation:
    recommendation.status = RecommendationStatus.REJECTED
    recommendation.decided_at = datetime.now(UTC)
    recommendation.decision_note = f"{decided_by}: {reason}"[:2000]
    session.flush()
    return recommendation


def narrative(candidate: Candidate) -> str:
    """The deterministic fallback text, used when a model fails or is not configured."""
    return (
        f"{candidate.observation} Evidence: {len(candidate.evidence)} measured item(s); "
        f"sample: {candidate.sample_size}. Limitations: {'; '.join(candidate.limitations)}"
    )


def list_recommendations(
    session: Session,
    *,
    status: RecommendationStatus | None = None,
    account_id: str | None = None,
    limit: int = 50,
) -> list[AnalyticsRecommendation]:
    stmt = sa.select(AnalyticsRecommendation).order_by(AnalyticsRecommendation.created_at.desc())
    if status is not None:
        stmt = stmt.where(AnalyticsRecommendation.status == status)
    if account_id is not None:
        stmt = stmt.where(AnalyticsRecommendation.account_id == account_id)
    return list(session.scalars(stmt.limit(limit)))
