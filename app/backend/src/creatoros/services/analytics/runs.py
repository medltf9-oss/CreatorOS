"""Collection runs: the idempotency unit, and the honest record of what happened.

A run answers "which logical collection attempt is this?". The key is
``account | target | object | window | utc bucket``, hashed into ``dedupe_key`` which is unique in
the database, so:

* a replayed queue job (same account, same target, same window, same hour) **resumes** the same run
  and writes no duplicate observations;
* a genuinely separate attempt (next hour, next window) opens a new run and appends; it never
  rewrites a closed run's rows;
* a run that already finished successfully is not re-executed at all, which is what makes at-least-once
  delivery safe for analytics.

The historical snapshot ledger stays append-only *between* runs. Retries inside a run are de-duplicated
by the snapshot writer's unique key, not by deleting anything.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import (
    AnalyticsErrorClass,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsWindowKind,
    Platform,
)
from ...db.models import AnalyticsCollectionRun, SocialAccount

#: Windows measured in hours use an hour bucket; the rest use a day bucket.
_HOURLY_WINDOWS = {AnalyticsWindowKind.INITIAL, AnalyticsWindowKind.H24}


@dataclass(slots=True)
class RunContext:
    run: AnalyticsCollectionRun
    reused: bool = False
    closed: bool = False

    @property
    def run_id(self) -> str:
        return self.run.id

    def as_dict(self) -> dict:
        return {
            "run_id": self.run.id,
            "reused": self.reused,
            "already_finished": self.closed,
            "outcome": self.run.outcome.value,
        }


@dataclass(slots=True)
class RunCounters:
    http_calls: int = 0
    metrics_attempted: int = 0
    metrics_observed: int = 0
    metrics_unavailable: int = 0
    metrics_written: int = 0
    notes: list[str] = field(default_factory=list)


def bucket_start(moment: datetime, window_kind: AnalyticsWindowKind | None) -> datetime:
    moment = moment.astimezone(UTC)
    if window_kind in _HOURLY_WINDOWS:
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def dedupe_key(
    *,
    account_id: str,
    target_kind: AnalyticsRunTarget,
    target_object_id: str | None,
    window_kind: AnalyticsWindowKind | None,
    moment: datetime,
) -> str:
    """Stable across retries of the same logical run, different for the next one."""
    bucket = bucket_start(moment, window_kind).isoformat()
    raw = "|".join(
        [
            account_id,
            target_kind.value,
            target_object_id or "",
            window_kind.value if window_kind else "",
            bucket,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def open_run(
    session: Session,
    *,
    account: SocialAccount,
    target_kind: AnalyticsRunTarget,
    target_object_id: str | None = None,
    window_kind: AnalyticsWindowKind | None = None,
    api_version: str,
    job_id: str | None = None,
    now: datetime | None = None,
) -> RunContext:
    """Open, or resume, the run for this logical attempt."""
    moment = now or datetime.now(UTC)
    key = dedupe_key(
        account_id=account.id,
        target_kind=target_kind,
        target_object_id=target_object_id,
        window_kind=window_kind,
        moment=moment,
    )
    existing = session.scalar(sa.select(AnalyticsCollectionRun).where(AnalyticsCollectionRun.dedupe_key == key))
    if existing is not None:
        # `finished_at` is the truth about closure, not `outcome`: a run is created as PARTIAL and
        # only `finish_run` sets a terminal outcome with a timestamp.
        if existing.finished_at is None:
            # A crashed or in-flight attempt: resume it rather than opening a second run for the
            # same logical slot.
            return RunContext(existing, reused=True, closed=False)
        return RunContext(existing, reused=True, closed=True)

    run = AnalyticsCollectionRun(
        account_id=account.id,
        platform=account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform)),
        target_kind=target_kind,
        target_object_id=target_object_id,
        job_id=job_id,
        window_kind=window_kind,
        started_at=moment,
        outcome=AnalyticsRunOutcome.PARTIAL,
        dedupe_key=key,
        api_version=api_version,
    )
    session.add(run)
    session.flush()
    return RunContext(run, reused=False, closed=False)


def finish_run(
    session: Session,
    context: RunContext,
    *,
    outcome: AnalyticsRunOutcome,
    counters: RunCounters | None = None,
    error_class: AnalyticsErrorClass | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    rate_limit_remaining: int | None = None,
    rate_limit_reset_at: datetime | None = None,
    now: datetime | None = None,
) -> None:
    run = context.run
    counts = counters or RunCounters()
    run.outcome = outcome
    run.finished_at = now or datetime.now(UTC)
    run.http_calls = counts.http_calls
    run.metrics_attempted = counts.metrics_attempted
    run.metrics_observed = counts.metrics_observed
    run.metrics_unavailable = counts.metrics_unavailable
    run.metrics_written = counts.metrics_written
    run.error_class = error_class
    run.error_code = error_code
    # Redacted by construction: codes and counts, never a token or a full URL with parameters.
    run.error_detail_redacted = (error_detail or None) and error_detail[:1000]
    run.rate_limit_remaining = rate_limit_remaining
    run.rate_limit_reset_at = rate_limit_reset_at
    session.flush()


def recent_runs(session: Session, account_id: str, *, limit: int = 20) -> list[AnalyticsCollectionRun]:
    return list(
        session.scalars(
            sa.select(AnalyticsCollectionRun)
            .where(AnalyticsCollectionRun.account_id == account_id)
            .order_by(AnalyticsCollectionRun.started_at.desc())
            .limit(limit)
        )
    )


def stale_running_runs(session: Session, *, older_than_minutes: int = 15, now: datetime | None = None) -> list[AnalyticsCollectionRun]:
    """Runs left RUNNING by a crash, which a recovery sweep can close as interrupted."""
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=older_than_minutes)
    return list(
        session.scalars(
            sa.select(AnalyticsCollectionRun).where(
                AnalyticsCollectionRun.finished_at.is_(None),
                AnalyticsCollectionRun.started_at < cutoff,
            )
        )
    )
