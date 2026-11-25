"""phase 8: analytics registry, observation ledger, runs, sync state, rollups, recommendations

The Phase 2 ``analytics_snapshot`` table could not express what Phase 8 needs:

* ``value`` is ``NOT NULL``, so "the provider did not return this metric" had to be written as a
  zero. Facebook's and Instagram's own documentation says an unavailable metric is returned as an
  empty data set, not as 0 - those are different facts.
* the unique key ``(platform, post_id, metric, hour_bucket)`` has no room for the provider object
  identity, the provider's aggregation period, the API version or an account-level metric, and
  SQLite treats ``NULL`` ``post_id`` values as distinct, so account rows could duplicate freely.
* there is no way to say *why* a measurement is missing (not supported / not authorized / below
  threshold / temporarily unavailable / expired).

The table is rebuilt (the Phase 6/7 technique) and keeps its name. It has never had a writer in
any released phase, so it is expected to be empty; if it is not, this migration refuses to run
rather than silently discarding rows it cannot interpret.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-25 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


PLATFORM = sa.Enum("facebook", "instagram", "tiktok", name="analytics_platform", native_enum=False, create_constraint=True)
OBJECT_TYPE = sa.Enum(
    "account", "post", "video", "reel", "story", name="analytics_object_type", native_enum=False, create_constraint=True
)
VALUE_KIND = sa.Enum(
    "scalar", "breakdown", "series", "object", name="analytics_value_kind", native_enum=False, create_constraint=True
)
METRIC_CLASS = sa.Enum(
    "cumulative", "period", "point_in_time", "ratio", "series",
    name="analytics_metric_class", native_enum=False, create_constraint=True,
)
AVAILABILITY = sa.Enum(
    "observed", "missing", "below_threshold", "not_supported", "not_authorized",
    "not_collected", "temporarily_unavailable", "expired", "redacted_by_policy", "unknown",
    name="analytics_availability", native_enum=False, create_constraint=True,
)
PERIOD = sa.Enum(
    "day", "week", "days_28", "month", "lifetime", "total_over_range", "instant",
    name="analytics_period", native_enum=False, create_constraint=True,
)
RUN_TARGET = sa.Enum(
    "account_sync", "discovery", "metrics", "backfill", "manual",
    name="analytics_run_target", native_enum=False, create_constraint=True,
)
RUN_OUTCOME = sa.Enum(
    "succeeded", "partial", "rate_limited", "not_authorized", "failed", "skipped",
    name="analytics_run_outcome", native_enum=False, create_constraint=True,
)
ERROR_CLASS = sa.Enum(
    "permanent", "transient", "authorization", "permission", "rate_limit", "threshold",
    "validation", "provider_processing",
    name="analytics_error_class", native_enum=False, create_constraint=True,
)
SYNC_STATUS = sa.Enum(
    "not_started", "discovering", "backfilling", "current", "stalled", "blocked",
    name="analytics_sync_status", native_enum=False, create_constraint=True,
)
AGG_SCOPE = sa.Enum(
    "publication", "account", "campaign", "platform", "content_group",
    name="analytics_agg_scope", native_enum=False, create_constraint=True,
)
WINDOW_KIND = sa.Enum(
    "initial", "h24", "h48", "d7", "d28", "lifetime", "custom",
    name="analytics_window_kind", native_enum=False, create_constraint=True,
)
AGG_METHOD = sa.Enum(
    "direct", "sum", "rate", "median", "delta", "ratio",
    name="analytics_agg_method", native_enum=False, create_constraint=True,
)
REC_KIND = sa.Enum(
    "topic", "format", "hook", "caption", "hashtag", "timing", "campaign", "growth", "retention",
    "experiment",
    name="analytics_rec_kind", native_enum=False, create_constraint=True,
)
REC_STATUS = sa.Enum(
    "draft", "reviewed", "accepted", "rejected", "superseded",
    name="analytics_rec_status", native_enum=False, create_constraint=True,
)
REPORT_KIND = sa.Enum(
    "overview", "platform_comparison", "account", "campaign", "content_comparison",
    "recommendations", "experiment",
    name="analytics_report_kind", native_enum=False, create_constraint=True,
)
REPORT_STATUS = sa.Enum("draft", "final", "archived", name="analytics_report_status", native_enum=False, create_constraint=True)
COMPLETENESS = sa.Enum(
    "complete", "partial", "small_sample", "unavailable",
    name="metric_completeness", native_enum=False, create_constraint=True,
)


def _table_exists(bind: sa.Connection, name: str) -> bool:
    return (
        bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"), {"name": name}
        ).fetchone()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "analytics_snapshot"):
        rows = bind.execute(sa.text("SELECT count(*) FROM analytics_snapshot")).scalar() or 0
        if rows:
            raise RuntimeError(
                "analytics_snapshot is not empty; Phase 8 rebuilds it and cannot interpret these rows. "
                "Export and inspect them before migrating."
            )

    # ------------------------------------------------------------ metric registry
    op.create_table(
        "analytics_metric_definition",
        sa.Column("metric_key", sa.String(80), nullable=False),
        sa.Column("platform", PLATFORM, nullable=False),
        sa.Column("object_type", OBJECT_TYPE, nullable=False),
        sa.Column("provider_metric_name", sa.String(120), nullable=False),
        sa.Column("provider_object", sa.String(40), nullable=False),
        sa.Column("source_endpoint", sa.String(300), nullable=False),
        sa.Column("source_doc_url", sa.String(500), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("value_kind", VALUE_KIND, nullable=False),
        sa.Column("metric_class", METRIC_CLASS, nullable=False),
        sa.Column("period_kinds_json", sa.JSON(), nullable=False),
        sa.Column("definition_text", sa.Text(), nullable=False),
        sa.Column("is_estimate", sa.Boolean(), nullable=False),
        sa.Column("is_cumulative", sa.Boolean(), nullable=False),
        sa.Column("is_deprecated", sa.Boolean(), nullable=False),
        sa.Column("deprecated_note", sa.String(300)),
        sa.Column("supersedes_key", sa.String(80)),
        sa.Column("superseded_by_key", sa.String(80)),
        sa.Column("api_version_min", sa.String(16)),
        sa.Column("api_version_max", sa.String(16)),
        sa.Column("required_permissions_json", sa.JSON(), nullable=False),
        sa.Column("account_threshold_json", sa.JSON(), nullable=False),
        sa.Column("availability_default", AVAILABILITY, nullable=False),
        sa.Column("preferred_for", sa.String(40)),
        sa.Column("catalog_version", sa.String(32), nullable=False),
        sa.Column("docs_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("metric_key", name=op.f("pk_analytics_metric_definition")),
        sa.CheckConstraint(
            "metric_class IN ('cumulative', 'period', 'point_in_time', 'ratio', 'series')",
            name=op.f("ck_analytics_metric_definition_ck_analytics_metric_class"),
        ),
        sa.CheckConstraint(
            "value_kind IN ('scalar', 'breakdown', 'series', 'object')",
            name=op.f("ck_analytics_metric_definition_ck_analytics_metric_value_kind"),
        ),
    )
    op.create_index(
        "ix_analytics_metric_platform_object",
        "analytics_metric_definition",
        ["platform", "object_type"],
        unique=False,
    )
    op.create_index("ix_analytics_metric_preferred", "analytics_metric_definition", ["preferred_for"], unique=False)

    # ------------------------------------------------------------ collection runs
    op.create_table(
        "analytics_collection_run",
        sa.Column("account_id", sa.String(26), nullable=False),
        sa.Column("platform", PLATFORM, nullable=False),
        sa.Column("target_kind", RUN_TARGET, nullable=False),
        sa.Column("target_object_id", sa.String(190)),
        sa.Column("job_id", sa.String(26)),
        sa.Column("window_kind", WINDOW_KIND),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", RUN_OUTCOME, nullable=False),
        sa.Column("error_class", ERROR_CLASS),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_detail_redacted", sa.Text()),
        sa.Column("http_calls", sa.Integer(), nullable=False),
        sa.Column("metrics_attempted", sa.Integer(), nullable=False),
        sa.Column("metrics_observed", sa.Integer(), nullable=False),
        sa.Column("metrics_unavailable", sa.Integer(), nullable=False),
        sa.Column("metrics_written", sa.Integer(), nullable=False),
        sa.Column("rate_limit_remaining", sa.Integer()),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True)),
        sa.Column("api_version", sa.String(16)),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["social_account.id"], name=op.f("fk_analytics_collection_run_account_id_social_account"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_collection_run")),
        sa.UniqueConstraint("dedupe_key", name="uq_analytics_run_dedupe"),
    )
    op.create_index("ix_analytics_collection_run_account_id", "analytics_collection_run", ["account_id"], unique=False)
    op.create_index("ix_analytics_collection_run_job_id", "analytics_collection_run", ["job_id"], unique=False)
    op.create_index("ix_analytics_run_account_started", "analytics_collection_run", ["account_id", "started_at"], unique=False)
    op.create_index("ix_analytics_run_outcome", "analytics_collection_run", ["outcome", "started_at"], unique=False)

    # ------------------------------------------------------------ observation ledger (rebuilt)
    op.drop_index("ix_analytics_snapshot_post_id", table_name="analytics_snapshot")
    op.drop_index("ix_analytics_snapshot_account_id", table_name="analytics_snapshot")
    op.drop_table("analytics_snapshot")

    op.create_table(
        "analytics_snapshot",
        sa.Column("account_id", sa.String(26), nullable=False),
        sa.Column("platform", PLATFORM, nullable=False),
        sa.Column("object_type", OBJECT_TYPE, nullable=False),
        sa.Column("provider_object_id", sa.String(190), nullable=False),
        sa.Column("publication_id", sa.String(26)),
        sa.Column("content_project_id", sa.String(26)),
        sa.Column("campaign_id", sa.String(26)),
        sa.Column("asset_id", sa.String(26)),
        sa.Column("metric_key", sa.String(80), nullable=False),
        sa.Column("value_kind", VALUE_KIND, nullable=False),
        sa.Column("period_kind", PERIOD, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("value_num", sa.Float()),
        sa.Column("value_json", sa.JSON()),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("availability", AVAILABILITY, nullable=False),
        sa.Column("availability_reason", sa.String(300)),
        sa.Column("breakdown_key", sa.String(120), nullable=False),
        sa.Column("api_version", sa.String(16), nullable=False),
        sa.Column("api_host", sa.String(60), nullable=False),
        sa.Column("source_ref", sa.String(300), nullable=False),
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_as_of", sa.DateTime(timezone=True)),
        sa.Column("completeness", COMPLETENESS, nullable=False),
        sa.Column("sample_size", sa.Integer()),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["social_account.id"], name=op.f("fk_analytics_snapshot_account_id_social_account"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"], ["publish_job.id"], name=op.f("fk_analytics_snapshot_publication_id_publish_job"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["metric_key"],
            ["analytics_metric_definition.metric_key"],
            name=op.f("fk_analytics_snapshot_metric_key_analytics_metric_definition"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["analytics_collection_run.id"], name=op.f("fk_analytics_snapshot_run_id_analytics_collection_run"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_snapshot")),
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
        sa.CheckConstraint(
            "availability = 'observed' OR value_num IS NULL",
            name="ck_analytics_unobserved_has_no_value",
        ),
        sa.CheckConstraint(
            "availability <> 'observed' OR value_num IS NOT NULL OR value_json IS NOT NULL",
            name="ck_analytics_observed_has_value",
        ),
        sa.CheckConstraint(
            "(value_kind = 'scalar' AND value_json IS NULL) OR (value_kind <> 'scalar' AND value_num IS NULL)",
            name="ck_analytics_value_kind_matches_column",
        ),
    )
    op.create_index("ix_analytics_snapshot_account_id", "analytics_snapshot", ["account_id"], unique=False)
    op.create_index("ix_analytics_snapshot_metric_key", "analytics_snapshot", ["metric_key"], unique=False)
    op.create_index("ix_analytics_snapshot_publication_id", "analytics_snapshot", ["publication_id"], unique=False)
    op.create_index("ix_analytics_snapshot_content_project_id", "analytics_snapshot", ["content_project_id"], unique=False)
    op.create_index("ix_analytics_snapshot_campaign_id", "analytics_snapshot", ["campaign_id"], unique=False)
    op.create_index("ix_analytics_snapshot_run_id", "analytics_snapshot", ["run_id"], unique=False)
    op.create_index("ix_analytics_snapshot_series", "analytics_snapshot", ["account_id", "object_type", "metric_key", "period_end"], unique=False)
    op.create_index("ix_analytics_snapshot_publication", "analytics_snapshot", ["publication_id", "metric_key", "collected_at"], unique=False)
    op.create_index("ix_analytics_snapshot_object", "analytics_snapshot", ["provider_object_id", "metric_key"], unique=False)

    # ------------------------------------------------------------ sync state
    op.create_table(
        "analytics_sync_state",
        sa.Column("account_id", sa.String(26), nullable=False),
        sa.Column("platform", PLATFORM, nullable=False),
        sa.Column("object_type", OBJECT_TYPE, nullable=False),
        sa.Column("state", SYNC_STATUS, nullable=False),
        sa.Column("discovery_cursor", sa.String(190)),
        sa.Column("discovery_complete", sa.Boolean(), nullable=False),
        sa.Column("backfill_until", sa.DateTime(timezone=True)),
        sa.Column("last_full_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_incremental_sync_at", sa.DateTime(timezone=True)),
        sa.Column("next_due_at", sa.DateTime(timezone=True)),
        sa.Column("poll_interval_s", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error_class", ERROR_CLASS),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("provider_retention_days", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["social_account.id"], name=op.f("fk_analytics_sync_state_account_id_social_account"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_sync_state")),
        sa.UniqueConstraint("account_id", "object_type", name="uq_analytics_sync_state"),
    )
    op.create_index("ix_analytics_sync_state_account_id", "analytics_sync_state", ["account_id"], unique=False)
    op.create_index("ix_analytics_sync_due", "analytics_sync_state", ["state", "next_due_at"], unique=False)

    # ------------------------------------------------------------ aggregations
    op.create_table(
        "analytics_aggregation",
        sa.Column("account_id", sa.String(26), nullable=False),
        sa.Column("platform", PLATFORM, nullable=False),
        sa.Column("scope", AGG_SCOPE, nullable=False),
        sa.Column("publication_id", sa.String(26)),
        sa.Column("publication_key", sa.String(26), nullable=False),
        sa.Column("campaign_key", sa.String(26), nullable=False),
        sa.Column("content_project_id", sa.String(26)),
        sa.Column("campaign_id", sa.String(26)),
        sa.Column("metric_key", sa.String(80), nullable=False),
        sa.Column("window_kind", WINDOW_KIND, nullable=False),
        sa.Column("window_hours", sa.Integer()),
        sa.Column("window_tolerance_minutes", sa.Integer()),
        sa.Column("value_num", sa.Float()),
        sa.Column("value_json", sa.JSON()),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("sample_size", sa.Integer()),
        sa.Column("availability", AVAILABILITY, nullable=False),
        sa.Column("method", AGG_METHOD, nullable=False),
        sa.Column("input_snapshot_ids_json", sa.JSON(), nullable=False),
        sa.Column("input_metric_keys_json", sa.JSON(), nullable=False),
        sa.Column("confidence_json", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aggregation_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["social_account.id"], name=op.f("fk_analytics_aggregation_account_id_social_account"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"], ["publish_job.id"], name=op.f("fk_analytics_aggregation_publication_id_publish_job"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_aggregation")),
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
        sa.CheckConstraint(
            "availability = 'observed' OR value_num IS NULL",
            name="ck_analytics_aggregation_unobserved_has_no_value",
        ),
    )
    op.create_index("ix_analytics_aggregation_account_id", "analytics_aggregation", ["account_id"], unique=False)
    op.create_index("ix_analytics_aggregation_content_project_id", "analytics_aggregation", ["content_project_id"], unique=False)
    op.create_index("ix_analytics_aggregation_campaign_id", "analytics_aggregation", ["campaign_id"], unique=False)
    op.create_index(
        "ix_analytics_aggregation_read", "analytics_aggregation", ["account_id", "scope", "metric_key", "window_kind"], unique=False
    )

    # ------------------------------------------------------------ recommendations
    op.create_table(
        "analytics_recommendation",
        sa.Column("account_id", sa.String(26)),
        sa.Column("platform", PLATFORM),
        sa.Column("campaign_id", sa.String(26)),
        sa.Column("content_project_id", sa.String(26)),
        sa.Column("kind", REC_KIND, nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("sample_size_json", sa.JSON(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("confidence", sa.Float()),
        sa.Column("confidence_method", sa.String(40)),
        sa.Column("limitations_json", sa.JSON(), nullable=False),
        sa.Column("proposed_experiment_json", sa.JSON(), nullable=False),
        sa.Column("expected_benefit_json", sa.JSON(), nullable=False),
        sa.Column("status", REC_STATUS, nullable=False),
        sa.Column("generated_by", sa.String(48), nullable=False),
        sa.Column("agent_task_id", sa.String(26)),
        sa.Column("approval_request_id", sa.String(26)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text()),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["social_account.id"], name=op.f("fk_analytics_recommendation_account_id_social_account"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_recommendation")),
    )
    op.create_index("ix_analytics_recommendation_account_id", "analytics_recommendation", ["account_id"], unique=False)
    op.create_index(
        "ix_analytics_recommendation_list", "analytics_recommendation", ["status", "kind", "created_at"], unique=False
    )
    op.create_index("ix_analytics_recommendation_account", "analytics_recommendation", ["account_id", "kind"], unique=False)

    # ------------------------------------------------------------ reports
    op.create_table(
        "analytics_report",
        sa.Column("kind", REPORT_KIND, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("filters_json", sa.JSON(), nullable=False),
        sa.Column("sections_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("path", sa.String(1024)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("generated_by", sa.String(64), nullable=False),
        sa.Column("agent_task_id", sa.String(26)),
        sa.Column("status", REPORT_STATUS, nullable=False),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_report")),
    )
    op.create_index("ix_analytics_report_kind_period", "analytics_report", ["kind", "period_end"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_analytics_report_kind_period", table_name="analytics_report")
    op.drop_table("analytics_report")

    op.drop_index("ix_analytics_recommendation_account", table_name="analytics_recommendation")
    op.drop_index("ix_analytics_recommendation_list", table_name="analytics_recommendation")
    op.drop_index("ix_analytics_recommendation_account_id", table_name="analytics_recommendation")
    op.drop_table("analytics_recommendation")

    op.drop_index("ix_analytics_aggregation_read", table_name="analytics_aggregation")
    op.drop_index("ix_analytics_aggregation_campaign_id", table_name="analytics_aggregation")
    op.drop_index("ix_analytics_aggregation_content_project_id", table_name="analytics_aggregation")
    op.drop_index("ix_analytics_aggregation_account_id", table_name="analytics_aggregation")
    op.drop_table("analytics_aggregation")

    op.drop_index("ix_analytics_sync_due", table_name="analytics_sync_state")
    op.drop_index("ix_analytics_sync_state_account_id", table_name="analytics_sync_state")
    op.drop_table("analytics_sync_state")

    for index in (
        "ix_analytics_snapshot_object",
        "ix_analytics_snapshot_publication",
        "ix_analytics_snapshot_series",
        "ix_analytics_snapshot_run_id",
        "ix_analytics_snapshot_campaign_id",
        "ix_analytics_snapshot_content_project_id",
        "ix_analytics_snapshot_publication_id",
        "ix_analytics_snapshot_metric_key",
        "ix_analytics_snapshot_account_id",
    ):
        op.drop_index(index, table_name="analytics_snapshot")
    op.drop_table("analytics_snapshot")

    # Restore the Phase 2 shape exactly, so a downgrade leaves the database as it was.
    op.create_table(
        "analytics_snapshot",
        sa.Column("platform", PLATFORM, nullable=False),
        sa.Column("account_id", sa.String(26)),
        sa.Column("post_id", sa.String(190)),
        sa.Column("metric", sa.String(80), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(24)),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hour_bucket", sa.String(13), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("completeness", COMPLETENESS, nullable=False),
        sa.Column("sample_size", sa.Integer()),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_snapshot")),
        sa.UniqueConstraint("platform", "post_id", "metric", "hour_bucket", name="uq_analytics_bucket"),
    )
    op.create_index("ix_analytics_snapshot_account_id", "analytics_snapshot", ["account_id"], unique=False)
    op.create_index("ix_analytics_snapshot_post_id", "analytics_snapshot", ["post_id"], unique=False)

    op.drop_index("ix_analytics_run_outcome", table_name="analytics_collection_run")
    op.drop_index("ix_analytics_run_account_started", table_name="analytics_collection_run")
    op.drop_index("ix_analytics_collection_run_job_id", table_name="analytics_collection_run")
    op.drop_index("ix_analytics_collection_run_account_id", table_name="analytics_collection_run")
    op.drop_table("analytics_collection_run")

    op.drop_index("ix_analytics_metric_preferred", table_name="analytics_metric_definition")
    op.drop_index("ix_analytics_metric_platform_object", table_name="analytics_metric_definition")
    op.drop_table("analytics_metric_definition")
