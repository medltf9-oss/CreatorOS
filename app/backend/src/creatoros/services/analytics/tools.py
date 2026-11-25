"""The five analytics tools agents may call, and the evidence contract they carry.

Every result contains an `evidence` list whose items name the rows they came from
(`snapshot_ids`, `aggregation_ids`). Numbers in an agent's prose are checked against
that list, so a statistic an agent did not read cannot reach a report.

Two asymmetries are deliberate:

* A metric whose availability is not `observed` is returned *with* that availability and a NULL
  value. Nothing here converts "not authorized" into zero.
* `analytics.recommendation.propose` writes a **draft**. It cannot approve, schedule or publish, and
  its handler imports nothing from the publishing execution path.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...db.enums import AnalyticsWindowKind, MetricAvailability, Platform, RecommendationStatus
from ...db.models import AnalyticsRecommendation, SocialAccount
from ..analytics import evidence as evidence_lib
from ..analytics import recommendations as rec_lib
from ..analytics import windows as window_lib
from ..analytics.comparison import MAX_GROUPS, compare_groups, group_observations

if TYPE_CHECKING:  # `toolbox` imports this module, so the import cannot be eager.
    from ..toolbox import ToolContext


def _fail(kind: str, message: str, **detail: Any) -> Any:
    """Raise a toolbox error without importing the toolbox at module scope (circular otherwise)."""
    from .. import toolbox

    raise getattr(toolbox, kind)(message, **detail)

#: How many rows one call may return, so an agent cannot pull the whole ledger into a prompt.
MAX_ROWS = 200


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalyticsMetricsReadInput(_Base):
    account_id: str | None = None
    publication_id: str | None = None
    campaign_id: str | None = None
    window_kind: str = "h24"
    metric_keys: list[str] | None = Field(default=None, max_length=20)
    limit: int = Field(default=50, ge=1, le=MAX_ROWS)


class AnalyticsPublicationsReadInput(_Base):
    account_id: str | None = None
    platform: Literal["facebook", "instagram", "tiktok"] | None = None
    include_metrics: bool = True
    limit: int = Field(default=50, ge=1, le=MAX_ROWS)


class AnalyticsComparisonReadInput(_Base):
    account_id: str
    metric_key: str
    attribute: Literal["topic", "format", "hashtag", "platform"] = "topic"
    group_by: Literal["median", "mean"] = "median"
    window_kind: str = "h24"
    max_groups: int = Field(default=3, ge=2, le=MAX_GROUPS)


class AnalyticsRecommendationsReadInput(_Base):
    account_id: str | None = None
    status: Literal["draft", "accepted", "rejected", "expired"] | None = None
    limit: int = Field(default=10, ge=1, le=50)


class EvidenceRef(_Base):
    metric_key: str
    snapshot_ids: list[str] = Field(default_factory=list)
    aggregation_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class AnalyticsRecommendationProposeInput(_Base):
    account_id: str
    kind: Literal["topic", "format", "retention", "hashtag", "timing", "growth", "campaign"]
    observation: str = Field(min_length=10, max_length=2000)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=40)
    sample_size: dict[str, int]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_method: Literal["wilson_95", "bootstrap_median_95", "interval_overlap"] | None = None
    limitations: list[str] = Field(min_length=1, max_length=10)
    proposed_experiment: dict[str, Any]
    expected_benefit: dict[str, Any]


# ------------------------------------------------------------------ helpers


def _account(ctx: ToolContext, account_id: str) -> SocialAccount:
    account = ctx.session.get(SocialAccount, account_id)
    if account is None:
        _fail("ToolInvalidInput", f"account {account_id} does not exist")
    return account


def _evidence_from_rows(rows: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for row in rows:
        payload.append(
            {
                "metric_key": row.metric_key,
                "provider_metric_name": getattr(row, "provider_metric_name", None) or row.metric_key.split(".")[-1],
                "unit": row.unit,
                "period_kind": str(getattr(row, "period_kind", "lifetime")),
                "availability": str(row.availability),
                "value": row.value_num,
                "value_json": row.value_json,
                "collected_at": getattr(row, "collected_at", None),
                "snapshot_ids": list(getattr(row, "input_snapshot_ids_json", None) or []),
                "aggregation_ids": [row.id] if hasattr(row, "publication_key") else [],
            }
        )
    return payload


# ------------------------------------------------------------------ handlers


def analytics_metrics_read(ctx: ToolContext, payload: AnalyticsMetricsReadInput) -> dict[str, Any]:
    """Observed metrics at one window, with availability preserved and ids to cite."""
    import sqlalchemy as sa

    from ...db.models import AnalyticsAggregation

    window = _window(payload.window_kind)

    stmt = sa.select(AnalyticsAggregation).where(AnalyticsAggregation.window_kind == window)
    if payload.account_id:
        _account(ctx, payload.account_id)
        stmt = stmt.where(AnalyticsAggregation.account_id == payload.account_id)
    if payload.publication_id:
        stmt = stmt.where(AnalyticsAggregation.publication_key == payload.publication_id)
    if payload.campaign_id:
        stmt = stmt.where(AnalyticsAggregation.campaign_key == payload.campaign_id)
    if payload.metric_keys:
        stmt = stmt.where(AnalyticsAggregation.metric_key.in_(payload.metric_keys))
    rows = list(ctx.session.scalars(stmt.order_by(AnalyticsAggregation.metric_key).limit(payload.limit)))
    if not rows:
        return {
            "window": payload.window_kind,
            "metrics": [],
            "evidence": [],
            "availability_summary": {},
            "note": "No aggregation exists for this query. That is 'not collected', never zero.",
        }
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.availability)
        counts[key] = counts.get(key, 0) + 1
    return {
        "window": payload.window_kind,
        "metrics": [
            {
                "metric_key": row.metric_key,
                "value": row.value_num,
                "value_json": row.value_json,
                "unit": row.unit,
                "availability": str(row.availability),
                "method": str(row.method),
                "publication_id": row.publication_key or None,
                "account_id": row.account_id,
                "aggregation_ids": [row.id],
                "snapshot_ids": list(row.input_snapshot_ids_json or []),
            }
            for row in rows
        ],
        "evidence": _evidence_from_rows(rows),
        "availability_summary": counts,
        "note": "A value is meaningful only when availability is 'observed'; anything else is unavailable, not zero.",
    }


def analytics_publications_read(ctx: ToolContext, payload: AnalyticsPublicationsReadInput) -> dict[str, Any]:
    """Published objects with their attribution and whether metrics exist for them."""
    import sqlalchemy as sa

    from ...db.models import AnalyticsAggregation, ContentProject, PlatformVariant, PublishJob

    stmt = sa.select(PublishJob).where(PublishJob.remote_post_id.is_not(None))
    if payload.account_id:
        _account(ctx, payload.account_id)
        stmt = stmt.where(PublishJob.account_id == payload.account_id)
    if payload.platform:
        stmt = stmt.where(PublishJob.platform == Platform(payload.platform))
    jobs = list(ctx.session.scalars(stmt.order_by(PublishJob.provider_published_at.desc()).limit(payload.limit)))

    items: list[dict[str, Any]] = []
    measured_ids: dict[str, list[str]] = {}
    if payload.include_metrics and jobs:
        rows = ctx.session.scalars(
            sa.select(AnalyticsAggregation).where(
                AnalyticsAggregation.publication_key.in_([job.id for job in jobs])
            )
        )
        for row in rows:
            if row.availability == MetricAvailability.OBSERVED:
                measured_ids.setdefault(row.publication_key, []).append(row.id)

    for job in jobs:
        variant = ctx.session.get(PlatformVariant, job.variant_id)
        project = ctx.session.get(ContentProject, variant.project_id) if variant is not None else None
        items.append(
            {
                "publication_id": job.id,
                "account_id": job.account_id,
                "platform": str(job.platform),
                "remote_post_id": job.remote_post_id,
                "published_at_utc": job.provider_published_at,
                "state": str(job.job_state),
                "campaign_id": project.campaign_id if project is not None else None,
                "content_project_id": project.id if project is not None else None,
                "variant_id": variant.id if variant is not None else None,
                "title": variant.title if variant is not None else None,
                "hashtags": list(job.hashtags_json or []),
                "metadata": dict(getattr(variant, "metadata_json", None) or {}),
                "has_metrics": bool(measured_ids.get(job.id)),
                "aggregation_ids": measured_ids.get(job.id, []),
            }
        )
    return {
        "publications": items,
        "count": len(items),
        "with_metrics": sum(1 for item in items if item["has_metrics"]),
        "note": "has_metrics is false when nothing has been collected yet: unknown, not zero performance.",
    }


def analytics_comparison_read(ctx: ToolContext, payload: AnalyticsComparisonReadInput) -> dict[str, Any]:
    """A deterministic comparison: group medians, intervals, sample sizes and explicit refusals."""
    import sqlalchemy as sa

    from ...db.models import AnalyticsSnapshot, ContentProject, PlatformVariant, PublishJob

    account = _account(ctx, payload.account_id)
    window = _window(payload.window_kind)

    jobs = list(
        ctx.session.scalars(
            sa.select(PublishJob)
            .where(PublishJob.account_id == account.id, PublishJob.remote_post_id.is_not(None))
            .order_by(PublishJob.provider_published_at.desc())
            .limit(MAX_ROWS)
        )
    )
    if not jobs:
        _fail("ToolUnavailable", "this account has no published objects with a provider id", code="NO_PUBLICATIONS")

    # One in-tolerance observation per publication at the requested window, never the nearest
    # arbitrary read: `select_window` refuses anything outside its tolerance.
    selected: list[tuple[Any, dict[str, Any]]] = []
    snapshot_ids: list[str] = []
    for job in jobs:
        selection = window_lib.select_window(
            ctx.session,
            account_id=account.id,
            provider_object_id=str(job.remote_post_id),
            metric_key=payload.metric_key,
            window_kind=window,
            published_at=job.provider_published_at or job.created_at,
        )
        if selection.snapshot_id is None:
            continue
        variant = ctx.session.get(PlatformVariant, job.variant_id)
        project = ctx.session.get(ContentProject, variant.project_id) if variant is not None else None
        metadata = dict(getattr(variant, "metadata_json", None) or {})
        selected.append(
            (
                job,
                {
                    "snapshot_id": selection.snapshot_id,
                    "value": selection.value_num,
                    "topic": metadata.get("topic") or metadata.get("pillar"),
                    "format": metadata.get("format") or metadata.get("video_format"),
                    "hashtags": list(job.hashtags_json or []),
                    "campaign_id": project.campaign_id if project is not None else None,
                },
            )
        )
        snapshot_ids.append(selection.snapshot_id)

    if not selected:
        return {
            "metric_key": payload.metric_key,
            "window": payload.window_kind,
            "groups": [],
            "result": None,
            "limitations": [
                (
                    f"no observation landed inside the +{window_lib.spec_for(window).hours} h window "
                    f"(±{window_lib.spec_for(window).tolerance_minutes} min) for this metric"
                )
            ],
            "note": "An empty result means 'not measured at this window', not 'zero'.",
        }

    snapshots = {
        row.id: row
        for row in ctx.session.scalars(
            sa.select(AnalyticsSnapshot).where(AnalyticsSnapshot.id.in_(snapshot_ids))
        )
    }
    members: dict[str, list[AnalyticsSnapshot]] = {}
    for _job, info in selected:
        snapshot = snapshots.get(info["snapshot_id"])
        if snapshot is None:
            continue
        for label in _labels_for(info, payload.attribute):
            members.setdefault(label, []).append(snapshot)

    # Keep the best-populated groups, deterministically ordered, within the comparison cap.
    ranked = sorted(members.items(), key=lambda item: (-len(item[1]), item[0]))[: payload.max_groups]
    cleaned: dict[str, list[Any]] = {}
    for label, rows in ranked:
        # One row per object: the latest observation wins, so a re-collection cannot double count.
        cleaned[label] = [snapshot for group in group_observations(rows).values() for snapshot in group]
    comparison = compare_groups(
        ctx.session,
        metric_key=payload.metric_key,
        window_kind=window,
        groups=cleaned,
        label=payload.metric_key,
    )
    payload_out = comparison.as_dict()
    value_by_id = {snapshot.id: snapshot.value_num for snapshot in snapshots.values()}
    return {
        "metric_key": payload.metric_key,
        "attribute": payload.attribute,
        "window": payload.window_kind,
        "groups": [
            {
                "label": group.label,
                "n": group.n,
                "availability": group.availability.value,
                "median": group.description.median,
                "evidence": [
                    {
                        "metric_key": payload.metric_key,
                        "snapshot_ids": [snapshot_id],
                        "aggregation_ids": [],
                        "value": value_by_id.get(snapshot_id),
                        "collected_at": snapshots[snapshot_id].collected_at if snapshot_id in snapshots else None,
                    }
                    for snapshot_id in group.snapshot_ids
                ],
            }
            for group in comparison.groups
        ],
        "verdicts": payload_out["verdicts"],
        "comparability": payload_out["comparability"],
        "limitations": payload_out["limitations"],
        "sufficient": payload_out["sufficient"],
        "note": "'inconclusive' is a result, not a failure: overlapping intervals do not rank groups.",
    }


def _labels_for(info: dict[str, Any], attribute: str) -> list[str]:
    if attribute == "hashtag":
        return list(info.get("hashtags") or [])
    if attribute == "platform":
        return ["platform"]
    value = info.get(attribute)
    return [str(value)] if value else []


def _window(name: str) -> AnalyticsWindowKind:
    try:
        window = AnalyticsWindowKind(name)
    except ValueError:
        _fail("ToolInvalidInput", f"{name!r} is not a known window")
    if window not in window_lib.WINDOW_SPECS:
        _fail("ToolInvalidInput", f"{name!r} has no fixed target offset and cannot be compared")
    return window


def analytics_recommendations_read(ctx: ToolContext, payload: AnalyticsRecommendationsReadInput) -> dict[str, Any]:
    """Stored recommendations, each already carrying its mandated fields."""
    status = RecommendationStatus(payload.status) if payload.status else None
    rows: list[AnalyticsRecommendation] = rec_lib.list_recommendations(
        ctx.session, status=status, account_id=payload.account_id, limit=payload.limit
    )
    return {
        "recommendations": [
            {
                "recommendation_id": row.id,
                "kind": str(row.kind),
                "status": str(row.status),
                "observation": row.observation,
                "evidence": list(row.evidence_json or []),
                "sample_size": dict(row.sample_size_json or {}),
                "confidence": row.confidence,
                "confidence_method": row.confidence_method,
                "limitations": list(row.limitations_json or []),
                "proposed_experiment": dict(row.proposed_experiment_json or {}),
                "expected_benefit": dict(row.expected_benefit_json or {}),
                "account_id": row.account_id,
                "platform": str(row.platform) if row.platform else None,
            }
            for row in rows
        ],
        "count": len(rows),
        "note": "Recommendations are drafts. Accepting one is a human decision and never publishes.",
    }


def analytics_recommendation_propose(ctx: ToolContext, payload: AnalyticsRecommendationProposeInput) -> dict[str, Any]:
    """Store a draft recommendation whose numbers must already exist in the evidence."""
    account = _account(ctx, payload.account_id)
    from ...db.models import AnalyticsAggregation, AnalyticsSnapshot

    items: list[evidence_lib.EvidenceItem] = []
    for ref in payload.evidence:
        for row in evidence_lib.resolve(
            ctx.session,
            [{"snapshot_ids": ref.snapshot_ids, "aggregation_ids": ref.aggregation_ids}],
        ):
            if isinstance(row, AnalyticsAggregation):
                items.append(evidence_lib.from_aggregation(ctx.session, row))
            elif isinstance(row, AnalyticsSnapshot):
                items.append(evidence_lib.from_snapshot(ctx.session, row))
    if not items:
        _fail(
            "ToolInvalidInput",
            "evidence_refs did not resolve to any stored observation: cite snapshot_ids or aggregation_ids",
            code="UNTRACEABLE_EVIDENCE",
        )
    try:
        evidence_lib.assert_numbers_traceable(payload.observation, items, allow=payload.sample_size)
    except evidence_lib.UntraceableEvidence as exc:
        _fail("ToolInvalidInput", str(exc), code="UNTRACEABLE_NUMBER")

    candidate = rec_lib.Candidate(
        kind=rec_lib.RecommendationKind(payload.kind),
        observation=payload.observation,
        evidence=items,
        sample_size=dict(payload.sample_size),
        confidence=payload.confidence,
        confidence_method=payload.confidence_method or rec_lib.CONFIDENCE_OVERLAP,
        limitations=list(payload.limitations),
        proposed_experiment=dict(payload.proposed_experiment),
        expected_benefit=dict(payload.expected_benefit),
        account_id=account.id,
        platform=account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform)),
        generated_by=f"agent:{ctx.agent_id}",
    )
    try:
        stored = rec_lib.RecommendationEngine(ctx.session).store(candidate)
    except rec_lib.RecommendationRejected as exc:
        _fail("ToolInvalidInput", str(exc), code=rec_lib.RecommendationRejected.code)
    except evidence_lib.UntraceableEvidence as exc:
        _fail("ToolInvalidInput", str(exc), code="UNTRACEABLE_EVIDENCE")
    ctx.session.commit()
    return {
        "recommendation_id": stored.id,
        "status": str(stored.status),
        "kind": str(stored.kind),
        "evidence_count": len(items),
        "created_at": datetime.now(UTC),
        "next_step": (
            "A human reviews this draft and may accept or reject it. Acceptance records an approval "
            "request for the experiment; it never schedules or publishes anything."
        ),
    }
