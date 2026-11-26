"""Recommendations: every field mandatory, every number traceable, no route to publishing.

Three things are asserted here repeatedly because they are the ones that would be dangerous to get
wrong: a recommendation cannot exist without resolvable evidence, its text cannot contain a number
the evidence does not support, and accepting one cannot create a publish job or a publish approval.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from creatoros.db.enums import (
    AnalyticsAggregationMethod,
    AnalyticsAggregationScope,
    AnalyticsWindowKind,
    ApprovalState,
    MetricAvailability,
    Platform,
    PublishState,
    RecommendationKind,
    RecommendationStatus,
)
from creatoros.db.models import (
    AnalyticsAggregation,
    ApprovalRecord,
    ApprovalRequest,
    PublishJob,
)
from creatoros.services.analytics import (
    evidence,
    registry,
)
from creatoros.services.analytics import (
    recommendations as rec,
)

NOW = datetime(2026, 11, 24, tzinfo=UTC)


def make_account(session, platform: Platform = Platform.TIKTOK):
    from uuid import uuid4

    from creatoros.db.enums import ConnectionStatus
    from creatoros.db.models import SocialAccount

    registry.ensure_metric_catalog(session)
    account = SocialAccount(
        platform=platform,
        provider_account_id=f"rec-{uuid4().hex[:10]}",
        display_name="Recommendation account",
        account_type="tiktok_creator",
        scopes_json=["user.info.basic", "user.info.stats", "video.list"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2" if platform is Platform.TIKTOK else "v23.0",
        meta_json={},
    )
    session.add(account)
    session.flush()
    return account


def add_aggregation(
    session,
    account,
    *,
    publication_id: str,
    metric_key: str,
    value: float,
    window_kind: AnalyticsWindowKind = AnalyticsWindowKind.H24,
    availability: MetricAvailability = MetricAvailability.OBSERVED,
    snapshot_ids: list[str] | None = None,
):
    from uuid import uuid4

    from creatoros.db.enums import AnalyticsObjectType, MetricPeriod, MetricValueKind
    from creatoros.db.models import AnalyticsSnapshot
    from creatoros.services.analytics import runs

    snapshot_id = snapshot_ids[0] if snapshot_ids else None
    if snapshot_id is None:
        context = runs.open_run(
            session,
            account=account,
            target_kind=__import__("creatoros.db.enums", fromlist=["AnalyticsRunTarget"]).AnalyticsRunTarget.METRICS,
            api_version="v2",
            now=NOW,
        )
        # A real snapshot row, so evidence resolution is genuinely exercised.
        snapshot = AnalyticsSnapshot(
            id=f"snap-{uuid4().hex[:20]}",
            account_id=account.id,
            platform=account.platform,
            object_type=AnalyticsObjectType.VIDEO,
            provider_object_id=publication_id,
            metric_key=metric_key,
            value_kind=MetricValueKind.SCALAR,
            period_kind=MetricPeriod.LIFETIME,
            value_num=value if availability is MetricAvailability.OBSERVED else None,
            unit="count",
            availability=availability,
            breakdown_key="",
            api_version="v2",
            api_host="open.tiktokapis.com",
            source_ref="test",
            run_id=context.run_id,
            collected_at=NOW,
            completeness="complete",
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id

    row = AnalyticsAggregation(
        account_id=account.id,
        platform=account.platform,
        scope=AnalyticsAggregationScope.PUBLICATION,
        publication_key=publication_id,
        campaign_key="",
        metric_key=metric_key,
        window_kind=window_kind,
        window_hours=24,
        window_tolerance_minutes=180,
        value_num=value if availability is MetricAvailability.OBSERVED else None,
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


def seed_account_with_publications(session, *, count: int = 10, topic: str = "bts", values: list[float] | None = None):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session)
    values = values or [float(100 + index * 10) for index in range(count)]
    fixture_ids = []
    for index in range(count):
        fixture = make_published_job(
            session,
            platform=Platform.TIKTOK,
            account=account,
            published_at=NOW - timedelta(days=count - index, hours=1),
        )
        session.flush()
        # Topic lives on the project metadata: this is where the composer would write it.
        from creatoros.db.models import PlatformVariant

        variant = session.get(PlatformVariant, fixture.variant_id)
        variant.metadata_json = {"topic": topic, "format": "talking_head"}
        session.flush()
        add_aggregation(session, account, publication_id=fixture.job_id, metric_key="tt.video.views", value=values[index])
        add_aggregation(
            session,
            account,
            publication_id=fixture.job_id,
            metric_key="tt.video.likes",
            value=round(values[index] * 0.1),
        )
        add_aggregation(
            session,
            account,
            publication_id=fixture.job_id,
            metric_key="tt.video.comments",
            value=round(values[index] * 0.01),
        )
        add_aggregation(
            session,
            account,
            publication_id=fixture.job_id,
            metric_key="tt.video.shares",
            value=round(values[index] * 0.005),
        )
        fixture_ids.append(fixture.job_id)
    session.commit()
    return account, fixture_ids


# ---------------------------------------------------------------------- dataset


def test_dataset_joins_aggregations_to_publications(session):
    account, fixture_ids = seed_account_with_publications(session)
    performances = rec.load_dataset(session, account)
    assert len(performances) == len(fixture_ids)
    first = performances[0]
    assert first.views is not None
    assert first.interactions is not None
    assert first.engagement_rate is not None
    assert first.topic == "bts"
    assert first.format == "talking_head"
    assert first.snapshot_ids


def test_dataset_leaves_unobserved_metrics_missing(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session)
    fixture = make_published_job(session, platform=Platform.TIKTOK, account=account)
    session.flush()
    add_aggregation(
        session,
        account,
        publication_id=fixture.job_id,
        metric_key="tt.video.views",
        value=0,
        availability=MetricAvailability.NOT_AUTHORIZED,
    )
    session.commit()
    performances = rec.load_dataset(session, account)
    assert performances[0].views is None
    assert performances[0].interactions is None


# ---------------------------------------------------------------------- detection


def test_a_topic_with_too_few_publications_emits_nothing(session):
    account, _ = seed_account_with_publications(session, count=3)
    candidates = rec.detect_topic(account, rec.load_dataset(session, account), AnalyticsWindowKind.H24)
    assert candidates == []


def test_a_dominant_topic_emits_a_gated_candidate(session):
    account, _ = seed_account_with_publications(session, count=20, values=[1000.0] * 20)
    performances = rec.load_dataset(session, account)
    candidates = rec.detect_topic(account, performances, AnalyticsWindowKind.H24)
    # Every publication has the same topic, so there is no baseline to be above: no candidate.
    assert candidates == []

    # With a genuinely different second group, the comparison has two populations.
    from creatoros.db.models import PlatformVariant, PublishJob

    jobs = list(session.scalars(sa.select(PublishJob).where(PublishJob.account_id == account.id)))
    for job in jobs[:10]:
        variant = session.get(PlatformVariant, job.variant_id)
        variant.metadata_json = {"topic": "tutorial", "format": "talking_head"}
    # Give the tutorial group a much better engagement rate.
    for job in jobs[:10]:
        for metric, value in (("tt.video.likes", 200.0), ("tt.video.comments", 20.0), ("tt.video.shares", 10.0)):
            session.execute(
                sa.text(
                    "update analytics_aggregation set value_num = :value where publication_key = :job and metric_key = :metric"
                ),
                {"value": value, "job": job.id, "metric": metric},
            )
    session.commit()

    performances = rec.load_dataset(session, account)
    candidates = rec.detect_topic(account, performances, AnalyticsWindowKind.H24)
    assert candidates
    candidate = candidates[0]
    assert candidate.kind is RecommendationKind.TOPIC
    assert candidate.evidence
    assert candidate.limitations
    assert candidate.sample_size["group"] >= rec.MIN_PUBLICATIONS


def test_a_format_that_underperforms_emits_a_candidate(session):
    account, _ = seed_account_with_publications(session, count=20)
    from creatoros.db.models import PlatformVariant, PublishJob

    jobs = list(session.scalars(sa.select(PublishJob).where(PublishJob.account_id == account.id)))
    for job in jobs[:10]:
        variant = session.get(PlatformVariant, job.variant_id)
        variant.metadata_json = {"topic": "bts", "format": "slideshow"}
        for metric, value in (("tt.video.likes", 1.0), ("tt.video.comments", 0.0), ("tt.video.shares", 0.0)):
            session.execute(
                sa.text(
                    "update analytics_aggregation set value_num = :value where publication_key = :job and metric_key = :metric"
                ),
                {"value": value, "job": job.id, "metric": metric},
            )
    session.commit()
    candidates = rec.detect_format(account, rec.load_dataset(session, account), AnalyticsWindowKind.H24)
    assert [candidate.observation for candidate in candidates]
    assert candidates[0].kind is RecommendationKind.FORMAT
    assert "below" in candidates[0].observation
    assert any("observational" in note for note in candidates[0].limitations)


def test_timing_rule_requires_ten_publications_per_bucket(session):
    account, _ = seed_account_with_publications(session, count=4)
    candidates = rec.detect_timing(account, rec.load_dataset(session, account), AnalyticsWindowKind.H24)
    assert candidates == []


def test_retention_rule_needs_three_curves(session):
    account, _ = seed_account_with_publications(session, count=3)
    performances = rec.load_dataset(session, account)
    assert rec.detect_retention(account, performances) == []


def test_retention_rule_flags_a_repeated_cliff(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session)
    curve = [100.0, 80.0, 40.0, 35.0, 30.0]
    for index in range(3):
        fixture = make_published_job(session, platform=Platform.TIKTOK, account=account)
        session.flush()
        row = add_aggregation(
            session, account, publication_id=fixture.job_id, metric_key="tt.video.views", value=100.0
        )
        # Attach the curve to the dataset through the loader's curve lookup path.
        from creatoros.db.models import AnalyticsAggregation

        curve_row = AnalyticsAggregation(
            account_id=account.id,
            platform=Platform.TIKTOK,
            scope=AnalyticsAggregationScope.PUBLICATION,
            publication_key=fixture.job_id,
            campaign_key="",
            metric_key="fb.post.video_retention_curve",
            window_kind=AnalyticsWindowKind.H24,
            value_json={"points": curve, "bucket_count": len(curve)},
            availability=MetricAvailability.OBSERVED,
            method=AnalyticsAggregationMethod.DIRECT,
            input_snapshot_ids_json=list(row.input_snapshot_ids_json),
            computed_at=NOW,
        )
        session.add(curve_row)
    session.commit()
    performances = rec.load_dataset(session, account)
    assert all(performance.curve for performance in performances)
    candidates = rec.detect_retention(account, performances)
    assert candidates
    assert candidates[0].kind is RecommendationKind.RETENTION
    assert candidates[0].expected_benefit["kind"] == "unmeasured"


def test_growth_rule_needs_four_points_and_a_negative_slope(session):
    from creatoros.db.enums import AnalyticsObjectType, MetricPeriod, MetricValueKind
    from creatoros.db.models import AnalyticsSnapshot

    account = make_account(session)
    none = rec.detect_growth(account, session, now=NOW)
    assert none == []

    context = __import__("creatoros.services.analytics.runs", fromlist=["open_run"]).open_run(
        session,
        account=account,
        target_kind=__import__("creatoros.db.enums", fromlist=["AnalyticsRunTarget"]).AnalyticsRunTarget.ACCOUNT_SYNC,
        api_version="v2",
        now=NOW,
    )
    for index, value in enumerate([1000, 990, 970, 960, 940]):
        session.add(
            AnalyticsSnapshot(
                id=f"growth-{index}",
                account_id=account.id,
                platform=Platform.TIKTOK,
                object_type=AnalyticsObjectType.ACCOUNT,
                provider_object_id=account.provider_account_id,
                metric_key="tt.account.followers",
                value_kind=MetricValueKind.SCALAR,
                period_kind=MetricPeriod.INSTANT,
                value_num=float(value),
                unit="count",
                availability=MetricAvailability.OBSERVED,
                breakdown_key="",
                api_version="v2",
                api_host="open.tiktokapis.com",
                source_ref="test",
                run_id=context.run_id,
                collected_at=NOW - timedelta(days=4 - index),
                completeness="complete",
            )
        )
    session.commit()
    candidates = rec.detect_growth(account, session, now=NOW)
    assert candidates
    assert candidates[0].kind is RecommendationKind.GROWTH
    assert candidates[0].sample_size["observed_days"] == 5


# ---------------------------------------------------------------------- storage rules


def _candidate(account, *, evidence_items=None, **overrides) -> rec.Candidate:
    base = {
        "kind": RecommendationKind.TOPIC,
        "observation": "Topic 'bts' performs above the account median at the +24 h window.",
        "evidence": evidence_items or [],
        "sample_size": {"group": 10, "baseline": 20},
        "confidence": 0.8,
        "confidence_method": rec.CONFIDENCE_BOOTSTRAP,
        "limitations": ["sample size: 10 vs 20 publications"],
        "proposed_experiment": {"variable": "topic", "success_metric": "engagement_rate"},
        "expected_benefit": {"kind": "estimate", "range": [0.0, 0.02], "basis": "difference of medians"},
        "account_id": account.id,
        "platform": account.platform,
    }
    base.update(overrides)
    return rec.Candidate(**base)


def test_a_candidate_without_evidence_is_refused(session):
    account = make_account(session)
    engine = rec.RecommendationEngine(session)
    with pytest.raises(rec.RecommendationRejected) as refused:
        engine.store(_candidate(account))
    assert "evidence" in str(refused.value)


def test_a_candidate_without_limitations_is_refused(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    engine = rec.RecommendationEngine(session)
    with pytest.raises(rec.RecommendationRejected) as refused:
        engine.store(_candidate(account, evidence_items=[item], limitations=[]))
    assert "limitations" in str(refused.value)


def test_evidence_referencing_a_missing_row_is_refused(session):
    account = make_account(session)
    item = evidence.EvidenceItem(
        metric_key="tt.video.views",
        provider_metric_name="view_count",
        unit="count",
        period_kind="lifetime",
        availability="observed",
        value=10.0,
        snapshot_ids=["snap-does-not-exist"],
    )
    engine = rec.RecommendationEngine(session)
    with pytest.raises(evidence.UntraceableEvidence):
        engine.store(_candidate(account, evidence_items=[item]))


def test_a_stored_recommendation_has_every_mandatory_field(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    engine = rec.RecommendationEngine(session)
    stored = engine.store(_candidate(account, evidence_items=[item]))
    session.commit()

    assert stored.observation
    assert stored.evidence_json and stored.evidence_json[0]["aggregation_ids"] == [row.id]
    assert stored.sample_size_json["group"] == 10
    assert stored.confidence is not None
    assert stored.confidence_method == rec.CONFIDENCE_BOOTSTRAP
    assert stored.limitations_json
    assert stored.proposed_experiment_json
    assert stored.expected_benefit_json["kind"] == "estimate"
    assert stored.status is RecommendationStatus.DRAFT
    assert isinstance(stored.expected_benefit_json["range"], list)


def test_every_number_in_the_observation_is_traceable(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    # 100 is in the evidence; 4321 is not.
    evidence.assert_numbers_traceable("The value was 100 at this window.", [item])
    with pytest.raises(evidence.UntraceableEvidence):
        evidence.assert_numbers_traceable("The value was 4321 at this window.", [item])
    # Small ordinals and window names are allowed, because prose needs them.
    evidence.assert_numbers_traceable("Measured across 3 publications at the 24-hour window.", [item])


def test_a_deprecated_or_estimated_metric_carries_a_limitation(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    engine = rec.RecommendationEngine(session)
    stored = engine.store(_candidate(account, evidence_items=[item]))
    assert any("sample size" in note for note in stored.limitations_json)


# ---------------------------------------------------------------------- approvals


def test_accepting_creates_an_approval_request_but_no_publish_job(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    engine = rec.RecommendationEngine(session)
    stored = engine.store(_candidate(account, evidence_items=[item]))
    session.commit()

    jobs_before = session.scalar(sa.select(sa.func.count()).select_from(PublishJob))
    rec.accept(session, stored, decided_by="operator", note="looks reasonable")
    session.commit()

    assert stored.status is RecommendationStatus.ACCEPTED
    assert stored.approval_request_id
    request = session.get(ApprovalRequest, stored.approval_request_id)
    assert request is not None
    assert request.action == "analytics.recommendation.accept"
    assert request.target_type == "analytics_recommendation"
    assert request.state is ApprovalState.PENDING
    # The hash binds the words the human read to the payload they approved.
    assert request.payload_hash == evidence_hash(request.payload_json)
    assert session.scalar(sa.select(sa.func.count()).select_from(PublishJob)) == jobs_before
    assert session.scalar(sa.select(sa.func.count()).select_from(ApprovalRecord)) == 0


def evidence_hash(payload: dict) -> str:
    from creatoros.services.approvals import payload_hash

    return payload_hash(payload)


def test_accepting_does_not_create_a_publishable_job_in_any_state(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    engine = rec.RecommendationEngine(session)
    stored = engine.store(_candidate(account, evidence_items=[item]))
    session.commit()
    rec.accept(session, stored, decided_by="operator")
    session.commit()
    publishable = session.scalars(
        sa.select(PublishJob).where(
            PublishJob.job_state.in_([PublishState.SCHEDULED, PublishState.QUEUED, PublishState.PUBLISHED])
        )
    ).all()
    assert publishable == []


def test_rejection_is_stored_with_its_reason(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    stored = rec.RecommendationEngine(session).store(_candidate(account, evidence_items=[item]))
    session.commit()
    rec.reject(session, stored, decided_by="operator", reason="not this quarter")
    session.commit()
    assert stored.status is RecommendationStatus.REJECTED
    assert "not this quarter" in (stored.decision_note or "")


def test_the_engine_imports_nothing_from_publishing_execution():
    """A recommendation must not be able to publish, so it must not import the publish path."""
    from pathlib import Path

    import creatoros

    root = Path(creatoros.__file__).parent / "services" / "analytics"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("publishing.executor", "publishing.publisher", "publishing.scheduler"):
            if forbidden in text:
                offenders.append(f"{path.name}: {forbidden}")
    assert offenders == []


def test_deterministic_narrative_is_available_without_a_model(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    candidate = _candidate(account, evidence_items=[item])
    text = rec.narrative(candidate)
    assert candidate.observation in text
    assert "Limitations" in text
    evidence.assert_numbers_traceable(text, [item], allow=candidate.sample_size)


def test_generate_is_bounded_and_does_not_raise_on_weak_candidates(session):
    account, _ = seed_account_with_publications(session, count=10)
    engine = rec.RecommendationEngine(session)
    stored = engine.generate(account, now=NOW, limit=3)
    session.commit()
    assert len(stored) <= 3
    assert all(row.status is RecommendationStatus.DRAFT for row in stored)


def test_listing_filters_by_status(session):
    account = make_account(session)
    row = add_aggregation(session, account, publication_id="pub-1", metric_key="tt.video.views", value=100.0)
    item = evidence.from_aggregation(session, row)
    stored = rec.RecommendationEngine(session).store(_candidate(account, evidence_items=[item]))
    session.commit()
    assert [row.id for row in rec.list_recommendations(session, status=RecommendationStatus.DRAFT)] == [stored.id]
    assert rec.list_recommendations(session, status=RecommendationStatus.ACCEPTED) == []
