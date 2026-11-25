"""Durable SQLite-backed job queue (INV-9).

Design notes
------------
* Claiming is a single atomic UPDATE ... RETURNING, so two workers can never
  claim the same row, and a crashed worker's job becomes visible again when its
  lease expires (visibility timeout) rather than being lost.
* Retries use exponential backoff with jitter and a `run_after` timestamp, so a
  failing provider gets slower and slower without blocking the queue.
* After `max_attempts` the job lands in `dead_letter` with its last error intact,
  because silently dropping failed work is indistinguishable from success.
* `idempotency_key` (job_type + key) prevents the same logical job from being
  enqueued twice while an equivalent one is still open.
"""
from __future__ import annotations

import random
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..core.ids import new_ulid
from ..db.enums import QueueState
from ..db.models import QueueJob

DEFAULT_LEASE_SECONDS = 60
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 3600


def utcnow() -> datetime:
    return datetime.now(UTC)


def worker_id(prefix: str = "worker") -> str:
    return f"{prefix}-{socket.gethostname()[:20]}-{uuid.uuid4().hex[:8]}"


def backoff_delay(attempts: int, *, base: int = BACKOFF_BASE_SECONDS, cap: int = BACKOFF_CAP_SECONDS) -> float:
    """Exponential backoff with full jitter (attempts is 1-based after failure)."""
    raw = min(cap, base * (2 ** max(0, attempts - 1)))
    return random.uniform(raw * 0.5, raw)


@dataclass(slots=True)
class EnqueueResult:
    job_id: str
    created: bool
    reason: str | None = None


class QueueService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- enqueue
    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        queue: str = "default",
        priority: int = 50,
        delay_seconds: float = 0.0,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
    ) -> EnqueueResult:
        if not job_type or not job_type.strip():
            raise ValueError("job_type is required")

        if idempotency_key:
            existing = self.session.scalar(
                sa.select(QueueJob).where(
                    QueueJob.job_type == job_type,
                    QueueJob.idempotency_key == idempotency_key,
                    QueueJob.state.in_(
                        [QueueState.QUEUED, QueueState.CLAIMED, QueueState.RUNNING]
                    ),
                )
            )
            if existing is not None:
                return EnqueueResult(existing.id, created=False, reason="duplicate_open_job")

        job = QueueJob(
            id=new_ulid(),
            queue=queue,
            job_type=job_type,
            payload_json=payload or {},
            priority=priority,
            state=QueueState.QUEUED,
            attempts=0,
            max_attempts=max_attempts,
            run_after=utcnow() + timedelta(seconds=delay_seconds),
            idempotency_key=idempotency_key,
        )
        self.session.add(job)
        self.session.flush()
        return EnqueueResult(job.id, created=True)

    # ------------------------------------------------------------------ claim
    def claim(self, *, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS, queue: str = "default") -> QueueJob | None:
        now = utcnow()
        claimable = sa.select(QueueJob.id).where(
            QueueJob.queue == queue,
            QueueJob.run_after <= now,
            QueueJob.cancel_requested.is_(False),
            sa.or_(
                QueueJob.state == QueueState.QUEUED,
                sa.and_(
                    QueueJob.state.in_([QueueState.CLAIMED, QueueState.RUNNING]),
                    QueueJob.lease_expires_at.is_not(None),
                    QueueJob.lease_expires_at < now,
                ),
            ),
        ).order_by(QueueJob.priority.asc(), QueueJob.run_after.asc()).limit(1).scalar_subquery()

        statement = (
            sa.update(QueueJob)
            .where(QueueJob.id == claimable, QueueJob.cancel_requested.is_(False))
            .values(
                state=QueueState.CLAIMED,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                attempts=QueueJob.attempts + 1,
                updated_at=now,
            )
            .returning(QueueJob.id)
        )
        claimed_id = self.session.scalar(statement)
        self.session.commit()
        if claimed_id is None:
            return None
        return self.session.get(QueueJob, claimed_id)

    # -------------------------------------------------------------- lifecycle
    def mark_running(self, job_id: str) -> None:
        self._update(job_id, state=QueueState.RUNNING, heartbeat_at=utcnow())

    def heartbeat(self, job_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._update(job_id, heartbeat_at=utcnow(), lease_expires_at=utcnow() + timedelta(seconds=lease_seconds))

    def complete(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        self._update(job_id, state=QueueState.SUCCEEDED, result_json=result or {}, finished_at=utcnow())

    def fail(self, job_id: str, error: str, *, retryable: bool = True) -> QueueState:
        job = self.session.get(QueueJob, job_id)
        if job is None:
            raise KeyError(job_id)
        if job.cancel_requested:
            job.state = QueueState.CANCELLED
            job.finished_at = utcnow()
            self.session.commit()
            return job.state

        if not retryable or job.attempts >= job.max_attempts:
            job.state = QueueState.DEAD_LETTER
            job.last_error = error[:4000]
            job.finished_at = utcnow()
            self.session.commit()
            return job.state

        delay = backoff_delay(job.attempts)
        job.state = QueueState.QUEUED
        job.last_error = error[:4000]
        job.run_after = utcnow() + timedelta(seconds=delay)
        job.lease_owner = None
        job.lease_expires_at = None
        self.session.commit()
        return job.state

    def cancel(self, job_id: str) -> QueueState:
        job = self.session.get(QueueJob, job_id)
        if job is None:
            raise KeyError(job_id)
        job.cancel_requested = True
        if job.state in (QueueState.QUEUED,):
            job.state = QueueState.CANCELLED
            job.finished_at = utcnow()
        self.session.commit()
        return job.state

    # ------------------------------------------------------------------ stats
    def stats(self, queue: str | None = None) -> dict[str, int]:
        stmt = sa.select(QueueJob.state, sa.func.count()).group_by(QueueJob.state)
        if queue:
            stmt = stmt.where(QueueJob.queue == queue)
        counts = {state.value: 0 for state in QueueState}
        for state, count in self.session.execute(stmt):
            counts[state.value if hasattr(state, "value") else str(state)] = count
        counts["total"] = sum(counts[state.value] for state in QueueState)
        return counts

    def due_count(self, queue: str = "default") -> int:
        return self.session.scalar(
            sa.select(sa.func.count()).select_from(QueueJob).where(
                QueueJob.queue == queue,
                QueueJob.state == QueueState.QUEUED,
                QueueJob.run_after <= utcnow(),
            )
        ) or 0

    def recent(self, limit: int = 50, state: QueueState | None = None) -> list[QueueJob]:
        stmt = sa.select(QueueJob).order_by(QueueJob.created_at.desc()).limit(min(limit, 200))
        if state is not None:
            stmt = stmt.where(QueueJob.state == state)
        return list(self.session.scalars(stmt))

    def _update(self, job_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        self.session.execute(sa.update(QueueJob).where(QueueJob.id == job_id).values(**values))
        self.session.commit()


JobHandler = Callable[[Session, QueueJob], dict[str, Any] | None]


class QueueWorker:
    """Single-process worker loop. Concurrency is controlled by the governor."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        handlers: dict[str, JobHandler],
        *,
        owner: str | None = None,
        queue: str = "default",
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.handlers = handlers
        self.owner = owner or worker_id()
        self.queue = queue
        self.lease_seconds = lease_seconds
        self.on_error = on_error
        self.processed = 0
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def run_once(self) -> bool:
        """Claim and execute at most one job. Returns True if a job ran."""
        session = self.session_factory()
        try:
            service = QueueService(session)
            job = service.claim(owner=self.owner, lease_seconds=self.lease_seconds, queue=self.queue)
            if job is None:
                return False
            job_id, job_type, payload = job.id, job.job_type, dict(job.payload_json or {})
            service.mark_running(job_id)
        finally:
            session.close()

        handler = self.handlers.get(job_type)
        session = self.session_factory()
        try:
            service = QueueService(session)
            if handler is None:
                service.fail(job_id, f"no handler registered for job_type={job_type!r}", retryable=False)
                return True
            try:
                result = handler(session, _JobView(job_id, job_type, payload))  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - handler failures must not kill the loop
                retryable = not isinstance(exc, ValueError)
                service.fail(job_id, f"{type(exc).__name__}: {exc}", retryable=retryable)
                if self.on_error:
                    self.on_error(job_id, exc)
                return True
            service.complete(job_id, result)
            self.processed += 1
            return True
        finally:
            session.close()

    def drain(self, max_jobs: int = 100) -> int:
        count = 0
        while count < max_jobs and not self.stopped:
            if not self.run_once():
                break
            count += 1
        return count


class _JobView:
    """Read-only view passed to handlers so they cannot mutate the ORM row."""

    __slots__ = ("id", "job_type", "payload")

    def __init__(self, job_id: str, job_type: str, payload: dict[str, Any]) -> None:
        self.id = job_id
        self.job_type = job_type
        self.payload = payload

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)
