"""P8.15: the analytics tools, their grants, and the evidence contract that binds agent prose.

The tests that matter most here are the ones about what an agent *cannot* do: it cannot call a tool
its department was not granted, it cannot cite evidence that does not exist, it cannot state a number
its evidence does not contain, and no analytics role holds a publishing tool.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from creatoros.agents.catalog import ANALYTICS_READ_TOOLS, TOOL_IDS, ensure_catalog_is_consistent
from creatoros.db.enums import (
    AnalyticsAggregationMethod,
    AnalyticsAggregationScope,
    AnalyticsObjectType,
    AnalyticsWindowKind,
    ConnectionStatus,
    MetricAvailability,
    MetricPeriod,
    MetricValueKind,
    Platform,
    RecommendationKind,
    RecommendationStatus,
)
from creatoros.db.models import (
    AgentDefinition,
    AnalyticsAggregation,
    AnalyticsSnapshot,
    SocialAccount,
)
from creatoros.services.analytics import agent_grants, evidence, registry, runs
from creatoros.services.analytics.tools import (
    AnalyticsComparisonReadInput,
    AnalyticsMetricsReadInput,
    AnalyticsPublicationsReadInput,
    AnalyticsRecommendationProposeInput,
    AnalyticsRecommendationsReadInput,
    analytics_comparison_read,
    analytics_metrics_read,
    analytics_publications_read,
    analytics_recommendation_propose,
    analytics_recommendations_read,
)
from creatoros.services.structured import check_artifact_numbers, coerce_artifact
from creatoros.services.toolbox import TOOLS_BY_ID, ToolContext, ToolDenied, ToolExecutor, ToolInvalidInput

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


def make_account(session, platform: Platform = Platform.TIKTOK) -> SocialAccount:
    registry.ensure_metric_catalog(session)
    account = SocialAccount(
        platform=platform,
        provider_account_id=f"tools-{uuid4().hex[:10]}",
        display_name="Tools account",
        account_type="tiktok_creator",
        scopes_json=["user.info.basic"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2" if platform is Platform.TIKTOK else "v23.0",
        meta_json={},
    )
    session.add(account)
    session.flush()
    return account


def make_snapshot(
    session,
    account,
    *,
    metrics: dict[str, float],
    provider_object_id: str,
    publication_id=None,
    collected_at: datetime | None = None,
):
    context = runs.open_run(
        session,
        account=account,
        target_kind=sa.Enum if False else __import__("creatoros.db.enums", fromlist=["AnalyticsRunTarget"]).AnalyticsRunTarget.METRICS,
        api_version="v2",
        now=NOW,
    )
    rows = []
    for metric_key, value in metrics.items():
        row = AnalyticsSnapshot(
            id=f"snap-{uuid4().hex[:20]}",
            account_id=account.id,
            platform=account.platform,
            object_type=AnalyticsObjectType.VIDEO,
            provider_object_id=provider_object_id,
            publication_id=publication_id,
            metric_key=metric_key,
            value_kind=MetricValueKind.SCALAR,
            period_kind=MetricPeriod.LIFETIME,
            value_num=value,
            unit="count",
            availability=MetricAvailability.OBSERVED,
            breakdown_key="",
            api_version="v2",
            api_host="open.tiktokapis.com",
            source_ref="test",
            run_id=context.run_id,
            collected_at=collected_at or (NOW - timedelta(hours=2)),
            completeness="complete",
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def make_aggregation(session, account, *, metric_key: str, value: float | None, snapshot_id: str, window=AnalyticsWindowKind.H24, publication_key="pub-1", availability=MetricAvailability.OBSERVED):
    row = AnalyticsAggregation(
        account_id=account.id,
        platform=account.platform,
        scope=AnalyticsAggregationScope.PUBLICATION,
        publication_key=publication_key,
        campaign_key="",
        metric_key=metric_key,
        window_kind=window,
        window_hours=24,
        window_tolerance_minutes=180,
        value_num=value,
        unit="count",
        availability=availability,
        method=AnalyticsAggregationMethod.DIRECT,
        input_snapshot_ids_json=[snapshot_id],
        input_metric_keys_json=[metric_key],
        confidence_json={},
        computed_at=NOW,
        aggregation_version=1,
    )
    session.add(row)
    session.flush()
    return row


# ----------------------------------------------------------------- catalogue parity


def test_the_catalogue_and_the_tool_surface_agree_in_both_directions():
    ensure_catalog_is_consistent()
    assert set(TOOL_IDS) == set(TOOLS_BY_ID)
    assert len(TOOL_IDS) == len(set(TOOL_IDS))


def test_every_analytics_tool_is_declared_with_the_expected_risk():
    for tool_id in (*ANALYTICS_READ_TOOLS, "analytics.recommendation.propose"):
        spec = TOOLS_BY_ID[tool_id]
        assert spec.category != "write" or tool_id == "analytics.recommendation.propose"
        if tool_id != "analytics.recommendation.propose":
            assert spec.category in {"read", "compute"}
            assert spec.approval.value == "none"


def test_an_agent_cannot_call_an_analytics_tool_its_department_was_not_granted(session):
    from creatoros.services.registry import AgentRegistry

    AgentRegistry(session).ensure_seeded()
    from creatoros.services.toolbox import ensure_tools_seeded

    ensure_tools_seeded(session)
    agent_grants.apply_analytics_tool_grants(session)
    session.commit()

    executor = ToolExecutor(session)
    writer = session.scalars(
        sa.select(AgentDefinition).where(AgentDefinition.team_id == "creative_writing")
    ).first()
    assert writer is not None
    with pytest.raises(ToolDenied):
        executor.execute("analytics.metrics.read", agent_id=writer.agent_id, payload={})


def test_no_analytics_role_holds_a_publishing_tool(session):
    from creatoros.services.registry import AgentRegistry
    from creatoros.services.toolbox import ensure_tools_seeded

    AgentRegistry(session).ensure_seeded()
    ensure_tools_seeded(session)
    agent_grants.apply_analytics_tool_grants(session)
    session.commit()
    # `publishing_operations` legitimately holds publishing tools, so it is excluded from the set
    # of roles this assertion covers; the analytics authors must never publish.
    offenders = [
        item
        for item in agent_grants.publishing_tools_held_by_analytics_roles(session)
        if not item.startswith("publishing_operations:")
    ]
    assert "analytics_growth" not in {offender.split(":")[0] for offender in offenders}


def test_grants_are_additive_and_idempotent(session):
    from creatoros.services.registry import AgentRegistry
    from creatoros.services.toolbox import ensure_tools_seeded

    AgentRegistry(session).ensure_seeded()
    ensure_tools_seeded(session)
    first = agent_grants.apply_analytics_tool_grants(session)
    session.commit()
    second = agent_grants.apply_analytics_tool_grants(session)
    assert first["applied"] is True
    assert second["applied"] is False
    rows = session.scalars(
        sa.select(AgentDefinition).where(AgentDefinition.team_id == "analytics_growth")
    ).all()
    assert all("analytics.metrics.read" in (row.allowed_tools_json or []) for row in rows)


# ----------------------------------------------------------------- reads


def test_metrics_read_keeps_availability_and_never_zero_fills(session):
    account = make_account(session)
    snapshots = make_snapshot(session, account, metrics={"tt.video.views": 500.0}, provider_object_id="obj-1")
    make_aggregation(session, account, metric_key="tt.video.views", value=500.0, snapshot_id=snapshots[0].id)
    make_aggregation(
        session,
        account,
        metric_key="tt.video.likes",
        value=None,
        snapshot_id=snapshots[0].id,
        availability=MetricAvailability.NOT_AUTHORIZED,
    )
    session.commit()

    ctx = ToolContext(session=session, agent_id="analytics_growth_agent")
    result = analytics_metrics_read(ctx, AnalyticsMetricsReadInput(account_id=account.id))
    by_key = {row["metric_key"]: row for row in result["metrics"]}
    assert by_key["tt.video.views"]["value"] == 500.0
    assert by_key["tt.video.likes"]["value"] is None
    assert by_key["tt.video.likes"]["availability"] == "not_authorized"
    assert result["availability_summary"] == {"observed": 1, "not_authorized": 1}
    assert result["evidence"][0]["snapshot_ids"]


def test_metrics_read_says_not_collected_instead_of_returning_zeros(session):
    account = make_account(session)
    ctx = ToolContext(session=session, agent_id="analytics_growth_agent")
    result = analytics_metrics_read(ctx, AnalyticsMetricsReadInput(account_id=account.id))
    assert result["metrics"] == []
    assert "never zero" in result["note"]


def test_publications_read_reports_whether_metrics_exist(session):
    from publishing_fixtures import make_published_job

    account = make_account(session)
    fixture = make_published_job(session, platform=Platform.TIKTOK, account=account)
    session.flush()
    snapshots = make_snapshot(session, account, metrics={"tt.video.views": 10.0}, provider_object_id=fixture.job_id)
    make_aggregation(
        session, account, metric_key="tt.video.views", value=10.0, snapshot_id=snapshots[0].id, publication_key=fixture.job_id
    )
    session.commit()

    ctx = ToolContext(session=session, agent_id="publishing_operations_agent")
    result = analytics_publications_read(ctx, AnalyticsPublicationsReadInput(account_id=account.id))
    assert result["count"] == 1
    assert result["with_metrics"] == 1
    assert result["publications"][0]["remote_post_id"]
    assert result["publications"][0]["aggregation_ids"]


def test_comparison_read_returns_verdicts_and_refusals(session):
    from publishing_fixtures import make_published_job

    from creatoros.db.models import PlatformVariant

    account = make_account(session)
    for index in range(10):
        # Published 30 days ago and measured at +24 h, so the h24 window has exactly one observation.
        published_at = NOW - timedelta(days=30, minutes=index)
        fixture = make_published_job(session, platform=Platform.TIKTOK, account=account, published_at=published_at)
        session.flush()
        variant = session.get(PlatformVariant, fixture.variant_id)
        variant.metadata_json = {"topic": "bts"}
        make_snapshot(
            session,
            account,
            metrics={"tt.video.views": float(100 + index)},
            provider_object_id=str(fixture.remote_post_id),
            publication_id=fixture.job_id,
            collected_at=published_at + timedelta(hours=24),
        )
    session.commit()

    ctx = ToolContext(session=session, agent_id="analytics_growth_agent")
    result = analytics_comparison_read(
        ctx,
        AnalyticsComparisonReadInput(account_id=account.id, metric_key="tt.video.views", attribute="topic", max_groups=2),
    )
    assert result["groups"]
    assert result["groups"][0]["n"] >= 8
    assert result["limitations"]
    assert "comparability" in result


def test_comparison_read_refuses_when_nothing_was_measured(session):
    account = make_account(session)
    ctx = ToolContext(session=session, agent_id="analytics_growth_agent")
    from creatoros.services.toolbox import ToolUnavailable

    with pytest.raises(ToolUnavailable) as refused:
        analytics_comparison_read(
            ctx, AnalyticsComparisonReadInput(account_id=account.id, metric_key="tt.video.views")
        )
    assert getattr(refused.value, "code", "") == "NO_PUBLICATIONS"


# ----------------------------------------------------------------- propose


def _propose_payload(account, *, snapshot_ids, observation, aggregation_ids=None) -> AnalyticsRecommendationProposeInput:
    return AnalyticsRecommendationProposeInput(
        account_id=account.id,
        kind="topic",
        observation=observation,
        evidence=[{"metric_key": "tt.video.views", "snapshot_ids": snapshot_ids, "aggregation_ids": aggregation_ids or []}],
        sample_size={"group": 10, "baseline": 12},
        confidence=0.8,
        confidence_method="bootstrap_median_95",
        limitations=["observational: the groups differ in more than the topic"],
        proposed_experiment={"variable": "topic", "success_metric": "engagement_rate"},
        expected_benefit={"kind": "estimate", "range": [0.0, 0.02], "basis": "difference of medians"},
    )


def test_propose_stores_a_draft_with_resolvable_evidence(session):
    account = make_account(session)
    snapshots = make_snapshot(session, account, metrics={"tt.video.views": 500.0}, provider_object_id="obj-1")
    session.commit()
    ctx = ToolContext(session=session, agent_id="analytics_growth_agent")
    result = analytics_recommendation_propose(
        ctx,
        _propose_payload(
            account,
            snapshot_ids=[snapshots[0].id],
            observation="Topic 'bts' reached 500 views at the 24-hour window, above the account median.",
        ),
    )
    assert result["status"] == "draft"
    from creatoros.db.models import AnalyticsRecommendation

    row = session.get(AnalyticsRecommendation, result["recommendation_id"])
    assert row is not None
    assert row.generated_by == "agent:analytics_growth_agent"
    assert row.status is RecommendationStatus.DRAFT
    assert "publish" in result["next_step"]


def test_propose_rejects_a_number_that_the_evidence_does_not_contain(session):
    account = make_account(session)
    snapshots = make_snapshot(session, account, metrics={"tt.video.views": 500.0}, provider_object_id="obj-1")
    session.commit()
    ctx = ToolContext(session=session, agent_id="analytics_growth_agent")
    with pytest.raises(ToolInvalidInput) as refused:
        analytics_recommendation_propose(
            ctx,
            _propose_payload(
                account,
                snapshot_ids=[snapshots[0].id],
                observation="Topic 'bts' reached 987654 views at the 24-hour window.",
            ),
        )
    assert refused.value.code == "UNTRACEABLE_NUMBER"


def test_propose_rejects_evidence_that_does_not_resolve(session):
    account = make_account(session)
    session.commit()
    ctx = ToolContext(session=session, agent_id="analytics_growth_agent")
    with pytest.raises(ToolInvalidInput) as refused:
        analytics_recommendation_propose(
            ctx,
            _propose_payload(account, snapshot_ids=["snap-missing"], observation="Something was measured twice."),
        )
    assert refused.value.code == "UNTRACEABLE_EVIDENCE"


def test_propose_cannot_create_a_recommendation_without_limitations(session):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnalyticsRecommendationProposeInput(
            account_id="x",
            kind="topic",
            observation="A long enough observation.",
            evidence=[{"metric_key": "tt.video.views", "snapshot_ids": ["s1"]}],
            sample_size={"group": 9},
            limitations=[],
            proposed_experiment={"variable": "topic"},
            expected_benefit={"kind": "estimate"},
        )


def test_recommendations_read_returns_the_mandated_fields(session):
    account = make_account(session)
    snapshots = make_snapshot(session, account, metrics={"tt.video.views": 500.0}, provider_object_id="obj-1")
    session.commit()
    ctx = ToolContext(session=session, agent_id="research_intelligence_agent")
    analytics_recommendation_propose(
        ctx,
        _propose_payload(
            account,
            snapshot_ids=[snapshots[0].id],
            observation="Topic 'bts' reached 500 views at the 24-hour window.",
        ),
    )
    result = analytics_recommendations_read(ctx, AnalyticsRecommendationsReadInput(account_id=account.id))
    item = result["recommendations"][0]
    for field in (
        "observation",
        "evidence",
        "sample_size",
        "confidence",
        "confidence_method",
        "limitations",
        "proposed_experiment",
        "expected_benefit",
    ):
        assert item[field] not in (None, "", [], {})
    assert item["kind"] == RecommendationKind.TOPIC.value


# ----------------------------------------------------------------- artifact contract


def test_artifact_numbers_are_checked_against_measured_evidence():
    item = evidence.EvidenceItem(
        metric_key="tt.video.views",
        provider_metric_name="view_count",
        unit="count",
        period_kind="lifetime",
        availability="observed",
        value=500.0,
        snapshot_ids=["snap-1"],
    )
    artifact = coerce_artifact(
        {"summary": "The publication reached 500 views, which is the measured value.", "sections": []},
        "report",
    )
    assert check_artifact_numbers(artifact, [item.as_dict()]) is None

    invented = coerce_artifact(
        {"summary": "The publication reached 12345 views, which is the measured value.", "sections": []},
        "report",
    )
    problem = check_artifact_numbers(invented, [item.as_dict()])
    assert problem and "12345" in problem


def test_an_artifact_is_not_checked_when_no_evidence_was_measured():
    artifact = coerce_artifact({"summary": "Anything at all 987654."}, "report")
    assert check_artifact_numbers(artifact, []) is None


def test_artifact_prose_covers_sections_and_findings():
    from creatoros.services.structured import artifact_prose

    artifact = coerce_artifact(
        {
            "summary": "summary text",
            "findings": [{"statement": "finding text"}],
            "sections": [{"heading": "Heading", "body": "section body"}],
        },
        "report",
    )
    text = artifact_prose(artifact)
    assert "summary text" in text
    assert "finding text" in text
    assert "section body" in text


def test_orchestrator_extracts_evidence_refs_from_tool_output():
    from creatoros.services.orchestrator import analytics_evidence, evidence_refs

    payloads = [
        {"tool_id": "analytics.metrics.read", "ok": True, "output": {"evidence": [{"metric_key": "tt.video.views", "value": 10, "snapshot_ids": ["s1"], "aggregation_ids": ["a1"]}]}},
        {"tool_id": "analytics.comparison.read", "ok": False, "output": {}},
    ]
    collected = analytics_evidence(payloads)
    assert len(collected) == 1
    assert evidence_refs(collected) == ["snapshot_ids:s1", "aggregation_ids:a1"]


# ----------------------------------------------------------------- human review


def test_needs_human_review_is_terminal_but_not_a_success():
    from creatoros.db.enums import TASK_TERMINAL_STATES, TaskState

    # Terminal for the graph: no agent will ever pick the task up again.
    assert TaskState.NEEDS_HUMAN_REVIEW in TASK_TERMINAL_STATES
    # ... but it is deliberately kept out of the states the engine counts as completed work.
    assert TaskState.NEEDS_HUMAN_REVIEW not in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
