"""Scheduler tests: due, future, missed, restart and timezone behaviour.

The important case is the missed one: an application that was closed when a post was due must not
publish silently, and must not lose the fact that it was late. Everything else in this file is about
the ticks around it being idempotent, which is what makes a restart safe.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from publishing_fixtures import bind_token, make_account, make_asset, make_composer
from social_fixtures import InMemoryVault

from creatoros.db.enums import PublishState
from creatoros.db.models import QueueJob
from creatoros.services.publishing.approvals import PublishApprovalService
from creatoros.services.publishing.errors import PublishingError
from creatoros.services.publishing.executor import JOB_TYPE, PublishExecutor
from creatoros.services.publishing.scheduler import (
    PublishScheduler,
    SchedulePolicy,
    late_boundary,
    next_wake_seconds,
    seconds_until,
)

VAULT = InMemoryVault()
NOW = datetime(2026, 11, 24, 9, 0, 0, tzinfo=UTC)


def scheduled_destination(session, *, when: datetime | None, approved: bool = True, tz="Europe/Paris"):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform="facebook")
    bind_token(session, account, VAULT)
    project = composer.create_intent(title="Morning routine", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id,
        platform="facebook",
        account_id=account.id,
        caption="Morning routine",
        scheduled_at_utc=when,
        display_timezone=tz,
    )
    job = destination.job
    if approved:
        approvals = PublishApprovalService(session)
        approvals.request(job)
        approvals.decide(job, approve=True, decided_by="local-user")
        if when is not None:
            job.job_state = PublishState.SCHEDULED
            session.flush()
    return composer, destination, job


def scheduler_for(session, *, executor=None) -> PublishScheduler:
    if executor is None:
        executor = PublishExecutor(session, vault=VAULT, publishers={})
    return PublishScheduler(session, clock=lambda: NOW, executor=executor)


def test_policy_defaults_to_ask_and_persists(session):
    scheduler = scheduler_for(session)
    assert scheduler.load_policy() == SchedulePolicy(policy="ask", grace_seconds=120)

    scheduler.set_policy(policy="publish_now", grace_seconds=300)
    assert scheduler.load_policy().policy == "publish_now"
    assert scheduler.load_policy().grace_seconds == 300

    with pytest.raises(PublishingError) as raised:
        scheduler.set_policy(policy="whenever")
    assert raised.value.code == "INVALID_LATE_POLICY"


def test_a_future_destination_is_left_alone(session):
    _, _, job = scheduled_destination(session, when=NOW + timedelta(hours=2))
    result = scheduler_for(session).tick(now=NOW)
    assert result.count("future") == 1
    assert result.count("enqueued") == 0
    assert job.job_state is PublishState.SCHEDULED
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0


def test_a_due_destination_is_queued_within_the_grace_window(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(seconds=30))
    result = scheduler_for(session).tick(now=NOW, grace_seconds=120)
    assert result.count("enqueued") == 1
    assert job.job_state is PublishState.READY
    queued = session.scalars(sa.select(QueueJob)).all()
    assert len(queued) == 1
    assert queued[0].job_type == JOB_TYPE
    assert queued[0].idempotency_key == f"publish:{job.id}"


def test_ticking_twice_queues_once(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(seconds=10))
    scheduler = scheduler_for(session)
    scheduler.tick(now=NOW)
    scheduler.tick(now=NOW)
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 1
    assert job.job_state is PublishState.READY


def test_a_missed_schedule_asks_instead_of_publishing(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(hours=14))
    result = scheduler_for(session).tick(now=NOW)
    assert result.count("missed") == 1
    assert job.job_state is PublishState.REQUIRES_USER_ACTION
    assert job.state_reason == "missed_schedule"
    assert job.missed_at is not None
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0, "nothing may be queued"


def test_publish_now_policy_queues_the_late_destination(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(hours=2))
    scheduler = scheduler_for(session)
    scheduler.set_policy(policy="publish_now")
    result = scheduler.tick(now=NOW)
    assert result.count("enqueued") == 1
    assert job.job_state is PublishState.READY
    assert job.missed_at is not None


def test_skip_policy_stops_without_queueing(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(hours=2))
    scheduler = scheduler_for(session)
    scheduler.set_policy(policy="skip")
    result = scheduler.tick(now=NOW)
    assert result.count("missed") == 1
    assert job.job_state is PublishState.REQUIRES_USER_ACTION
    assert job.state_reason == "missed_schedule_skipped"
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0


def test_a_missed_schedule_without_an_approval_is_not_queued_even_under_publish_now(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(hours=3), approved=False)
    job.job_state = PublishState.SCHEDULED
    session.flush()
    scheduler = scheduler_for(session)
    scheduler.set_policy(policy="publish_now")
    result = scheduler.tick(now=NOW)
    assert result.count("skipped") == 1
    assert job.state_reason == "missed_schedule_no_approval"
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0


def test_a_due_destination_without_an_approval_is_not_queued(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(seconds=5), approved=False)
    job.job_state = PublishState.SCHEDULED
    session.flush()
    result = scheduler_for(session).tick(now=NOW)
    assert result.count("skipped") == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0


def test_a_destination_without_a_schedule_is_never_examined(session):
    _, _, job = scheduled_destination(session, when=None)
    result = scheduler_for(session).tick(now=NOW)
    assert result.scanned == 0
    assert result.decisions == []
    assert job.job_state is not PublishState.READY


def test_answering_ask_with_publish_now_queues_it(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(hours=5))
    scheduler = scheduler_for(session)
    scheduler.tick(now=NOW)
    assert job.job_state is PublishState.REQUIRES_USER_ACTION

    scheduler.publish_missed_now(job)
    assert job.job_state is PublishState.READY
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 1


def test_answering_ask_with_a_new_time_reschedules_it(session):
    _, _, job = scheduled_destination(session, when=NOW - timedelta(hours=5))
    scheduler = scheduler_for(session)
    scheduler.tick(now=NOW)

    later = NOW + timedelta(hours=3)
    scheduler.reschedule_missed(job, when=later, display_timezone="Europe/Paris")
    assert job.job_state is PublishState.SCHEDULED
    assert job.scheduled_at_utc == later
    assert job.missed_at is None
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0


def test_publishing_or_rescheduling_a_destination_that_was_not_missed_is_refused(session):
    _, _, job = scheduled_destination(session, when=NOW + timedelta(hours=1))
    scheduler = scheduler_for(session)
    with pytest.raises(PublishingError) as raised:
        scheduler.publish_missed_now(job)
    assert raised.value.code == "NOT_A_MISSED_SCHEDULE"
    with pytest.raises(PublishingError):
        scheduler.reschedule_missed(job, when=NOW + timedelta(hours=2))


def test_a_timezone_change_keeps_the_wall_clock_the_user_chose(session):
    # 09:00 in Paris is 08:00 UTC in November.
    when = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)
    _, _, job = scheduled_destination(session, when=when, tz="Europe/Paris")
    scheduler = scheduler_for(session)

    scheduler.reschedule_local(job, display_timezone="America/New_York")
    # 09:00 New York in November is 14:00 UTC: the clock time survived, the instant did not.
    assert job.display_timezone == "America/New_York"
    assert job.scheduled_at_utc.hour == 14
    assert job.scheduled_at_utc.date() == when.date()


def test_clearing_a_schedule_returns_the_destination_to_ready(session):
    _, _, job = scheduled_destination(session, when=NOW + timedelta(hours=1))
    scheduler = scheduler_for(session)
    scheduler.schedule(job, when=None)
    assert job.scheduled_at_utc is None
    assert job.job_state is PublishState.READY


def test_a_naive_timestamp_is_refused(session):
    _, _, job = scheduled_destination(session, when=NOW + timedelta(hours=1))
    scheduler = scheduler_for(session)
    with pytest.raises(PublishingError) as raised:
        scheduler.schedule(job, when=datetime(2026, 11, 25, 9, 0))
    assert raised.value.code == "INVALID_SCHEDULE"


def test_an_unknown_timezone_is_refused(session):
    _, _, job = scheduled_destination(session, when=NOW + timedelta(hours=1))
    scheduler = scheduler_for(session)
    with pytest.raises(PublishingError) as raised:
        scheduler.schedule(job, when=NOW, display_timezone="Mars/Base")
    assert raised.value.code == "INVALID_TIMEZONE"


def test_helpers_agree_about_the_boundary():
    policy = SchedulePolicy(policy="ask", grace_seconds=300)
    boundary = late_boundary(policy, now=NOW)
    assert boundary == NOW - timedelta(seconds=300)
    assert seconds_until(NOW + timedelta(seconds=60), now=NOW) == pytest.approx(60)
    assert seconds_until(None) is None
    from creatoros.services.publishing.scheduler import TickResult

    empty = TickResult()
    assert next_wake_seconds(empty) == 30.0


def test_a_scheduler_without_an_executor_still_moves_state(session):
    """The API can drive the scheduler without a queue; only the enqueue step is skipped."""
    _, _, job = scheduled_destination(session, when=NOW - timedelta(seconds=5))
    scheduler = PublishScheduler(session, clock=lambda: NOW, executor=None)
    result = scheduler.tick(now=NOW)
    assert result.count("enqueued") == 1
    assert job.job_state is PublishState.READY
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0


def test_the_sweeper_thread_ticks_and_survives_an_error(session):
    """The sweeper is a thread, so the thing under test is that it keeps going and closes sessions."""
    import threading
    import time

    from creatoros.db.session import get_session_factory
    from creatoros.services.queue import QueueWorker
    from creatoros.workers.runner import BackgroundWorker, scheduler_interval

    ticks: list[int] = []
    failures: list[int] = []

    class FlakyScheduler:
        def tick(self):
            ticks.append(1)
            if len(ticks) == 1:
                failures.append(1)
                raise RuntimeError("boom")
            from creatoros.services.publishing.scheduler import TickResult

            return TickResult()

    def factory(_session):
        return FlakyScheduler()

    # A real factory, not the test's session: the sweeper runs on its own thread and must own its
    # session, which is exactly the mistake this test is here to catch.
    worker = BackgroundWorker(
        QueueWorker(get_session_factory(), {}, owner="test"),
        scheduler_factory=factory,
    )
    worker.start_background(poll_interval=0.05, media_poll_interval=0.05, sweep_interval=0.05)
    deadline = time.time() + 5
    while len(ticks) < 2 and time.time() < deadline:
        time.sleep(0.02)
    worker.stop(timeout=2.0)

    assert len(ticks) >= 2, "the sweeper must keep ticking after an error"
    assert failures, "the first tick was supposed to fail"
    assert worker.last_error and "boom" in worker.last_error
    assert not any(t.is_alive() for t in threading.enumerate() if t.name == "creatoros-scheduler")
    assert scheduler_interval() == 15.0
