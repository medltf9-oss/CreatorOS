"""Ingestion: one writer, run-scoped idempotency, no downgrade, shapes preserved.

These tests use a fake adapter and a real database, because the properties under test are properties
of the *ledger*: the same logical run must not duplicate rows, a retry must not erase a measurement,
a second logical run must append rather than rewrite, and a structured value must survive the round
trip through SQLite unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from social_fixtures import run as run_coro

from creatoros.db.enums import (
    AnalyticsObjectType,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsWindowKind,
    ConnectionStatus,
    MetricAvailability,
    MetricPeriod,
    MetricValueKind,
    Platform,
)
from creatoros.db.models import AnalyticsCollectionRun, AnalyticsSnapshot, SocialAccount
from creatoros.integrations.analytics.base import (
    AnalyticsAdapter,
    CollectRequest,
    CollectResult,
    ObservationDraft,
)
from creatoros.integrations.analytics.base import skipped as skipped_draft
from creatoros.integrations.analytics.catalog import catalogue_by_key
from creatoros.integrations.analytics.errors import AnalyticsError
from creatoros.services.analytics import registry, runs
from creatoros.services.analytics.budget import BudgetGovernor, Limit
from creatoros.services.analytics.collector import AnalyticsCollector
from creatoros.services.analytics.snapshots import Attribution, SnapshotWriter, attribute

NOW = datetime(2026, 11, 24, 10, 30, tzinfo=UTC)


def make_account(session, platform: Platform = Platform.TIKTOK, *, scopes: list[str] | None = None) -> SocialAccount:
    from uuid import uuid4

    account = SocialAccount(
        platform=platform,
        provider_account_id=f"acct-{platform.value}-{uuid4().hex[:8]}",
        display_name="Test account",
        account_type="tiktok_creator" if platform is Platform.TIKTOK else "facebook_page",
        scopes_json=scopes if scopes is not None else ["user.info.basic", "user.info.stats", "video.list"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2" if platform is Platform.TIKTOK else "v23.0",
        meta_json={},
    )
    session.add(account)
    session.flush()
    return account


class FakeAdapter(AnalyticsAdapter):
    """Returns whatever the test told it to, in the exact shape the collector expects."""

    platform_name = "tiktok"

    def __init__(self, drafts: list[ObservationDraft] | None = None, *, calls: int = 1) -> None:
        super().__init__(transport=None)
        self._drafts = drafts or []
        self._calls = calls
        self.requests: list[CollectRequest] = []
        self.fail_with: Exception | None = None

    async def _collect(self, request: CollectRequest, *, token: str, api_version: str) -> CollectResult:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        return CollectResult(
            drafts=[_copy(draft) for draft in self._drafts if draft.metric_key in {spec.key for spec in request.specs}],
            calls=self._calls,
        )


def _copy(draft: ObservationDraft) -> ObservationDraft:
    return ObservationDraft(
        metric_key=draft.metric_key,
        provider_metric_name=draft.provider_metric_name,
        availability=draft.availability,
        value_kind=draft.value_kind,
        value_num=draft.value_num,
        value_json=draft.value_json,
        period_kind=draft.period_kind,
        period_start=draft.period_start,
        period_end=draft.period_end,
        unit=draft.unit,
        note=draft.note,
    )


def observed(key: str, value: float, **kwargs) -> ObservationDraft:
    spec = catalogue_by_key()[key]
    return ObservationDraft(
        metric_key=key,
        provider_metric_name=spec.provider_metric_name,
        availability=MetricAvailability.OBSERVED,
        value_kind=spec.value_kind,
        value_num=value,
        period_kind=kwargs.pop("period_kind", MetricPeriod.INSTANT),
        unit=spec.unit,
        **kwargs,
    )


def collected(session, account, adapter: FakeAdapter, **kwargs):
    collector = AnalyticsCollector(
        session,
        adapters={account.platform: adapter},
        budget=BudgetGovernor(clock=lambda: 0.0),
        token_resolver=_token,
        now=kwargs.pop("now", NOW),
    )
    return run_coro(collector.collect_account(account, token="t", **kwargs))


async def _token(account) -> str:
    return "resolved-token"


def rows(session) -> list[AnalyticsSnapshot]:
    return list(session.scalars(sa.select(AnalyticsSnapshot).order_by(AnalyticsSnapshot.metric_key)))


# ------------------------------------------------------------------ one run, one row


def test_a_collection_writes_one_row_per_metric_and_closes_its_run(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    adapter = FakeAdapter([observed("tt.account.followers", 52000), observed("tt.account.video_count", 142)])
    outcome = collected(session, account, adapter)
    session.commit()

    assert outcome.observed == 2
    assert outcome.written == 2
    stored = rows(session)
    assert {row.metric_key: row.value_num for row in stored} == {
        "tt.account.followers": 52000.0,
        "tt.account.video_count": 142.0,
    }
    assert all(row.api_version == "v2" for row in stored)
    assert all(row.api_host == "open.tiktokapis.com" for row in stored)
    assert all(row.run_id for row in stored)

    collection_run = session.scalar(sa.select(AnalyticsCollectionRun))
    assert collection_run.outcome is AnalyticsRunOutcome.SUCCEEDED
    assert collection_run.finished_at is not None
    assert collection_run.metrics_observed == 2
    assert collection_run.metrics_written == 2


def test_replaying_the_same_logical_run_writes_no_duplicate_row(session):
    """Requirement 3: a retried job resumes the run; the ledger does not grow."""
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    payload = [observed("tt.account.followers", 52000)]

    first = collected(session, account, FakeAdapter(payload))
    session.commit()
    second = collected(session, account, FakeAdapter(payload), now=NOW + timedelta(minutes=2))
    session.commit()

    assert first.runs[0]["already_finished"] is False
    assert second.runs[0]["already_finished"] is True
    assert len(rows(session)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsCollectionRun)) == 1


def test_a_new_logical_run_appends_and_never_rewrites_the_old_one(session):
    """Append-only *between* runs: tomorrow's value must not overwrite today's."""
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    collected(session, account, FakeAdapter([observed("tt.account.followers", 52000)]))
    session.commit()
    later = NOW + timedelta(days=1)
    collected(session, account, FakeAdapter([observed("tt.account.followers", 53100)]), now=later)
    session.commit()

    stored = rows(session)
    assert len(stored) == 2
    assert sorted(row.value_num for row in stored) == [52000.0, 53100.0]
    assert len({row.run_id for row in stored}) == 2


def test_two_accounts_are_never_merged(session):
    registry.ensure_metric_catalog(session)
    first = make_account(session, Platform.TIKTOK)
    second = make_account(session, Platform.TIKTOK)
    collected(session, first, FakeAdapter([observed("tt.account.followers", 10)]))
    collected(session, second, FakeAdapter([observed("tt.account.followers", 20)]))
    session.commit()
    assert sorted(row.value_num for row in rows(session)) == [10.0, 20.0]


# ------------------------------------------------------------------ retry semantics


def test_a_retry_cannot_downgrade_an_observed_value(session):
    """The heart of requirement 3: 'we could not read it this time' must not erase a measurement."""
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    writer = SnapshotWriter(session)
    run_row = runs.open_run(
        session,
        account=account,
        target_kind=AnalyticsRunTarget.ACCOUNT_SYNC,
        window_kind=None,
        api_version="v2",
        now=NOW,
    )
    spec = catalogue_by_key()["tt.account.followers"]
    first = writer.write(
        run_id=run_row.run_id,
        account_id=account.id,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_object_id=account.provider_account_id,
        drafts=[observed("tt.account.followers", 52000)],
        api_version="v2",
        api_host="open.tiktokapis.com",
        source_ref="account",
        collected_at=NOW,
    )
    retry = writer.write(
        run_id=run_row.run_id,
        account_id=account.id,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_object_id=account.provider_account_id,
        drafts=[skipped_draft(spec, MetricAvailability.MISSING, "provider did not answer on retry")],
        api_version="v2",
        api_host="open.tiktokapis.com",
        source_ref="account",
        collected_at=NOW,
    )
    session.commit()

    assert first.inserted == 1
    assert retry.kept_observation == 1
    stored = rows(session)
    assert len(stored) == 1
    assert stored[0].availability is MetricAvailability.OBSERVED
    assert stored[0].value_num == 52000.0


def test_a_retry_may_upgrade_a_missing_value_to_observed(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    writer = SnapshotWriter(session)
    context = runs.open_run(
        session,
        account=account,
        target_kind=AnalyticsRunTarget.ACCOUNT_SYNC,
        api_version="v2",
        now=NOW,
    )
    spec = catalogue_by_key()["tt.account.followers"]
    kwargs = {
        "run_id": context.run_id,
        "account_id": account.id,
        "platform": Platform.TIKTOK,
        "object_type": AnalyticsObjectType.ACCOUNT,
        "provider_object_id": account.provider_account_id,
        "api_version": "v2",
        "api_host": "open.tiktokapis.com",
        "source_ref": "account",
        "collected_at": NOW,
    }
    writer.write(drafts=[skipped_draft(spec, MetricAvailability.TEMPORARILY_UNAVAILABLE, "throttled")], **kwargs)
    result = writer.write(drafts=[observed("tt.account.followers", 52000)], **kwargs)
    session.commit()
    assert result.updated == 1
    stored = rows(session)
    assert len(stored) == 1
    assert stored[0].availability is MetricAvailability.OBSERVED
    assert stored[0].value_num == 52000.0


def test_repeating_an_identical_write_is_a_no_op(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    writer = SnapshotWriter(session)
    context = runs.open_run(
        session, account=account, target_kind=AnalyticsRunTarget.ACCOUNT_SYNC, api_version="v2", now=NOW
    )
    kwargs = {
        "run_id": context.run_id,
        "account_id": account.id,
        "platform": Platform.TIKTOK,
        "object_type": AnalyticsObjectType.ACCOUNT,
        "provider_object_id": account.provider_account_id,
        "api_version": "v2",
        "api_host": "open.tiktokapis.com",
        "source_ref": "account",
        "collected_at": NOW,
    }
    writer.write(drafts=[observed("tt.account.followers", 52000)], **kwargs)
    again = writer.write(drafts=[observed("tt.account.followers", 52000)], **kwargs)
    session.commit()
    assert again.unchanged == 1
    assert len(rows(session)) == 1


# ------------------------------------------------------------------ shapes and constraints


def test_a_series_is_stored_whole_and_a_missing_row_has_no_value(session):
    """Requirement 4 plus the anti-zero rule, through the real CHECK constraints."""
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    drafts = [
        ObservationDraft(
            metric_key="tt.video.retention_curve",
            provider_metric_name="retention_graph",
            availability=MetricAvailability.NOT_SUPPORTED,
            value_kind=MetricValueKind.SERIES,
            period_kind=MetricPeriod.LIFETIME,
            unit="ratio",
            note="not exposed by any approved API",
        ),
        observed("tt.video.views", 18200, period_kind=MetricPeriod.LIFETIME, period_start=None, period_end=None),
    ]
    # The catalogue refuses to request the unsupported metric, so write it directly: this proves the
    # writer stores a documented non-answer without inventing a value for it.
    writer = SnapshotWriter(session)
    context = runs.open_run(
        session, account=account, target_kind=AnalyticsRunTarget.METRICS, api_version="v2", now=NOW
    )
    writer.write(
        run_id=context.run_id,
        account_id=account.id,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_object_id="7300000000000000000",
        drafts=drafts,
        api_version="v2",
        api_host="open.tiktokapis.com",
        source_ref="video",
        collected_at=NOW,
    )
    session.commit()

    stored = {row.metric_key: row for row in rows(session)}
    assert stored["tt.video.retention_curve"].value_num is None
    assert stored["tt.video.retention_curve"].value_json is None
    assert stored["tt.video.retention_curve"].availability is MetricAvailability.NOT_SUPPORTED
    assert stored["tt.video.views"].value_num == 18200.0
    assert stored["tt.video.views"].value_json is None

    # And the database itself refuses a value on a non-observed row.
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "update analytics_snapshot set value_num = 0 where metric_key = 'tt.video.retention_curve'"
            )
        )
    session.rollback()


def test_structured_values_survive_as_json(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.INSTAGRAM)
    draft = ObservationDraft(
        metric_key="ig.media.views",
        provider_metric_name="views",
        availability=MetricAvailability.OBSERVED,
        value_kind=MetricValueKind.BREAKDOWN,
        value_json={"FOLLOWER": 12.0, "NON_FOLLOWER": 5.0},
        period_kind=MetricPeriod.LIFETIME,
        unit="count",
    )
    writer = SnapshotWriter(session)
    context = runs.open_run(
        session, account=account, target_kind=AnalyticsRunTarget.METRICS, api_version="v23.0", now=NOW
    )
    writer.write(
        run_id=context.run_id,
        account_id=account.id,
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_object_id=account.provider_account_id,
        drafts=[draft],
        api_version="v23.0",
        api_host="graph.instagram.com",
        source_ref="media",
        collected_at=NOW,
    )
    session.commit()
    stored = rows(session)[0]
    assert stored.value_json == {"FOLLOWER": 12.0, "NON_FOLLOWER": 5.0}
    assert stored.value_num is None


def test_refused_metrics_are_written_with_their_reason_and_not_requested(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK, scopes=["video.list"])
    adapter = FakeAdapter([observed("tt.video.views", 100, period_kind=MetricPeriod.LIFETIME)])
    outcome = collected(
        session,
        account,
        adapter,
        object_type=AnalyticsObjectType.ACCOUNT,
    )
    session.commit()
    assert outcome.ok
    stored = {row.metric_key: row for row in rows(session)}
    # The account counters were refused for lack of user.info.stats, and nothing was requested.
    assert stored["tt.account.followers"].availability is MetricAvailability.NOT_AUTHORIZED
    assert "user.info.stats" in (stored["tt.account.followers"].availability_reason or "")
    # Nothing was collectable, so no HTTP request was built at all.
    assert adapter.requests == []


# ------------------------------------------------------------------ attribution


def test_attribution_is_captured_as_strings(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    fixture = make_published_job(session, platform=Platform.TIKTOK, account=account)
    session.commit()
    attribution = attribute(
        session, platform=Platform.TIKTOK, provider_object_id=fixture.remote_post_id, account_id=account.id
    )
    assert attribution.publication_id == fixture.job_id
    assert attribution.content_project_id
    assert attribution.campaign_id is not None
    assert attribution.asset_id

    writer = SnapshotWriter(session)
    context = runs.open_run(
        session, account=account, target_kind=AnalyticsRunTarget.METRICS, api_version="v2", now=NOW
    )
    writer.write(
        run_id=context.run_id,
        account_id=account.id,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_object_id=fixture.remote_post_id,
        drafts=[observed("tt.video.views", 10, period_kind=MetricPeriod.LIFETIME)],
        api_version="v2",
        api_host="open.tiktokapis.com",
        source_ref="video",
        attribution=attribution,
        collected_at=NOW,
    )
    session.commit()
    stored = rows(session)[0]
    assert stored.publication_id == fixture.job_id
    assert stored.content_project_id == attribution.content_project_id
    assert stored.campaign_id == attribution.campaign_id


def test_deleting_the_publication_keeps_the_performance(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    fixture = make_published_job(session, platform=Platform.TIKTOK, account=account)
    session.commit()
    attribution = attribute(
        session, platform=Platform.TIKTOK, provider_object_id=fixture.remote_post_id, account_id=account.id
    )
    writer = SnapshotWriter(session)
    context = runs.open_run(
        session, account=account, target_kind=AnalyticsRunTarget.METRICS, api_version="v2", now=NOW
    )
    writer.write(
        run_id=context.run_id,
        account_id=account.id,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        provider_object_id=fixture.remote_post_id,
        drafts=[observed("tt.video.views", 10, period_kind=MetricPeriod.LIFETIME)],
        api_version="v2",
        api_host="open.tiktokapis.com",
        source_ref="video",
        attribution=attribution,
        collected_at=NOW,
    )
    session.commit()
    session.execute(sa.text("delete from publish_job where id = :id"), {"id": fixture.job_id})
    session.commit()
    stored = rows(session)[0]
    assert stored.publication_id is None
    assert stored.content_project_id == attribution.content_project_id
    assert stored.value_num == 10.0


# ------------------------------------------------------------------ failures and budgets


def test_a_run_level_failure_is_recorded_not_faked(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    adapter = FakeAdapter([observed("tt.account.followers", 1)])
    adapter.fail_with = AnalyticsError("TOKEN_EXPIRED", "the token expired")
    outcome = collected(session, account, adapter)
    session.commit()

    collection_run = session.scalar(sa.select(AnalyticsCollectionRun))
    assert collection_run.outcome is AnalyticsRunOutcome.FAILED
    assert collection_run.error_code == "TOKEN_EXPIRED"
    assert collection_run.error_class is not None
    assert rows(session) == []
    assert outcome.skipped_reason is None
    assert outcome.observed == 0


def test_a_retired_api_version_is_refused_before_any_request(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.FACEBOOK, scopes=["read_insights", "pages_read_engagement"])
    account.api_version = "v19.0"
    session.flush()
    adapter = FakeAdapter([observed("fb.page.media_views", 1)])
    outcome = collected(session, account, adapter, now=datetime(2026, 12, 1, tzinfo=UTC))
    session.commit()
    assert outcome.skipped_reason and "GRAPH_VERSION_UNSUPPORTED" in outcome.skipped_reason
    assert adapter.requests == []
    assert rows(session) == []
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsCollectionRun)) == 0


def test_the_local_budget_stops_a_second_collection_in_the_window(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    budget = BudgetGovernor(clock=lambda: 0.0, limits={Platform.TIKTOK: Limit(calls=1, window_s=60)})
    collector = AnalyticsCollector(
        session,
        adapters={Platform.TIKTOK: FakeAdapter([observed("tt.account.followers", 1)])},
        budget=budget,
        token_resolver=_token,
        now=NOW,
    )
    first = run_coro(collector.collect_account(account, token="t", window_kind=AnalyticsWindowKind.INITIAL))
    second = run_coro(collector.collect_account(account, token="t", window_kind=AnalyticsWindowKind.D7))
    session.commit()
    assert first.skipped_reason is None
    assert second.skipped_reason == "ANALYTICS_BUDGET_EXCEEDED"
    skipped_run = session.scalars(
        sa.select(AnalyticsCollectionRun).where(AnalyticsCollectionRun.outcome == AnalyticsRunOutcome.SKIPPED)
    ).all()
    assert skipped_run


def test_run_dedupe_key_is_stable_within_a_bucket_and_differs_across_windows():
    key_one = runs.dedupe_key(
        account_id="a",
        target_kind=AnalyticsRunTarget.ACCOUNT_SYNC,
        target_object_id=None,
        window_kind=AnalyticsWindowKind.H24,
        moment=datetime(2026, 11, 24, 10, 1, tzinfo=UTC),
    )
    key_two = runs.dedupe_key(
        account_id="a",
        target_kind=AnalyticsRunTarget.ACCOUNT_SYNC,
        target_object_id=None,
        window_kind=AnalyticsWindowKind.H24,
        moment=datetime(2026, 11, 24, 10, 59, tzinfo=UTC),
    )
    key_three = runs.dedupe_key(
        account_id="a",
        target_kind=AnalyticsRunTarget.ACCOUNT_SYNC,
        target_object_id=None,
        window_kind=AnalyticsWindowKind.D28,
        moment=datetime(2026, 11, 24, 10, 1, tzinfo=UTC),
    )
    assert key_one == key_two
    assert key_one != key_three
    assert len(key_one) == 64


def test_attribution_dataclass_defaults_are_empty():
    empty = Attribution()
    assert empty.publication_id is None
    assert empty.content_project_id is None


def test_two_writers_cannot_write_the_same_run_key_twice(session):
    """The unique constraint is the backstop under the writer's upsert."""
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    writer = SnapshotWriter(session)
    context = runs.open_run(
        session, account=account, target_kind=AnalyticsRunTarget.ACCOUNT_SYNC, api_version="v2", now=NOW
    )
    kwargs = {
        "run_id": context.run_id,
        "account_id": account.id,
        "platform": Platform.TIKTOK,
        "object_type": AnalyticsObjectType.ACCOUNT,
        "provider_object_id": account.provider_account_id,
        "api_version": "v2",
        "api_host": "open.tiktokapis.com",
        "source_ref": "account",
        "collected_at": NOW,
    }
    day_start = datetime(2026, 11, 24, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    writer.write(
        drafts=[
            ObservationDraft(
                metric_key="tt.account.followers",
                provider_metric_name="follower_count",
                availability=MetricAvailability.OBSERVED,
                value_kind=MetricValueKind.SCALAR,
                value_num=5.0,
                period_kind=MetricPeriod.DAY,
                period_start=day_start,
                period_end=day_end,
                unit="count",
            )
        ],
        **kwargs,
    )
    session.commit()
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
        sa.text(
            "insert into analytics_snapshot (id, account_id, platform, object_type, provider_object_id, "
            "metric_key, value_kind, period_kind, period_start, period_end, value_num, unit, availability, "
            "breakdown_key, api_version, api_host, source_ref, run_id, collected_at, completeness, "
            "created_at, updated_at) "
                "values ('dup', :account, 'tiktok', 'account', :pid, 'tt.account.followers', 'scalar', 'day', "
            ":start, :end, 6.0, 'count', 'observed', '', 'v2', 'open.tiktokapis.com', 'account', :run, :now, "
            "'complete', :now, :now)"
        ),
        {
            "account": account.id,
            "pid": account.provider_account_id,
            "run": context.run_id,
            "now": NOW,
            # Stored as naive UTC by `UtcDateTime`, so the duplicate has to be written the same way.
            "start": "2026-11-24 00:00:00.000000",
            "end": "2026-11-25 00:00:00.000000",
        },
    )
    session.rollback()
