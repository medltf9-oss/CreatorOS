"""The publish scheduler: when a scheduled destination becomes a queued one.

Three things this deliberately does not do:

* it does not publish anything. It moves a destination to `ready` and enqueues a job; only the
  executor publishes.
* it does not decide for the user when a schedule was missed. The default policy is `ask` (D11): a
  destination whose time passed while the application was closed stops and says so, because
  "published 14 hours late" is a decision a person should make.
* it does not trust wall-clock arithmetic. Times are stored as absolute UTC instants plus the IANA
  zone the user chose, so a daylight-saving change never moves a schedule by an hour.

What it does do is survive restarts: `tick()` is idempotent, because the queue's idempotency key is
per destination and `missed_at` records that a destination was already examined.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import PublishState, can_transition
from ...db.models import AppSetting, PublishJob
from ..backup import AuditService
from .errors import PublishingError

SETTING_KEY = "publish.late_policy"
DEFAULT_GRACE_SECONDS = 120
POLICIES = ("ask", "publish_now", "skip")
Action = Literal["enqueued", "missed", "future", "idle", "skipped"]


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    policy: str = "ask"
    grace_seconds: int = DEFAULT_GRACE_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return {"policy": self.policy, "grace_seconds": self.grace_seconds}


@dataclass(frozen=True, slots=True)
class Decision:
    job_id: str
    action: Action
    reason: str
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "reason": self.reason,
            "state": self.state,
        }


@dataclass(slots=True)
class TickResult:
    scanned: int = 0
    decisions: list[Decision] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for decision in self.decisions if decision.action == action)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "enqueued": self.count("enqueued"),
            "missed": self.count("missed"),
            "future": self.count("future"),
            "idle": self.count("idle"),
            "decisions": [decision.as_dict() for decision in self.decisions],
        }


class PublishScheduler:
    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
        executor: Any | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self.session = session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.audit = audit or AuditService(session)
        self._executor = executor

    # ------------------------------------------------------------------- policy
    def load_policy(self) -> SchedulePolicy:
        row = self.session.get(AppSetting, SETTING_KEY)
        if row is None:
            return SchedulePolicy()
        value = dict(row.value_json or {})
        policy = str(value.get("policy") or "ask")
        grace = value.get("grace_seconds")
        return SchedulePolicy(
            policy=policy if policy in POLICIES else "ask",
            grace_seconds=int(grace) if isinstance(grace, (int, float)) else DEFAULT_GRACE_SECONDS,
        )

    def set_policy(self, *, policy: str, grace_seconds: int | None = None, actor: str = "local-user") -> SchedulePolicy:
        if policy not in POLICIES:
            raise PublishingError(
                "INVALID_LATE_POLICY",
                f"{policy!r} is not a late policy; use ask, publish_now or skip.",
                http_status=422,
                detail={"allowed": list(POLICIES)},
            )
        resolved = SchedulePolicy(
            policy=policy,
            grace_seconds=DEFAULT_GRACE_SECONDS if grace_seconds is None else max(0, min(int(grace_seconds), 86_400)),
        )
        row = self.session.get(AppSetting, SETTING_KEY)
        if row is None:
            self.session.add(AppSetting(key=SETTING_KEY, value_json=resolved.as_dict(), scope="app"))
        else:
            row.value_json = resolved.as_dict()
        self.session.flush()
        self.audit.record(
            "publish.schedule.policy",
            resource_type="app_setting",
            resource_id=SETTING_KEY,
            actor=actor,
            metadata=resolved.as_dict(),
        )
        return resolved

    # -------------------------------------------------------------- scheduling
    def schedule(
        self,
        job: PublishJob,
        *,
        when: datetime | None,
        display_timezone: str | None = None,
    ) -> PublishJob:
        """Set (or clear) the intended time. Approval and queueing stay separate steps."""
        if when is not None and when.tzinfo is None:
            raise PublishingError(
                "INVALID_SCHEDULE",
                "A schedule must carry a time zone; send an aware UTC timestamp.",
                http_status=422,
            )
        if display_timezone is not None:
            try:
                ZoneInfo(display_timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise PublishingError(
                    "INVALID_TIMEZONE",
                    f"{display_timezone!r} is not an IANA time zone.",
                    http_status=422,
                    detail={"timezone": display_timezone},
                ) from exc
            job.display_timezone = display_timezone
        job.scheduled_at_utc = when.astimezone(UTC) if when is not None else None
        job.missed_at = None
        if when is None and _state(job) is PublishState.SCHEDULED:
            job.job_state = PublishState.READY
            job.state_reason = "schedule_cleared"
        self.session.flush()
        self.audit.record(
            "publish.schedule.set",
            resource_type="publish_job",
            resource_id=job.id,
            metadata={
                "scheduled_at_utc": job.scheduled_at_utc.isoformat() if job.scheduled_at_utc else None,
                "display_timezone": job.display_timezone,
            },
        )
        return job

    def reschedule_local(self, job: PublishJob, *, display_timezone: str) -> PublishJob:
        """Keep the clock time the user chose and move it to another zone.

        Changing the workspace timezone must not silently move a 09:00 post to 03:00, and must not
        silently keep the same instant either. The wall clock is what a person scheduled.
        """
        try:
            target = ZoneInfo(display_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise PublishingError(
                "INVALID_TIMEZONE", f"{display_timezone!r} is not an IANA time zone.", http_status=422
            ) from exc
        if job.scheduled_at_utc is None:
            job.display_timezone = display_timezone
            self.session.flush()
            return job
        current = job.scheduled_at_utc
        source_zone = ZoneInfo(job.display_timezone or "UTC")
        local = current.astimezone(source_zone)
        moved = local.replace(tzinfo=target).astimezone(UTC)
        job.scheduled_at_utc = moved
        job.display_timezone = display_timezone
        self.session.flush()
        self.audit.record(
            "publish.schedule.rezone",
            resource_type="publish_job",
            resource_id=job.id,
            metadata={
                "from_zone": str(source_zone),
                "to_zone": display_timezone,
                "from_utc": current.isoformat(),
                "to_utc": moved.isoformat(),
            },
        )
        return job

    # ------------------------------------------------------------------- tick
    def due_jobs(self, now: datetime | None = None) -> list[PublishJob]:
        moment = now or self.clock()
        return list(
            self.session.scalars(
                sa.select(PublishJob)
                .where(
                    PublishJob.scheduled_at_utc.is_not(None),
                    PublishJob.scheduled_at_utc <= moment,
                    PublishJob.job_state.in_([PublishState.SCHEDULED, PublishState.APPROVED]),
                )
                .order_by(PublishJob.scheduled_at_utc)
            )
        )

    def tick(self, *, now: datetime | None = None, grace_seconds: int | None = None) -> TickResult:
        """One pass over scheduled destinations. Safe to call on every wake-up and after a restart."""
        moment = (now or self.clock()).astimezone(UTC)
        policy = self.load_policy()
        grace = grace_seconds if grace_seconds is not None else policy.grace_seconds
        result = TickResult()

        pending = list(
            self.session.scalars(
                sa.select(PublishJob)
                .where(
                    PublishJob.scheduled_at_utc.is_not(None),
                    PublishJob.job_state.in_([PublishState.SCHEDULED, PublishState.APPROVED]),
                )
                .order_by(PublishJob.scheduled_at_utc)
            )
        )
        result.scanned = len(pending)
        for job in pending:
            scheduled = _aware(job.scheduled_at_utc)
            assert scheduled is not None
            if scheduled > moment:
                result.decisions.append(
                    Decision(job.id, "future", "scheduled_in_the_future", str(job.job_state))
                )
                continue

            lateness = (moment - scheduled).total_seconds()
            if lateness > grace:
                decision = self._missed(job, policy, lateness)
                result.decisions.append(decision)
                continue

            if not job.approval_request_id:
                # Nothing to publish yet: say so rather than queueing a job that will refuse itself.
                job.state_reason = "approval_missing_at_schedule_time"
                self.session.flush()
                result.decisions.append(
                    Decision(job.id, "skipped", "approval_missing", str(job.job_state))
                )
                continue

            self._move(job, PublishState.READY, reason="schedule_due")
            enqueue = getattr(self._executor, "enqueue", None)
            if enqueue is not None:
                enqueue(job)
            result.decisions.append(Decision(job.id, "enqueued", "schedule_due", str(job.job_state)))

        self.session.flush()
        return result

    def _missed(self, job: PublishJob, policy: SchedulePolicy, lateness: float) -> Decision:
        job.missed_at = job.missed_at or self.clock()
        minutes = int(lateness // 60)
        if policy.policy == "publish_now":
            if not job.approval_request_id:
                job.state_reason = "missed_schedule_no_approval"
                self.session.flush()
                return Decision(job.id, "skipped", "missed_schedule_no_approval", str(job.job_state))
            self._move(job, PublishState.READY, reason="missed_schedule_publish_now")
            enqueue = getattr(self._executor, "enqueue", None)
            if enqueue is not None:
                enqueue(job)
            self.audit.record(
                "publish.schedule.late_publish",
                resource_type="publish_job",
                resource_id=job.id,
                metadata={"late_minutes": minutes, "policy": policy.policy},
            )
            return Decision(job.id, "enqueued", "missed_schedule_publish_now", str(job.job_state))

        # Both `ask` and `skip` stop and wait for a human; they differ in what the GUI proposes first
        # (publish now vs reschedule), and nothing is published either way.
        reason = "missed_schedule" if policy.policy == "ask" else "missed_schedule_skipped"
        if _state(job) is not PublishState.REQUIRES_USER_ACTION:
            if can_transition(_state(job), PublishState.REQUIRES_USER_ACTION):
                self._move(job, PublishState.REQUIRES_USER_ACTION, reason=reason)
            else:
                job.state_reason = reason[:300]
        job.state_reason = reason[:300]
        self.session.flush()
        self.audit.record(
            "publish.schedule.missed",
            resource_type="publish_job",
            resource_id=job.id,
            outcome="missed",
            metadata={"late_minutes": minutes, "policy": policy.policy, "state": str(job.job_state)},
        )
        return Decision(job.id, "missed", reason, str(job.job_state))

    # ---------------------------------------------------------------- workflow
    def publish_missed_now(self, job: PublishJob, *, actor: str = "local-user") -> PublishJob:
        """The user answered `ask` with "publish it now"."""
        if job.missed_at is None:
            raise PublishingError(
                "NOT_A_MISSED_SCHEDULE",
                "This destination has not missed a schedule.",
                http_status=409,
                detail={"state": str(job.job_state)},
            )
        self._move(job, PublishState.READY, reason="user_published_missed_schedule")
        enqueue = getattr(self._executor, "enqueue", None)
        if enqueue is not None:
            enqueue(job)
        self.audit.record(
            "publish.schedule.publish_now",
            resource_type="publish_job",
            resource_id=job.id,
            actor=actor,
            metadata={"missed_at": job.missed_at.isoformat()},
        )
        return job

    def reschedule_missed(self, job: PublishJob, *, when: datetime, display_timezone: str | None = None) -> PublishJob:
        if job.missed_at is None:
            raise PublishingError(
                "NOT_A_MISSED_SCHEDULE", "This destination has not missed a schedule.", http_status=409
            )
        self._move(job, PublishState.SCHEDULED, reason="rescheduled_by_user")
        self.schedule(job, when=when, display_timezone=display_timezone)
        return job

    def _move(self, job: PublishJob, state: PublishState, *, reason: str) -> None:
        current = _state(job)
        if current is state:
            return
        if not can_transition(current, state):
            raise PublishingError(
                "ILLEGAL_STATE_TRANSITION",
                f"cannot move a destination from {current.value} to {state.value}",
                http_status=409,
                detail={"from": current.value, "to": state.value},
            )
        job.job_state = state
        job.state_reason = reason[:300]


def _state(job: PublishJob) -> PublishState:
    return job.job_state if isinstance(job.job_state, PublishState) else PublishState(str(job.job_state))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def next_wake_seconds(result: TickResult, *, default: float = 30.0) -> float:
    """How long the loop may sleep: short when something is due soon, longer when nothing is."""
    if result.count("future") == 0:
        return default
    return min(default, 10.0)


def seconds_until(instant: datetime | None, *, now: datetime | None = None) -> float | None:
    if instant is None:
        return None
    moment = (now or utcnow()).astimezone(UTC)
    target = _aware(instant)
    assert target is not None
    return (target - moment).total_seconds()


def late_boundary(policy: SchedulePolicy, *, now: datetime | None = None) -> datetime:
    """Anything scheduled before this instant is late under the current policy."""
    moment = (now or utcnow()).astimezone(UTC)
    return moment - timedelta(seconds=policy.grace_seconds)
