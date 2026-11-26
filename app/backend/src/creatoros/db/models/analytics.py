"""Phase 8 analytics: metric registry, observation ledger, runs, sync state, rollups,
recommendations and reports.

Design rules that the schema enforces (not only the code):

* **A metric that was not observed has no value.** ``value_num`` may only be set when
  ``availability == observed``. The provider itself returns an empty data set rather than a zero, so
  turning "not returned" into 0 would be the first lie in the product.
* **The provider's payload shape is preserved.** ``value_kind`` (copied onto each row) decides
  whether the measurement lives in ``value_num`` or in ``value_json``; the two are mutually
  exclusive, so a 40-point retention curve can never be flattened into a float.
* **Attribution survives deletion.** ``account_id`` and ``publication_id`` are foreign keys
  (``CASCADE`` / ``SET NULL``); ``content_project_id``, ``campaign_id`` and ``asset_id`` are plain
  strings captured at collection time, because deleting a draft project must not erase what a
  published video did.
* **Runs are the idempotency unit.** Every observation points at the run that produced it, and a
  replayed job resumes that run instead of opening a second one.
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, IdMixin, TimestampMixin, UtcDateTime, enum_column
from ..enums import (
    AnalyticsAggregationMethod,
    AnalyticsAggregationScope,
    AnalyticsErrorClass,
    AnalyticsObjectType,
    AnalyticsReportKind,
    AnalyticsReportStatus,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsSyncStatus,
    AnalyticsWindowKind,
    MetricAvailability,
    MetricClass,
    MetricCompleteness,
    MetricPeriod,
    MetricValueKind,
    Platform,
    RecommendationKind,
    RecommendationStatus,
)


class AnalyticsMetricDefinition(Base, TimestampMixin):
    """The metric registry: one row per provider metric, with its identity preserved.

    Seeded from ``integrations/analytics/catalog.py`` and versioned, so an observation stored two
    years ago can still be explained after the code changed or the provider renamed the metric.
    """

    __tablename__ = "analytics_metric_definition"
    __table_args__ = (
        sa.Index("ix_analytics_metric_platform_object", "platform", "object_type"),
        sa.Index("ix_analytics_metric_preferred", "preferred_for"),
        sa.CheckConstraint(
            "metric_class IN ('cumulative', 'period', 'point_in_time', 'ratio', 'series')",
            name="ck_analytics_metric_class",
        ),
        sa.CheckConstraint(
            "value_kind IN ('scalar', 'breakdown', 'series', 'object')",
            name="ck_analytics_metric_value_kind",
        ),
    )

    metric_key: Mapped[str] = mapped_column(sa.String(80), primary_key=True)
    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "analytics_metric_platform"), nullable=False)
    object_type: Mapped[AnalyticsObjectType] = mapped_column(
        enum_column(AnalyticsObjectType, "analytics_metric_object_type"), nullable=False
    )
    #: The exact string sent to the provider (`ig_reels_avg_watch_time`), never a translation.
    provider_metric_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    provider_object: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    source_endpoint: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    source_doc_url: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    value_kind: Mapped[MetricValueKind] = mapped_column(
        enum_column(MetricValueKind, "analytics_value_kind"), nullable=False
    )
    metric_class: Mapped[MetricClass] = mapped_column(
        enum_column(MetricClass, "analytics_metric_class"), nullable=False
    )
    period_kinds_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    definition_text: Mapped[str] = mapped_column(sa.Text, default="", nullable=False)
    is_estimate: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    is_cumulative: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    is_deprecated: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    deprecated_note: Mapped[str | None] = mapped_column(sa.String(300))
    supersedes_key: Mapped[str | None] = mapped_column(sa.String(80))
    superseded_by_key: Mapped[str | None] = mapped_column(sa.String(80))
    api_version_min: Mapped[str | None] = mapped_column(sa.String(16))
    api_version_max: Mapped[str | None] = mapped_column(sa.String(16))
    required_permissions_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    account_threshold_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    availability_default: Mapped[MetricAvailability] = mapped_column(
        enum_column(MetricAvailability, "analytics_metric_default_availability"),
        default=MetricAvailability.NOT_COLLECTED,
        nullable=False,
    )
    preferred_for: Mapped[str | None] = mapped_column(sa.String(40))
    catalog_version: Mapped[str] = mapped_column(sa.String(32), default="1", nullable=False)
    docs_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AnalyticsCollectionRun(Base, IdMixin, TimestampMixin):
    """One logical collection attempt for one account and target.

    ``dedupe_key`` is unique, so a replayed queue job resumes this run instead of opening a second
    one. ``error_detail_redacted`` never holds a token or a full URL.
    """

    __tablename__ = "analytics_collection_run"
    __table_args__ = (
        sa.UniqueConstraint("dedupe_key", name="uq_analytics_run_dedupe"),
        sa.Index("ix_analytics_run_account_started", "account_id", "started_at"),
        sa.Index("ix_analytics_run_outcome", "outcome", "started_at"),
    )

    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "analytics_run_platform"), nullable=False)
    target_kind: Mapped[AnalyticsRunTarget] = mapped_column(
        enum_column(AnalyticsRunTarget, "analytics_run_target"), nullable=False
    )
    target_object_id: Mapped[str | None] = mapped_column(sa.String(190))
    job_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    window_kind: Mapped[AnalyticsWindowKind | None] = mapped_column(enum_column(AnalyticsWindowKind, "analytics_run_window"))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    outcome: Mapped[AnalyticsRunOutcome] = mapped_column(
        enum_column(AnalyticsRunOutcome, "analytics_run_outcome"),
        default=AnalyticsRunOutcome.SUCCEEDED,
        nullable=False,
    )
    error_class: Mapped[AnalyticsErrorClass | None] = mapped_column(enum_column(AnalyticsErrorClass, "analytics_run_error_class"))
    error_code: Mapped[str | None] = mapped_column(sa.String(80))
    error_detail_redacted: Mapped[str | None] = mapped_column(sa.Text)
    http_calls: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    metrics_attempted: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    metrics_observed: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    metrics_unavailable: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    metrics_written: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    rate_limit_remaining: Mapped[int | None] = mapped_column(sa.Integer)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    api_version: Mapped[str | None] = mapped_column(sa.String(16))
    dedupe_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(sa.String(64))

    snapshots: Mapped[list[AnalyticsSnapshot]] = relationship(back_populates="run", cascade="all, delete-orphan")


class AnalyticsSnapshot(Base, IdMixin, TimestampMixin):
    """The observation ledger. Append-only between runs.

    A row says *what the provider answered, for which object, over which window, under which API
    version* — including the answer "it did not answer", which is why ``value_num`` is nullable.
    """

    __tablename__ = "analytics_snapshot"
    __table_args__ = (
        sa.UniqueConstraint(
            "run_id",
            "provider_object_id",
            "metric_key",
            "period_kind",
            "period_start",
            "period_end",
            "breakdown_key",
            name="uq_analytics_observation",
        ),
        sa.Index("ix_analytics_snapshot_series", "account_id", "object_type", "metric_key", "period_end"),
        sa.Index("ix_analytics_snapshot_publication", "publication_id", "metric_key", "collected_at"),
        sa.Index("ix_analytics_snapshot_object", "provider_object_id", "metric_key"),
        # The anti-zero rule, enforced by the database itself.
        sa.CheckConstraint(
            "availability = 'observed' OR value_num IS NULL",
            name="ck_analytics_unobserved_has_no_value",
        ),
        sa.CheckConstraint(
            "availability <> 'observed' OR value_num IS NOT NULL OR value_json IS NOT NULL",
            name="ck_analytics_observed_has_value",
        ),
        # The original payload shape, preserved: scalar in value_num, everything else in value_json.
        sa.CheckConstraint(
            "(value_kind = 'scalar' AND value_json IS NULL) OR (value_kind <> 'scalar' AND value_num IS NULL)",
            name="ck_analytics_value_kind_matches_column",
        ),
    )

    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "analytics_snapshot_platform"), nullable=False)
    object_type: Mapped[AnalyticsObjectType] = mapped_column(
        enum_column(AnalyticsObjectType, "analytics_snapshot_object_type"), nullable=False
    )
    provider_object_id: Mapped[str] = mapped_column(sa.String(190), nullable=False)
    publication_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("publish_job.id", ondelete="SET NULL"), index=True
    )
    # Attribution captured at collection time, deliberately not foreign keys (see module docstring).
    content_project_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    campaign_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    asset_id: Mapped[str | None] = mapped_column(sa.String(26))
    metric_key: Mapped[str] = mapped_column(
        sa.ForeignKey("analytics_metric_definition.metric_key", ondelete="RESTRICT"), nullable=False, index=True
    )
    value_kind: Mapped[MetricValueKind] = mapped_column(
        enum_column(MetricValueKind, "analytics_snapshot_value_kind"), nullable=False
    )
    period_kind: Mapped[MetricPeriod] = mapped_column(
        enum_column(MetricPeriod, "analytics_snapshot_period"), nullable=False
    )
    period_start: Mapped[datetime | None] = mapped_column(UtcDateTime)
    period_end: Mapped[datetime | None] = mapped_column(UtcDateTime)
    value_num: Mapped[float | None] = mapped_column(sa.Float)
    # `none_as_null=True` matters: a JSON column would otherwise store the JSON literal `null`,
    # and the CHECK constraints would never see a real SQL NULL.
    value_json: Mapped[dict | list | None] = mapped_column(sa.JSON(none_as_null=True))
    unit: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    availability: Mapped[MetricAvailability] = mapped_column(
        enum_column(MetricAvailability, "analytics_snapshot_availability"), nullable=False
    )
    availability_reason: Mapped[str | None] = mapped_column(sa.String(300))
    breakdown_key: Mapped[str] = mapped_column(sa.String(120), default="", nullable=False)
    api_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    api_host: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    source_ref: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("analytics_collection_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    collected_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    data_as_of: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completeness: Mapped[MetricCompleteness] = mapped_column(
        enum_column(MetricCompleteness, "analytics_snapshot_completeness"),
        default=MetricCompleteness.COMPLETE,
        nullable=False,
    )
    sample_size: Mapped[int | None] = mapped_column(sa.Integer)

    run: Mapped[AnalyticsCollectionRun] = relationship(back_populates="snapshots")


class AnalyticsSyncState(Base, IdMixin, TimestampMixin):
    """Per account and object type: discovery cursor, watermarks and when to collect next."""

    __tablename__ = "analytics_sync_state"
    __table_args__ = (
        sa.UniqueConstraint("account_id", "object_type", name="uq_analytics_sync_state"),
        sa.Index("ix_analytics_sync_due", "state", "next_due_at"),
    )

    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "analytics_sync_platform"), nullable=False)
    object_type: Mapped[AnalyticsObjectType] = mapped_column(
        enum_column(AnalyticsObjectType, "analytics_sync_object_type"), nullable=False
    )
    state: Mapped[AnalyticsSyncStatus] = mapped_column(
        enum_column(AnalyticsSyncStatus, "analytics_sync_status"),
        default=AnalyticsSyncStatus.NOT_STARTED,
        nullable=False,
    )
    discovery_cursor: Mapped[str | None] = mapped_column(sa.String(190))
    discovery_complete: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    backfill_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_full_sync_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_incremental_sync_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    next_due_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    poll_interval_s: Mapped[int] = mapped_column(sa.Integer, default=86400, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error_class: Mapped[AnalyticsErrorClass | None] = mapped_column(
        enum_column(AnalyticsErrorClass, "analytics_sync_error_class")
    )
    last_error_code: Mapped[str | None] = mapped_column(sa.String(80))
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    provider_retention_days: Mapped[int | None] = mapped_column(sa.Integer)
    notes: Mapped[str | None] = mapped_column(sa.Text)


class AnalyticsAggregation(Base, IdMixin, TimestampMixin):
    """A deterministic rollup: a performance window, a group comparison, an account total.

    ``method``, the input snapshot ids and the aggregation version are stored so a number shown in
    the UI (or quoted by an agent) can always be reproduced.
    """

    __tablename__ = "analytics_aggregation"
    __table_args__ = (
        sa.UniqueConstraint(
            "account_id",
            "scope",
            "publication_key",
            "campaign_key",
            "metric_key",
            "window_kind",
            "aggregation_version",
            name="uq_analytics_aggregation_key",
        ),
        sa.Index("ix_analytics_aggregation_read", "account_id", "scope", "metric_key", "window_kind"),
        sa.CheckConstraint(
            "availability = 'observed' OR value_num IS NULL",
            name="ck_analytics_aggregation_unobserved_has_no_value",
        ),
    )

    account_id: Mapped[str] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "analytics_agg_platform"), nullable=False)
    scope: Mapped[AnalyticsAggregationScope] = mapped_column(
        enum_column(AnalyticsAggregationScope, "analytics_agg_scope"), nullable=False
    )
    publication_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("publish_job.id", ondelete="SET NULL")
    )
    #: ``publication_id`` may become NULL when a publication row is deleted, which would break the
    #: unique key; these stable copies keep one row per logical aggregation.
    publication_key: Mapped[str] = mapped_column(sa.String(26), default="", nullable=False)
    campaign_key: Mapped[str] = mapped_column(sa.String(26), default="", nullable=False)
    content_project_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    campaign_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    metric_key: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    window_kind: Mapped[AnalyticsWindowKind] = mapped_column(
        enum_column(AnalyticsWindowKind, "analytics_agg_window"), nullable=False
    )
    window_hours: Mapped[int | None] = mapped_column(sa.Integer)
    window_tolerance_minutes: Mapped[int | None] = mapped_column(sa.Integer)
    value_num: Mapped[float | None] = mapped_column(sa.Float)
    # `none_as_null=True` matters: a JSON column would otherwise store the JSON literal `null`,
    # and the CHECK constraints would never see a real SQL NULL.
    value_json: Mapped[dict | list | None] = mapped_column(sa.JSON(none_as_null=True))
    unit: Mapped[str] = mapped_column(sa.String(24), default="", nullable=False)
    sample_size: Mapped[int | None] = mapped_column(sa.Integer)
    availability: Mapped[MetricAvailability] = mapped_column(
        enum_column(MetricAvailability, "analytics_agg_availability"), nullable=False
    )
    method: Mapped[AnalyticsAggregationMethod] = mapped_column(
        enum_column(AnalyticsAggregationMethod, "analytics_agg_method"), nullable=False
    )
    input_snapshot_ids_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    input_metric_keys_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    confidence_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    aggregation_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class AnalyticsRecommendation(Base, IdMixin, TimestampMixin):
    """A testable suggestion with its evidence, never an instruction to publish."""

    __tablename__ = "analytics_recommendation"
    __table_args__ = (
        sa.Index("ix_analytics_recommendation_list", "status", "kind", "created_at"),
        sa.Index("ix_analytics_recommendation_account", "account_id", "kind"),
    )

    account_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[Platform | None] = mapped_column(enum_column(Platform, "analytics_rec_platform"))
    campaign_id: Mapped[str | None] = mapped_column(sa.String(26))
    content_project_id: Mapped[str | None] = mapped_column(sa.String(26))
    kind: Mapped[RecommendationKind] = mapped_column(
        enum_column(RecommendationKind, "analytics_rec_kind"), nullable=False
    )
    observation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    sample_size_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(UtcDateTime)
    period_end: Mapped[datetime | None] = mapped_column(UtcDateTime)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    confidence_method: Mapped[str | None] = mapped_column(sa.String(40))
    limitations_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    proposed_experiment_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    expected_benefit_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    status: Mapped[RecommendationStatus] = mapped_column(
        enum_column(RecommendationStatus, "analytics_rec_status"),
        default=RecommendationStatus.DRAFT,
        nullable=False,
    )
    generated_by: Mapped[str] = mapped_column(sa.String(48), default="deterministic", nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(sa.String(26))
    approval_request_id: Mapped[str | None] = mapped_column(sa.String(26))
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decision_note: Mapped[str | None] = mapped_column(sa.Text)


class AnalyticsReport(Base, IdMixin, TimestampMixin):
    """A generated, immutable report: filters, sections, the file and its hash."""

    __tablename__ = "analytics_report"
    __table_args__ = (sa.Index("ix_analytics_report_kind_period", "kind", "period_end"),)

    kind: Mapped[AnalyticsReportKind] = mapped_column(
        enum_column(AnalyticsReportKind, "analytics_report_kind"), nullable=False
    )
    period_start: Mapped[datetime | None] = mapped_column(UtcDateTime)
    period_end: Mapped[datetime | None] = mapped_column(UtcDateTime)
    filters_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    sections_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    path: Mapped[str | None] = mapped_column(sa.String(1024))
    sha256: Mapped[str | None] = mapped_column(sa.String(64))
    bytes: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    generated_by: Mapped[str] = mapped_column(sa.String(64), default="user", nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(sa.String(26))
    status: Mapped[AnalyticsReportStatus] = mapped_column(
        enum_column(AnalyticsReportStatus, "analytics_report_status"),
        default=AnalyticsReportStatus.DRAFT,
        nullable=False,
    )
