"""Per-account sync state: what is due, what is blocked, and what is too old to ask for.

Three numbers decide whether a collection should happen:

* ``next_due_at`` — the provider's cadence, not our impatience. Meta insights are ~24 h behind and
  Instagram documents up to 48 h of delay, so polling faster than daily produces identical values
  and wastes the allowance;
* ``consecutive_failures`` — after three failures the account is *stalled* (slow down), and after
  five it is *blocked* (a human must look). A permission or credential failure blocks immediately,
  because retrying a revoked token is how an app gets rate limited for no information at all;
* ``backfill_until``/``provider_retention_days`` — the retention floor. Facebook keeps two years,
  Instagram keeps media for two years but account insights for 90 days, and TikTok documents no
  retention window. Asking for a date the provider has already deleted produces a confusing empty
  result, so the floor is applied before the request is built.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import (
    AnalyticsErrorClass,
    AnalyticsObjectType,
    AnalyticsSyncStatus,
    AnalyticsWindowKind,
    Platform,
)
from ...db.models import AnalyticsSyncState, SocialAccount

#: How long to wait between collections, per object type, in seconds.
POLL_INTERVALS: dict[AnalyticsObjectType, int] = {
    AnalyticsObjectType.ACCOUNT: 24 * 3600,
    AnalyticsObjectType.POST: 6 * 3600,
    AnalyticsObjectType.VIDEO: 6 * 3600,
    AnalyticsObjectType.REEL: 6 * 3600,
    AnalyticsObjectType.STORY: 6 * 3600,
}

#: Provider retention. `None` means "not documented", which is not the same as "forever".
RETENTION_DAYS: dict[tuple[Platform, AnalyticsObjectType], int | None] = {
    (Platform.FACEBOOK, AnalyticsObjectType.ACCOUNT): 730,
    (Platform.FACEBOOK, AnalyticsObjectType.POST): 730,
    (Platform.FACEBOOK, AnalyticsObjectType.VIDEO): 730,
    (Platform.FACEBOOK, AnalyticsObjectType.REEL): 730,
    (Platform.INSTAGRAM, AnalyticsObjectType.ACCOUNT): 90,
    (Platform.INSTAGRAM, AnalyticsObjectType.POST): 730,
    (Platform.INSTAGRAM, AnalyticsObjectType.VIDEO): 730,
    (Platform.INSTAGRAM, AnalyticsObjectType.REEL): 730,
    (Platform.INSTAGRAM, AnalyticsObjectType.STORY): 1,
    (Platform.TIKTOK, AnalyticsObjectType.ACCOUNT): None,
    (Platform.TIKTOK, AnalyticsObjectType.VIDEO): None,
}

#: Errors that mean "stop until a human fixes the connection", not "try again in a minute".
BLOCKING_ERROR_CLASSES = {AnalyticsErrorClass.AUTHORIZATION, AnalyticsErrorClass.PERMISSION}
STALL_AFTER_FAILURES = 3
BLOCK_AFTER_FAILURES = 5
BACKOFF_BASE_S = 300
BACKOFF_CAP_S = 6 * 3600

#: Performance windows, as hours after publication. A window that has passed is collected once.
WINDOWS: tuple[tuple[AnalyticsWindowKind, int], ...] = (
    (AnalyticsWindowKind.INITIAL, 1),
    (AnalyticsWindowKind.H24, 24),
    (AnalyticsWindowKind.H48, 48),
    (AnalyticsWindowKind.D7, 24 * 7),
    (AnalyticsWindowKind.D28, 24 * 28),
)


@dataclass(frozen=True, slots=True)
class DueWindow:
    window_kind: AnalyticsWindowKind
    target_at: datetime


def state_for(
    session: Session,
    account: SocialAccount,
    object_type: AnalyticsObjectType,
) -> AnalyticsSyncState:
    """Get or create the state row for one account and object type."""
    state = session.scalar(
        sa.select(AnalyticsSyncState).where(
            AnalyticsSyncState.account_id == account.id,
            AnalyticsSyncState.object_type == object_type,
        )
    )
    if state is not None:
        return state
    platform = account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform))
    state = AnalyticsSyncState(
        account_id=account.id,
        platform=platform,
        object_type=object_type,
        state=AnalyticsSyncStatus.NOT_STARTED,
        poll_interval_s=POLL_INTERVALS.get(object_type, 6 * 3600),
        consecutive_failures=0,
        provider_retention_days=RETENTION_DAYS.get((platform, object_type)),
        next_due_at=None,
        discovery_complete=False,
    )
    session.add(state)
    session.flush()
    return state


def retention_floor(state: AnalyticsSyncState, *, now: datetime | None = None) -> datetime | None:
    """The oldest moment the provider still has data for, or ``None`` when undocumented."""
    if state.provider_retention_days is None:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=state.provider_retention_days)


def windows_due(
    published_at: datetime | None,
    *,
    now: datetime | None = None,
    already_collected: set[AnalyticsWindowKind] | None = None,
) -> list[DueWindow]:
    """Which performance windows have passed and have not been collected yet, oldest first."""
    if published_at is None:
        return []
    moment = now or datetime.now(UTC)
    done = already_collected or set()
    due: list[DueWindow] = []
    for window_kind, hours in WINDOWS:
        target = published_at + timedelta(hours=hours)
        if target <= moment and window_kind not in done:
            due.append(DueWindow(window_kind=window_kind, target_at=target))
    return due


def is_due(state: AnalyticsSyncState, *, now: datetime | None = None) -> bool:
    if state.state is AnalyticsSyncStatus.BLOCKED:
        return False
    if state.next_due_at is None:
        return True
    return state.next_due_at <= (now or datetime.now(UTC))


def due_states(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[AnalyticsSyncState]:
    """States a scheduler sweep should act on: due, not blocked, not mid-flight."""
    moment = now or datetime.now(UTC)
    return list(
        session.scalars(
            sa.select(AnalyticsSyncState)
            .where(
                AnalyticsSyncState.state.notin_(
                    [AnalyticsSyncStatus.BLOCKED, AnalyticsSyncStatus.DISCOVERING, AnalyticsSyncStatus.BACKFILLING]
                ),
                sa.or_(AnalyticsSyncState.next_due_at.is_(None), AnalyticsSyncState.next_due_at <= moment),
            )
            .order_by(AnalyticsSyncState.next_due_at.asc().nulls_first())
            .limit(limit)
        )
    )


def mark_discovering(state: AnalyticsSyncState, *, now: datetime | None = None) -> None:
    state.state = AnalyticsSyncStatus.DISCOVERING


def mark_backfilling(state: AnalyticsSyncState, until: datetime | None) -> None:
    state.state = AnalyticsSyncStatus.BACKFILLING
    if until is not None:
        state.backfill_until = until


def record_success(
    state: AnalyticsSyncState,
    *,
    now: datetime | None = None,
    full: bool = False,
) -> None:
    moment = now or datetime.now(UTC)
    state.state = AnalyticsSyncStatus.CURRENT
    state.consecutive_failures = 0
    state.last_error_class = None
    state.last_error_code = None
    state.last_success_at = moment
    if full:
        state.last_full_sync_at = moment
    else:
        state.last_incremental_sync_at = moment
    state.next_due_at = moment + timedelta(seconds=max(60, int(state.poll_interval_s or 3600)))


def record_failure(
    state: AnalyticsSyncState,
    *,
    error_class: AnalyticsErrorClass | None,
    error_code: str | None,
    now: datetime | None = None,
) -> None:
    """Back off, stall, or block — the three answers to a failure that are not "retry now"."""
    moment = now or datetime.now(UTC)
    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    state.last_error_class = error_class
    state.last_error_code = (error_code or "")[:80] or None
    if error_class in BLOCKING_ERROR_CLASSES:
        state.state = AnalyticsSyncStatus.BLOCKED
        state.next_due_at = None
        state.notes = f"blocked: {error_code}"
        return
    delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (state.consecutive_failures - 1)))
    state.state = (
        AnalyticsSyncStatus.STALLED
        if state.consecutive_failures >= STALL_AFTER_FAILURES
        else state.state
    )
    if state.consecutive_failures >= BLOCK_AFTER_FAILURES:
        state.state = AnalyticsSyncStatus.BLOCKED
        state.notes = f"blocked after {state.consecutive_failures} failures: {error_code}"
    state.next_due_at = moment + timedelta(seconds=delay)


def unblock(state: AnalyticsSyncState, *, now: datetime | None = None) -> None:
    """A human fixed the connection; the account resumes at the normal cadence."""
    state.state = AnalyticsSyncStatus.NOT_STARTED
    state.consecutive_failures = 0
    state.next_due_at = now or datetime.now(UTC)


def note_discovery_cursor(state: AnalyticsSyncState, *, cursor: str | None, complete: bool) -> None:
    state.discovery_cursor = cursor
    state.discovery_complete = complete


def summary(states: list[AnalyticsSyncState]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for state in states:
        counts[state.state.value] = counts.get(state.state.value, 0) + 1
    return counts
