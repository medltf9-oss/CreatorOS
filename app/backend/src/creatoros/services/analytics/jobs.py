"""Analytics jobs: what the queue lane actually executes.

Four job types, all on the existing durable queue and the existing sweeper:

* ``analytics.account_sync`` — the account itself (followers, page counters, account insights);
* ``analytics.object_sync`` — one published object at one performance window;
* ``analytics.discovery`` — find published/known objects for an account;
* ``analytics.backfill`` — collect the windows that have passed and are still available;
* ``analytics.retention`` — apply the retention policy, in bounded batches, audited;
* ``analytics.version_check`` — record a look at the provider documentation (never a version bump).

Handlers are thin: they translate a payload into a collector call and return counters. They never
decide policy (the registry and sync state do), never parse provider payloads (the adapters do) and
never publish anything.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ...db.enums import AnalyticsObjectType, AnalyticsWindowKind
from ...db.models import SocialAccount
from ..queue import QueueService
from . import discovery
from . import sync_state as sync

log = logging.getLogger("creatoros.analytics")

QUEUE = "analytics"

JOB_ACCOUNT_SYNC = "analytics.account_sync"
JOB_OBJECT_SYNC = "analytics.object_sync"
JOB_DISCOVERY = "analytics.discovery"
JOB_BACKFILL = "analytics.backfill"
JOB_RETENTION = "analytics.retention"
JOB_VERSION_CHECK = "analytics.version_check"

JOB_TYPES = (
    JOB_ACCOUNT_SYNC,
    JOB_OBJECT_SYNC,
    JOB_DISCOVERY,
    JOB_BACKFILL,
    JOB_RETENTION,
    JOB_VERSION_CHECK,
)

#: Below the sweeper's other work: a collection is never more urgent than a scheduled publish.
PRIORITY_ACCOUNT_SYNC = 40
PRIORITY_DISCOVERY = 45
PRIORITY_OBJECT_SYNC = 40
PRIORITY_BACKFILL = 55
#: Maintenance runs below everything a person is waiting for.
PRIORITY_RETENTION = 70
PRIORITY_VERSION_CHECK = 75


def enqueue_account_sync(
    session: Session,
    account: SocialAccount,
    *,
    window_kind: AnalyticsWindowKind | None = None,
    delay_seconds: float = 0.0,
) -> Any:
    return QueueService(session).enqueue(
        JOB_ACCOUNT_SYNC,
        {
            "account_id": account.id,
            "window_kind": window_kind.value if window_kind else None,
        },
        queue=QUEUE,
        priority=PRIORITY_ACCOUNT_SYNC,
        delay_seconds=delay_seconds,
        idempotency_key=f"{JOB_ACCOUNT_SYNC}:{account.id}:{window_kind.value if window_kind else 'default'}",
        max_attempts=3,
    )


def enqueue_object_sync(
    session: Session,
    account: SocialAccount,
    *,
    provider_object_id: str,
    object_type: AnalyticsObjectType,
    window_kind: AnalyticsWindowKind,
    publication_id: str | None = None,
    product_type: str | None = None,
    delay_seconds: float = 0.0,
) -> Any:
    return QueueService(session).enqueue(
        JOB_OBJECT_SYNC,
        {
            "account_id": account.id,
            "provider_object_id": provider_object_id,
            "object_type": object_type.value,
            "window_kind": window_kind.value,
            "publication_id": publication_id,
            "product_type": product_type,
        },
        queue=QUEUE,
        priority=PRIORITY_OBJECT_SYNC,
        delay_seconds=delay_seconds,
        idempotency_key=f"{JOB_OBJECT_SYNC}:{account.id}:{provider_object_id}:{window_kind.value}",
        max_attempts=3,
    )


def enqueue_discovery(session: Session, account: SocialAccount) -> Any:
    return QueueService(session).enqueue(
        JOB_DISCOVERY,
        {"account_id": account.id},
        queue=QUEUE,
        priority=PRIORITY_DISCOVERY,
        idempotency_key=f"{JOB_DISCOVERY}:{account.id}",
        max_attempts=2,
    )


def enqueue_backfill(session: Session, account: SocialAccount) -> Any:
    return QueueService(session).enqueue(
        JOB_BACKFILL,
        {"account_id": account.id},
        queue=QUEUE,
        priority=PRIORITY_BACKFILL,
        idempotency_key=f"{JOB_BACKFILL}:{account.id}",
        max_attempts=2,
    )


# ---------------------------------------------------------------------- handlers


def enqueue_retention(session: Session, *, retention_days: int, batch_limit: int = 2000) -> Any:
    """Queue one retention pass. The idempotency key means a double tick cannot queue two."""
    return QueueService(session).enqueue(
        JOB_RETENTION,
        {"retention_days": int(retention_days), "batch_limit": int(batch_limit)},
        queue=QUEUE,
        priority=PRIORITY_RETENTION,
        idempotency_key=f"{JOB_RETENTION}:{retention_days}:{batch_limit}",
        max_attempts=2,
    )


def enqueue_version_check(session: Session) -> Any:
    return QueueService(session).enqueue(
        JOB_VERSION_CHECK,
        {},
        queue=QUEUE,
        priority=PRIORITY_VERSION_CHECK,
        idempotency_key=JOB_VERSION_CHECK,
        max_attempts=1,
    )


def build_analytics_handlers(session_factory) -> dict[str, Any]:
    """Handlers for the `analytics` lane. One collector, one policy, four entry points."""

    def handle_account_sync(session: Session, job: Any) -> dict:
        from .collector import AnalyticsCollector

        account = _account(session, job.payload.get("account_id"))
        window_kind = _window(job.payload.get("window_kind"))
        collector = AnalyticsCollector(session)
        outcome = asyncio.run(
            collector.collect_account(account, object_type=AnalyticsObjectType.ACCOUNT, window_kind=window_kind)
        )
        _record_state(session, account, AnalyticsObjectType.ACCOUNT, outcome)
        return outcome.as_dict()

    def handle_object_sync(session: Session, job: Any) -> dict:
        from .collector import AnalyticsCollector

        account = _account(session, job.payload.get("account_id"))
        object_type = AnalyticsObjectType(str(job.payload.get("object_type") or "post"))
        window_kind = _window(job.payload.get("window_kind"))
        provider_object_id = str(job.payload.get("provider_object_id") or "")
        if not provider_object_id:
            raise ValueError("analytics.object_sync requires provider_object_id")
        collector = AnalyticsCollector(session)
        outcome = asyncio.run(
            collector.collect_account(
                account,
                object_type=object_type,
                window_kind=window_kind,
                object_id=provider_object_id,
                publication_id=job.payload.get("publication_id"),
                product_type=job.payload.get("product_type"),
                job_id=job.id,
            )
        )
        _record_state(session, account, object_type, outcome)
        return outcome.as_dict()

    def handle_discovery(session: Session, job: Any) -> dict:
        account = _account(session, job.payload.get("account_id"))
        published = discovery.discover_published_objects(session, account)
        unlinked = discovery.unlinked_objects(session, account)
        state = sync.state_for(session, account, AnalyticsObjectType.VIDEO)
        discovery.note_discovery(state, published)
        return {
            "account_id": account.id,
            "published": len(published),
            "unlinked": len(unlinked),
            "objects": [entry.as_dict() for entry in published[:50]],
        }

    def handle_backfill(session: Session, job: Any) -> dict:
        account = _account(session, job.payload.get("account_id"))
        candidates = discovery.backfill_candidates(session, account)
        queued = 0
        for discovered, due in candidates:
            for window in due:
                enqueue_object_sync(
                    session,
                    account,
                    provider_object_id=discovered.provider_object_id,
                    object_type=discovered.object_type,
                    window_kind=window.window_kind,
                    publication_id=discovered.publication_id,
                )
                queued += 1
        # A backfill that finds nothing is not a failure: it means every window is collected.
        return {"account_id": account.id, "candidates": len(candidates), "queued": queued}

    def handle_retention(session: Session, job: Any) -> dict:
        """Delete what the policy allows, bounded. Reports what is left so the caller can re-queue."""
        from .retention import RetentionPolicy, execute

        days = int(job.payload.get("retention_days") or 365)
        limit = int(job.payload.get("batch_limit") or 2000)
        policy = RetentionPolicy(retention_days=days, batch_limit=limit)
        result = execute(session, policy, dry_run=False, actor="analytics.sweeper")
        payload = result.as_dict()
        payload["work_remaining"] = sum(result.remaining.values())
        return payload

    def handle_version_check(session: Session, job: Any) -> dict:
        from .version_watch import observe

        observation = observe(session)
        return observation.as_dict()

    return {
        JOB_ACCOUNT_SYNC: handle_account_sync,
        JOB_OBJECT_SYNC: handle_object_sync,
        JOB_DISCOVERY: handle_discovery,
        JOB_BACKFILL: handle_backfill,
        JOB_RETENTION: handle_retention,
        JOB_VERSION_CHECK: handle_version_check,
    }


def _account(session: Session, account_id: Any) -> SocialAccount:
    account = session.get(SocialAccount, str(account_id or ""))
    if account is None:
        raise ValueError(f"social account {account_id!r} does not exist")
    return account


def _window(value: Any) -> AnalyticsWindowKind | None:
    if not value:
        return None
    try:
        return AnalyticsWindowKind(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown analytics window: {value!r}") from exc


def _record_state(session: Session, account: SocialAccount, object_type: AnalyticsObjectType, outcome: Any) -> None:
    """One place where a collection outcome becomes sync state.

    A skipped run (budget, already finished) is not a failure and must not push the account into
    backoff; a failed run records the error class so the scheduler backs off correctly.
    """
    state = sync.state_for(session, account, object_type)
    if outcome.skipped_reason:
        return
    if not outcome.ok:
        return
    if outcome.warnings and not outcome.runs:
        sync.record_failure(state, error_class=None, error_code="ANALYTICS_UNKNOWN", now=datetime.now(UTC))
        return
    failed = [entry for entry in outcome.runs if entry.get("reason") not in {None, "already_finished"}]
    if failed:
        code = str(failed[0].get("reason"))
        sync.record_failure(
            state,
            error_class=discovery.classify_discovery_error(code),
            error_code=code,
            now=datetime.now(UTC),
        )
        return
    sync.record_success(state, now=datetime.now(UTC), full=True)
