"""Reconciliation: what actually happened at the provider.

The executor sends a request; sometimes no answer comes back. At that moment the honest state is
`unknown_remote_state`, and the only way out is to *look* at the provider. This module looks, writes
down what it saw, and then moves the destination according to a rule rather than a guess:

* the object exists -> the destination is published, with the permalink the provider reported;
* the provider says there is no such object -> the destination is **not** resubmitted. It goes to
  `requires_user_action` so a person decides to retry, because a resubmit is a decision that spends
  an approval and can double-post;
* the provider cannot say yet (still processing, or an answer we do not recognise) -> the destination
  stays `unknown_remote_state` and the case stays open.

Every check is a row in `publish_reconciliation`, including the ones that decided nothing, so "we
looked and it was still processing" is visible later instead of being a silence.

A person can also resolve a case by hand (`resolve_manually`): the provider API may be gone, the post
may have been deleted in the app. That row records who said so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import PublishState, ReconciliationDecision, can_transition
from ...db.models import PublishAttempt, PublishJob, PublishReconciliation, SocialAccount
from ...integrations.social.errors import SocialError
from ...integrations.social.publishing.publishers.base import PublishContext, RemoteStatus
from ..backup import AuditService
from ..social.tokens import TokenService
from .errors import PublishingError

#: Provider state strings that mean "this object exists at the provider".
PUBLISHED_STATES = frozenset(
    {
        "published",
        "ready",
        "finished",
        "inbox",  # TikTok inbox upload: the video exists, the creator still has to post it
        "sent_to_user_inbox",
        "publish_complete",
    }
)
#: Provider state strings that mean "nothing exists, it is safe to say so".
ABSENT_STATES = frozenset({"missing", "not_found", "deleted", "expired", "error", "failed"})
#: Provider state strings that mean "ask again later".
PENDING_STATES = frozenset(
    {"processing", "uploading", "processing_upload", "in_progress", "pending", "unknown", ""}
)


@dataclass(frozen=True, slots=True)
class Observation:
    """One look at the provider, in the terms the case and the journal use."""

    provider_state: str
    decision: ReconciliationDecision
    detail: str | None = None
    permalink: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_state": self.provider_state,
            "decision": self.decision.value,
            "detail": self.detail,
            "permalink": self.permalink,
            "evidence": dict(self.evidence),
        }


def observe(status: RemoteStatus) -> Observation:
    """Turn a provider answer into a decision. Deterministic, and the only place states are read."""
    state = str(status.state or "").strip().lower()
    if state in PUBLISHED_STATES:
        decision = ReconciliationDecision.REMOTE_EXISTS
    elif state in ABSENT_STATES:
        decision = ReconciliationDecision.NO_REMOTE_OBJECT
    else:
        decision = ReconciliationDecision.UNDETERMINED
    return Observation(
        provider_state=state or "unknown",
        decision=decision,
        detail=status.detail,
        permalink=status.permalink,
        evidence={
            "provider_object_id": status.provider_object_id,
            "finished": status.finished,
            "failed": status.failed,
        },
    )


class ReconciliationService:
    def __init__(
        self,
        session: Session,
        *,
        probe: Callable[[PublishJob], Any] | None = None,
        audit: AuditService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.audit = audit or AuditService(session)
        self.clock = clock or utcnow
        self._probe = probe

    # ------------------------------------------------------------------ reading
    def cases(self, job: PublishJob) -> list[PublishReconciliation]:
        return list(
            self.session.scalars(
                sa.select(PublishReconciliation)
                .where(PublishReconciliation.job_id == job.id)
                .order_by(PublishReconciliation.checked_at.desc())
            )
        )

    def open_cases(self, *, limit: int = 50) -> list[PublishReconciliation]:
        """The newest case per destination, when that case decided nothing.

        An older undetermined row must not keep a destination in the list after a later check (or a
        person) resolved it, so only the latest row per job counts.
        """
        rows = self.session.scalars(
            sa.select(PublishReconciliation).order_by(PublishReconciliation.id.desc()).limit(limit * 5)
        )
        seen: set[str] = set()
        open_cases: list[PublishReconciliation] = []
        for case in rows:
            if case.job_id in seen:
                continue
            seen.add(case.job_id)
            if case.resolved_by is not None:
                continue
            job = self.session.get(PublishJob, case.job_id)
            if job is not None and _state(job) in {
                PublishState.UNKNOWN_REMOTE_STATE,
                PublishState.NEEDS_RECONCILIATION,
            }:
                open_cases.append(case)
            if len(open_cases) >= limit:
                break
        return open_cases

    def unresolved_jobs(self, *, limit: int = 50) -> list[PublishJob]:
        return list(
            self.session.scalars(
                sa.select(PublishJob)
                .where(
                    PublishJob.job_state.in_(
                        [PublishState.UNKNOWN_REMOTE_STATE, PublishState.NEEDS_RECONCILIATION]
                    )
                )
                .order_by(PublishJob.updated_at)
                .limit(limit)
            )
        )

    # ----------------------------------------------------------------- checking
    def _require_object(self, job: PublishJob) -> str:
        provider_object_id = job.provider_media_id or job.provider_upload_session_id
        if not provider_object_id:
            raise PublishingError(
                "NOTHING_TO_RECONCILE",
                "This destination never got a provider id, so there is nothing to look up. "
                "Its media was not accepted; fix the cause and publish again.",
                http_status=409,
                detail={"state": str(job.job_state)},
            )
        return provider_object_id

    async def check(self, job: PublishJob) -> Observation:
        """Ask the provider about this destination and write down the answer."""
        provider_object_id = self._require_object(job)
        try:
            status = await self._status(job, provider_object_id)
        except SocialError as error:
            observation = Observation(
                provider_state="unreachable",
                decision=ReconciliationDecision.UNDETERMINED,
                detail=f"{error.code}: {error.message}"[:300],
                evidence={"error_code": error.code},
            )
        except PublishingError:
            raise
        except Exception as error:  # noqa: BLE001 - a probe failure is a fact to record, not a crash
            observation = Observation(
                provider_state="unreachable",
                decision=ReconciliationDecision.UNDETERMINED,
                detail=f"{type(error).__name__}: {error}"[:300],
                evidence={"error_type": type(error).__name__},
            )
        else:
            observation = observe(status)

        self._record(job, observation, resolved_by=None)
        job.remote_checked_at = self.clock()
        job.reconcile_attempts = int(job.reconcile_attempts or 0) + 1
        self._apply(job, observation, actor="system")
        return observation

    async def check_all(self, *, limit: int = 20) -> dict[str, Any]:
        checked: list[dict[str, Any]] = []
        for job in self.unresolved_jobs(limit=limit):
            try:
                observation = await self.check(job)
            except PublishingError as error:
                checked.append({"job_id": job.id, "error": error.code})
                continue
            checked.append({"job_id": job.id, **observation.as_dict(), "state": str(job.job_state)})
        return {"checked": checked}

    async def _status(self, job: PublishJob, provider_object_id: str) -> RemoteStatus:
        if self._probe is not None:
            result = self._probe(job)
            return await result if hasattr(result, "__await__") else result
        return await self._probe_live(job, provider_object_id)

    async def _probe_live(self, job: PublishJob, provider_object_id: str) -> RemoteStatus:
        """The real lookup, through the same publisher the executor used."""
        from .executor import PublishExecutor

        account = self.session.get(SocialAccount, job.account_id)
        if account is None:
            raise PublishingError("ACCOUNT_MISSING", "The destination's account no longer exists.", http_status=409)
        executor = PublishExecutor(self.session)
        token = await TokenService(self.session, executor.vault).access_token(account)
        ctx = PublishContext(
            job_id=job.id,
            account_provider_id=account.provider_account_id,
            access_token=token,
            mode=job.publish_mode,
            caption=job.caption or "",
            title=job.title,
        )
        publisher = executor._publisher(job, job.publish_mode)
        return await publisher.check_status(ctx, provider_object_id)

    # --------------------------------------------------------------- resolution
    def resolve_manually(
        self,
        job: PublishJob,
        *,
        decision: ReconciliationDecision | str,
        note: str,
        actor: str = "local-user",
        permalink: str | None = None,
    ) -> PublishReconciliation:
        """A person says what is true at the provider, with a note explaining how they know."""
        resolved = ReconciliationDecision(str(decision))
        observation = Observation(
            provider_state="manual",
            decision=resolved,
            detail=note[:300],
            permalink=permalink,
            evidence={"manual": True, "note": note[:500], "actor": actor},
        )
        case = self._record(job, observation, resolved_by=actor)
        self._apply(job, observation, actor=actor)
        return case

    def _record(
        self, job: PublishJob, observation: Observation, *, resolved_by: str | None
    ) -> PublishReconciliation:
        latest = self.session.scalar(
            sa.select(PublishAttempt.id)
            .where(PublishAttempt.job_id == job.id)
            .order_by(PublishAttempt.attempt_no.desc())
            .limit(1)
        )
        case = PublishReconciliation(
            job_id=job.id,
            attempt_id=latest,
            checked_at=self.clock(),
            provider_state=observation.provider_state[:80],
            decision=observation.decision,
            evidence_json={
                **observation.evidence,
                "detail": observation.detail,
                "permalink": observation.permalink,
            },
            resolved_by=resolved_by,
        )
        self.session.add(case)
        self.session.flush()
        return case

    def _apply(self, job: PublishJob, observation: Observation, *, actor: str) -> None:
        current = _state(job)
        if observation.decision is ReconciliationDecision.REMOTE_EXISTS:
            self._move(job, PublishState.PUBLISHED, reason="reconciled_remote_exists")
            if observation.permalink:
                job.provider_permalink = observation.permalink
            self.audit.record(
                "publish.reconcile.published",
                resource_type="publish_job",
                resource_id=job.id,
                actor=actor,
                metadata={"provider_state": observation.provider_state, "permalink": observation.permalink},
            )
            return

        if observation.decision is ReconciliationDecision.NO_REMOTE_OBJECT:
            # Deliberately not a resubmit: a retry re-spends an approval and can double-post.
            self._move(job, PublishState.REQUIRES_USER_ACTION, reason="reconciled_remote_absent")
            self.audit.record(
                "publish.reconcile.absent",
                resource_type="publish_job",
                resource_id=job.id,
                actor=actor,
                metadata={"provider_state": observation.provider_state, "from": current.value},
            )
            return

        # Undetermined: the state is already unknown, and saying so again is the whole answer.
        if current is not PublishState.UNKNOWN_REMOTE_STATE and can_transition(
            current, PublishState.UNKNOWN_REMOTE_STATE
        ):
            self._move(job, PublishState.UNKNOWN_REMOTE_STATE, reason="reconciled_still_unknown")
        else:
            job.state_reason = f"reconciled_still_unknown:{observation.provider_state}"[:300]
        self.audit.record(
            "publish.reconcile.undetermined",
            resource_type="publish_job",
            resource_id=job.id,
            actor=actor,
            metadata={"provider_state": observation.provider_state, "detail": observation.detail},
        )

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
