"""Sync state and discovery: cadence, backoff, retention and honest attribution.

The behaviour these tests pin down is mostly about *not* asking: not asking faster than the provider
updates, not asking for data the provider has deleted, not asking again after a revoked token, and
not claiming an object we did not publish.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from creatoros.db.enums import (
    AnalyticsErrorClass,
    AnalyticsObjectType,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsSyncStatus,
    AnalyticsWindowKind,
    Platform,
    PublishMode,
)
from creatoros.db.models import AnalyticsSyncState
from creatoros.integrations.analytics.catalog import catalogue_by_key
from creatoros.services.analytics import discovery, registry, runs
from creatoros.services.analytics import sync_state as sync

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def make_account(session, platform: Platform = Platform.TIKTOK):
    from publishing_fixtures import make_account as fixture_account

    return fixture_account(session, platform=platform)


# ------------------------------------------------------------------ cadence


def test_state_is_created_once_per_account_and_object_type(session):
    account = make_account(session)
    first = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    second = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    video = sync.state_for(session, account, AnalyticsObjectType.VIDEO)
    session.commit()
    assert first.id == second.id
    assert first.id != video.id
    assert first.state is AnalyticsSyncStatus.NOT_STARTED
    assert first.poll_interval_s == 24 * 3600
    assert video.poll_interval_s == 6 * 3600
    assert first.provider_retention_days is None  # TikTok documents no retention window
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSyncState)) == 2


def test_a_never_collected_account_is_due_and_a_fresh_one_is_not(session):
    account = make_account(session)
    state = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    assert sync.is_due(state, now=NOW) is True

    sync.record_success(state, now=NOW)
    session.commit()
    assert state.state is AnalyticsSyncStatus.CURRENT
    assert state.last_success_at == NOW
    assert state.next_due_at == NOW + timedelta(hours=24)
    assert sync.is_due(state, now=NOW + timedelta(hours=1)) is False
    assert sync.is_due(state, now=NOW + timedelta(hours=25)) is True


def test_due_states_skips_blocked_and_in_flight(session):
    account = make_account(session)
    due = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    blocked = sync.state_for(session, account, AnalyticsObjectType.VIDEO)
    sync.record_failure(
        blocked, error_class=AnalyticsErrorClass.PERMISSION, error_code="SCOPE_NOT_GRANTED", now=NOW
    )
    discovering = sync.state_for(session, account, AnalyticsObjectType.POST)
    sync.mark_discovering(discovering)
    session.commit()
    assert [state.id for state in sync.due_states(session, now=NOW)] == [due.id]


# ------------------------------------------------------------------ failure handling


def test_a_transient_failure_backs_off_exponentially(session):
    account = make_account(session)
    state = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    sync.record_failure(state, error_class=AnalyticsErrorClass.TRANSIENT, error_code="PROVIDER_TIMEOUT", now=NOW)
    assert state.consecutive_failures == 1
    assert state.next_due_at == NOW + timedelta(seconds=sync.BACKOFF_BASE_S)
    sync.record_failure(state, error_class=AnalyticsErrorClass.TRANSIENT, error_code="PROVIDER_TIMEOUT", now=NOW)
    assert state.next_due_at == NOW + timedelta(seconds=sync.BACKOFF_BASE_S * 2)
    for _ in range(6):
        sync.record_failure(state, error_class=AnalyticsErrorClass.TRANSIENT, error_code="x", now=NOW)
    assert (state.next_due_at - NOW).total_seconds() <= sync.BACKOFF_CAP_S


def test_repeated_failures_stall_then_block(session):
    account = make_account(session)
    state = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    for _ in range(sync.STALL_AFTER_FAILURES):
        sync.record_failure(state, error_class=AnalyticsErrorClass.TRANSIENT, error_code="PROVIDER_UNAVAILABLE", now=NOW)
    assert state.state is AnalyticsSyncStatus.STALLED
    for _ in range(sync.BLOCK_AFTER_FAILURES):
        sync.record_failure(state, error_class=AnalyticsErrorClass.TRANSIENT, error_code="PROVIDER_UNAVAILABLE", now=NOW)
    assert state.state is AnalyticsSyncStatus.BLOCKED


def test_a_revoked_token_blocks_immediately_instead_of_retrying(session):
    account = make_account(session)
    state = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    sync.record_failure(state, error_class=AnalyticsErrorClass.AUTHORIZATION, error_code="TOKEN_REVOKED", now=NOW)
    session.commit()
    assert state.state is AnalyticsSyncStatus.BLOCKED
    assert state.next_due_at is None
    assert sync.is_due(state, now=NOW + timedelta(days=30)) is False
    assert discovery.blocked_summary([state])[0]["error_code"] == "TOKEN_REVOKED"

    sync.unblock(state, now=NOW)
    assert state.state is AnalyticsSyncStatus.NOT_STARTED
    assert sync.is_due(state, now=NOW) is True


# ------------------------------------------------------------------ retention


def test_retention_floor_is_provider_specific(session):
    account = make_account(session, Platform.FACEBOOK)
    state = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    assert state.provider_retention_days == 730
    assert sync.retention_floor(state, now=NOW) == NOW - timedelta(days=730)

    ig = make_account(session, Platform.INSTAGRAM)
    ig_account = sync.state_for(session, ig, AnalyticsObjectType.ACCOUNT)
    ig_media = sync.state_for(session, ig, AnalyticsObjectType.POST)
    # Instagram documents 90 days for account insights but two years for media.
    assert ig_account.provider_retention_days == 90
    assert ig_media.provider_retention_days == 730
    assert sync.retention_floor(ig_account, now=NOW) == NOW - timedelta(days=90)


def test_objects_older_than_retention_are_not_backfilled(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.INSTAGRAM)
    old = make_published_job(
        session,
        platform=Platform.INSTAGRAM,
        account=account,
        published_at=NOW - timedelta(days=400),
    )
    session.commit()
    assert discovery.backfill_candidates(session, account, now=NOW) == []
    assert old.job_id


# ------------------------------------------------------------------ windows


def test_windows_become_due_as_time_passes_and_only_once():
    published = NOW - timedelta(hours=30)
    due = sync.windows_due(published, now=NOW)
    assert [window.window_kind for window in due] == [
        AnalyticsWindowKind.INITIAL,
        AnalyticsWindowKind.H24,
    ]
    later = sync.windows_due(published, now=NOW + timedelta(days=8))
    assert AnalyticsWindowKind.D7 in {window.window_kind for window in later}
    assert AnalyticsWindowKind.D28 not in {window.window_kind for window in later}

    done = {AnalyticsWindowKind.INITIAL, AnalyticsWindowKind.H24}
    assert sync.windows_due(published, now=NOW, already_collected=done) == []


def test_a_collected_window_is_not_requested_again(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.INSTAGRAM)
    fixture = make_published_job(
        session,
        platform=Platform.INSTAGRAM,
        account=account,
        published_at=NOW - timedelta(hours=30),
    )
    session.commit()
    candidates = discovery.backfill_candidates(session, account, now=NOW)
    assert candidates and candidates[0][0].publication_id == fixture.job_id

    # Record a successful h24 run: the window is then no longer due.
    context = runs.open_run(
        session,
        account=account,
        target_kind=AnalyticsRunTarget.METRICS,
        target_object_id=fixture.remote_post_id,
        window_kind=AnalyticsWindowKind.H24,
        api_version="v23.0",
        now=NOW,
    )
    runs.finish_run(session, context, outcome=AnalyticsRunOutcome.SUCCEEDED, now=NOW)
    session.commit()

    remaining = discovery.backfill_candidates(session, account, now=NOW)
    windows = {window.window_kind for _, due in remaining for window in due}
    assert AnalyticsWindowKind.H24 not in windows
    assert AnalyticsWindowKind.INITIAL in windows


def test_a_failed_window_stays_due(session):
    from publishing_fixtures import make_published_job

    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.INSTAGRAM)
    fixture = make_published_job(
        session, platform=Platform.INSTAGRAM, account=account, published_at=NOW - timedelta(hours=30)
    )
    context = runs.open_run(
        session,
        account=account,
        target_kind=AnalyticsRunTarget.METRICS,
        target_object_id=fixture.remote_post_id,
        window_kind=AnalyticsWindowKind.H24,
        api_version="v23.0",
        now=NOW,
    )
    runs.finish_run(session, context, outcome=AnalyticsRunOutcome.FAILED, now=NOW)
    session.commit()
    windows = {
        window.window_kind
        for _, due in discovery.backfill_candidates(session, account, now=NOW)
        for window in due
    }
    assert AnalyticsWindowKind.H24 in windows


# ------------------------------------------------------------------ discovery and attribution


def test_published_objects_are_discovered_with_their_publication(session):
    from publishing_fixtures import make_published_job

    account = make_account(session, Platform.TIKTOK)
    fixture = make_published_job(
        session, platform=Platform.TIKTOK, account=account, published_at=NOW - timedelta(hours=5)
    )
    session.commit()
    discovered = discovery.discover_published_objects(session, account, since=NOW - timedelta(days=1))
    assert len(discovered) == 1
    entry = discovered[0]
    assert entry.provider_object_id == fixture.remote_post_id
    assert entry.publication_id == fixture.job_id
    assert entry.object_type is AnalyticsObjectType.VIDEO
    assert entry.linked is True
    assert entry.source == discovery.SOURCE_PUBLISH_JOB


def test_provider_objects_we_did_not_publish_stay_unlinked(session):
    account = make_account(session, Platform.TIKTOK)
    session.commit()
    videos = [
        {"id": "7300000000000000001", "title": "someone else's", "create_time": int(NOW.timestamp())},
        {"id": "", "title": "broken"},
    ]
    discovered = discovery.discover_provider_objects(videos, session=session, account=account)
    assert len(discovered) == 1
    assert discovered[0].linked is False
    assert discovered[0].publication_id is None
    assert discovered[0].source == discovery.SOURCE_PROVIDER_LIST
    assert discovered[0].object_type is AnalyticsObjectType.VIDEO


def test_unlinked_objects_lists_observed_but_unattributed_ids(session):
    registry.ensure_metric_catalog(session)
    from publishing_fixtures import make_published_job

    account = make_account(session, Platform.TIKTOK)
    fixture = make_published_job(session, platform=Platform.TIKTOK, account=account)
    from creatoros.services.analytics.snapshots import SnapshotWriter, attribute

    writer = SnapshotWriter(session)
    spec = catalogue_by_key()["tt.video.views"]
    context = runs.open_run(
        session, account=account, target_kind=AnalyticsRunTarget.METRICS, api_version="v2", now=NOW
    )
    for object_id in (fixture.remote_post_id, "7300000000000009999"):
        from creatoros.db.enums import MetricAvailability, MetricPeriod, MetricValueKind
        from creatoros.integrations.analytics.base import ObservationDraft

        writer.write(
            run_id=context.run_id,
            account_id=account.id,
            platform=Platform.TIKTOK,
            object_type=AnalyticsObjectType.VIDEO,
            provider_object_id=object_id,
            drafts=[
                ObservationDraft(
                    metric_key="tt.video.views",
                    provider_metric_name=spec.provider_metric_name,
                    availability=MetricAvailability.OBSERVED,
                    value_kind=MetricValueKind.SCALAR,
                    value_num=5.0,
                    period_kind=MetricPeriod.LIFETIME,
                    unit="count",
                )
            ],
            api_version="v2",
            api_host="open.tiktokapis.com",
            source_ref="video",
            attribution=attribute(
                session, platform=Platform.TIKTOK, provider_object_id=object_id, account_id=account.id
            ),
            collected_at=NOW,
        )
    session.commit()
    assert discovery.unlinked_objects(session, account) == ["7300000000000009999"]


def test_publish_mode_decides_the_object_type():
    assert discovery.object_type_for(Platform.TIKTOK, "direct_post") is AnalyticsObjectType.VIDEO
    assert discovery.object_type_for(Platform.INSTAGRAM, PublishMode.REELS.value) is AnalyticsObjectType.REEL
    assert discovery.object_type_for(Platform.INSTAGRAM, "feed") is AnalyticsObjectType.POST
    assert discovery.object_type_for(Platform.FACEBOOK, PublishMode.VIDEO.value) is AnalyticsObjectType.VIDEO


def test_discovery_marks_the_cursor_without_inventing_one(session):
    account = make_account(session)
    state = sync.state_for(session, account, AnalyticsObjectType.VIDEO)
    discovery.note_discovery(state, [])
    assert state.discovery_cursor is None
    assert state.discovery_complete is True

    entry = discovery.DiscoveredObject(
        provider_object_id="7300000000000000003",
        object_type=AnalyticsObjectType.VIDEO,
        publication_id=None,
        published_at=NOW,
    )
    discovery.note_discovery(state, [entry])
    assert state.discovery_cursor == "7300000000000000003"


def test_an_account_with_no_publications_yields_no_candidates(session):
    registry.ensure_metric_catalog(session)
    account = make_account(session, Platform.TIKTOK)
    session.commit()
    assert discovery.backfill_candidates(session, account, now=NOW) == []


def test_discovery_error_classification_is_conservative():
    assert discovery.classify_discovery_error("TOKEN_EXPIRED") is AnalyticsErrorClass.AUTHORIZATION
    assert discovery.classify_discovery_error("SCOPE_NOT_GRANTED") is AnalyticsErrorClass.PERMISSION
    assert discovery.classify_discovery_error("RATE_LIMITED") is AnalyticsErrorClass.RATE_LIMIT
    assert discovery.classify_discovery_error("SOMETHING_NEW") is AnalyticsErrorClass.TRANSIENT
    assert discovery.classify_discovery_error(None) is AnalyticsErrorClass.TRANSIENT


def test_blocked_summary_reports_only_blocked_states(session):
    account = make_account(session)
    blocked = sync.state_for(session, account, AnalyticsObjectType.ACCOUNT)
    sync.record_failure(
        blocked, error_class=AnalyticsErrorClass.PERMISSION, error_code="SCOPE_NOT_GRANTED", now=NOW
    )
    healthy = sync.state_for(session, account, AnalyticsObjectType.VIDEO)
    sync.record_success(healthy, now=NOW)
    session.commit()
    summary = discovery.blocked_summary([blocked, healthy])
    assert len(summary) == 1
    assert summary[0]["account_id"] == account.id
    assert summary[0]["error_class"] == "permission"


@pytest.mark.parametrize("object_type", list(AnalyticsObjectType))
def test_every_object_type_has_a_poll_interval(object_type):
    assert sync.POLL_INTERVALS.get(object_type)
