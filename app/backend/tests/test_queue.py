"""Queue durability tests - the highest-risk component in Phase 2.

These tests assert the properties that keep a publish job from being lost or
executed twice: atomic claim, lease recovery after a crash, backoff, dead-letter
and idempotent enqueue.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from creatoros.db.enums import QueueState
from creatoros.db.models import QueueJob
from creatoros.services.queue import QueueService, QueueWorker, backoff_delay, worker_id


def test_enqueue_creates_job(session):
    result = QueueService(session).enqueue("noop", {"a": 1})
    assert result.created is True
    job = session.get(QueueJob, result.job_id)
    assert job is not None
    assert job.state == QueueState.QUEUED
    assert job.payload_json == {"a": 1}


def test_enqueue_requires_job_type(session):
    with pytest.raises(ValueError):
        QueueService(session).enqueue("  ")


def test_idempotent_enqueue_does_not_duplicate(session):
    service = QueueService(session)
    first = service.enqueue("noop", {"x": 1}, idempotency_key="abc")
    second = service.enqueue("noop", {"x": 2}, idempotency_key="abc")
    assert first.job_id == second.job_id
    assert second.created is False
    assert second.reason == "duplicate_open_job"
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 1


def test_idempotency_key_reusable_after_completion(session):
    service = QueueService(session)
    first = service.enqueue("noop", {}, idempotency_key="key-1")
    service.claim(owner=worker_id(), lease_seconds=30)
    service.complete(first.job_id)
    second = service.enqueue("noop", {}, idempotency_key="key-1")
    assert second.created is True
    assert second.job_id != first.job_id


def test_claim_is_atomic_across_workers(session):
    from creatoros.db.session import session_scope

    QueueService(session).enqueue("noop", {"n": 1})
    session.commit()
    with session_scope() as other:
        first = QueueService(session).claim(owner="w1", lease_seconds=30)
        second = QueueService(other).claim(owner="w2", lease_seconds=30)
    assert first is not None
    assert second is None, "two workers claimed the same job"


def test_claim_respects_priority_then_schedule(session):
    service = QueueService(session)
    low = service.enqueue("noop", {}, priority=90)
    high = service.enqueue("noop", {}, priority=10)
    claimed = service.claim(owner="w", lease_seconds=30)
    assert claimed is not None and claimed.id == high.job_id
    assert service.claim(owner="w", lease_seconds=30).id == low.job_id


def test_delayed_job_is_not_claimable_early(session):
    service = QueueService(session)
    service.enqueue("noop", {}, delay_seconds=60)
    assert service.claim(owner="w", lease_seconds=30) is None


def test_expired_lease_is_reclaimed_after_crash(session):
    service = QueueService(session)
    job = service.enqueue("noop", {})
    claimed = service.claim(owner="dead-worker", lease_seconds=1)
    assert claimed is not None
    # Simulate the worker dying: lease expires, state stays CLAIMED/RUNNING.
    session.execute(
        sa.update(QueueJob)
        .where(QueueJob.id == job.job_id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5), state=QueueState.RUNNING)
    )
    session.commit()
    reclaimed = service.claim(owner="live-worker", lease_seconds=30)
    assert reclaimed is not None and reclaimed.id == job.job_id
    assert reclaimed.lease_owner == "live-worker"
    assert reclaimed.attempts == 2


def test_failure_schedules_backoff_and_requeues(session):
    service = QueueService(session)
    job = service.enqueue("noop", {}, max_attempts=3)
    service.claim(owner="w", lease_seconds=30)
    state = service.fail(job.job_id, "boom")
    session.expire_all()
    row = session.get(QueueJob, job.job_id)
    assert state == QueueState.QUEUED
    assert row.last_error == "boom"
    assert row.run_after > datetime.now(UTC)
    assert row.lease_owner is None


def test_backoff_delays_the_next_attempt(session):
    """After a failure the job must not be claimable until its backoff elapses."""
    service = QueueService(session)
    job = service.enqueue("noop", {}, max_attempts=3)
    assert service.claim(owner="w", lease_seconds=30) is not None
    service.fail(job.job_id, "boom")
    assert service.claim(owner="w", lease_seconds=30) is None, "job retried without waiting for backoff"


def test_dead_letter_after_max_attempts(session):
    service = QueueService(session)
    job = service.enqueue("noop", {}, max_attempts=2)
    for _ in range(2):
        assert service.claim(owner="w", lease_seconds=30) is not None
        service.fail(job.job_id, "still broken")
        # simulate waiting out the backoff window
        session.execute(
            sa.update(QueueJob)
            .where(QueueJob.id == job.job_id)
            .values(run_after=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()
    session.expire_all()
    row = session.get(QueueJob, job.job_id)
    assert row.state == QueueState.DEAD_LETTER
    assert row.last_error == "still broken"
    assert row.attempts == 2


def test_non_retryable_failure_goes_straight_to_dead_letter(session):
    service = QueueService(session)
    job = service.enqueue("noop", {}, max_attempts=5)
    service.claim(owner="w", lease_seconds=30)
    service.fail(job.job_id, "handler missing", retryable=False)
    session.expire_all()
    assert session.get(QueueJob, job.job_id).state == QueueState.DEAD_LETTER


def test_cancel_queued_job(session):
    service = QueueService(session)
    job = service.enqueue("noop", {})
    assert service.cancel(job.job_id) == QueueState.CANCELLED


def test_worker_runs_handler_and_records_result(session):
    from creatoros.db.session import get_session_factory

    service = QueueService(session)
    job = service.enqueue("noop", {"hello": "world"})
    session.commit()
    seen: list[str] = []

    def handler(_session, view):
        seen.append(view.payload["hello"])
        return {"ok": True}

    worker = QueueWorker(get_session_factory(), {"noop": handler}, owner="test-worker")
    assert worker.run_once() is True
    session.expire_all()
    row = session.get(QueueJob, job.job_id)
    assert row.state == QueueState.SUCCEEDED
    assert row.result_json == {"ok": True}
    assert seen == ["world"]


def test_worker_fails_job_when_handler_missing(session):
    from creatoros.db.session import get_session_factory

    service = QueueService(session)
    job = service.enqueue("unregistered", {})
    session.commit()
    worker = QueueWorker(get_session_factory(), {}, owner="test-worker")
    assert worker.run_once() is True
    session.expire_all()
    assert session.get(QueueJob, job.job_id).state == QueueState.DEAD_LETTER


def test_worker_isolates_handler_exceptions(session):
    from creatoros.db.session import get_session_factory

    service = QueueService(session)
    job = service.enqueue("explode", {}, max_attempts=1)
    session.commit()

    def handler(_session, _view):
        raise RuntimeError("provider timeout")

    worker = QueueWorker(get_session_factory(), {"explode": handler}, owner="w")
    assert worker.run_once() is True
    session.expire_all()
    row = session.get(QueueJob, job.job_id)
    assert row.state == QueueState.DEAD_LETTER
    assert "provider timeout" in (row.last_error or "")


def test_drain_processes_queue_in_priority_order(session):
    from creatoros.db.session import get_session_factory

    service = QueueService(session)
    order: list[str] = []
    for name, prio in (("a", 80), ("b", 10), ("c", 50)):
        service.enqueue("record", {"name": name}, priority=prio)
    session.commit()

    def handler(_session, view):
        order.append(view.payload["name"])
        return {}

    worker = QueueWorker(get_session_factory(), {"record": handler}, owner="w")
    assert worker.drain(max_jobs=10) == 3
    assert order == ["b", "c", "a"]


def test_stats_and_due_count(session):
    service = QueueService(session)
    service.enqueue("noop", {})
    service.enqueue("noop", {}, delay_seconds=120)
    stats = service.stats()
    assert stats["queued"] == 2
    assert stats["total"] == 2
    assert service.due_count() == 1


def test_backoff_is_bounded_and_jittered():
    values = {round(backoff_delay(1), 3) for _ in range(20)}
    assert len(values) > 1, "backoff should be jittered"
    assert all(0 <= v <= 3600 for v in [backoff_delay(30) for _ in range(20)])


def test_job_survives_new_session(session):
    """Durability: a fresh session (simulating a restart) still sees the job."""
    from creatoros.db.session import session_scope

    job = QueueService(session).enqueue("noop", {"durable": True})
    service_session_closed = job.job_id
    session.commit()
    with session_scope() as fresh:
        row = QueueService(fresh).claim(owner="after-restart", lease_seconds=30)
    assert row is not None and row.id == service_session_closed
    assert row.payload_json["durable"] is True


def test_ulids_are_strictly_increasing_even_within_one_millisecond():
    """Ordering by (created_at, id) must match creation order.

    Rows written in the same millisecond would otherwise come back shuffled, which
    made execution history and benchmark "latest" lookups nondeterministic.
    """
    from itertools import pairwise

    from creatoros.core.ids import new_ulid, ulid_timestamp

    ids = [new_ulid() for _ in range(2_000)]
    assert all(len(value) == 26 for value in ids)
    assert all(later > earlier for earlier, later in pairwise(ids))
    assert all(0 < ulid_timestamp(value) <= 2**48 - 1 for value in ids)
