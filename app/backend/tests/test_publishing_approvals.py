"""Approval binding: the tamper matrix, the ledger and the single-use rule.

The interesting failures are not "it worked". They are: a caption edited after approval, a schedule
moved, the media swapped for a different file, an approval reused for a second publication, and an
approval replayed by an agent. Each of those has a test that must fail closed.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
import sqlalchemy as sa
from publishing_fixtures import make_account, make_asset, make_composer

from creatoros.db.enums import ApprovalState, CapabilityState, Platform, PublishState
from creatoros.db.models import (
    ApprovalRecord,
    ApprovalRequest,
    CapabilitySnapshot,
    PlatformVariant,
    PublishJob,
)
from creatoros.services.publishing.approvals import (
    CONSENT_TEXT_VERSION,
    PublishApprovalService,
    canonical_json,
    hash_payload,
)
from creatoros.services.publishing.errors import PublishingError


def ready_destination(session, *, platform: Platform = Platform.FACEBOOK, caption: str = "First caption"):
    """A draft destination whose content validates cleanly."""
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=platform)
    project = composer.create_intent(title="Morning routine", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id,
        platform=platform.value,
        account_id=account.id,
        title="Morning routine",
        caption=caption,
        hashtags=["morning"],
    )
    return composer, destination.variant, destination.job


def test_the_payload_is_canonical_and_reproducible(session):
    _, _variant, job = ready_destination(session)
    service = PublishApprovalService(session)

    first = service.canonical_payload(job)
    second = service.canonical_payload(job)
    assert first == second
    assert canonical_json(first) == canonical_json(second)
    assert service.payload_hash(job) == hash_payload(first)
    assert len(service.payload_hash(job)) == 64

    # Key order in the mapping must not matter: that is what "canonical" buys.
    reordered = dict(reversed(list(first.items())))
    assert hash_payload(reordered) == service.payload_hash(job)


def test_requesting_approval_moves_the_destination_and_records_the_hash(session):
    _, _variant, job = ready_destination(session)
    service = PublishApprovalService(session)

    row = service.request(job, requested_by="local-user")
    assert row.state is ApprovalState.PENDING
    assert row.target_type == "publish_job"
    assert row.target_id == job.id
    assert row.payload_hash == job.payload_hash == job.approval_payload_hash
    assert job.approval_request_id == row.id
    assert job.job_state is PublishState.AWAITING_APPROVAL
    assert "Morning routine" not in row.summary or "Facebook" in row.summary
    assert row.risks_json, "the human is told what approval means"


def test_asking_twice_for_the_same_content_does_not_create_a_second_request(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    first = service.request(job)
    second = service.request(job)
    assert first.id == second.id
    assert session.scalar(sa.select(sa.func.count()).select_from(ApprovalRequest)) == 1


def test_requesting_approval_for_content_that_fails_validation_is_refused(session):
    # Instagram caps captions at 2,200 characters; Facebook does not.
    composer, variant, job = ready_destination(session, platform=Platform.INSTAGRAM)
    composer.update_destination(variant.id, fields={"caption": "x" * 2_400})
    service = PublishApprovalService(session)
    with pytest.raises(PublishingError) as raised:
        service.request(job)
    assert raised.value.code == "VALIDATION_FAILED"
    assert "text.caption_too_long" in raised.value.detail["codes"]


def test_approving_moves_the_destination_to_approved_and_records_the_decision(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    row = service.decide(job, approve=True, decided_by="local-user", note="looks good")
    assert row.state is ApprovalState.APPROVED
    assert row.decided_by == "local-user"
    assert job.job_state is PublishState.APPROVED


def test_rejecting_returns_the_destination_to_draft(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=False, note="caption is wrong")
    assert job.job_state is PublishState.DRAFT
    assert job.state_reason == "approval_rejected"


def test_consuming_writes_one_ledger_row_and_marks_the_request_used(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    request_row = service.request(job)
    service.decide(job, approve=True)

    record = service.consume(job)
    assert record.target_type == "publish_job"
    assert record.target_id == job.id
    assert record.payload_hash == request_row.payload_hash
    assert record.consent_text_version == CONSENT_TEXT_VERSION
    assert record.result == "granted"
    assert job.approval_record_id == record.id

    session.refresh(request_row)
    assert request_row.consumed_at is not None
    assert session.scalar(sa.select(sa.func.count()).select_from(ApprovalRecord)) == 1


def test_consent_is_single_use(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=True)
    service.consume(job)

    with pytest.raises(PublishingError) as raised:
        service.verify(job)
    assert raised.value.code == "APPROVAL_NOT_USABLE"

    with pytest.raises(PublishingError):
        service.consume(job)


def test_consuming_without_a_decision_is_refused(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    with pytest.raises(PublishingError) as raised:
        service.consume(job)
    assert raised.value.code == "APPROVAL_NOT_USABLE"


def test_an_expired_approval_cannot_be_spent(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    row = service.request(job)
    service.decide(job, approve=True)
    row.expires_at = row.expires_at - timedelta(days=30)
    session.flush()

    with pytest.raises(PublishingError) as raised:
        service.verify(job)
    assert raised.value.code == "APPROVAL_EXPIRED"


TAMPER_CASES = {
    "caption": lambda composer, variant, job: setattr(job, "caption", "A different caption"),
    "title": lambda composer, variant, job: setattr(job, "title", "A different title"),
    "hashtags": lambda composer, variant, job: setattr(job, "hashtags_json", ["other"]),
    "options": lambda composer, variant, job: setattr(job, "options_json", {"published": False}),
    "privacy": lambda composer, variant, job: setattr(variant, "privacy_setting", "private"),
    "schedule": lambda composer, variant, job: setattr(
        job, "scheduled_at_utc", job.scheduled_at_utc or job.created_at
    ),
    "timezone": lambda composer, variant, job: setattr(job, "display_timezone", "Europe/Paris"),
    "asset": lambda composer, variant, job: setattr(job, "asset_sha256", "e" * 64),
    "derivative": lambda composer, variant, job: setattr(job, "derivative_sha256", "d" * 64),
    "account": lambda composer, variant, job: _swap_account(session_of(job), job),
    "validation": lambda composer, variant, job: setattr(
        variant, "validation_report_json", {"outcome": "failed", "findings": []}
    ),
    "mode": lambda composer, variant, job: setattr(job, "publish_mode", "inbox"),
}


def session_of(job):
    return sa.inspect(job).session


def _swap_account(session, job):
    replacement = make_account(session, platform=Platform.FACEBOOK, provider_account_id="page-other")
    job.account_id = replacement.id


@pytest.mark.parametrize("field", sorted(TAMPER_CASES))
def test_any_change_after_approval_invalidates_the_consent(session, field):
    composer, variant, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=True)
    assert service.verify(job) is not None  # sanity: valid before tampering

    TAMPER_CASES[field](composer, variant, job)
    session.flush()

    with pytest.raises(PublishingError) as raised:
        service.verify(job)
    assert raised.value.code == "APPROVAL_HASH_MISMATCH", f"{field} was not detected"


def test_editing_through_the_composer_revokes_rather_than_leaving_a_stale_hash(session):
    composer, variant, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=True)

    composer.update_destination(variant.id, fields={"caption": "A changed caption"})
    assert job.approval_request_id is None
    assert job.payload_hash is None
    assert job.job_state is PublishState.VALIDATION_PENDING
    with pytest.raises(PublishingError) as raised:
        service.verify(job)
    assert raised.value.code == "APPROVAL_MISSING"


def test_the_payload_records_the_capability_it_was_approved_against(session):
    _composer, _variant, job = ready_destination(session)
    snapshot = CapabilitySnapshot(
        account_id=job.account_id,
        checked_at=job.created_at,
        checks_json={},
        overall_state=CapabilityState.AVAILABLE,
        granted_json=["pages_manage_posts"],
    )
    session.add(snapshot)
    session.flush()

    payload = PublishApprovalService(session).canonical_payload(job)
    assert payload["capabilities"]["capability"] == "facebook.page.publish_video"
    assert payload["capabilities"]["state"] == "available"
    assert payload["capabilities"]["snapshot_id"] == snapshot.id


def test_a_stored_payload_hash_that_disagrees_with_the_rows_is_refused(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=True)
    job.payload_hash = "0" * 64
    session.flush()

    with pytest.raises(PublishingError) as raised:
        service.verify(job)
    assert raised.value.code == "APPROVAL_HASH_MISMATCH"
    assert raised.value.detail["current_hash"] == job.approval_payload_hash


def test_an_approval_request_whose_payload_was_edited_is_refused(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    row = service.request(job)
    service.decide(job, approve=True)
    # Simulate a database-level edit of the stored payload: the job's own hash still matches.
    tampered = dict(row.payload_json)
    tampered["content"] = {**tampered["content"], "caption": "Something else entirely"}
    row.payload_json = tampered
    session.flush()

    with pytest.raises(PublishingError) as raised:
        service.verify(job)
    assert raised.value.code == "APPROVAL_HASH_MISMATCH"


def test_a_destination_without_a_request_says_so_instead_of_raising_a_key_error(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    with pytest.raises(PublishingError) as raised:
        service.verify(job)
    assert raised.value.code == "APPROVAL_MISSING"
    with pytest.raises(PublishingError):
        service.decide(job, approve=True)


def test_approval_is_refused_once_the_destination_is_publishing(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=True)
    job.job_state = PublishState.UPLOADING
    session.flush()
    with pytest.raises(PublishingError) as raised:
        service.request(job)
    assert raised.value.code == "DESTINATION_NOT_AWAITING_APPROVAL"


def test_the_consent_text_is_versioned_and_stored_with_the_ledger_row(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=True, decided_by="local-user")
    record = service.consume(job)
    assert record.consent_text_version == CONSENT_TEXT_VERSION
    assert CONSENT_TEXT_VERSION in json.dumps(service.canonical_payload(job))


def test_two_destinations_never_share_an_approval(session):
    composer = make_composer(session)
    asset = make_asset(session)
    first_account = make_account(session, platform=Platform.FACEBOOK, provider_account_id="page-a")
    second_account = make_account(session, platform=Platform.FACEBOOK, provider_account_id="page-b")
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    one = composer.add_destination(
        project.id, platform="facebook", account_id=first_account.id, caption="same text"
    )
    two = composer.add_destination(
        project.id, platform="facebook", account_id=second_account.id, caption="same text"
    )

    service = PublishApprovalService(session)
    row = service.request(one.job)
    with pytest.raises(PublishingError) as raised:
        service.decide(two.job, approve=True)
    assert raised.value.code == "APPROVAL_MISSING"
    assert two.job.approval_request_id is None
    assert row.target_id == one.job.id
    assert (
        service.payload_hash(one.job) != service.payload_hash(two.job)
    ), "the account is part of what was approved"


def test_a_second_job_for_the_same_content_is_not_the_same_consent(session):
    _composer, variant, job = ready_destination(session)
    branch = PublishJob(
        variant_id=variant.id,
        platform="facebook",
        account_id=job.account_id,
        display_timezone=job.display_timezone,
        job_state=PublishState.DRAFT,
        attempts=0,
        idempotency_key="manual-test-key",
        intent_key=job.intent_key,
        asset_id=job.asset_id,
        asset_sha256=job.asset_sha256,
        publish_mode=job.publish_mode,
        caption=job.caption,
        hashtags_json=list(job.hashtags_json),
    )
    session.add(branch)
    session.flush()
    service = PublishApprovalService(session)
    assert service.payload_hash(job) != service.payload_hash(branch)
    assert service.canonical_payload(branch)["job_id"] == branch.id


def test_verification_does_not_mutate_the_destination(session):
    _, _, job = ready_destination(session)
    service = PublishApprovalService(session)
    service.request(job)
    service.decide(job, approve=True)
    before = (
        job.job_state,
        job.payload_hash,
        job.approval_payload_hash,
        job.approval_request_id,
        variant_state(session, job),
    )
    service.verify(job)
    assert (job.job_state, job.payload_hash, job.approval_payload_hash, job.approval_request_id) == before[:4]
    assert variant_state(session, job) == before[4]


def variant_state(session, job) -> str:
    variant = session.get(PlatformVariant, job.variant_id)
    return variant.validation_report_json.get("outcome")
