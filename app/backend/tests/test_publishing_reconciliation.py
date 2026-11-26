"""Reconciliation tests.

The rule under test is "look, then decide - never resubmit". Each test sets a destination to the
state the executor leaves behind when an answer went missing, feeds a provider answer, and asserts
both the destination state and the audit row that justified it.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from publishing_fixtures import bind_token, make_account, make_asset, make_composer
from social_fixtures import InMemoryVault

from creatoros.db.enums import PublishState, ReconciliationDecision
from creatoros.db.models import PublishReconciliation
from creatoros.integrations.social.errors import SocialError
from creatoros.integrations.social.publishing.publishers.base import RemoteStatus
from creatoros.services.publishing.approvals import PublishApprovalService
from creatoros.services.publishing.errors import PublishingError
from creatoros.services.publishing.reconciliation import (
    Observation,
    ReconciliationService,
    observe,
)

VAULT = InMemoryVault()
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def unknown_destination(session, *, provider_id: str | None = "video-1"):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform="facebook")
    bind_token(session, account, VAULT)
    project = composer.create_intent(title="Morning routine", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="Morning routine"
    )
    job = destination.job
    approvals = PublishApprovalService(session)
    approvals.request(job)
    approvals.decide(job, approve=True, decided_by="local-user")
    job.job_state = PublishState.UNKNOWN_REMOTE_STATE
    job.state_reason = "finish_timeout"
    job.provider_media_id = provider_id
    session.flush()
    return job


def service(session, *, answer) -> ReconciliationService:
    async def probe(_job):
        if isinstance(answer, Exception):
            raise answer
        return answer

    return ReconciliationService(session, probe=probe, clock=lambda: NOW)


def status(state: str, **kwargs):
    return RemoteStatus(provider_object_id=kwargs.pop("provider_object_id", "video-1"), state=state, **kwargs)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("ready", ReconciliationDecision.REMOTE_EXISTS),
        ("published", ReconciliationDecision.REMOTE_EXISTS),
        ("publish_complete", ReconciliationDecision.REMOTE_EXISTS),
        ("inbox", ReconciliationDecision.REMOTE_EXISTS),
        ("missing", ReconciliationDecision.NO_REMOTE_OBJECT),
        ("expired", ReconciliationDecision.NO_REMOTE_OBJECT),
        ("failed", ReconciliationDecision.NO_REMOTE_OBJECT),
        ("processing", ReconciliationDecision.UNDETERMINED),
        ("processing_upload", ReconciliationDecision.UNDETERMINED),
        ("something_new", ReconciliationDecision.UNDETERMINED),
        ("", ReconciliationDecision.UNDETERMINED),
    ],
)
def test_provider_states_map_to_the_three_decisions(state, expected):
    assert observe(status(state)).decision is expected


def test_a_provider_says_it_exists_so_it_is_published(session):
    job = unknown_destination(session)
    observation = asyncio.run(
        service(session, answer=status("ready", permalink="https://fb.example/v/1")).check(job)
    )
    assert observation.decision is ReconciliationDecision.REMOTE_EXISTS
    assert job.job_state is PublishState.PUBLISHED
    assert job.provider_permalink == "https://fb.example/v/1"
    assert job.state_reason == "reconciled_remote_exists"
    assert job.remote_checked_at == NOW
    assert job.reconcile_attempts == 1


def test_a_provider_says_it_does_not_exist_and_nothing_is_resubmitted(session):
    job = unknown_destination(session)
    asyncio.run(service(session, answer=status("missing")).check(job))
    assert job.job_state is PublishState.REQUIRES_USER_ACTION
    assert job.state_reason == "reconciled_remote_absent"
    # The important part: the destination is not queued again, and its approval is not re-spent.
    import sqlalchemy as sa

    from creatoros.db.models import QueueJob

    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 0


def test_a_provider_that_is_still_processing_leaves_the_state_unknown(session):
    job = unknown_destination(session)
    asyncio.run(service(session, answer=status("processing", detail="encoding")).check(job))
    assert job.job_state is PublishState.UNKNOWN_REMOTE_STATE
    assert job.state_reason == "reconciled_still_unknown:processing"
    case = session.scalars(sa.select(PublishReconciliation)).one()
    assert case.resolved_by is None, "an undetermined case stays open"
    assert case.evidence_json["detail"] == "encoding"


def test_an_unreachable_provider_is_undetermined_not_failed(session):
    job = unknown_destination(session)
    error = SocialError("TRANSPORT_ERROR", "connection reset")
    observation = asyncio.run(service(session, answer=error).check(job))
    assert observation.decision is ReconciliationDecision.UNDETERMINED
    assert observation.provider_state == "unreachable"
    assert job.job_state is PublishState.UNKNOWN_REMOTE_STATE
    case = session.scalars(sa.select(PublishReconciliation)).one()
    assert case.evidence_json["error_code"] == "TRANSPORT_ERROR"


def test_a_destination_without_a_provider_id_cannot_be_reconciled(session):
    job = unknown_destination(session, provider_id=None)
    with pytest.raises(PublishingError) as raised:
        asyncio.run(service(session, answer=status("ready")).check(job))
    assert raised.value.code == "NOTHING_TO_RECONCILE"
    assert session.scalar(sa.select(sa.func.count()).select_from(PublishReconciliation)) == 0


def test_every_check_is_recorded_even_when_it_decides_nothing(session):
    job = unknown_destination(session)
    svc = service(session, answer=status("processing"))
    asyncio.run(svc.check(job))
    asyncio.run(svc.check(job))
    cases = svc.cases(job)
    assert len(cases) == 2, "two looks, two rows"
    assert all(case.decision is ReconciliationDecision.UNDETERMINED for case in cases)
    assert job.reconcile_attempts == 2


def test_a_manual_resolution_records_who_said_so(session):
    job = unknown_destination(session)
    svc = service(session, answer=status("processing"))
    case = svc.resolve_manually(
        job,
        decision=ReconciliationDecision.REMOTE_EXISTS,
        note="I can see the post on the Page",
        permalink="https://fb.example/v/1",
        actor="local-user",
    )
    assert job.job_state is PublishState.PUBLISHED
    assert job.provider_permalink == "https://fb.example/v/1"
    assert case.resolved_by == "local-user"
    assert case.evidence_json["manual"] is True
    assert case.evidence_json["note"] == "I can see the post on the Page"


def test_a_manual_resolution_that_says_absent_asks_the_user(session):
    job = unknown_destination(session)
    svc = service(session, answer=status("processing"))
    svc.resolve_manually(
        job,
        decision=ReconciliationDecision.NO_REMOTE_OBJECT,
        note="Nothing on the Page",
    )
    assert job.job_state is PublishState.REQUIRES_USER_ACTION
    assert job.state_reason == "reconciled_remote_absent"


def test_open_cases_and_unresolved_jobs_drive_the_attention_list(session):
    job = unknown_destination(session)
    svc = service(session, answer=status("processing"))
    asyncio.run(svc.check(job))
    assert [case.job_id for case in svc.open_cases()] == [job.id]
    assert [item.id for item in svc.unresolved_jobs()] == [job.id]

    svc.resolve_manually(job, decision=ReconciliationDecision.REMOTE_EXISTS, note="visible")
    assert svc.open_cases() == [], "a resolved case leaves the queue"
    assert svc.unresolved_jobs() == [], "and so does its destination"


def test_check_all_walks_every_unresolved_destination(session):
    job = unknown_destination(session)
    svc = service(session, answer=status("ready", permalink="https://fb.example/v/1"))
    import asyncio

    result = asyncio.run(svc.check_all())
    assert result["checked"][0]["job_id"] == job.id
    assert result["checked"][0]["state"] == "published"
    assert job.job_state is PublishState.PUBLISHED


def test_reconciliation_of_a_published_destination_is_idempotent(session):
    job = unknown_destination(session)
    svc = service(session, answer=status("ready", permalink="https://fb.example/v/1"))
    asyncio.run(svc.check(job))
    first = job.provider_permalink
    observation = Observation(provider_state="ready", decision=ReconciliationDecision.REMOTE_EXISTS)
    svc._apply(job, observation, actor="system")  # a second look decides the same thing
    assert job.job_state is PublishState.PUBLISHED
    assert job.provider_permalink == first
