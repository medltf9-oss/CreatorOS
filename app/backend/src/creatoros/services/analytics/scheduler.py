"""The analytics dispatcher, run by the **existing** sweeper thread.

There is no second scheduler and no fifth thread. `BackgroundWorker._scheduler_loop` already ticks
every `CREATOROS_SCHEDULER_SECONDS` (15 s) with a fresh session; this object is called from that same
tick, after `PublishScheduler.tick()`, so publishing keeps priority when both are due.

What a tick does:

1. **Adopt** connected accounts that have no sync state yet, so a newly connected account is
   collected without anyone pressing a button.
2. **Dispatch** the states that are due: account sync for the account, discovery/backfill when there
   is work. Blocked accounts are skipped; stalled accounts are already backoff-delayed.
3. **Maintain** the ledger: a retention pass at most once every 24 h and a version check at most once
   every 7 days, on the same lane, below collection priority. Both are idempotent through the queue's
   ``idempotency_key``, so a restart cannot queue a second copy.
4. **Report** counts for the log line the sweeper already writes.

Everything is idempotent: the queue's `idempotency_key` means a double tick cannot enqueue two
collections for the same account and window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import AnalyticsObjectType, AnalyticsSyncStatus, ConnectionStatus, Platform
from ...db.models import AnalyticsSyncState, SocialAccount
from . import discovery, jobs, sync_state

log = logging.getLogger("creatoros.analytics")

#: Object types a provider can analyse at account level. Media types get states lazily.
ACCOUNT_OBJECT_TYPES = {Platform.FACEBOOK, Platform.INSTAGRAM, Platform.TIKTOK}
SUPPORTED_PLATFORMS = frozenset(ACCOUNT_OBJECT_TYPES)


#: Maintenance cadence. Retention is daily; a version check does not need to be.
RETENTION_INTERVAL_HOURS = 24
VERSION_CHECK_INTERVAL_DAYS = 7


@dataclass(slots=True)
class AnalyticsTickResult:
    accounts_seen: int = 0
    states_adopted: int = 0
    dispatched: int = 0
    blocked: int = 0
    skipped: int = 0
    retention_queued: bool = False
    version_check_queued: bool = False
    details: list[dict] = field(default_factory=list)

    def count(self, key: str) -> int:
        # The sweeper logs `enqueued`; for analytics that is exactly `dispatched`.
        if key == "enqueued":
            key = "dispatched"
        return int(getattr(self, key, 0) or 0)

    def as_dict(self) -> dict:
        return {
            "accounts_seen": self.accounts_seen,
            "states_adopted": self.states_adopted,
            "dispatched": self.dispatched,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "retention_queued": self.retention_queued,
            "version_check_queued": self.version_check_queued,
        }


class AnalyticsScheduler:
    """Dispatch due analytics work. Never collects anything itself."""

    def __init__(
        self,
        session: Session,
        *,
        enabled: bool = True,
        now: datetime | None = None,
        limit: int = 50,
    ) -> None:
        self.session = session
        self.enabled = enabled
        self._now = now
        self.limit = limit

    def tick(self) -> AnalyticsTickResult:
        result = AnalyticsTickResult()
        if not self.enabled:
            return result
        moment = self._now or datetime.now(UTC)

        accounts = list(
            self.session.scalars(
                sa.select(SocialAccount)
                .where(
                    SocialAccount.connection_status == ConnectionStatus.CONNECTED,
                    SocialAccount.platform.in_(list(SUPPORTED_PLATFORMS)),
                )
                .limit(500)
            )
        )
        result.accounts_seen = len(accounts)
        by_id = {account.id: account for account in accounts}

        self._maintenance(moment, result)

        for account in accounts:
            state = sync_state.state_for(self.session, account, AnalyticsObjectType.ACCOUNT)
            if state.state is AnalyticsSyncStatus.NOT_STARTED:
                result.states_adopted += 1

        due = sync_state.due_states(self.session, now=moment, limit=self.limit)
        for state in due:
            account = by_id.get(state.account_id)
            if account is None:
                result.skipped += 1
                continue
            if state.state is AnalyticsSyncStatus.BLOCKED:
                result.blocked += 1
                continue
            payload = self._dispatch(account, state)
            if payload is None:
                result.skipped += 1
                continue
            result.dispatched += 1
            result.details.append(payload)
        return result

    # ------------------------------------------------------------------ internals
    def _maintenance(self, moment: datetime, result: AnalyticsTickResult) -> None:
        """Queue retention and the version check when they are due — never inline.

        Both are enqueued on the analytics lane, so a slow retention pass cannot make a publishing tick
        late. When the queue already holds an open job with the same idempotency key, nothing is added.
        """
        from ...db.models import AuditEvent
        from . import retention as retention_service

        last_retention = self.session.scalar(
            sa.select(AuditEvent)
            .where(AuditEvent.action == retention_service.AUDIT_EXECUTE)
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
        due_at = moment - timedelta(hours=RETENTION_INTERVAL_HOURS)
        if last_retention is None or (last_retention.created_at and last_retention.created_at < due_at):
            retention_days = self._retention_days()
            if retention_days:
                jobs.enqueue_retention(self.session, retention_days=retention_days)
                result.retention_queued = True

        last_check = self.session.scalar(
            sa.select(AuditEvent)
            .where(AuditEvent.action == "analytics.version.check")
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
        due_check = moment - timedelta(days=VERSION_CHECK_INTERVAL_DAYS)
        if last_check is None or (last_check.created_at and last_check.created_at < due_check):
            jobs.enqueue_version_check(self.session)
            result.version_check_queued = True

    def _retention_days(self) -> int | None:
        from ...db.models import AppSetting

        row = self.session.get(AppSetting, "analytics.settings")
        stored = dict(row.value_json or {}) if row is not None else {}
        value = stored.get("retention_days", 365)
        try:
            days = int(value)
        except (TypeError, ValueError):
            return None
        return days if days >= 1 else None

    def _dispatch(self, account: SocialAccount, state: AnalyticsSyncState) -> dict | None:
        if state.object_type is AnalyticsObjectType.ACCOUNT:
            enqueued = jobs.enqueue_account_sync(self.session, account)
            return {"account_id": account.id, "job": "account_sync", "job_id": enqueued.job_id}

        # A media state exists only because we discovered objects for it: backfill the windows that
        # are still due, and stop asking when nothing is left.
        candidates = discovery.backfill_candidates(self.session, account, now=self._now)
        if not candidates:
            sync_state.note_discovery_cursor(state, cursor=state.discovery_cursor, complete=True)
            return None
        enqueued = jobs.enqueue_backfill(self.session, account)
        return {"account_id": account.id, "job": "backfill", "job_id": enqueued.job_id}


class CombinedScheduler:
    """Publishing first, then analytics, in the same sweep and the same thread.

    Publishing wins when both are due: a scheduled post that misses its slot is worse than an
    analytics refresh that waits one tick.
    """

    def __init__(self, publish_scheduler, analytics_scheduler) -> None:
        self.publish_scheduler = publish_scheduler
        self.analytics_scheduler = analytics_scheduler

    def tick(self):
        publish_result = self.publish_scheduler.tick()
        analytics_result = self.analytics_scheduler.tick()
        return _MergedTick(publish_result, analytics_result)


@dataclass(slots=True)
class _MergedTick:
    publish: object
    analytics: AnalyticsTickResult

    def count(self, key: str) -> int:
        total = 0
        if hasattr(self.publish, "count"):
            total += int(self.publish.count(key) or 0)
        total += int(self.analytics.count(key) or 0)
        return total

    def as_dict(self) -> dict:
        merged: dict = {}
        if hasattr(self.publish, "as_dict"):
            merged.update(self.publish.as_dict())
        merged["analytics"] = self.analytics.as_dict()
        return merged
