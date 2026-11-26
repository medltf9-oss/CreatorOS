"""Analytics retention: a policy, a preview, and a bounded, auditable deletion.

The rule this module exists to keep: **analytics history expires; business history does not.** A
published video, a campaign, a social account, a media asset, a metric definition, a sync state and a
decided recommendation are never deleted here, whatever their age. What expires is the observation
ledger and the derived rollups that only summarise it.

Per-category policy, each with its own justification (a single duration for everything would delete a
decision someone made and keep a redundant aggregate):

============  ==========================  =========================================================
category      eligible when               why
============  ==========================  =========================================================
aggregations  every input observation is  a rollup that can no longer be reproduced from the ledger
              also eligible, or all of    is either redundant (inputs gone) or unverifiable; the
              its inputs are already      observations themselves expire first
              gone
observations  ``collected_at`` older      the provider's own retention window (Facebook 2 years,
              than ``retention_days``     Instagram 2 years for media, 90 days for account metrics,
              and the run that wrote it   TikTok counters are current-state only) is the ceiling;
              has finished                we keep a configurable period below it
runs          ``finished_at`` older than   an execution record with no observations left is history
              the cutoff and no
              observation still refers
              to it
reports       older than                  a report is a saved artefact, kept longer than the data it
              ``max(retention_days,       describes because it is the only record of what a number
              730)`` or archived         looked like on the day it was generated
superseded    ``superseded`` and older    a superseded draft was replaced; it is not a decision.
recommenda-   than the cutoff             ``accepted``/``rejected`` rows are decisions and are kept
tions
orphan files  a report file no row         a crash between write and flush leaves a file nobody can
              points at, older than 1     reach
              day
============  ==========================  =========================================================

Deletion order matters: aggregations are removed before the observations they cite, and runs after the
observations that cascade from them, so ``PRAGMA foreign_key_check`` stays empty at every step.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.paths import resolve_under_root
from ...db.enums import AnalyticsReportStatus, RecommendationStatus
from ...db.models import (
    AnalyticsAggregation,
    AnalyticsCollectionRun,
    AnalyticsRecommendation,
    AnalyticsReport,
    AnalyticsSnapshot,
)
from ..backup import AuditService

log = logging.getLogger("creatoros.analytics")

AUDIT_PREVIEW = "analytics.retention.preview"
AUDIT_EXECUTE = "analytics.retention.execute"

#: A report is kept at least this long, whatever ``retention_days`` says.
REPORT_MIN_RETENTION_DAYS = 730
#: Rows deleted per category per execution. The job re-enqueues itself while work remains.
DEFAULT_BATCH_LIMIT = 2000
#: A report file with no row pointing at it is only removed once it is clearly abandoned.
ORPHAN_FILE_GRACE_HOURS = 24


@dataclass(slots=True)
class RetentionPolicy:
    """The retention decision, made once and then applied consistently by preview and execution."""

    retention_days: int
    batch_limit: int = DEFAULT_BATCH_LIMIT
    now: datetime | None = None

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if self.batch_limit < 1:
            raise ValueError("batch_limit must be at least 1")
        self.now = self.now or datetime.now(UTC)

    @property
    def cutoff(self) -> datetime:
        return self.now - timedelta(days=self.retention_days)

    @property
    def report_cutoff(self) -> datetime:
        return self.now - timedelta(days=max(self.retention_days, REPORT_MIN_RETENTION_DAYS))

    def as_dict(self) -> dict[str, Any]:
        return {
            "retention_days": self.retention_days,
            "report_retention_days": max(self.retention_days, REPORT_MIN_RETENTION_DAYS),
            "batch_limit": self.batch_limit,
            "cutoff": self.cutoff.isoformat(),
            "report_cutoff": self.report_cutoff.isoformat(),
        }


@dataclass(slots=True)
class CategoryPlan:
    name: str
    count: int
    ids: list[str] = field(default_factory=list)
    reason: str = ""
    bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "count": self.count, "reason": self.reason, "bytes": self.bytes}


@dataclass(slots=True)
class RetentionPlan:
    policy: RetentionPolicy
    aggregations: CategoryPlan
    observations: CategoryPlan
    runs: CategoryPlan
    reports: CategoryPlan
    recommendations: CategoryPlan
    orphan_files: CategoryPlan
    protected: dict[str, int] = field(default_factory=dict)

    @property
    def categories(self) -> list[CategoryPlan]:
        return [
            self.aggregations,
            self.observations,
            self.runs,
            self.reports,
            self.recommendations,
            self.orphan_files,
        ]

    @property
    def total_rows(self) -> int:
        return sum(
            plan.count
            for plan in (self.aggregations, self.observations, self.runs, self.reports, self.recommendations)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.as_dict(),
            "categories": [plan.as_dict() for plan in self.categories],
            "total_rows": self.total_rows,
            "total_bytes": sum(plan.bytes for plan in self.categories),
            "protected": dict(self.protected),
        }


@dataclass(slots=True)
class RetentionResult:
    dry_run: bool
    deleted: dict[str, int]
    remaining: dict[str, int]
    bytes_freed: int
    audit_event_id: str | None
    foreign_key_violations: int
    integrity_ok: bool
    duration_ms: int
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "deleted": dict(self.deleted),
            "remaining": dict(self.remaining),
            "bytes_freed": self.bytes_freed,
            "audit_event_id": self.audit_event_id,
            "foreign_key_violations": self.foreign_key_violations,
            "integrity_ok": self.integrity_ok,
            "duration_ms": self.duration_ms,
            "errors": list(self.errors),
        }


def _reports_dir() -> Path:
    return resolve_under_root("media_exports", "reports")


def _snapshot_ids(session: Session) -> set[str]:
    return set(session.scalars(sa.select(AnalyticsSnapshot.id)))


def _aggregation_inputs(row: AnalyticsAggregation) -> list[str]:
    value = row.input_snapshot_ids_json or []
    return [str(item) for item in value] if isinstance(value, list) else []


def plan(session: Session, policy: RetentionPolicy, *, capped: bool = True) -> RetentionPlan:
    """Decide what is eligible. Reads only; no row is modified, no file is touched.

    ``capped`` limits the returned ids to ``policy.batch_limit`` (what an execution would take now).
    ``capped=False`` counts everything eligible, which is what "how much is left" must mean — a capped
    count would report 2 remaining forever after a 2-row batch.
    """
    existing_snapshots = _snapshot_ids(session)

    eligible_snapshots = list(
        session.scalars(
            sa.select(AnalyticsSnapshot.id)
            .join(AnalyticsCollectionRun, AnalyticsCollectionRun.id == AnalyticsSnapshot.run_id)
            .where(
                AnalyticsSnapshot.collected_at < policy.cutoff,
                AnalyticsCollectionRun.finished_at.is_not(None),
            )
            .limit(policy.batch_limit if capped else None)
        )
    )
    eligible_snapshot_set = set(eligible_snapshots)

    # An aggregation is redundant when every observation it cites is either about to disappear or
    # already gone; an aggregation whose inputs still exist is reproducible and stays.
    aggregation_ids: list[str] = []
    for row in session.scalars(sa.select(AnalyticsAggregation).order_by(AnalyticsAggregation.computed_at.asc())):
        inputs = _aggregation_inputs(row)
        if not inputs:
            if row.computed_at < policy.cutoff:
                aggregation_ids.append(row.id)
            continue
        if all(item in eligible_snapshot_set or item not in existing_snapshots for item in inputs):
            aggregation_ids.append(row.id)
        if capped and len(aggregation_ids) >= policy.batch_limit:
            break

    remaining_snapshots = existing_snapshots - eligible_snapshot_set
    run_eligible: list[str] = []
    run_protected = 0
    for run in session.scalars(
        sa.select(AnalyticsCollectionRun)
        .where(
            AnalyticsCollectionRun.finished_at.is_not(None),
            AnalyticsCollectionRun.finished_at < policy.cutoff,
        )
        .limit(policy.batch_limit if capped else None)
    ):
        still_referenced = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(AnalyticsSnapshot)
                .where(AnalyticsSnapshot.run_id == run.id, AnalyticsSnapshot.id.in_(remaining_snapshots or [""]))
            )
            or 0
        )
        if still_referenced:
            run_protected += 1
            continue
        run_eligible.append(run.id)

    report_rows = list(
        session.scalars(
            sa.select(AnalyticsReport).where(
                sa.or_(
                    AnalyticsReport.created_at < policy.report_cutoff,
                    AnalyticsReport.status == AnalyticsReportStatus.ARCHIVED,
                )
            )
        )
    )
    report_ids = [row.id for row in report_rows][: policy.batch_limit if capped else len(report_rows)]
    report_bytes = sum(int(row.bytes or 0) for row in report_rows if row.id in set(report_ids))

    superseded = list(
        session.scalars(
            sa.select(AnalyticsRecommendation.id)
            .where(
                AnalyticsRecommendation.status == RecommendationStatus.SUPERSEDED,
                AnalyticsRecommendation.created_at < policy.cutoff,
            )
            .limit(policy.batch_limit if capped else None)
        )
    )

    referenced_files = {
        Path(row.path).name for row in session.scalars(sa.select(AnalyticsReport)) if row.path
    }
    orphan_names: list[str] = []
    orphan_bytes = 0
    folder = _reports_dir()
    if folder.is_dir():
        threshold = policy.now - timedelta(hours=ORPHAN_FILE_GRACE_HOURS)
        for candidate in folder.iterdir():
            if not candidate.is_file():
                continue
            if candidate.name in referenced_files:
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if datetime.fromtimestamp(stat.st_mtime, UTC) >= threshold:
                continue
            orphan_names.append(candidate.name)
            orphan_bytes += stat.st_size
            if capped and len(orphan_names) >= policy.batch_limit:
                break

    protected = {
        "publications": int(
            session.scalar(sa.select(sa.func.count()).select_from(sa.table("publish_job"))) or 0
        ),
        "campaigns": int(session.scalar(sa.select(sa.func.count()).select_from(sa.table("campaign"))) or 0),
        "accounts": int(session.scalar(sa.select(sa.func.count()).select_from(sa.table("social_account"))) or 0),
        "media_assets": int(session.scalar(sa.select(sa.func.count()).select_from(sa.table("media_asset"))) or 0),
        "metric_definitions": int(
            session.scalar(sa.select(sa.func.count()).select_from(sa.table("analytics_metric_definition"))) or 0
        ),
        "decided_recommendations": int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(AnalyticsRecommendation)
                .where(
                    AnalyticsRecommendation.status.in_(
                        [RecommendationStatus.ACCEPTED, RecommendationStatus.REJECTED]
                    )
                )
            )
            or 0
        ),
        "runs_protected_by_observations": run_protected,
    }

    return RetentionPlan(
        policy=policy,
        aggregations=CategoryPlan(
            "aggregations",
            len(aggregation_ids),
            aggregation_ids,
            "every observation this rollup cites is expiring or already gone",
        ),
        observations=CategoryPlan(
            "observations",
            len(eligible_snapshots),
            eligible_snapshots,
            f"collected before {policy.cutoff.date().isoformat()} by a finished run",
        ),
        runs=CategoryPlan(
            "collection_runs",
            len(run_eligible),
            run_eligible,
            "finished before the cutoff and no observation still refers to it",
        ),
        reports=CategoryPlan(
            "reports",
            len(report_ids),
            report_ids,
            f"generated before {policy.report_cutoff.date().isoformat()} or explicitly archived",
            report_bytes,
        ),
        recommendations=CategoryPlan(
            "superseded_recommendations",
            len(superseded),
            superseded,
            "replaced by a newer recommendation before the cutoff; decisions are not deleted",
        ),
        orphan_files=CategoryPlan(
            "orphan_files",
            len(orphan_names),
            orphan_names,
            "a report file with no record pointing at it, abandoned for over a day",
            orphan_bytes,
        ),
        protected=protected,
    )


def _integrity(session: Session) -> tuple[bool, int]:
    violations = list(session.execute(sa.text("PRAGMA foreign_key_check")).fetchall())
    ok_row = session.execute(sa.text("PRAGMA integrity_check")).fetchone()
    integrity_ok = bool(ok_row and str(ok_row[0]).lower() == "ok")
    return integrity_ok and not violations, len(violations)


def preview(session: Session, policy: RetentionPolicy, *, audit: bool = True) -> RetentionPlan:
    """Read-only: what would be deleted, with the reason per category."""
    result = plan(session, policy)
    if audit:
        AuditService(session).record(
            AUDIT_PREVIEW,
            resource_type="analytics_retention",
            metadata=result.as_dict(),
        )
    return result


def execute(
    session: Session,
    policy: RetentionPolicy,
    *,
    dry_run: bool = False,
    actor: str = "system",
) -> RetentionResult:
    """Apply the plan in one bounded transaction and record what happened.

    ``dry_run`` computes the plan, records it and changes nothing — the same code path, so a dry run
    cannot disagree with the real one about eligibility.
    """
    started = time.monotonic()
    result_plan = plan(session, policy)
    deleted: dict[str, int] = {}
    errors: list[str] = []
    bytes_freed = 0

    if dry_run:
        for category in result_plan.categories:
            deleted[category.name] = 0
    else:
        # 1. Files first: a row is removed only after its file is, so a failure leaves a reachable file
        #    rather than a row pointing at nothing.
        for report_id in result_plan.reports.ids:
            row = session.get(AnalyticsReport, report_id)
            if row is None:
                continue
            try:
                from .reports import delete_report_file

                delete_report_file(row)
                bytes_freed += int(row.bytes or 0)
            except Exception as exc:  # noqa: BLE001 - a missing file must not block the row's removal
                errors.append(f"report file {report_id}: {exc}")
        for name in result_plan.orphan_files.ids:
            try:
                (_reports_dir() / name).unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"orphan file {name}: {exc}")

        # 2. Derived rows before the observations they cite.
        if result_plan.aggregations.ids:
            deleted["aggregations"] = int(
                session.execute(
                    sa.delete(AnalyticsAggregation).where(AnalyticsAggregation.id.in_(result_plan.aggregations.ids))
                ).rowcount
                or 0
            )
        if result_plan.reports.ids:
            deleted["reports"] = int(
                session.execute(
                    sa.delete(AnalyticsReport).where(AnalyticsReport.id.in_(result_plan.reports.ids))
                ).rowcount
                or 0
            )
        if result_plan.recommendations.ids:
            deleted["superseded_recommendations"] = int(
                session.execute(
                    sa.delete(AnalyticsRecommendation).where(
                        AnalyticsRecommendation.id.in_(result_plan.recommendations.ids),
                        AnalyticsRecommendation.status == RecommendationStatus.SUPERSEDED,
                    )
                ).rowcount
                or 0
            )

        # 3. Observations, then the runs whose cascade would otherwise remove live rows.
        if result_plan.observations.ids:
            deleted["observations"] = int(
                session.execute(
                    sa.delete(AnalyticsSnapshot).where(AnalyticsSnapshot.id.in_(result_plan.observations.ids))
                ).rowcount
                or 0
            )
        if result_plan.runs.ids:
            deleted["collection_runs"] = int(
                session.execute(
                    sa.delete(AnalyticsCollectionRun).where(AnalyticsCollectionRun.id.in_(result_plan.runs.ids))
                ).rowcount
                or 0
            )
        deleted.setdefault("aggregations", 0)
        deleted.setdefault("reports", 0)
        deleted.setdefault("observations", 0)
        deleted.setdefault("collection_runs", 0)
        deleted.setdefault("superseded_recommendations", 0)
        deleted["orphan_files"] = 0 if dry_run else len(result_plan.orphan_files.ids)

    session.flush()
    integrity_ok, violations = _integrity(session)
    remaining = plan(session, policy, capped=False)

    event = AuditService(session).record(
        AUDIT_EXECUTE,
        resource_type="analytics_retention",
        outcome="success" if integrity_ok else "warning",
        actor=actor,
        metadata={
            "dry_run": dry_run,
            "policy": policy.as_dict(),
            "deleted": deleted,
            "planned": {category.name: category.count for category in result_plan.categories},
            "remaining": {category.name: category.count for category in remaining.categories},
            "bytes_freed": bytes_freed,
            "protected": result_plan.protected,
            "foreign_key_violations": violations,
            "integrity_ok": integrity_ok,
            "errors": errors,
        },
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "analytics retention dry_run=%s deleted=%s remaining=%s integrity_ok=%s",
        dry_run,
        deleted,
        {category.name: category.count for category in remaining.categories},
        integrity_ok,
    )
    return RetentionResult(
        dry_run=dry_run,
        deleted=deleted,
        remaining={category.name: category.count for category in remaining.categories},
        bytes_freed=bytes_freed,
        audit_event_id=event.id,
        foreign_key_violations=violations,
        integrity_ok=integrity_ok,
        duration_ms=duration_ms,
        errors=errors,
    )


def status(session: Session, policy: RetentionPolicy) -> dict[str, Any]:
    """The retention facts diagnostics reports: policy, eligibility now, and the last execution."""
    from ...db.models import AuditEvent

    last = session.scalar(
        sa.select(AuditEvent)
        .where(AuditEvent.action == AUDIT_EXECUTE)
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    last_execution = None
    if last is not None:
        meta = last.metadata_redacted_json or {}
        last_execution = {
            "id": last.id,
            "at": last.created_at.isoformat() if last.created_at else None,
            "dry_run": bool(meta.get("dry_run")),
            "deleted": meta.get("deleted") or {},
            "bytes_freed": int(meta.get("bytes_freed") or 0),
            "integrity_ok": bool(meta.get("integrity_ok")),
            "actor": last.actor,
        }
    current = plan(session, policy, capped=False)
    return {
        "policy": policy.as_dict(),
        "eligible": {category.name: category.count for category in current.categories},
        "eligible_bytes": sum(category.bytes for category in current.categories),
        "last_execution": last_execution,
    }


__all__ = [
    "AUDIT_EXECUTE",
    "AUDIT_PREVIEW",
    "DEFAULT_BATCH_LIMIT",
    "REPORT_MIN_RETENTION_DAYS",
    "RetentionPlan",
    "RetentionPolicy",
    "RetentionResult",
    "execute",
    "plan",
    "preview",
    "status",
]
