"""Pydantic schemas for the `/analytics` API.

Two rules are encoded here rather than left to the route bodies:

* **A metric payload always carries its availability.** ``value`` may be NULL; it is never
  substituted with ``0``. ``value_json`` keeps a series/breakdown payload whole, so a retention
  curve is never flattened into a scalar.
* **Requests are strict.** ``extra="forbid"`` means an unexpected field is a 422 with the normal
  error envelope, not a silently ignored typo.

Timestamps are timezone-aware UTC on the way out (the database stores UTC already).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..db.enums import (
    AnalyticsAggregationMethod,
    AnalyticsAggregationScope,
    AnalyticsObjectType,
    AnalyticsReportFormat,
    AnalyticsReportKind,
    AnalyticsReportStatus,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsSyncStatus,
    AnalyticsWindowKind,
    ConsentStatus,
    MetricAvailability,
    MetricClass,
    MetricValueKind,
    OAuthRoute,
    Platform,
    RecommendationKind,
    RecommendationStatus,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------- metric payload


class MetricValueOut(_Out):
    """One measurement, exactly as stored.

    ``availability`` is authoritative: anything other than ``observed`` means the provider did not
    return a usable number and ``value`` is NULL. ``value_json`` is the untouched structured payload
    for series/breakdown/object metrics.
    """

    metric_key: str
    provider_metric_name: str | None = None
    unit: str
    value_kind: MetricValueKind
    metric_class: str | None = None
    period_kind: str
    availability: MetricAvailability
    availability_reason: str | None = None
    value: float | None = None
    value_json: Any | None = None
    collected_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    sample_size: int | None = None
    method: str | None = None
    confidence: dict[str, Any] | None = None
    snapshot_ids: list[str] = Field(default_factory=list)
    aggregation_ids: list[str] = Field(default_factory=list)


class MetricDefinitionOut(_Out):
    metric_key: str
    platform: Platform
    object_type: AnalyticsObjectType
    provider_metric_name: str
    provider_object: str
    source_doc_url: str
    unit: str
    value_kind: MetricValueKind
    metric_class: MetricClass
    period_kinds_json: list[str] = Field(default_factory=list)
    definition_text: str = ""
    is_estimate: bool = False
    is_cumulative: bool = False
    is_deprecated: bool = False
    deprecated_note: str | None = None
    supersedes_key: str | None = None
    superseded_by_key: str | None = None
    api_version_min: str | None = None
    api_version_max: str | None = None
    required_permissions_json: list | dict = Field(default_factory=list)
    account_threshold_json: dict[str, Any] = Field(default_factory=dict)
    availability_default: MetricAvailability
    preferred_for: str | None = None
    catalog_version: str


class AvailabilityRowOut(_Out):
    metric_key: str
    allowed: bool
    reason: str
    availability_if_skipped: MetricAvailability | None = None
    detail: str


class AvailabilityOut(_Out):
    account_id: str
    platform: Platform
    object_type: AnalyticsObjectType
    api_version: str | None = None
    summary: dict[str, int] = Field(default_factory=dict)
    metrics: list[AvailabilityRowOut] = Field(default_factory=list)


# --------------------------------------------------------------------- catalog / versions / windows


class VersionsOut(_Out):
    platforms: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime


class WindowSpecOut(_Out):
    kind: AnalyticsWindowKind
    hours: int
    tolerance_minutes: int


class WindowsOut(_Out):
    windows: list[WindowSpecOut] = Field(default_factory=list)
    note: str = "An observation outside its window tolerance is refused, never substituted."


# --------------------------------------------------------------------- overview / freshness / capability


class AnalyticsOverviewOut(_Out):
    accounts_total: int
    accounts_connected: int
    publications_measured: int
    observations: int
    aggregations: int
    recommendations_by_status: dict[str, int] = Field(default_factory=dict)
    latest_collection_at: datetime | None = None
    generated_at: datetime


class AccountFreshnessOut(_Out):
    account_id: str
    platform: Platform
    display_name: str
    last_collected_at: datetime | None = None
    age_hours: float | None = None
    stale: bool = True
    next_due_at: datetime | None = None
    state: AnalyticsSyncStatus | None = None
    blocked_reason: str | None = None


class FreshnessOut(_Out):
    accounts: list[AccountFreshnessOut] = Field(default_factory=list)
    generated_at: datetime


class CapabilitiesOut(_Out):
    platforms: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime


# --------------------------------------------------------------------- accounts


class AccountSummaryOut(_Out):
    id: str
    platform: Platform
    provider_account_id: str
    display_name: str
    username: str | None = None
    account_type: str
    connection_status: str
    token_expires_at: datetime | None = None
    last_verified_at: datetime | None = None
    observations: int = 0
    publications: int = 0
    last_collected_at: datetime | None = None


class SyncStateOut(_Out):
    account_id: str
    platform: Platform
    object_type: AnalyticsObjectType
    state: AnalyticsSyncStatus
    discovery_complete: bool = False
    backfill_until: datetime | None = None
    last_full_sync_at: datetime | None = None
    last_incremental_sync_at: datetime | None = None
    next_due_at: datetime | None = None
    poll_interval_s: int = 86400
    consecutive_failures: int = 0
    last_error_class: str | None = None
    last_error_code: str | None = None
    last_success_at: datetime | None = None
    notes: str | None = None


class AccountDetailOut(_Out):
    account: AccountSummaryOut
    sync_states: list[SyncStateOut] = Field(default_factory=list)


class GrowthPointOut(_Out):
    bucket_start: datetime
    metric_key: str
    value: float | None = None
    value_json: Any | None = None
    availability: MetricAvailability
    collected_at: datetime | None = None


class GrowthOut(_Out):
    account_id: str
    metric_keys: list[str] = Field(default_factory=list)
    points: list[GrowthPointOut] = Field(default_factory=list)
    availability_summary: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------- publications


class PublicationSummaryOut(_Out):
    publication_id: str
    account_id: str
    platform: Platform
    remote_post_id: str
    provider_published_at: datetime | None = None
    state: str
    content_project_id: str | None = None
    campaign_id: str | None = None
    variant_id: str | None = None
    title: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    has_metrics: bool = False
    aggregation_ids: list[str] = Field(default_factory=list)


class PublicationDetailOut(_Out):
    publication: PublicationSummaryOut
    metrics: list[MetricValueOut] = Field(default_factory=list)


class RetentionCurveOut(_Out):
    metric_key: str
    unit: str
    collected_at: datetime | None = None
    value_json: Any | None = None
    availability: MetricAvailability


class RetentionOut(_Out):
    publication_id: str
    available: bool
    reason: str | None = None
    curves: list[RetentionCurveOut] = Field(default_factory=list)


class TimelinePointOut(_Out):
    collected_at: datetime
    metric_key: str
    window_kind: str | None = None
    availability: MetricAvailability
    value: float | None = None
    value_json: Any | None = None
    unit: str
    run_id: str


class TimelineOut(_Out):
    publication_id: str
    points: list[TimelinePointOut] = Field(default_factory=list)


# --------------------------------------------------------------------- series / platforms / snapshots / aggregations


class SeriesPointOut(_Out):
    bucket_start: datetime
    bucket_end: datetime
    value: float | None = None
    availability: MetricAvailability
    observations: int = 0


class SeriesOut(_Out):
    account_id: str
    metric_key: str
    bucket: str
    points: list[SeriesPointOut] = Field(default_factory=list)
    availability_summary: dict[str, int] = Field(default_factory=dict)
    note: str = "A bucket with no observation is 'not_collected', never zero."


class PlatformSummaryOut(_Out):
    platform: Platform
    connected_accounts: int = 0
    publications: int = 0
    observations: int = 0
    aggregations: int = 0
    observed_metrics: int = 0
    last_collected_at: datetime | None = None


class PlatformsOut(_Out):
    platforms: list[PlatformSummaryOut] = Field(default_factory=list)


class SnapshotOut(_Out):
    id: str
    account_id: str
    platform: Platform
    object_type: AnalyticsObjectType
    provider_object_id: str
    publication_id: str | None = None
    metric_key: str
    value_kind: MetricValueKind
    period_kind: str
    period_start: datetime | None = None
    period_end: datetime | None = None
    availability: MetricAvailability
    availability_reason: str | None = None
    value: float | None = None
    value_json: Any | None = None
    unit: str
    collected_at: datetime
    data_as_of: datetime | None = None
    completeness: str
    sample_size: int | None = None
    breakdown_key: str
    api_version: str
    run_id: str


class AggregationOut(_Out):
    id: str
    account_id: str
    platform: Platform
    scope: AnalyticsAggregationScope
    publication_key: str
    campaign_key: str
    metric_key: str
    window_kind: AnalyticsWindowKind
    window_hours: int | None = None
    window_tolerance_minutes: int | None = None
    value: float | None = None
    value_json: Any | None = None
    unit: str
    sample_size: int | None = None
    availability: MetricAvailability
    method: AnalyticsAggregationMethod
    input_snapshot_ids: list[str] = Field(default_factory=list)
    confidence: dict[str, Any] = Field(default_factory=dict)
    computed_at: datetime
    aggregation_version: int


# --------------------------------------------------------------------- campaigns


class CampaignSummaryOut(_Out):
    campaign_id: str
    name: str
    goal: str | None = None
    starts_on: datetime | None = None
    ends_on: datetime | None = None
    projects: int = 0
    publications: int = 0
    observations: int = 0


class CampaignDetailOut(_Out):
    campaign: CampaignSummaryOut
    publications: list[PublicationSummaryOut] = Field(default_factory=list)
    metrics: list[MetricValueOut] = Field(default_factory=list)


# --------------------------------------------------------------------- comparison / hashtags / timing


class ComparisonRequest(_Strict):
    account_id: str = Field(min_length=1, max_length=64)
    metric_key: str = Field(min_length=1, max_length=80)
    attribute: Literal["topic", "format", "hashtag", "platform"] = "topic"
    group_by: Literal["median", "mean"] = "median"
    window_kind: str = Field(default="h24", max_length=24)
    max_groups: int = Field(default=3, ge=2, le=5)


class ComparisonGroupOut(_Out):
    label: str
    n: int
    availability: MetricAvailability
    median: float | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ComparisonOut(_Out):
    metric_key: str
    attribute: str
    window: str
    groups: list[ComparisonGroupOut] = Field(default_factory=list)
    verdicts: Any | None = None
    comparability: Any | None = None
    limitations: list[str] = Field(default_factory=list)
    sufficient: bool | None = None
    note: str = ""


class TimingBucketOut(_Out):
    label: str
    n: int
    observation: str
    sample_size: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    confidence_method: str | None = None
    limitations: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class TimingOut(_Out):
    account_id: str
    window: str
    buckets: list[TimingBucketOut] = Field(default_factory=list)
    note: str = "A bucket with fewer than ten publications is not reported."


# --------------------------------------------------------------------- recommendations


class RecommendationOut(_Out):
    id: str
    account_id: str | None = None
    platform: Platform | None = None
    campaign_id: str | None = None
    content_project_id: str | None = None
    kind: RecommendationKind
    observation: str
    sample_size: dict[str, Any] = Field(default_factory=dict)
    period_start: datetime | None = None
    period_end: datetime | None = None
    confidence: float | None = None
    confidence_method: str | None = None
    limitations: list[str] = Field(default_factory=list)
    proposed_experiment: dict[str, Any] = Field(default_factory=dict)
    expected_benefit: dict[str, Any] = Field(default_factory=dict)
    status: RecommendationStatus
    generated_by: str
    approval_request_id: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime


class RecommendationDetailOut(_Out):
    recommendation: RecommendationOut
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class DecisionRequest(_Strict):
    note: str | None = Field(default=None, max_length=2000)


class RejectRequest(_Strict):
    reason: str = Field(min_length=1, max_length=2000)


class RefreshRequest(_Strict):
    account_id: str = Field(min_length=1, max_length=64)
    window_kind: str = Field(default="h24", max_length=24)
    limit: int = Field(default=10, ge=1, le=50)


class RefreshOut(_Out):
    account_id: str
    created: int
    recommendation_ids: list[str] = Field(default_factory=list)
    note: str = "Drafts only. Accepting one creates an approval request; nothing is published."


# --------------------------------------------------------------------- reports / exports


class ReportCreate(_Strict):
    kind: AnalyticsReportKind = AnalyticsReportKind.OVERVIEW
    account_id: str | None = Field(default=None, max_length=64)
    period_start: datetime | None = None
    period_end: datetime | None = None
    title: str | None = Field(default=None, max_length=200)
    #: Only formats the service can actually write. An unknown value is a 422, not a silent fallback.
    format: AnalyticsReportFormat = AnalyticsReportFormat.JSON

    @model_validator(mode="after")
    def _period_order(self) -> ReportCreate:
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("period_start must not be after period_end")
        return self


class ReportOut(_Out):
    id: str
    kind: AnalyticsReportKind
    period_start: datetime | None = None
    period_end: datetime | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
    sha256: str | None = None
    bytes: int = 0
    status: AnalyticsReportStatus
    generated_by: str
    created_at: datetime
    #: Derived from ``filters.format`` so a client never has to guess from the file name.
    format: AnalyticsReportFormat = AnalyticsReportFormat.JSON


# --------------------------------------------------------------------- collection / runs / sync / settings


class JobAcceptedOut(_Out):
    job_id: str
    queue: str
    job_type: str
    state: str
    created: bool
    note: str = ""


class PruneRequest(_Strict):
    """A retention request. ``dry_run`` is the default: deletion is opt-in, never implied."""

    older_than_days: int | None = Field(default=None, ge=1, le=3650, description="defaults to the stored retention_days")
    batch_limit: int = Field(default=2000, ge=1, le=20000)
    dry_run: bool = True


class RetentionCategoryOut(_Out):
    name: str
    count: int
    reason: str
    bytes: int = 0


class RetentionPolicyOut(_Out):
    retention_days: int
    report_retention_days: int
    batch_limit: int
    cutoff: datetime
    report_cutoff: datetime


class RetentionPreviewOut(_Out):
    policy: RetentionPolicyOut
    categories: list[RetentionCategoryOut] = Field(default_factory=list)
    total_rows: int
    total_bytes: int
    protected: dict[str, int] = Field(default_factory=dict)
    applied: bool = False
    reason: str = "preview only: nothing was deleted"

    #: Old field names kept so a P8.17 client keeps parsing. They are aliases, not extra work.
    older_than_days: int = 0
    finished_runs: int = 0
    observations: int = 0


class RetentionExecuteOut(_Out):
    dry_run: bool
    deleted: dict[str, int] = Field(default_factory=dict)
    remaining: dict[str, int] = Field(default_factory=dict)
    bytes_freed: int
    audit_event_id: str | None = None
    foreign_key_violations: int
    integrity_ok: bool
    duration_ms: int
    errors: list[str] = Field(default_factory=list)


class VersionCheckOut(_Out):
    checked_at: datetime
    status: str
    method: str
    source: str
    documented: dict[str, str] = Field(default_factory=dict)
    note: str


class RunOut(_Out):
    id: str
    account_id: str
    platform: Platform
    target_kind: AnalyticsRunTarget
    target_object_id: str | None = None
    window_kind: AnalyticsWindowKind | None = None
    started_at: datetime
    finished_at: datetime | None = None
    outcome: AnalyticsRunOutcome
    error_class: str | None = None
    error_code: str | None = None
    http_calls: int = 0
    metrics_attempted: int = 0
    metrics_observed: int = 0
    metrics_unavailable: int = 0
    metrics_written: int = 0
    api_version: str | None = None


class AnalyticsSettingsOut(_Out):
    settings: dict[str, Any] = Field(default_factory=dict)
    bounds: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class AnalyticsSettingsUpdate(_Strict):
    settings: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------- workflows


class WorkflowStageOut(_Out):
    key: str
    agent_id: str
    task_type: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    depth: int
    priority: int


class WorkflowDefinitionOut(_Out):
    key: str
    title: str
    objective: str
    stages: list[WorkflowStageOut] = Field(default_factory=list)


class WorkflowsOut(_Out):
    workflows: list[WorkflowDefinitionOut] = Field(default_factory=list)


class WorkflowCreate(_Strict):
    key: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    publication_id: str | None = Field(default=None, max_length=190)
    metric_key: str | None = Field(default=None, max_length=80)
    window_kind: str | None = Field(default=None, max_length=24)
    period: str | None = Field(default=None, max_length=80)
    objective: str | None = Field(default=None, min_length=8, max_length=2000)


class WorkflowRunOut(_Out):
    objective_id: str
    queue_job_id: str | None = None
    state: str
    workflow: str
    stages: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- diagnostics


class DiagnosticsOut(_Out):
    metric_definitions: int
    observations: int
    aggregations: int
    publications: int
    accounts: int
    open_runs: int
    blocked_sync_states: list[dict[str, Any]] = Field(default_factory=list)
    stale_runs: int
    versions: list[dict[str, Any]] = Field(default_factory=list)
    #: P8.24: the states a release check actually needs, each with the facts behind it.
    retention: dict[str, Any] = Field(default_factory=dict)
    reports: dict[str, Any] = Field(default_factory=dict)
    collection: dict[str, Any] = Field(default_factory=dict)
    version_watch: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime


# --------------------------------------------------------------------- consent (P8.20)


class ConsentPlanOut(_Out):
    """What analytics is missing on one account, why, and who must act.

    ``status`` distinguishes the cases the UI must not conflate: consent the user can give,
    developer review only the app owner can obtain, a Page task assigned in Facebook, and an account
    whose login path can never read insights.
    """

    account_id: str
    platform: Platform
    route: OAuthRoute | None = None
    account_type: str
    provider_account_id: str
    capability_key: str | None = None
    capability_label: str | None = None
    capability_state: str
    status: ConsentStatus
    required_scopes: list[str] = Field(default_factory=list)
    granted_scopes: list[str] = Field(default_factory=list)
    missing_scopes: list[str] = Field(default_factory=list)
    requested_scopes: list[str] = Field(default_factory=list)
    scope_reasons: dict[str, str] = Field(default_factory=dict)
    page_tasks_required: list[str] = Field(default_factory=list)
    page_tasks_held: list[str] = Field(default_factory=list)
    page_tasks_missing: list[str] = Field(default_factory=list)
    page_tasks_known: bool = False
    app_mode: str | None = None
    developer_approval_required: bool = False
    can_start: bool = False
    blocked_by: list[str] = Field(default_factory=list)
    blockers_text: list[str] = Field(default_factory=list)
    metrics_blocked: list[dict[str, Any]] = Field(default_factory=list)
    docs_url: str | None = None
    detail: str = ""


class ConsentStartRequest(_Strict):
    """Only a user-intent note. A client may never name a scope: the backend computes them."""

    intent: str | None = Field(default=None, max_length=200)


class ConsentStartOut(_Out):
    status: ConsentStatus
    account_id: str
    plan: ConsentPlanOut | None = None
    session_id: str | None = None
    authorization_url: str | None = None
    redirect_uri: str | None = None
    expires_at: str | None = None
    scopes_requested: list[str] = Field(default_factory=list)
    pkce: str | None = None
    app_mode: str | None = None
    poll_url: str | None = None
    message: str = ""


class ConsentOverviewOut(_Out):
    accounts: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    generated_at: datetime