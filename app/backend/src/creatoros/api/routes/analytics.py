"""The analytics API: a read-first surface over the stored analytics ledger.

Every route calls an existing service; none of them re-implements collection, aggregation,
comparison or recommendation logic. The rules that make the surface honest:

* **Availability travels with the number.** A metric payload always has ``availability``; when it
  is not ``observed`` the value is NULL. Nothing here turns "the provider did not say" into ``0``.
* **Structured payloads stay structured.** ``value_json`` is passed through untouched.
* **Expensive work is queued, not inline.** Sync and backfill enqueue on the existing ``analytics``
  lane and return the real job id and its real state. Nothing reports "complete" because a job was
  created.
* **No secrets leave.** Tokens, vault contents and provider error bodies are never selected.
"""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from ...db.enums import (
    AnalyticsAggregationScope,
    AnalyticsObjectType,
    AnalyticsReportFormat,
    AnalyticsReportKind,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsSyncStatus,
    AnalyticsWindowKind,
    ConnectionStatus,
    MetricAvailability,
    MetricValueKind,
    Platform,
    RecommendationStatus,
)
from ...db.models import (
    AnalyticsAggregation,
    AnalyticsCollectionRun,
    AnalyticsMetricDefinition,
    AnalyticsRecommendation,
    AnalyticsReport,
    AnalyticsSnapshot,
    AnalyticsSyncState,
    AppSetting,
    Campaign,
    ContentProject,
    PlatformVariant,
    PublishJob,
    SocialAccount,
)
from ...schemas.analytics import (
    AccountDetailOut,
    AccountFreshnessOut,
    AccountSummaryOut,
    AggregationOut,
    AnalyticsOverviewOut,
    AnalyticsSettingsOut,
    AnalyticsSettingsUpdate,
    AvailabilityOut,
    AvailabilityRowOut,
    CampaignDetailOut,
    CampaignSummaryOut,
    CapabilitiesOut,
    ComparisonGroupOut,
    ComparisonOut,
    ComparisonRequest,
    ConsentOverviewOut,
    ConsentPlanOut,
    ConsentStartOut,
    ConsentStartRequest,
    DecisionRequest,
    DiagnosticsOut,
    FreshnessOut,
    GrowthOut,
    GrowthPointOut,
    JobAcceptedOut,
    MetricDefinitionOut,
    MetricValueOut,
    PlatformsOut,
    PlatformSummaryOut,
    PruneRequest,
    PublicationDetailOut,
    PublicationSummaryOut,
    RecommendationDetailOut,
    RecommendationOut,
    RefreshOut,
    RefreshRequest,
    RejectRequest,
    ReportCreate,
    ReportOut,
    RetentionCategoryOut,
    RetentionCurveOut,
    RetentionExecuteOut,
    RetentionOut,
    RetentionPolicyOut,
    RetentionPreviewOut,
    RunOut,
    SeriesOut,
    SeriesPointOut,
    SnapshotOut,
    SyncStateOut,
    TimelineOut,
    TimelinePointOut,
    TimingBucketOut,
    TimingOut,
    VersionCheckOut,
    VersionsOut,
    WindowsOut,
    WindowSpecOut,
    WorkflowCreate,
    WorkflowDefinitionOut,
    WorkflowRunOut,
    WorkflowsOut,
    WorkflowStageOut,
)
from ...schemas.api import Page
from ...services.analytics import (
    aggregation,
    discovery,
    jobs,
    recommendations,
    registry,
    retention,
    runs,
    version_monitor,
    version_watch,
    windows,
    workflows,
)
from ...services.analytics.reports import (
    ReportFormatUnsupported,
    ReportNotFound,
    create_report,
    delete_report_file,
    report_file,
    report_format,
    report_media_type,
)
from ...services.analytics.tools import (
    AnalyticsComparisonReadInput,
    analytics_comparison_read,
)
from ...services.toolbox import ToolContext, ToolError
from ..deps import Audit, DbSession, Paging

router = APIRouter(prefix="/analytics", tags=["analytics"])

FOLLOWER_PREFERENCE = "followers"
ANALYTICS_SETTINGS_KEY = "analytics.settings"
SETTINGS_BOUNDS: dict[str, tuple[int, int]] = {
    "account_sync_interval_s": (300, 172800),
    "object_window_months": (1, 36),
    "retention_days": (7, 3650),
    "recommendation_min_publications": (3, 50),
}
SETTINGS_DEFAULTS: dict[str, Any] = {
    "collection_enabled": True,
    "account_sync_interval_s": 86400,
    "object_window_months": 12,
    "retention_days": 365,
    "recommendation_min_publications": 8,
}


# --------------------------------------------------------------------- helpers
def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _range(column, start: datetime | None, end: datetime | None) -> list:
    start, end = _utc(start), _utc(end)
    if start and end and start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: start must be before end")
    clauses = []
    if start:
        clauses.append(column >= start)
    if end:
        clauses.append(column <= end)
    return clauses


def _account_or_404(session: DbSession, account_id: str) -> SocialAccount:
    account = session.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"ACCOUNT_NOT_FOUND: {account_id}")
    return account


def _definition(session: DbSession, metric_key: str) -> AnalyticsMetricDefinition | None:
    return session.get(AnalyticsMetricDefinition, metric_key)


def _metric_from_snapshot(session: DbSession, row: AnalyticsSnapshot) -> MetricValueOut:
    definition = _definition(session, row.metric_key)
    return MetricValueOut(
        metric_key=row.metric_key,
        provider_metric_name=definition.provider_metric_name if definition else None,
        unit=row.unit,
        value_kind=MetricValueKind(row.value_kind),
        metric_class=definition.metric_class.value if definition else None,
        period_kind=row.period_kind.value,
        availability=MetricAvailability(row.availability),
        availability_reason=row.availability_reason,
        value=row.value_num,
        value_json=row.value_json,
        collected_at=row.collected_at,
        period_start=row.period_start,
        period_end=row.period_end,
        sample_size=row.sample_size,
        snapshot_ids=[row.id],
    )


def _metric_from_aggregation(session: DbSession, row: AnalyticsAggregation) -> MetricValueOut:
    definition = _definition(session, row.metric_key)
    return MetricValueOut(
        metric_key=row.metric_key,
        provider_metric_name=definition.provider_metric_name if definition else None,
        unit=row.unit,
        value_kind=MetricValueKind(definition.value_kind) if definition else MetricValueKind.SCALAR,
        metric_class=definition.metric_class.value if definition else None,
        period_kind=row.window_kind.value,
        availability=MetricAvailability(row.availability),
        value=row.value_num,
        value_json=row.value_json,
        collected_at=row.computed_at,
        sample_size=row.sample_size,
        method=row.method.value,
        confidence=dict(row.confidence_json or {}) or None,
        snapshot_ids=list(row.input_snapshot_ids_json or []),
        aggregation_ids=[row.id],
    )


def _metric_from_evidence(item: dict[str, Any]) -> MetricValueOut:
    value_json = item.get("value_json")
    kind = MetricValueKind.SERIES if isinstance(value_json, (list, dict)) else MetricValueKind.SCALAR
    return MetricValueOut(
        metric_key=str(item.get("metric_key") or ""),
        provider_metric_name=item.get("provider_metric_name"),
        unit=str(item.get("unit") or ""),
        value_kind=kind,
        period_kind=str(item.get("period_kind") or ""),
        availability=MetricAvailability(str(item.get("availability") or "unknown")),
        value=item.get("value"),
        value_json=value_json,
        collected_at=item.get("collected_at"),
        period_start=item.get("period_start"),
        period_end=item.get("period_end"),
        sample_size=item.get("sample_size"),
        snapshot_ids=list(item.get("snapshot_ids") or []),
        aggregation_ids=list(item.get("aggregation_ids") or []),
    )


def _snapshot_out(row: AnalyticsSnapshot) -> SnapshotOut:
    return SnapshotOut(
        id=row.id,
        account_id=row.account_id,
        platform=Platform(row.platform),
        object_type=AnalyticsObjectType(row.object_type),
        provider_object_id=row.provider_object_id,
        publication_id=row.publication_id,
        metric_key=row.metric_key,
        value_kind=MetricValueKind(row.value_kind),
        period_kind=row.period_kind.value,
        period_start=row.period_start,
        period_end=row.period_end,
        availability=MetricAvailability(row.availability),
        availability_reason=row.availability_reason,
        value=row.value_num,
        value_json=row.value_json,
        unit=row.unit,
        collected_at=row.collected_at,
        data_as_of=row.data_as_of,
        completeness=row.completeness.value,
        sample_size=row.sample_size,
        breakdown_key=row.breakdown_key,
        api_version=row.api_version,
        run_id=row.run_id,
    )


def _aggregation_out(row: AnalyticsAggregation) -> AggregationOut:
    return AggregationOut(
        id=row.id,
        account_id=row.account_id,
        platform=Platform(row.platform),
        scope=AnalyticsAggregationScope(row.scope),
        publication_key=row.publication_key,
        campaign_key=row.campaign_key,
        metric_key=row.metric_key,
        window_kind=AnalyticsWindowKind(row.window_kind),
        window_hours=row.window_hours,
        window_tolerance_minutes=row.window_tolerance_minutes,
        value=row.value_num,
        value_json=row.value_json,
        unit=row.unit,
        sample_size=row.sample_size,
        availability=MetricAvailability(row.availability),
        method=row.method,
        input_snapshot_ids=list(row.input_snapshot_ids_json or []),
        confidence=dict(row.confidence_json or {}),
        computed_at=row.computed_at,
        aggregation_version=row.aggregation_version,
    )


def _sync_state_out(row: AnalyticsSyncState) -> SyncStateOut:
    return SyncStateOut(
        account_id=row.account_id,
        platform=Platform(row.platform),
        object_type=AnalyticsObjectType(row.object_type),
        state=AnalyticsSyncStatus(row.state),
        discovery_complete=row.discovery_complete,
        backfill_until=row.backfill_until,
        last_full_sync_at=row.last_full_sync_at,
        last_incremental_sync_at=row.last_incremental_sync_at,
        next_due_at=row.next_due_at,
        poll_interval_s=row.poll_interval_s,
        consecutive_failures=row.consecutive_failures,
        last_error_class=row.last_error_class.value if row.last_error_class else None,
        last_error_code=row.last_error_code,
        last_success_at=row.last_success_at,
        notes=row.notes,
    )


def _run_out(row: AnalyticsCollectionRun) -> RunOut:
    return RunOut(
        id=row.id,
        account_id=row.account_id,
        platform=Platform(row.platform),
        target_kind=AnalyticsRunTarget(row.target_kind),
        target_object_id=row.target_object_id,
        window_kind=AnalyticsWindowKind(row.window_kind) if row.window_kind else None,
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=row.outcome,
        error_class=row.error_class.value if row.error_class else None,
        error_code=row.error_code,
        http_calls=row.http_calls,
        metrics_attempted=row.metrics_attempted,
        metrics_observed=row.metrics_observed,
        metrics_unavailable=row.metrics_unavailable,
        metrics_written=row.metrics_written,
        api_version=row.api_version,
    )


def _recommendation_out(row: AnalyticsRecommendation) -> RecommendationOut:
    return RecommendationOut(
        id=row.id,
        account_id=row.account_id,
        platform=Platform(row.platform) if row.platform else None,
        campaign_id=row.campaign_id,
        content_project_id=row.content_project_id,
        kind=row.kind,
        observation=row.observation,
        sample_size=dict(row.sample_size_json or {}),
        period_start=row.period_start,
        period_end=row.period_end,
        confidence=row.confidence,
        confidence_method=row.confidence_method,
        limitations=list(row.limitations_json or []),
        proposed_experiment=dict(row.proposed_experiment_json or {}),
        expected_benefit=dict(row.expected_benefit_json or {}),
        status=row.status,
        generated_by=row.generated_by,
        approval_request_id=row.approval_request_id,
        decided_at=row.decided_at,
        decision_note=row.decision_note,
        created_at=row.created_at,
    )


def _report_out(row: AnalyticsReport) -> ReportOut:
    try:
        fmt = report_format(row)
    except Exception:  # noqa: BLE001 - an unreadable format must not hide the row
        fmt = AnalyticsReportFormat.JSON
    return ReportOut(
        id=row.id,
        kind=row.kind,
        period_start=row.period_start,
        period_end=row.period_end,
        filters=dict(row.filters_json or {}),
        sections=list(row.sections_json or []),
        summary=row.summary,
        sha256=row.sha256,
        bytes=row.bytes,
        status=row.status,
        generated_by=row.generated_by,
        created_at=row.created_at,
        format=fmt,
    )


def _page(session: DbSession, stmt, paging: Paging) -> tuple[list, int]:
    total = session.scalar(sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = list(session.scalars(stmt.limit(paging.limit).offset(paging.offset)))
    return items, total


def _publication_summary(session: DbSession, job: PublishJob, measured: set[str]) -> PublicationSummaryOut:
    variant = session.get(PlatformVariant, job.variant_id)
    project = session.get(ContentProject, variant.project_id) if variant is not None else None
    return PublicationSummaryOut(
        publication_id=job.id,
        account_id=job.account_id,
        platform=Platform(job.platform),
        remote_post_id=job.remote_post_id or "",
        provider_published_at=job.provider_published_at,
        state=job.job_state.value,
        content_project_id=project.id if project is not None else None,
        campaign_id=project.campaign_id if project is not None else None,
        variant_id=variant.id if variant is not None else None,
        title=variant.title if variant is not None else None,
        hashtags=list(job.hashtags_json or []),
        has_metrics=job.id in measured,
        aggregation_ids=[],
    )


def _measured_publications(session: DbSession, job_ids: list[str]) -> set[str]:
    if not job_ids:
        return set()
    rows = session.scalars(
        sa.select(AnalyticsAggregation.publication_key).where(
            AnalyticsAggregation.publication_key.in_(job_ids),
            AnalyticsAggregation.availability == MetricAvailability.OBSERVED,
        )
    )
    return {row for row in rows if row}


def _availability(account: SocialAccount, object_type: AnalyticsObjectType, session: DbSession) -> AvailabilityOut:
    platform = Platform(account.platform)
    planned = registry.plan(
        session,
        platform=platform,
        object_type=object_type,
        granted_scopes=set(account.scopes_json or []),
        api_version=account.api_version,
    )
    return AvailabilityOut(
        account_id=account.id,
        platform=platform,
        object_type=object_type,
        api_version=account.api_version,
        summary=registry.summary(planned),
        metrics=[AvailabilityRowOut(**item.as_dict()) for item in planned],
    )


def _analytics_settings(session: DbSession) -> dict[str, Any]:
    row = session.get(AppSetting, ANALYTICS_SETTINGS_KEY)
    stored = dict(row.value_json or {}) if row is not None else {}
    return {**SETTINGS_DEFAULTS, **{key: value for key, value in stored.items() if key in SETTINGS_DEFAULTS}}


def _validated_settings(changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in SETTINGS_DEFAULTS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"VALIDATION_ERROR: unknown setting {key!r}")
        if key == "collection_enabled":
            if not isinstance(value, bool):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: collection_enabled must be a boolean")
            clean[key] = value
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"VALIDATION_ERROR: {key} must be an integer")
        low, high = SETTINGS_BOUNDS[key]
        if not low <= value <= high:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"VALIDATION_ERROR: {key} must be between {low} and {high}",
            )
        clean[key] = value
    return clean


# --------------------------------------------------------------------- overview / freshness / capabilities


@router.get("/overview", response_model=AnalyticsOverviewOut)
def overview(session: DbSession, account_id: str | None = None) -> AnalyticsOverviewOut:
    def count(model) -> int:
        stmt = sa.select(sa.func.count()).select_from(model)
        if account_id and hasattr(model, "account_id"):
            stmt = stmt.where(model.account_id == account_id)
        return int(session.scalar(stmt) or 0)

    status_counts: dict[str, int] = {}
    for state in RecommendationStatus:
        stmt = sa.select(sa.func.count()).select_from(AnalyticsRecommendation).where(
            AnalyticsRecommendation.status == state
        )
        if account_id:
            stmt = stmt.where(AnalyticsRecommendation.account_id == account_id)
        status_counts[state.value] = int(session.scalar(stmt) or 0)

    pub_stmt = sa.select(sa.func.count()).select_from(PublishJob).where(PublishJob.remote_post_id.is_not(None))
    if account_id:
        pub_stmt = pub_stmt.where(PublishJob.account_id == account_id)
    latest = session.scalar(sa.select(sa.func.max(AnalyticsSnapshot.collected_at)))
    accounts_total = int(session.scalar(sa.select(sa.func.count()).select_from(SocialAccount)) or 0)
    connected = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(SocialAccount)
            .where(SocialAccount.connection_status == ConnectionStatus.CONNECTED)
        )
        or 0
    )
    return AnalyticsOverviewOut(
        accounts_total=accounts_total,
        accounts_connected=connected,
        publications_measured=int(session.scalar(pub_stmt) or 0),
        observations=count(AnalyticsSnapshot),
        aggregations=count(AnalyticsAggregation),
        recommendations_by_status=status_counts,
        latest_collection_at=latest,
        generated_at=datetime.now(UTC),
    )


@router.get("/freshness", response_model=FreshnessOut)
def freshness(session: DbSession, stale_after_hours: Annotated[int, Query(ge=1, le=24 * 30)] = 48) -> FreshnessOut:
    threshold = datetime.now(UTC) - timedelta(hours=stale_after_hours)
    accounts = list(session.scalars(sa.select(SocialAccount).order_by(SocialAccount.platform)))
    now = datetime.now(UTC)
    items: list[AccountFreshnessOut] = []
    for account in accounts:
        last = session.scalar(
            sa.select(sa.func.max(AnalyticsSnapshot.collected_at)).where(AnalyticsSnapshot.account_id == account.id)
        )
        state = session.scalars(
            sa.select(AnalyticsSyncState)
            .where(AnalyticsSyncState.account_id == account.id)
            .order_by(AnalyticsSyncState.next_due_at)
        ).first()
        age = (now - _utc(last)).total_seconds() / 3600 if last else None
        items.append(
            AccountFreshnessOut(
                account_id=account.id,
                platform=Platform(account.platform),
                display_name=account.display_name,
                last_collected_at=last,
                age_hours=round(age, 1) if age is not None else None,
                stale=last is None or _utc(last) < threshold,
                next_due_at=state.next_due_at if state else None,
                state=AnalyticsSyncStatus(state.state) if state else None,
                blocked_reason=(state.notes or state.last_error_code) if state else None,
            )
        )
    return FreshnessOut(accounts=items, generated_at=now)


@router.get("/capabilities", response_model=CapabilitiesOut)
def capabilities(session: DbSession) -> CapabilitiesOut:
    platforms: list[dict[str, Any]] = []
    for platform in Platform:
        accounts = list(
            session.scalars(sa.select(SocialAccount).where(SocialAccount.platform == platform))
        )
        total = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(AnalyticsMetricDefinition)
                .where(AnalyticsMetricDefinition.platform == platform)
            )
            or 0
        )
        planned: dict[str, int] = {}
        for account in accounts:
            planned_now = registry.plan(
                session,
                platform=platform,
                object_type=AnalyticsObjectType.ACCOUNT,
                granted_scopes=set(account.scopes_json or []),
                api_version=account.api_version,
            )
            for key, value in registry.summary(planned_now).items():
                planned[key] = planned.get(key, 0) + value
        version = version_monitor.report(session, platform).as_dict()
        platforms.append(
            {
                "platform": platform.value,
                "connected_accounts": len(accounts),
                "metrics_total": total,
                "planned_account_metrics": planned,
                "configured_version": version["configured"],
                "latest_documented": version["latest_documented"],
                "retired": version["retired"],
                "note": (
                    "no connected account; availability is unknown until one is connected"
                    if not accounts
                    else "planned from the scopes actually granted on the connected account(s)"
                ),
            }
        )
    return CapabilitiesOut(platforms=platforms, generated_at=datetime.now(UTC))


@router.get("/versions", response_model=VersionsOut)
def versions(session: DbSession) -> VersionsOut:
    """Pinned vs documented per platform, plus **when** that was last verified.

    ``observation_status`` is the honest part: ``never_checked``, ``current``, ``stale`` (the recorded
    look is older than 90 days) or ``unavailable`` (a requested live check reached nothing). A stale
    observation never turns into a version bump here — no route writes ``provider_app_config``.
    """
    return VersionsOut(platforms=version_watch.platforms(session), generated_at=datetime.now(UTC))


@router.post("/versions/check", response_model=VersionCheckOut)
def check_versions(session: DbSession, audit: Audit) -> VersionCheckOut:
    """Record a look at the official documentation. Offline by default; never bumps a version."""
    observation = version_watch.observe(session)
    audit.record(
        "analytics.version.check",
        resource_type="app_setting",
        resource_id=version_watch.WATCH_KEY,
        metadata={"status": observation.status, "method": observation.method},
    )
    return VersionCheckOut(**observation.as_dict())


@router.get("/windows", response_model=WindowsOut)
def window_specs() -> WindowsOut:
    return WindowsOut(
        windows=[
            WindowSpecOut(
                kind=kind,
                hours=windows.WINDOW_SPECS[kind][0] // 60,
                tolerance_minutes=windows.WINDOW_SPECS[kind][1],
            )
            for kind in windows.WINDOW_ORDER
        ]
    )


# --------------------------------------------------------------------- registry / availability


@router.get("/metrics", response_model=Page[MetricDefinitionOut])
def list_metrics(
    session: DbSession,
    paging: Paging,
    platform: Platform | None = None,
    object_type: AnalyticsObjectType | None = None,
    include_deprecated: bool = False,
    preferred_for: str | None = None,
) -> Page[MetricDefinitionOut]:
    stmt = sa.select(AnalyticsMetricDefinition)
    if platform:
        stmt = stmt.where(AnalyticsMetricDefinition.platform == platform)
    if object_type:
        stmt = stmt.where(AnalyticsMetricDefinition.object_type == object_type)
    if not include_deprecated:
        stmt = stmt.where(AnalyticsMetricDefinition.is_deprecated.is_(False))
    if preferred_for:
        stmt = stmt.where(AnalyticsMetricDefinition.preferred_for == preferred_for)
    stmt = stmt.order_by(AnalyticsMetricDefinition.metric_key)
    items, total = _page(session, stmt, paging)
    return Page(
        items=[MetricDefinitionOut.model_validate(row, from_attributes=True) for row in items],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/metrics/{metric_key}", response_model=MetricDefinitionOut)
def get_metric(metric_key: str, session: DbSession) -> MetricDefinitionOut:
    row = _definition(session, metric_key)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"METRIC_NOT_FOUND: {metric_key}")
    return MetricDefinitionOut.model_validate(row, from_attributes=True)


@router.get("/availability", response_model=AvailabilityOut)
def availability(
    session: DbSession,
    account_id: str,
    object_type: AnalyticsObjectType = AnalyticsObjectType.ACCOUNT,
) -> AvailabilityOut:
    account = _account_or_404(session, account_id)
    return _availability(account, object_type, session)


@router.get("/accounts/{account_id}/capabilities", response_model=AvailabilityOut)
def account_capabilities(
    account_id: str,
    session: DbSession,
    object_type: AnalyticsObjectType = AnalyticsObjectType.ACCOUNT,
) -> AvailabilityOut:
    account = _account_or_404(session, account_id)
    return _availability(account, object_type, session)


# --------------------------------------------------------------------- accounts


@router.get("/accounts", response_model=Page[AccountSummaryOut])
def list_accounts(session: DbSession, paging: Paging, platform: Platform | None = None) -> Page[AccountSummaryOut]:
    stmt = sa.select(SocialAccount)
    if platform:
        stmt = stmt.where(SocialAccount.platform == platform)
    items, total = _page(session, stmt.order_by(SocialAccount.platform, SocialAccount.display_name), paging)
    return Page(
        items=[_account_summary(session, account) for account in items],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


def _account_summary(session: DbSession, account: SocialAccount) -> AccountSummaryOut:
    observations = int(
        session.scalar(
            sa.select(sa.func.count()).select_from(AnalyticsSnapshot).where(AnalyticsSnapshot.account_id == account.id)
        )
        or 0
    )
    publications = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(PublishJob)
            .where(PublishJob.account_id == account.id, PublishJob.remote_post_id.is_not(None))
        )
        or 0
    )
    last = session.scalar(
        sa.select(sa.func.max(AnalyticsSnapshot.collected_at)).where(AnalyticsSnapshot.account_id == account.id)
    )
    return AccountSummaryOut(
        id=account.id,
        platform=Platform(account.platform),
        provider_account_id=account.provider_account_id,
        display_name=account.display_name,
        username=account.username,
        account_type=account.account_type.value if hasattr(account.account_type, "value") else str(account.account_type),
        connection_status=account.connection_status.value if hasattr(account.connection_status, "value") else str(account.connection_status),
        token_expires_at=account.token_expires_at,
        last_verified_at=account.last_verified_at,
        observations=observations,
        publications=publications,
        last_collected_at=last,
    )


@router.get("/accounts/{account_id}", response_model=AccountDetailOut)
def get_account(account_id: str, session: DbSession) -> AccountDetailOut:
    account = _account_or_404(session, account_id)
    states = list(
        session.scalars(
            sa.select(AnalyticsSyncState)
            .where(AnalyticsSyncState.account_id == account.id)
            .order_by(AnalyticsSyncState.object_type)
        )
    )
    return AccountDetailOut(account=_account_summary(session, account), sync_states=[_sync_state_out(s) for s in states])


@router.get("/accounts/{account_id}/growth", response_model=GrowthOut)
def account_growth(
    account_id: str,
    session: DbSession,
    start: datetime | None = None,
    end: datetime | None = None,
) -> GrowthOut:
    account = _account_or_404(session, account_id)
    platform = Platform(account.platform)
    definitions = [
        row
        for row in registry.definitions_for(session, platform, AnalyticsObjectType.ACCOUNT)
        if row.preferred_for == FOLLOWER_PREFERENCE
    ]
    keys = [row.metric_key for row in definitions]
    if not keys:
        return GrowthOut(account_id=account.id, metric_keys=[], points=[], availability_summary={})
    stmt = sa.select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.account_id == account.id, AnalyticsSnapshot.metric_key.in_(keys)
    )
    for clause in _range(AnalyticsSnapshot.collected_at, start, end):
        stmt = stmt.where(clause)
    rows = list(session.scalars(stmt.order_by(AnalyticsSnapshot.collected_at)))
    summary: dict[str, int] = {}
    points: list[GrowthPointOut] = []
    for row in rows:
        state = MetricAvailability(row.availability)
        summary[state.value] = summary.get(state.value, 0) + 1
        points.append(
            GrowthPointOut(
                bucket_start=row.collected_at,
                metric_key=row.metric_key,
                value=row.value_num,
                value_json=row.value_json,
                availability=state,
                collected_at=row.collected_at,
            )
        )
    return GrowthOut(account_id=account.id, metric_keys=keys, points=points, availability_summary=summary)


@router.get("/accounts/{account_id}/metrics", response_model=list[MetricValueOut])
def account_metrics(
    account_id: str,
    session: DbSession,
    metric_key: str | None = None,
    window_kind: AnalyticsWindowKind | None = None,
) -> list[MetricValueOut]:
    account = _account_or_404(session, account_id)
    rows = aggregation.aggregations_for(session, account=account, metric_key=metric_key, window_kind=window_kind)
    return [_metric_from_aggregation(session, row) for row in rows if row.scope is AnalyticsAggregationScope.ACCOUNT]


# --------------------------------------------------------------------- publications


def _publication_stmt():
    return sa.select(PublishJob).where(PublishJob.remote_post_id.is_not(None))


@router.get("/publications", response_model=Page[PublicationSummaryOut])
def list_publications(
    session: DbSession,
    paging: Paging,
    account_id: str | None = None,
    platform: Platform | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Page[PublicationSummaryOut]:
    stmt = _publication_stmt()
    if account_id:
        _account_or_404(session, account_id)
        stmt = stmt.where(PublishJob.account_id == account_id)
    if platform:
        stmt = stmt.where(PublishJob.platform == platform)
    for clause in _range(PublishJob.provider_published_at, start, end):
        stmt = stmt.where(clause)
    stmt = stmt.order_by(PublishJob.provider_published_at.desc(), PublishJob.id.desc())
    items, total = _page(session, stmt, paging)
    measured = _measured_publications(session, [job.id for job in items])
    return Page(
        items=[_publication_summary(session, job, measured) for job in items],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


def _publication_or_404(session: DbSession, publication_id: str) -> PublishJob:
    job = session.get(PublishJob, publication_id)
    if job is None or not job.remote_post_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"PUBLICATION_NOT_FOUND: {publication_id}")
    return job


@router.get("/publications/{publication_id}", response_model=PublicationDetailOut)
def get_publication(publication_id: str, session: DbSession) -> PublicationDetailOut:
    job = _publication_or_404(session, publication_id)
    measured = _measured_publications(session, [job.id])
    rows = list(
        session.scalars(
            sa.select(AnalyticsAggregation)
            .where(AnalyticsAggregation.publication_key == job.id)
            .order_by(AnalyticsAggregation.metric_key, AnalyticsAggregation.window_kind)
        )
    )
    return PublicationDetailOut(
        publication=_publication_summary(session, job, measured),
        metrics=[_metric_from_aggregation(session, row) for row in rows],
    )


@router.get("/publications/{publication_id}/metrics", response_model=list[MetricValueOut])
def publication_metrics(
    publication_id: str,
    session: DbSession,
    window_kind: AnalyticsWindowKind | None = None,
    metric_key: str | None = None,
) -> list[MetricValueOut]:
    job = _publication_or_404(session, publication_id)
    stmt = sa.select(AnalyticsAggregation).where(AnalyticsAggregation.publication_key == job.id)
    if window_kind:
        stmt = stmt.where(AnalyticsAggregation.window_kind == window_kind)
    if metric_key:
        stmt = stmt.where(AnalyticsAggregation.metric_key == metric_key)
    rows = list(session.scalars(stmt.order_by(AnalyticsAggregation.metric_key, AnalyticsAggregation.window_kind)))
    return [_metric_from_aggregation(session, row) for row in rows]


@router.get("/publications/{publication_id}/retention", response_model=RetentionOut)
def publication_retention(publication_id: str, session: DbSession) -> RetentionOut:
    job = _publication_or_404(session, publication_id)
    rows = list(
        session.scalars(
            sa.select(AnalyticsSnapshot)
            .where(
                AnalyticsSnapshot.provider_object_id == job.remote_post_id,
                AnalyticsSnapshot.metric_key.like("%retention_curve"),
            )
            .order_by(AnalyticsSnapshot.collected_at.desc())
        )
    )
    if not rows:
        return RetentionOut(
            publication_id=job.id,
            available=False,
            reason="no retention curve is stored for this publication; the provider may not expose one",
            curves=[],
        )
    return RetentionOut(
        publication_id=job.id,
        available=any(row.availability is MetricAvailability.OBSERVED for row in rows),
        reason=None if any(row.availability is MetricAvailability.OBSERVED for row in rows) else "the metric is stored but was not observed",
        curves=[
            RetentionCurveOut(
                metric_key=row.metric_key,
                unit=row.unit,
                collected_at=row.collected_at,
                value_json=row.value_json,
                availability=MetricAvailability(row.availability),
            )
            for row in rows
        ],
    )


@router.get("/publications/{publication_id}/timeline", response_model=TimelineOut)
def publication_timeline(publication_id: str, session: DbSession) -> TimelineOut:
    job = _publication_or_404(session, publication_id)
    rows = list(
        session.scalars(
            sa.select(AnalyticsSnapshot)
            .where(AnalyticsSnapshot.provider_object_id == job.remote_post_id)
            .order_by(AnalyticsSnapshot.collected_at)
        )
    )
    return TimelineOut(
        publication_id=job.id,
        points=[
            TimelinePointOut(
                collected_at=row.collected_at,
                metric_key=row.metric_key,
                window_kind=None,
                availability=MetricAvailability(row.availability),
                value=row.value_num,
                value_json=row.value_json,
                unit=row.unit,
                run_id=row.run_id,
            )
            for row in rows
        ],
    )


# --------------------------------------------------------------------- series / platforms / snapshots / aggregations


@router.get("/series", response_model=SeriesOut)
def series(
    session: DbSession,
    account_id: str,
    metric_key: str,
    start: datetime | None = None,
    end: datetime | None = None,
    bucket: Literal["hour", "day", "week"] = "day",
) -> SeriesOut:
    _account_or_404(session, account_id)
    if start and end:
        span = _utc(end) - _utc(start)  # type: ignore[operator]
        if span > timedelta(days=730):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: range is limited to 730 days")
        if bucket == "hour" and span > timedelta(days=31):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: hourly series is limited to 31 days")
    stmt = sa.select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.account_id == account_id, AnalyticsSnapshot.metric_key == metric_key
    )
    for clause in _range(AnalyticsSnapshot.collected_at, start, end):
        stmt = stmt.where(clause)
    rows = list(session.scalars(stmt.order_by(AnalyticsSnapshot.collected_at)))

    def floor(moment: datetime) -> datetime:
        moment = _utc(moment)  # type: ignore[arg-type]
        if bucket == "hour":
            return moment.replace(minute=0, second=0, microsecond=0)
        if bucket == "week":
            day = moment - timedelta(days=moment.weekday())
            return day.replace(hour=0, minute=0, second=0, microsecond=0)
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)

    step = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}[bucket]
    grouped: dict[datetime, list[AnalyticsSnapshot]] = {}
    for row in rows:
        grouped.setdefault(floor(row.collected_at), []).append(row)
    summary: dict[str, int] = {}
    points: list[SeriesPointOut] = []
    for bucket_start in sorted(grouped):
        latest = grouped[bucket_start][-1]
        state = MetricAvailability(latest.availability)
        summary[state.value] = summary.get(state.value, 0) + 1
        points.append(
            SeriesPointOut(
                bucket_start=bucket_start,
                bucket_end=bucket_start + step,
                value=latest.value_num,
                availability=state,
                observations=len(grouped[bucket_start]),
            )
        )
    if not points and rows == [] and start and end:
        # An empty range is reported as one explicit not_collected point, never as zeros.
        summary[MetricAvailability.NOT_COLLECTED.value] = 1
        points.append(
            SeriesPointOut(
                bucket_start=_utc(start),  # type: ignore[arg-type]
                bucket_end=_utc(start) + step,  # type: ignore[operator]
                value=None,
                availability=MetricAvailability.NOT_COLLECTED,
                observations=0,
            )
        )
    return SeriesOut(
        account_id=account_id,
        metric_key=metric_key,
        bucket=bucket,
        points=points,
        availability_summary=summary,
    )


@router.get("/platforms", response_model=PlatformsOut)
def platforms(session: DbSession) -> PlatformsOut:
    result: list[PlatformSummaryOut] = []
    for platform in Platform:
        accounts = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(SocialAccount).where(SocialAccount.platform == platform)
            )
            or 0
        )
        publications = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(PublishJob)
                .where(PublishJob.platform == platform, PublishJob.remote_post_id.is_not(None))
            )
            or 0
        )
        observations = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(AnalyticsSnapshot).where(AnalyticsSnapshot.platform == platform)
            )
            or 0
        )
        aggregations = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(AnalyticsAggregation).where(
                    AnalyticsAggregation.platform == platform
                )
            )
            or 0
        )
        observed = int(
            session.scalar(
                sa.select(sa.func.count(sa.distinct(AnalyticsSnapshot.metric_key))).where(
                    AnalyticsSnapshot.platform == platform,
                    AnalyticsSnapshot.availability == MetricAvailability.OBSERVED,
                )
            )
            or 0
        )
        last = session.scalar(
            sa.select(sa.func.max(AnalyticsSnapshot.collected_at)).where(AnalyticsSnapshot.platform == platform)
        )
        result.append(
            PlatformSummaryOut(
                platform=platform,
                connected_accounts=accounts,
                publications=publications,
                observations=observations,
                aggregations=aggregations,
                observed_metrics=observed,
                last_collected_at=last,
            )
        )
    return PlatformsOut(platforms=result)


@router.get("/snapshots", response_model=Page[SnapshotOut])
def list_snapshots(
    session: DbSession,
    paging: Paging,
    account_id: str | None = None,
    metric_key: str | None = None,
    publication_id: str | None = None,
    object_type: AnalyticsObjectType | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Page[SnapshotOut]:
    stmt = sa.select(AnalyticsSnapshot)
    if account_id:
        stmt = stmt.where(AnalyticsSnapshot.account_id == account_id)
    if metric_key:
        stmt = stmt.where(AnalyticsSnapshot.metric_key == metric_key)
    if publication_id:
        stmt = stmt.where(AnalyticsSnapshot.publication_id == publication_id)
    if object_type:
        stmt = stmt.where(AnalyticsSnapshot.object_type == object_type)
    for clause in _range(AnalyticsSnapshot.collected_at, start, end):
        stmt = stmt.where(clause)
    stmt = stmt.order_by(AnalyticsSnapshot.collected_at.desc(), AnalyticsSnapshot.id.desc())
    items, total = _page(session, stmt, paging)
    return Page(items=[_snapshot_out(row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/aggregations", response_model=Page[AggregationOut])
def list_aggregations(
    session: DbSession,
    paging: Paging,
    account_id: str | None = None,
    scope: AnalyticsAggregationScope | None = None,
    metric_key: str | None = None,
    window_kind: AnalyticsWindowKind | None = None,
    version: int | None = None,
) -> Page[AggregationOut]:
    stmt = sa.select(AnalyticsAggregation)
    if account_id:
        stmt = stmt.where(AnalyticsAggregation.account_id == account_id)
    if scope:
        stmt = stmt.where(AnalyticsAggregation.scope == scope)
    if metric_key:
        stmt = stmt.where(AnalyticsAggregation.metric_key == metric_key)
    if window_kind:
        stmt = stmt.where(AnalyticsAggregation.window_kind == window_kind)
    if version is not None:
        stmt = stmt.where(AnalyticsAggregation.aggregation_version == version)
    stmt = stmt.order_by(AnalyticsAggregation.computed_at.desc(), AnalyticsAggregation.id.desc())
    items, total = _page(session, stmt, paging)
    return Page(items=[_aggregation_out(row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


# --------------------------------------------------------------------- campaigns


@router.get("/campaigns", response_model=Page[CampaignSummaryOut])
def list_campaigns(session: DbSession, paging: Paging) -> Page[CampaignSummaryOut]:
    items, total = _page(session, sa.select(Campaign).order_by(Campaign.name), paging)
    return Page(
        items=[_campaign_summary(session, campaign) for campaign in items],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


def _campaign_summary(session: DbSession, campaign: Campaign) -> CampaignSummaryOut:
    projects = int(
        session.scalar(
            sa.select(sa.func.count()).select_from(ContentProject).where(ContentProject.campaign_id == campaign.id)
        )
        or 0
    )
    publications = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(PublishJob)
            .join(PlatformVariant, PlatformVariant.id == PublishJob.variant_id)
            .join(ContentProject, ContentProject.id == PlatformVariant.project_id)
            .where(ContentProject.campaign_id == campaign.id, PublishJob.remote_post_id.is_not(None))
        )
        or 0
    )
    observations = int(
        session.scalar(
            sa.select(sa.func.count()).select_from(AnalyticsSnapshot).where(AnalyticsSnapshot.campaign_id == campaign.id)
        )
        or 0
    )
    return CampaignSummaryOut(
        campaign_id=campaign.id,
        name=campaign.name,
        goal=campaign.goal,
        starts_on=campaign.starts_on,
        ends_on=campaign.ends_on,
        projects=projects,
        publications=publications,
        observations=observations,
    )


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign(campaign_id: str, session: DbSession) -> CampaignDetailOut:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"CAMPAIGN_NOT_FOUND: {campaign_id}")
    jobs = list(
        session.scalars(
            sa.select(PublishJob)
            .join(PlatformVariant, PlatformVariant.id == PublishJob.variant_id)
            .join(ContentProject, ContentProject.id == PlatformVariant.project_id)
            .where(ContentProject.campaign_id == campaign.id, PublishJob.remote_post_id.is_not(None))
            .order_by(PublishJob.provider_published_at.desc())
        )
    )
    measured = _measured_publications(session, [job.id for job in jobs])
    rows = list(
        session.scalars(
            sa.select(AnalyticsAggregation)
            .where(AnalyticsAggregation.campaign_key == campaign.id)
            .order_by(AnalyticsAggregation.metric_key)
        )
    )
    return CampaignDetailOut(
        campaign=_campaign_summary(session, campaign),
        publications=[_publication_summary(session, job, measured) for job in jobs],
        metrics=[_metric_from_aggregation(session, row) for row in rows],
    )


# --------------------------------------------------------------------- comparison / hashtags / timing


def _comparison(session: DbSession, payload: ComparisonRequest) -> ComparisonOut:
    _account_or_404(session, payload.account_id)
    try:
        result = analytics_comparison_read(
            ToolContext(session=session, agent_id="api", actor="local-user"),
            AnalyticsComparisonReadInput(
                account_id=payload.account_id,
                metric_key=payload.metric_key,
                attribute=payload.attribute,
                group_by=payload.group_by,
                window_kind=payload.window_kind,
                max_groups=payload.max_groups,
            ),
        )
    except ToolError as exc:
        raise HTTPException(
            exc.status, detail={"code": exc.code, "message": str(exc), **(exc.detail or {})}
        ) from exc
    return ComparisonOut(
        metric_key=result.get("metric_key", payload.metric_key),
        attribute=result.get("attribute", payload.attribute),
        window=result.get("window", payload.window_kind),
        groups=[
            ComparisonGroupOut(
                label=group["label"],
                n=group["n"],
                availability=MetricAvailability(group["availability"]),
                median=group.get("median"),
                evidence=list(group.get("evidence") or []),
            )
            for group in result.get("groups") or []
        ],
        verdicts=result.get("verdicts"),
        comparability=result.get("comparability"),
        limitations=list(result.get("limitations") or []),
        sufficient=result.get("sufficient"),
        note=str(result.get("note") or ""),
    )


@router.post("/compare", response_model=ComparisonOut)
def compare(payload: ComparisonRequest, session: DbSession) -> ComparisonOut:
    return _comparison(session, payload)


@router.get("/hashtags", response_model=ComparisonOut)
def hashtags(
    session: DbSession,
    account_id: str,
    metric_key: str,
    window_kind: str = "h24",
    max_groups: Annotated[int, Query(ge=2, le=5)] = 3,
) -> ComparisonOut:
    return _comparison(
        session,
        ComparisonRequest(
            account_id=account_id,
            metric_key=metric_key,
            attribute="hashtag",
            window_kind=window_kind,
            max_groups=max_groups,
        ),
    )


@router.get("/timing", response_model=TimingOut)
def timing(
    session: DbSession,
    account_id: str,
    window_kind: AnalyticsWindowKind = AnalyticsWindowKind.H24,
) -> TimingOut:
    account = _account_or_404(session, account_id)
    performances = recommendations.load_dataset(session, account, window_kind=window_kind)
    candidates = recommendations.detect_timing(account, performances, window_kind)
    return TimingOut(
        account_id=account.id,
        window=window_kind.value,
        buckets=[
            TimingBucketOut(
                label=_label_from_observation(candidate.observation),
                n=int((candidate.sample_size or {}).get("group") or 0),
                observation=candidate.observation,
                sample_size=dict(candidate.sample_size or {}),
                confidence=candidate.confidence,
                confidence_method=candidate.confidence_method,
                limitations=list(candidate.limitations),
                evidence=[item.as_dict() for item in candidate.evidence],
            )
            for candidate in candidates
        ],
    )


def _label_from_observation(observation: str) -> str:
    marker = "Publishing on "
    if marker in observation:
        return observation.split(marker, 1)[1].split(" ", 1)[0][:40]
    return observation[:40]


# --------------------------------------------------------------------- recommendations


@router.get("/recommendations", response_model=Page[RecommendationOut])
def list_recommendations(
    session: DbSession,
    paging: Paging,
    status_filter: Annotated[RecommendationStatus | None, Query(alias="status")] = None,
    account_id: str | None = None,
    kind: str | None = None,
) -> Page[RecommendationOut]:
    stmt = sa.select(AnalyticsRecommendation)
    if status_filter:
        stmt = stmt.where(AnalyticsRecommendation.status == status_filter)
    if account_id:
        stmt = stmt.where(AnalyticsRecommendation.account_id == account_id)
    if kind:
        stmt = stmt.where(AnalyticsRecommendation.kind == kind)
    stmt = stmt.order_by(AnalyticsRecommendation.created_at.desc(), AnalyticsRecommendation.id.desc())
    items, total = _page(session, stmt, paging)
    return Page(items=[_recommendation_out(row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/recommendations/{recommendation_id}", response_model=RecommendationDetailOut)
def get_recommendation(recommendation_id: str, session: DbSession) -> RecommendationDetailOut:
    row = session.get(AnalyticsRecommendation, recommendation_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"RECOMMENDATION_NOT_FOUND: {recommendation_id}")
    return RecommendationDetailOut(
        recommendation=_recommendation_out(row),
        evidence=[_metric_from_evidence(item).model_dump() for item in (row.evidence_json or [])],
    )


def _recommendation_or_404(session: DbSession, recommendation_id: str) -> AnalyticsRecommendation:
    row = session.get(AnalyticsRecommendation, recommendation_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"RECOMMENDATION_NOT_FOUND: {recommendation_id}")
    return row


@router.post("/recommendations/{recommendation_id}/accept", response_model=RecommendationOut)
def accept_recommendation(
    recommendation_id: str, body: DecisionRequest, session: DbSession, audit: Audit
) -> RecommendationOut:
    row = _recommendation_or_404(session, recommendation_id)
    if row.status not in (RecommendationStatus.DRAFT, RecommendationStatus.REVIEWED):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"CONFLICT: only a draft or reviewed recommendation can be accepted (current: {row.status.value})",
        )
    updated = recommendations.accept(session, row, decided_by="local-user", note=body.note)
    audit.record(
        "analytics.recommendation.accept",
        resource_type="analytics_recommendation",
        resource_id=row.id,
        metadata={"approval_request_id": updated.approval_request_id},
    )
    return _recommendation_out(updated)


@router.post("/recommendations/{recommendation_id}/reject", response_model=RecommendationOut)
def reject_recommendation(
    recommendation_id: str, body: RejectRequest, session: DbSession, audit: Audit
) -> RecommendationOut:
    row = _recommendation_or_404(session, recommendation_id)
    if row.status not in (RecommendationStatus.DRAFT, RecommendationStatus.REVIEWED):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"CONFLICT: only a draft or reviewed recommendation can be rejected (current: {row.status.value})",
        )
    updated = recommendations.reject(session, row, decided_by="local-user", reason=body.reason)
    audit.record(
        "analytics.recommendation.reject",
        resource_type="analytics_recommendation",
        resource_id=row.id,
        metadata={"reason": body.reason[:200]},
    )
    return _recommendation_out(updated)


@router.post("/recommendations/refresh", response_model=RefreshOut)
def refresh_recommendations(body: RefreshRequest, session: DbSession, audit: Audit) -> RefreshOut:
    account = _account_or_404(session, body.account_id)
    try:
        window = AnalyticsWindowKind(body.window_kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: unknown window") from exc
    stored = recommendations.RecommendationEngine(session).generate(account, window_kind=window, limit=body.limit)
    audit.record(
        "analytics.recommendations.refresh",
        resource_type="analytics_recommendation",
        resource_id=account.id,
        metadata={"created": len(stored)},
    )
    return RefreshOut(
        account_id=account.id,
        created=len(stored),
        recommendation_ids=[row.id for row in stored],
    )


# --------------------------------------------------------------------- reports / exports


@router.get("/reports", response_model=Page[ReportOut])
def list_reports(session: DbSession, paging: Paging, kind: AnalyticsReportKind | None = None) -> Page[ReportOut]:
    stmt = sa.select(AnalyticsReport)
    if kind:
        stmt = stmt.where(AnalyticsReport.kind == kind)
    stmt = stmt.order_by(AnalyticsReport.created_at.desc(), AnalyticsReport.id.desc())
    items, total = _page(session, stmt, paging)
    return Page(items=[_report_out(row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.post("/reports", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
def make_report(body: ReportCreate, session: DbSession, audit: Audit) -> ReportOut:
    try:
        report = create_report(
            session,
            kind=body.kind,
            account_id=body.account_id,
            period_start=body.period_start,
            period_end=body.period_end,
            title=body.title,
            report_format=body.format,
        )
    except ReportFormatUnsupported as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "REPORT_FORMAT_UNSUPPORTED", "message": str(exc)},
        ) from exc
    except ReportNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "ACCOUNT_NOT_FOUND", "message": str(exc)}) from exc
    audit.record("analytics.report.create", resource_type="analytics_report", resource_id=report.id)
    return _report_out(report)


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: str, session: DbSession) -> ReportOut:
    report = session.get(AnalyticsReport, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"REPORT_NOT_FOUND: {report_id}")
    return _report_out(report)


@router.get("/reports/{report_id}/file")
def download_report(report_id: str, session: DbSession) -> FileResponse:
    report = session.get(AnalyticsReport, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"REPORT_NOT_FOUND: {report_id}")
    try:
        path = report_file(report)
    except ReportNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "REPORT_FILE_MISSING", "message": str(exc)}) from exc
    return FileResponse(path, media_type=report_media_type(report), filename=path.name)


@router.delete("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(report_id: str, session: DbSession, audit: Audit) -> None:
    report = session.get(AnalyticsReport, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"REPORT_NOT_FOUND: {report_id}")
    delete_report_file(report)
    session.delete(report)
    audit.record("analytics.report.delete", resource_type="analytics_report", resource_id=report_id)


def _csv_response(header: list[str], rows: list[list[Any]], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/metrics.csv")
def export_metrics(
    session: DbSession,
    account_id: str | None = None,
    metric_key: str | None = None,
    window_kind: AnalyticsWindowKind | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Response:
    stmt = sa.select(AnalyticsAggregation)
    if account_id:
        stmt = stmt.where(AnalyticsAggregation.account_id == account_id)
    if metric_key:
        stmt = stmt.where(AnalyticsAggregation.metric_key == metric_key)
    if window_kind:
        stmt = stmt.where(AnalyticsAggregation.window_kind == window_kind)
    for clause in _range(AnalyticsAggregation.computed_at, start, end):
        stmt = stmt.where(clause)
    rows = list(session.scalars(stmt.order_by(AnalyticsAggregation.metric_key).limit(50000)))
    header = [
        "account_id",
        "platform",
        "scope",
        "publication_key",
        "campaign_key",
        "metric_key",
        "window_kind",
        "value",
        "value_json",
        "unit",
        "availability",
        "method",
        "sample_size",
        "computed_at",
    ]
    body = [
        [
            row.account_id,
            row.platform.value,
            row.scope.value,
            row.publication_key,
            row.campaign_key,
            row.metric_key,
            row.window_kind.value,
            "" if row.value_num is None else row.value_num,
            "" if row.value_json is None else str(row.value_json),
            row.unit,
            row.availability.value,
            row.method.value,
            "" if row.sample_size is None else row.sample_size,
            row.computed_at.isoformat(),
        ]
        for row in rows
    ]
    return _csv_response(header, body, "analytics-metrics.csv")


@router.get("/export/publications.csv")
def export_publications(
    session: DbSession,
    account_id: str | None = None,
    platform: Platform | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Response:
    stmt = _publication_stmt()
    if account_id:
        stmt = stmt.where(PublishJob.account_id == account_id)
    if platform:
        stmt = stmt.where(PublishJob.platform == platform)
    for clause in _range(PublishJob.provider_published_at, start, end):
        stmt = stmt.where(clause)
    jobs = list(session.scalars(stmt.order_by(PublishJob.provider_published_at.desc()).limit(50000)))
    measured = _measured_publications(session, [job.id for job in jobs])
    header = [
        "publication_id",
        "account_id",
        "platform",
        "remote_post_id",
        "provider_published_at",
        "state",
        "has_metrics",
        "hashtags",
    ]
    body = [
        [
            job.id,
            job.account_id,
            job.platform.value,
            job.remote_post_id,
            job.provider_published_at.isoformat() if job.provider_published_at else "",
            job.job_state.value,
            job.id in measured,
            " ".join(job.hashtags_json or []),
        ]
        for job in jobs
    ]
    return _csv_response(header, body, "analytics-publications.csv")


# --------------------------------------------------------------------- collection control


def _accepted(job_type: str, result) -> JobAcceptedOut:
    return JobAcceptedOut(
        job_id=result.job_id,
        queue=jobs.QUEUE,
        job_type=job_type,
        state="queued" if result.created else "already_queued",
        created=bool(result.created),
        note=str(result.reason or ""),
    )


@router.post("/sync/{account_id}", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
def sync_account(
    account_id: str,
    session: DbSession,
    audit: Audit,
    window_kind: AnalyticsWindowKind | None = None,
) -> JobAcceptedOut:
    account = _account_or_404(session, account_id)
    result = jobs.enqueue_account_sync(session, account, window_kind=window_kind)
    audit.record("analytics.sync.enqueue", resource_type="social_account", resource_id=account.id, metadata={"job_id": result.job_id})
    return _accepted(jobs.JOB_ACCOUNT_SYNC, result)


@router.post("/backfill/{account_id}", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
def backfill_account(account_id: str, session: DbSession, audit: Audit) -> JobAcceptedOut:
    account = _account_or_404(session, account_id)
    result = jobs.enqueue_backfill(session, account)
    audit.record("analytics.backfill.enqueue", resource_type="social_account", resource_id=account.id, metadata={"job_id": result.job_id})
    return _accepted(jobs.JOB_BACKFILL, result)


def _retention_policy(session: DbSession, body: PruneRequest) -> retention.RetentionPolicy:
    stored = _analytics_settings(session)
    days = body.older_than_days if body.older_than_days is not None else int(stored["retention_days"])
    try:
        return retention.RetentionPolicy(retention_days=days, batch_limit=body.batch_limit)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"VALIDATION_ERROR: {exc}") from exc


@router.post("/prune", response_model=RetentionPreviewOut)
def prune_preview(body: PruneRequest, session: DbSession) -> RetentionPreviewOut:
    """What retention *would* remove, per category, with the reason for each.

    Read-only even when ``dry_run`` is false: deletion lives on `/analytics/retention/execute`, so a
    client that still calls the P8.17 preview path cannot destroy history by accident.
    """
    policy = _retention_policy(session, body)
    plan = retention.preview(session, policy)
    data = plan.as_dict()
    return RetentionPreviewOut(
        policy=RetentionPolicyOut(**{key: data["policy"][key] for key in ("retention_days", "report_retention_days", "batch_limit", "cutoff", "report_cutoff")}),
        categories=[RetentionCategoryOut(**item) for item in data["categories"]],
        total_rows=plan.total_rows,
        total_bytes=sum(item.bytes for item in plan.categories),
        protected=plan.protected,
        applied=False,
        reason="preview only: nothing was deleted; use POST /analytics/retention/execute to apply",
        older_than_days=policy.retention_days,
        finished_runs=plan.runs.count,
        observations=plan.observations.count,
    )


@router.post("/retention/execute", response_model=RetentionExecuteOut)
def retention_execute(body: PruneRequest, session: DbSession, audit: Audit) -> RetentionExecuteOut:
    """Apply retention in one bounded transaction, or record a dry run.

    The audit event written by the service is the record of what happened; `audit_event_id` in the
    response points at it so a person can find the exact execution afterwards.
    """
    policy = _retention_policy(session, body)
    result = retention.execute(session, policy, dry_run=body.dry_run, actor="local-user")
    audit.record(
        "analytics.retention.requested",
        resource_type="analytics_retention",
        resource_id=result.audit_event_id,
        metadata={"dry_run": body.dry_run, "batch_limit": body.batch_limit},
    )
    return RetentionExecuteOut(**result.as_dict())


@router.get("/runs", response_model=Page[RunOut])
def list_runs(
    session: DbSession,
    paging: Paging,
    account_id: str | None = None,
    outcome: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Page[RunOut]:
    stmt = sa.select(AnalyticsCollectionRun)
    if account_id:
        stmt = stmt.where(AnalyticsCollectionRun.account_id == account_id)
    if outcome:
        stmt = stmt.where(AnalyticsCollectionRun.outcome == outcome)
    for clause in _range(AnalyticsCollectionRun.started_at, start, end):
        stmt = stmt.where(clause)
    stmt = stmt.order_by(AnalyticsCollectionRun.started_at.desc(), AnalyticsCollectionRun.id.desc())
    items, total = _page(session, stmt, paging)
    return Page(items=[_run_out(row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, session: DbSession) -> RunOut:
    row = session.get(AnalyticsCollectionRun, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"RUN_NOT_FOUND: {run_id}")
    return _run_out(row)


@router.get("/sync-state", response_model=Page[SyncStateOut])
def list_sync_state(
    session: DbSession,
    paging: Paging,
    account_id: str | None = None,
    state_filter: Annotated[AnalyticsSyncStatus | None, Query(alias="state")] = None,
) -> Page[SyncStateOut]:
    stmt = sa.select(AnalyticsSyncState)
    if account_id:
        stmt = stmt.where(AnalyticsSyncState.account_id == account_id)
    if state_filter:
        stmt = stmt.where(AnalyticsSyncState.state == state_filter)
    stmt = stmt.order_by(AnalyticsSyncState.account_id, AnalyticsSyncState.object_type)
    items, total = _page(session, stmt, paging)
    return Page(items=[_sync_state_out(row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


# --------------------------------------------------------------------- settings


@router.get("/settings", response_model=AnalyticsSettingsOut)
def get_analytics_settings(session: DbSession) -> AnalyticsSettingsOut:
    return AnalyticsSettingsOut(
        settings=_analytics_settings(session),
        bounds={key: {"min": low, "max": high} for key, (low, high) in SETTINGS_BOUNDS.items()},
        note="Stored preferences. They do not change a running collect; the sweeper reads them on its next tick.",
    )


@router.put("/settings", response_model=AnalyticsSettingsOut)
def update_analytics_settings(body: AnalyticsSettingsUpdate, session: DbSession, audit: Audit) -> AnalyticsSettingsOut:
    changes = _validated_settings(body.settings)
    merged = {**_analytics_settings(session), **changes}
    row = session.get(AppSetting, ANALYTICS_SETTINGS_KEY)
    if row is None:
        row = AppSetting(key=ANALYTICS_SETTINGS_KEY, value_json=merged, scope="app")
        session.add(row)
    else:
        row.value_json = merged
    audit.record(
        "analytics.settings.update",
        resource_type="app_setting",
        resource_id=ANALYTICS_SETTINGS_KEY,
        metadata={"keys": sorted(changes)},
    )
    return AnalyticsSettingsOut(
        settings=merged,
        bounds={key: {"min": low, "max": high} for key, (low, high) in SETTINGS_BOUNDS.items()},
        note="Stored preferences. They do not change a running collect; the sweeper reads them on its next tick.",
    )


# --------------------------------------------------------------------- workflows


@router.get("/workflows", response_model=WorkflowsOut)
def list_workflows() -> WorkflowsOut:
    return WorkflowsOut(
        workflows=[
            WorkflowDefinitionOut(
                key=workflow.key,
                title=workflow.title,
                objective=workflow.objective,
                stages=[
                    WorkflowStageOut(
                        key=stage.key,
                        agent_id=stage.agent_id,
                        task_type=stage.task_type,
                        title=stage.title,
                        depends_on=list(stage.depends_on),
                        depth=stage.depth,
                        priority=stage.priority,
                    )
                    for stage in workflow.stages
                ],
            )
            for workflow in workflows.WORKFLOWS.values()
        ]
    )


@router.post("/workflows", response_model=WorkflowRunOut, status_code=status.HTTP_202_ACCEPTED)
def start_workflow(body: WorkflowCreate, session: DbSession, audit: Audit) -> WorkflowRunOut:
    _account_or_404(session, body.account_id)
    if body.key not in workflows.WORKFLOWS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"WORKFLOW_NOT_FOUND: {body.key}")
    if body.window_kind:
        try:
            AnalyticsWindowKind(body.window_kind)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: unknown window") from exc
    try:
        objective = workflows.create_workflow(
            session,
            key=body.key,
            account_id=body.account_id,
            objective=body.objective,
            project_id=body.project_id,
            publication_id=body.publication_id,
            metric_key=body.metric_key,
            window_kind=body.window_kind,
            period=body.period,
            enqueue=True,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"VALIDATION_ERROR: {exc}") from exc
    audit.record(
        "analytics.workflow.enqueue",
        resource_type="agent_objective",
        resource_id=objective.id,
        metadata={"workflow": body.key, "account_id": body.account_id},
    )
    return WorkflowRunOut(
        objective_id=objective.id,
        queue_job_id=objective.queue_job_id,
        state=str(objective.state),
        workflow=body.key,
        stages=list((objective.plan_json or {}).get("stages") or []),
    )


# --------------------------------------------------------------------- diagnostics


@router.get("/diagnostics", response_model=DiagnosticsOut)
def analytics_diagnostics(session: DbSession) -> DiagnosticsOut:
    """Facts a release check needs, each with the evidence behind it.

    Nothing here says "healthy" or "connected" on its own: the counts are what is stored, the version
    rows say which version is configured and when it was last verified, and the collection block says
    when each account last succeeded. A blocked account is reported as blocked, never as stale-by-zero.
    """
    states = list(session.scalars(sa.select(AnalyticsSyncState)))
    blocked = discovery.blocked_summary(states)
    stale = runs.stale_running_runs(session)
    version_rows = version_monitor.report_all(session)
    policy = retention.RetentionPolicy(retention_days=int(_analytics_settings(session)["retention_days"]))
    retention_state = retention.status(session, policy)
    watch = version_watch.load(session)

    accounts = int(session.scalar(sa.select(sa.func.count()).select_from(SocialAccount)) or 0)

    # Collection: per-account success and the age of the freshest observation. An account with no
    # snapshot at all is "never collected", not "0 days old".
    last_success = session.execute(
        sa.select(
            AnalyticsCollectionRun.account_id,
            sa.func.max(AnalyticsCollectionRun.finished_at),
        )
        .where(AnalyticsCollectionRun.outcome == AnalyticsRunOutcome.SUCCEEDED)
        .group_by(AnalyticsCollectionRun.account_id)
    ).all()
    last_failed = session.execute(
        sa.select(AnalyticsCollectionRun.account_id, sa.func.count())
        .where(AnalyticsCollectionRun.outcome.in_([AnalyticsRunOutcome.FAILED, AnalyticsRunOutcome.PARTIAL]))
        .group_by(AnalyticsCollectionRun.account_id)
    ).all()
    newest_observation = session.scalar(sa.select(sa.func.max(AnalyticsSnapshot.collected_at)))
    oldest_observation = session.scalar(sa.select(sa.func.min(AnalyticsSnapshot.collected_at)))
    now = datetime.now(UTC)
    collection = {
        "accounts": accounts,
        "last_success_by_account": {
            account_id: finished.isoformat() if finished else None for account_id, finished in last_success
        },
        "failed_runs_by_account": {account_id: int(count) for account_id, count in last_failed},
        "never_collected_accounts": sorted(
            {row.account_id for row in states} - {account_id for account_id, _ in last_success}
        ),
        "newest_observation_at": newest_observation.isoformat() if newest_observation else None,
        "oldest_observation_at": oldest_observation.isoformat() if oldest_observation else None,
        "newest_observation_age_hours": (
            round((now - newest_observation.replace(tzinfo=UTC)).total_seconds() / 3600, 1)
            if newest_observation
            else None
        ),
    }

    report_rows = list(session.scalars(sa.select(AnalyticsReport)))
    missing_files = 0
    for row in report_rows:
        try:
            report_file(row)
        except ReportNotFound:
            missing_files += 1
    reports_state = {
        "total": len(report_rows),
        "by_format": {
            fmt: sum(1 for row in report_rows if (row.filters_json or {}).get("format", "json") == fmt)
            for fmt in ("json", "csv", "pdf")
        },
        "last_generated_at": max((row.created_at for row in report_rows), default=None).isoformat()
        if report_rows
        else None,
        "rows_without_file": missing_files,
        "formats_implemented": [item.value for item in AnalyticsReportFormat],
    }

    warnings: list[str] = []
    if blocked:
        warnings.append(f"{len(blocked)} sync state(s) are blocked and need attention")
    if stale:
        warnings.append(f"{len(stale)} collection run(s) look abandoned")
    retired = [row["platform"] for row in version_rows if row.get("retired")]
    if retired:
        warnings.append(f"retired API version configured for: {', '.join(retired)}")
    if accounts == 0:
        warnings.append("no connected account: analytics collection has nothing to read")
    if collection["never_collected_accounts"]:
        warnings.append(
            f"{len(collection['never_collected_accounts'])} account(s) have never completed a collection"
        )
    if missing_files:
        warnings.append(f"{missing_files} report row(s) point at a file that is missing")
    if not retention_state["last_execution"]:
        warnings.append("retention has never been executed; eligible rows are accumulating")
    elif retention_state["last_execution"]["integrity_ok"] is False:
        warnings.append("the last retention execution reported an integrity problem")
    warnings.extend(version_watch.warnings(session))

    return DiagnosticsOut(
        metric_definitions=int(session.scalar(sa.select(sa.func.count()).select_from(AnalyticsMetricDefinition)) or 0),
        observations=int(session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot)) or 0),
        aggregations=int(session.scalar(sa.select(sa.func.count()).select_from(AnalyticsAggregation)) or 0),
        publications=int(
            session.scalar(
                sa.select(sa.func.count()).select_from(PublishJob).where(PublishJob.remote_post_id.is_not(None))
            )
            or 0
        ),
        accounts=accounts,
        open_runs=int(
            session.scalar(
                sa.select(sa.func.count()).select_from(AnalyticsCollectionRun).where(AnalyticsCollectionRun.finished_at.is_(None))
            )
            or 0
        ),
        blocked_sync_states=blocked,
        stale_runs=len(stale),
        versions=version_watch.platforms(session),
        retention=retention_state,
        reports=reports_state,
        collection=collection,
        version_watch=watch,
        warnings=warnings,
        generated_at=now,
    )


# --------------------------------------------------------------------- consent (P8.20)


def _consent_service(session: DbSession):
    from ...services.social.consent import ConsentService
    from .social import get_vault

    return ConsentService(session, get_vault())


@router.get("/consent", response_model=ConsentOverviewOut)
def consent_overview(session: DbSession) -> ConsentOverviewOut:
    """Every connected account whose analytics is blocked by something a user can act on.

    A convenience read for the Social Accounts page; it never contacts a provider and never starts a
    flow. The per-account plan below is the authoritative version.
    """
    from ...services.social.consent import accounts_needing_consent

    rows = accounts_needing_consent(session)
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["status"]] = totals.get(row["status"], 0) + 1
    return ConsentOverviewOut(accounts=rows, totals=totals, generated_at=datetime.now(UTC))


@router.get("/consent/{account_id}", response_model=ConsentPlanOut)
def consent_plan(account_id: str, session: DbSession) -> ConsentPlanOut:
    """What analytics is missing on this account, why, and whether a user can fix it."""
    account = _account_or_404(session, account_id)
    plan = _consent_service(session).plan(account)
    return ConsentPlanOut(**plan.to_dict())


@router.post("/consent/{account_id}", response_model=ConsentStartOut)
async def start_consent(
    account_id: str, body: ConsentStartRequest, session: DbSession, audit: Audit
) -> ConsentStartOut:
    """Begin a provider-specific permission upgrade for one existing account.

    The backend calculates the required scopes; the client can only express intent. Existing account
    identity and credentials are preserved: the callback adopts the new credential only for the same
    provider identity and only after the identity is re-verified.
    """
    from ...integrations.social.errors import SocialError

    account = _account_or_404(session, account_id)
    service = _consent_service(session)
    try:
        result = await service.start(account, context={"intent": body.intent} if body.intent else None)
    except SocialError as error:
        raise HTTPException(
            status_code=error.http_status, detail={"code": error.code, "message": error.message}
        )
    audit.record(
        "analytics.consent.requested",
        resource_type="social_account",
        resource_id=account.id,
        metadata={
            "status": result["status"],
            "session_id": result.get("session_id"),
            "platform": account.platform.value,
            "route": account.route.value if account.route else None,
        },
    )
    return ConsentStartOut(**result)