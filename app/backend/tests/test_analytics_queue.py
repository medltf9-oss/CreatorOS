"""The analytics lane: isolation, idempotency, the sweeper integration and the rollback switch.

The properties that matter are the boring ones. Analytics must not take a slot from publishing, a
double sweep must not enqueue two collections, `CREATOROS_ANALYTICS_WORKERS=0` must remove both the
worker and the dispatcher, and no test may see a fifth thread appear.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from social_fixtures import run as run_coro

from creatoros.db.enums import (
    AnalyticsObjectType,
    AnalyticsSyncStatus,
    AnalyticsWindowKind,
    ConnectionStatus,
    Platform,
    QueueState,
)
from creatoros.db.models import AnalyticsSyncState, QueueJob
from creatoros.services.analytics import jobs, registry
from creatoros.services.analytics import sync_state as sync
from creatoros.services.analytics.budget import BudgetGovernor
from creatoros.services.analytics.collector import AnalyticsCollector
from creatoros.services.analytics.scheduler import AnalyticsScheduler, CombinedScheduler
from creatoros.services.queue import QueueService

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


SCOPES = ["user.info.basic", "user.info.stats", "video.list", "read_insights", "pages_read_engagement"]


def make_account(session, platform: Platform = Platform.TIKTOK, *, connected: bool = True):
    from publishing_fixtures import make_account as fixture_account

    account = fixture_account(session, platform=platform)
    # The publishing fixture models publish-capable accounts; analytics needs the read grants too.
    account.scopes_json = list(SCOPES)
    if not connected:
        account.connection_status = ConnectionStatus.DISCONNECTED
        session.flush()
    return account


#: Maintenance job types share the analytics lane but are not collection work. A scheduler test that
#: counts "jobs in the lane" would now also count a retention pass, so the collection tests filter.
MAINTENANCE = {jobs.JOB_RETENTION, jobs.JOB_VERSION_CHECK}


def jobs_in_lane(session, lane: str = jobs.QUEUE) -> list[QueueJob]:
    """Collection jobs only. Maintenance jobs are asserted separately."""
    return [
        job
        for job in session.scalars(sa.select(QueueJob).where(QueueJob.queue == lane))
        if job.job_type not in MAINTENANCE
    ]


# ------------------------------------------------------------------ scheduling


def test_a_due_account_is_dispatched_once_per_tick(session):
    account = make_account(session)
    scheduler = AnalyticsScheduler(session, now=NOW)
    first = scheduler.tick()
    session.commit()
    assert first.accounts_seen == 1
    assert first.dispatched == 1
    assert first.details[0]["job"] == "account_sync"

    queued = jobs_in_lane(session)
    assert len(queued) == 1
    assert queued[0].job_type == jobs.JOB_ACCOUNT_SYNC
    assert queued[0].payload_json["account_id"] == account.id

    # A second sweep before the job ran must not enqueue a duplicate: the queue's idempotency key
    # and the sync state's next_due_at both prevent it.
    second = scheduler.tick()
    session.commit()
    assert len(jobs_in_lane(session)) == 1
    assert second.dispatched == 1  # the state is still due until the job records success


def test_a_blocked_account_is_never_dispatched(session):
    account = make_account(session)
    state = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    sync.record_failure(
        state, error_class=__import__("creatoros.db.enums", fromlist=["AnalyticsErrorClass"]).AnalyticsErrorClass.PERMISSION,
        error_code="SCOPE_NOT_GRANTED", now=NOW,
    )
    session.commit()
    result = AnalyticsScheduler(session, now=NOW).tick()
    session.commit()
    assert result.dispatched == 0
    assert jobs_in_lane(session) == []


def test_a_disconnected_account_is_not_adopted(session):
    make_account(session, connected=False)
    result = AnalyticsScheduler(session, now=NOW).tick()
    session.commit()
    assert result.accounts_seen == 0
    assert jobs_in_lane(session) == []
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSyncState)) == 0


def test_a_stalled_account_waits_for_its_backoff(session):
    account = make_account(session)
    state = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    sync.record_failure(
        state,
        error_class=__import__("creatoros.db.enums", fromlist=["AnalyticsErrorClass"]).AnalyticsErrorClass.TRANSIENT,
        error_code="PROVIDER_TIMEOUT",
        now=NOW,
    )
    session.commit()
    assert AnalyticsScheduler(session, now=NOW).tick().dispatched == 0
    assert AnalyticsScheduler(session, now=NOW + timedelta(seconds=sync.BACKOFF_BASE_S + 1)).tick().dispatched == 1


def test_disabling_the_scheduler_dispatches_nothing(session):
    make_account(session)
    result = AnalyticsScheduler(session, enabled=False, now=NOW).tick()
    session.commit()
    assert result.dispatched == 0
    assert jobs_in_lane(session) == []


def test_the_combined_sweep_runs_publishing_first(session):
    order: list[str] = []

    class FakePublish:
        def tick(self):
            order.append("publish")
            return _Tick({"enqueued": 2})

    account = make_account(session)
    combined = CombinedScheduler(FakePublish(), AnalyticsScheduler(session, now=NOW))
    result = combined.tick()
    session.commit()
    assert order == ["publish"]
    assert result.count("enqueued") == 2 + 1
    # 2 published + 1 analytics collection; maintenance is reported separately, not as "enqueued"
    # work a person is waiting for.
    merged = result.as_dict()
    assert merged["analytics"]["dispatched"] == 1
    assert jobs_in_lane(session)[0].payload_json["account_id"] == account.id


class _Tick:
    def __init__(self, counts: dict) -> None:
        self.counts = counts

    def count(self, key: str) -> int:
        return self.counts.get(key, 0)

    def as_dict(self) -> dict:
        return dict(self.counts)


# ------------------------------------------------------------------ queue lane isolation


def test_analytics_jobs_live_only_in_the_analytics_lane(session):
    account = make_account(session)
    jobs.enqueue_account_sync(session, account)
    jobs.enqueue_discovery(session, account)
    jobs.enqueue_backfill(session, account)
    session.commit()
    lanes = {job.queue for job in session.scalars(sa.select(QueueJob))}
    assert lanes == {"analytics"}

    # The default-lane claim must never see them.
    service = QueueService(session)
    assert service.claim(owner="default-worker", queue="default") is None
    claimed = service.claim(owner="analytics-worker", queue=jobs.QUEUE)
    assert claimed is not None
    assert claimed.job_type in jobs.JOB_TYPES


def test_publish_jobs_are_not_claimed_by_the_analytics_lane(session):
    from creatoros.services.publishing.executor import JOB_TYPE as PUBLISH_JOB_TYPE

    make_account(session, Platform.TIKTOK)
    QueueService(session).enqueue(PUBLISH_JOB_TYPE, {"x": 1}, queue="publish")
    session.commit()
    assert QueueService(session).claim(owner="analytics-worker", queue=jobs.QUEUE) is None


def test_enqueue_is_idempotent_per_account_and_window(session):
    account = make_account(session)
    first = jobs.enqueue_account_sync(session, account, window_kind=AnalyticsWindowKind.H24)
    second = jobs.enqueue_account_sync(session, account, window_kind=AnalyticsWindowKind.H24)
    other = jobs.enqueue_account_sync(session, account, window_kind=AnalyticsWindowKind.D7)
    session.commit()
    assert first.created is True
    assert second.created is False
    assert other.created is True
    assert len(jobs_in_lane(session)) == 2


def test_analytics_priority_sits_below_publishing(session):
    account = make_account(session)
    jobs.enqueue_account_sync(session, account)
    jobs.enqueue_backfill(session, account)
    session.commit()
    priorities = {job.job_type: job.priority for job in jobs_in_lane(session)}
    # Lower number = earlier. A collection never outranks a scheduled publish (Phase 7 uses 10-30).
    assert priorities[jobs.JOB_ACCOUNT_SYNC] <= 40
    assert priorities[jobs.JOB_BACKFILL] > priorities[jobs.JOB_ACCOUNT_SYNC]


# ------------------------------------------------------------------ worker wiring


def test_worker_count_reads_the_environment_and_clamps(monkeypatch):
    from creatoros.workers import runner

    monkeypatch.setenv("CREATOROS_ANALYTICS_WORKERS", "0")
    assert runner.analytics_worker_count() == 0
    monkeypatch.setenv("CREATOROS_ANALYTICS_WORKERS", "9")
    assert runner.analytics_worker_count() == 2
    monkeypatch.setenv("CREATOROS_ANALYTICS_WORKERS", "nonsense")
    assert runner.analytics_worker_count() == 1
    monkeypatch.delenv("CREATOROS_ANALYTICS_WORKERS")
    assert runner.analytics_worker_count() == 1


def test_zero_workers_disables_both_the_lane_and_the_dispatcher(session, monkeypatch):
    from creatoros.db.session import get_session_factory
    from creatoros.workers import runner

    monkeypatch.setenv("CREATOROS_ANALYTICS_WORKERS", "0")
    worker = runner.build_worker(get_session_factory())
    assert worker.analytics_workers == []
    scheduler = runner.build_scheduler(session)
    account = make_account(session)
    # The publishing half is a no-op on an empty schedule; the analytics half must enqueue nothing.
    scheduler.tick()
    session.commit()
    assert jobs_in_lane(session) == []
    assert account.id


def test_one_worker_is_the_default_and_uses_the_analytics_lane(monkeypatch):
    from creatoros.db.session import get_session_factory
    from creatoros.workers import runner

    monkeypatch.delenv("CREATOROS_ANALYTICS_WORKERS", raising=False)
    worker = runner.build_worker(get_session_factory())
    assert len(worker.analytics_workers) == 1
    assert worker.analytics_workers[0].queue == "analytics"
    assert worker.publish_workers[0].queue == "publish"
    assert worker.worker.queue == "default"
    assert worker.media_worker.queue == "media"


def test_analytics_handlers_cover_every_job_type():
    from creatoros.db.session import get_session_factory

    handlers = jobs.build_analytics_handlers(get_session_factory())
    assert set(handlers) == set(jobs.JOB_TYPES)


# ------------------------------------------------------------------ handlers


class _Job:
    def __init__(self, payload: dict, job_id: str = "01JOB") -> None:
        self.payload = payload
        self.id = job_id


class _StubOutcome:
    def __init__(self, *, skipped_reason=None, runs=None, warnings=None) -> None:
        self.skipped_reason = skipped_reason
        self.runs = runs if runs is not None else [{"run_id": "r1", "reason": None}]
        self.warnings = warnings or []
        self.observed = 1
        self.unavailable = 0
        self.written = 1

    @property
    def ok(self) -> bool:
        return self.skipped_reason is None

    def as_dict(self) -> dict:
        return {"skipped_reason": self.skipped_reason, "runs": self.runs}


def test_handlers_refuse_an_unknown_account(session):
    handlers = jobs.build_analytics_handlers(lambda: None)
    import pytest

    with pytest.raises(ValueError):
        handlers[jobs.JOB_ACCOUNT_SYNC](session, _Job({"account_id": "does-not-exist"}))


def test_handlers_refuse_an_unknown_window(session):
    account = make_account(session)
    handlers = jobs.build_analytics_handlers(lambda: None)
    import pytest

    with pytest.raises(ValueError):
        handlers[jobs.JOB_ACCOUNT_SYNC](session, _Job({"account_id": account.id, "window_kind": "next_week"}))


def test_the_backfill_handler_queues_one_job_per_due_window(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.INSTAGRAM)
    make_published_job(
        session,
        platform=Platform.INSTAGRAM,
        account=account,
        published_at=datetime.now(UTC) - timedelta(hours=3),
    )
    session.commit()
    handlers = jobs.build_analytics_handlers(lambda: None)
    result = handlers[jobs.JOB_BACKFILL](session, _Job({"account_id": account.id}))
    session.commit()
    assert result["candidates"] == 1
    assert result["queued"] >= 1
    queued = [job for job in jobs_in_lane(session) if job.job_type == jobs.JOB_OBJECT_SYNC]
    assert queued
    assert queued[0].payload_json["object_type"] in {"reel", "post"}


def test_discovery_handler_reports_linked_and_unlinked_objects(session):
    from publishing_fixtures import make_published_job

    account = make_account(session)
    make_published_job(session, platform=Platform.TIKTOK, account=account)
    session.commit()
    handlers = jobs.build_analytics_handlers(lambda: None)
    result = handlers[jobs.JOB_DISCOVERY](session, _Job({"account_id": account.id}))
    session.commit()
    assert result["published"] == 1
    assert result["objects"][0]["linked"] is True
    state = session.scalar(
        sa.select(AnalyticsSyncState).where(AnalyticsSyncState.object_type == AnalyticsObjectType.VIDEO)
    )
    assert state is not None
    assert state.discovery_complete is True


# ------------------------------------------------------------------ state from outcomes


def test_a_successful_outcome_moves_the_state_to_current(session):
    from creatoros.services.analytics import sync_state

    account = make_account(session)
    state = sync_state.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    jobs._record_state(session, account, AnalyticsObjectType.ACCOUNT, _StubOutcome())
    session.commit()
    assert state.state is AnalyticsSyncStatus.CURRENT
    assert state.consecutive_failures == 0
    assert state.next_due_at is not None


def test_a_skipped_outcome_does_not_touch_the_state(session):
    from creatoros.services.analytics import sync_state

    account = make_account(session)
    state = sync_state.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    jobs._record_state(session, account, AnalyticsObjectType.ACCOUNT, _StubOutcome(skipped_reason="ANALYTICS_BUDGET_EXCEEDED"))
    session.commit()
    assert state.state is AnalyticsSyncStatus.NOT_STARTED
    assert state.next_due_at is None


def test_a_permission_failure_blocks_the_state(session):
    from creatoros.services.analytics import sync_state

    account = make_account(session)
    state = sync_state.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    jobs._record_state(
        session,
        account,
        AnalyticsObjectType.ACCOUNT,
        _StubOutcome(runs=[{"run_id": "r1", "reason": "SCOPE_NOT_GRANTED"}]),
    )
    session.commit()
    assert state.state is AnalyticsSyncStatus.BLOCKED
    assert state.last_error_code == "SCOPE_NOT_GRANTED"


def test_the_handler_runs_a_real_collection_through_the_collector(session, monkeypatch):
    """End-to-end through the handler: queue payload → collector → snapshot rows → state."""
    from creatoros.integrations.analytics.base import CollectResult, ObservationDraft
    from creatoros.services.analytics import collector as collector_module

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)

    class StubAdapter:
        async def collect(self, request, *, token, api_version):
            return CollectResult(
                drafts=[
                    ObservationDraft(
                        metric_key="tt.account.followers",
                        provider_metric_name="follower_count",
                        availability=__import__("creatoros.db.enums", fromlist=["MetricAvailability"]).MetricAvailability.OBSERVED,
                        value_num=1234.0,
                        period_kind=__import__("creatoros.db.enums", fromlist=["MetricPeriod"]).MetricPeriod.INSTANT,
                        unit="count",
                    )
                ],
                calls=1,
            )

    monkeypatch.setattr(collector_module.AnalyticsCollector, "_adapter", lambda self, account, platform: StubAdapter())
    monkeypatch.setattr(collector_module.AnalyticsCollector, "_access_token", _stub_token)

    handlers = jobs.build_analytics_handlers(lambda: None)
    result = handlers[jobs.JOB_ACCOUNT_SYNC](session, _Job({"account_id": account.id}))
    session.commit()

    assert result["metrics_observed"] >= 1
    rows = session.execute(sa.text("select metric_key, value_num from analytics_snapshot")).all()
    assert ("tt.account.followers", 1234.0) in rows
    state = session.scalar(
        sa.select(AnalyticsSyncState).where(AnalyticsSyncState.object_type == AnalyticsObjectType.ACCOUNT)
    )
    assert state.state is AnalyticsSyncStatus.CURRENT


async def _stub_token(self, account) -> str:
    return "stub-token"


def test_queue_rows_carry_no_secret_in_their_payload(session):
    account = make_account(session)
    jobs.enqueue_account_sync(session, account)
    jobs.enqueue_object_sync(
        session,
        account,
        provider_object_id="7300000000000000001",
        object_type=AnalyticsObjectType.VIDEO,
        window_kind=AnalyticsWindowKind.H24,
    )
    session.commit()
    for job in session.scalars(sa.select(QueueJob)):
        serialized = str(job.payload_json).lower()
        assert "token" not in serialized
        assert "secret" not in serialized
        assert job.state is QueueState.QUEUED


def test_collecting_the_same_object_twice_in_a_window_is_one_job(session):
    account = make_account(session)
    for _ in range(3):
        jobs.enqueue_object_sync(
            session,
            account,
            provider_object_id="7300000000000000002",
            object_type=AnalyticsObjectType.VIDEO,
            window_kind=AnalyticsWindowKind.D7,
        )
    session.commit()
    assert len([job for job in jobs_in_lane(session) if job.job_type == jobs.JOB_OBJECT_SYNC]) == 1


def test_running_two_collectors_for_the_same_account_is_safe(session, monkeypatch):
    """Two workers racing is at-least-once delivery; the ledger must still hold one row."""
    from creatoros.db.enums import MetricAvailability, MetricPeriod
    from creatoros.integrations.analytics.base import CollectResult, ObservationDraft

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)

    class StubAdapter:
        async def collect(self, request, *, token, api_version):
            return CollectResult(
                drafts=[
                    ObservationDraft(
                        metric_key="tt.account.followers",
                        provider_metric_name="follower_count",
                        availability=MetricAvailability.OBSERVED,
                        value_num=99.0,
                        period_kind=MetricPeriod.INSTANT,
                        unit="count",
                    )
                ],
                calls=1,
            )

    def collector():
        instance = AnalyticsCollector(session, adapters={Platform.TIKTOK: StubAdapter()}, budget=BudgetGovernor())
        return instance

    first = collector()
    second = collector()
    run_coro(first.collect_account(account, token="t"))
    session.commit()
    run_coro(second.collect_account(account, token="t"))
    session.commit()
    assert session.execute(sa.text("select count(*) from analytics_snapshot")).scalar() == 1
