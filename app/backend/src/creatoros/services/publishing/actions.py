"""The four state-changing verbs a person can apply to one destination.

`publish_now`, `retry`, `cancel` and `reschedule` all live here rather than in the routes, because
each of them has a rule that must hold no matter which caller (GUI, agent tool, test) invokes it:

* **publish_now** runs the executor's preflight first and refuses with the readable reason instead of
  queueing a job that would immediately block itself. It never publishes inline: it enqueues.
* **retry** exists only for a destination that failed, and it re-checks the approval. A consumed or
  stale approval cannot be reused - consent is per payload - so a retry without a live approval goes
  back to `draft`, where the user can approve the current content again. That is a deliberate
  difference from "just queue it again".
* **cancel** refuses once the destination is known to be at the provider, because "cancelled" would
  then be a lie: the post exists. Those go through reconciliation instead.
* **reschedule** delegates to the scheduler, which owns time handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...db.enums import PublishState, can_transition
from ...db.models import PublishJob
from ..backup import AuditService
from .approvals import PublishApprovalService
from .errors import PublishingError
from .executor import PublishExecutor
from .history import PublishHistory
from .scheduler import PublishScheduler

#: States where "publish now" is a question worth asking.
PUBLISHABLE = (
    PublishState.APPROVED,
    PublishState.SCHEDULED,
    PublishState.READY,
    PublishState.FAILED,
    PublishState.REQUIRES_USER_ACTION,
)
#: States where a cancelled destination would be a false statement.
REMOTE_IN_PLAY = (
    PublishState.UPLOADING,
    PublishState.UPLOADED,
    PublishState.PROCESSING,
    PublishState.UNKNOWN_REMOTE_STATE,
    PublishState.NEEDS_RECONCILIATION,
    PublishState.PUBLISHED,
    PublishState.PARTIALLY_PUBLISHED,
)


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    action: str
    state: str
    queued: bool
    reason: str
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "state": self.state,
            "queued": self.queued,
            "reason": self.reason,
            "detail": dict(self.detail or {}),
        }


class PublishActions:
    def __init__(
        self,
        session: Session,
        *,
        executor: PublishExecutor | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self.session = session
        self.audit = audit or AuditService(session)
        self.executor = executor or PublishExecutor(session)
        self.approvals = PublishApprovalService(session)

    # ------------------------------------------------------------------ publish
    def publish_now(self, job: PublishJob, *, actor: str = "local-user") -> ActionOutcome:
        state = _state(job)
        if state not in PUBLISHABLE:
            raise PublishingError(
                "NOT_PUBLISHABLE_NOW",
                f"A destination in {state.value} is not something to queue right now.",
                http_status=409,
                detail={"state": state.value},
            )
        report = self.executor.preflight(job)
        if not report.ok:
            # preflight has already written the first reason onto the destination.
            first = report.issues[0]
            self.audit.record(
                "publish.publish_now",
                resource_type="publish_job",
                resource_id=job.id,
                actor=actor,
                outcome="denied",
                metadata={"code": first.code, "from": state.value},
            )
            return ActionOutcome(
                action="publish_now",
                state=str(job.job_state),
                queued=False,
                reason=first.code,
                detail={"issues": [issue.as_dict() for issue in report.issues]},
            )
        self._move(job, PublishState.READY, reason=f"queued_by_{actor}")
        key = self.executor.enqueue(job)
        self.audit.record(
            "publish.publish_now",
            resource_type="publish_job",
            resource_id=job.id,
            actor=actor,
            metadata={"queue_key": key, "from": state.value},
        )
        return ActionOutcome(
            action="publish_now",
            state=str(job.job_state),
            queued=True,
            reason="queued",
            detail={"queue_key": key},
        )

    # -------------------------------------------------------------------- retry
    def retry(self, job: PublishJob, *, actor: str = "local-user") -> ActionOutcome:
        state = _state(job)
        if state is not PublishState.FAILED and state not in {
            PublishState.BLOCKED,
            PublishState.REQUIRES_USER_ACTION,
        }:
            raise PublishingError(
                "NOT_RETRYABLE",
                "Only a destination that failed, is blocked, or needs a decision can be retried.",
                http_status=409,
                detail={"state": state.value},
            )
        try:
            self.approvals.verify(job)
        except PublishingError as error:
            # Consent is per payload and was spent by the failed attempt: go back to draft so the
            # current content can be approved again, and say why.
            self._move(job, PublishState.DRAFT, reason=f"retry_requires_approval:{error.code}")
            self.audit.record(
                "publish.retry.blocked",
                resource_type="publish_job",
                resource_id=job.id,
                actor=actor,
                outcome="denied",
                metadata={"code": error.code, "from": state.value},
            )
            return ActionOutcome(
                action="retry",
                state=str(job.job_state),
                queued=False,
                reason="approval_required",
                detail={"code": error.code, "message": error.message},
            )

        self._move(job, PublishState.READY, reason=f"retried_by_{actor}")
        key = self.executor.enqueue(job)
        self.audit.record(
            "publish.retry",
            resource_type="publish_job",
            resource_id=job.id,
            actor=actor,
            metadata={"queue_key": key, "from": state.value},
        )
        return ActionOutcome(
            action="retry", state=str(job.job_state), queued=True, reason="queued", detail={"queue_key": key}
        )

    # ------------------------------------------------------------------- cancel
    def cancel(self, job: PublishJob, *, reason: str | None = None, actor: str = "local-user") -> ActionOutcome:
        state = _state(job)
        if state in REMOTE_IN_PLAY:
            raise PublishingError(
                "REMOTE_STATE_IN_PLAY",
                "This destination may already exist at the provider, so it cannot be called cancelled. "
                "Reconcile it first, then decide.",
                http_status=409,
                detail={"state": state.value, "hint": "run a reconciliation check"},
            )
        if state is PublishState.CANCELLED:
            return ActionOutcome("cancel", str(job.job_state), False, "already_cancelled")
        self._move(job, PublishState.CANCELLED, reason=(reason or f"cancelled_by_{actor}")[:300])
        self.audit.record(
            "publish.cancel",
            resource_type="publish_job",
            resource_id=job.id,
            actor=actor,
            metadata={"from": state.value, "reason": reason},
        )
        return ActionOutcome("cancel", str(job.job_state), False, "cancelled")

    # --------------------------------------------------------------- reschedule
    def reschedule(
        self,
        job: PublishJob,
        *,
        when: datetime | None,
        display_timezone: str | None = None,
        actor: str = "local-user",
    ) -> ActionOutcome:
        scheduler = PublishScheduler(self.session, executor=self.executor)
        scheduler.schedule(job, when=when, display_timezone=display_timezone)
        target = PublishState.SCHEDULED if when is not None else None
        if target is not None and _state(job) is PublishState.APPROVED:
            self._move(job, PublishState.SCHEDULED, reason="scheduled_by_user")
        self.audit.record(
            "publish.reschedule",
            resource_type="publish_job",
            resource_id=job.id,
            actor=actor,
            metadata={"scheduled_at_utc": job.scheduled_at_utc.isoformat() if job.scheduled_at_utc else None},
        )
        return ActionOutcome(
            "reschedule",
            str(job.job_state),
            False,
            "scheduled" if job.scheduled_at_utc else "schedule_cleared",
        )

    # -------------------------------------------------------------------- helpers
    def describe(self, job: PublishJob) -> dict[str, Any]:
        """The action affordances for one destination, with the reason each is unavailable."""
        state = _state(job)
        history = PublishHistory(self.session)
        detail = history.job(job.id) or {}
        can_publish = state in PUBLISHABLE
        can_retry = state is PublishState.FAILED or state in {
            PublishState.BLOCKED,
            PublishState.REQUIRES_USER_ACTION,
        }
        can_cancel = state not in REMOTE_IN_PLAY and state is not PublishState.CANCELLED
        return {
            "state": state.value,
            "publish_now": {"enabled": can_publish, "reason": None if can_publish else _why_publish(state)},
            "retry": {"enabled": can_retry, "reason": None if can_retry else "only a failed or blocked destination can be retried"},
            "cancel": {
                "enabled": can_cancel,
                "reason": None
                if can_cancel
                else "the post may already exist at the provider; reconcile before cancelling",
            },
            "reconcile": {
                "enabled": bool(job.provider_media_id or job.provider_upload_session_id),
                "reason": None
                if (job.provider_media_id or job.provider_upload_session_id)
                else "no provider id yet, so there is nothing to look up",
            },
            "history": detail,
        }

    def _move(self, job: PublishJob, state: PublishState, *, reason: str) -> None:
        current = _state(job)
        if current is state:
            job.state_reason = reason[:300]
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


def _why_publish(state: PublishState) -> str:
    if state in REMOTE_IN_PLAY:
        return "this destination is already on its way to the provider"
    if state is PublishState.CANCELLED:
        return "a cancelled destination stays cancelled"
    if state in {PublishState.DRAFT, PublishState.VALIDATION_PENDING, PublishState.AWAITING_APPROVAL}:
        return "it needs an approval before it can be published"
    return f"a destination in {state.value} cannot be published"
