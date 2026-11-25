"""Composer service tests: campaigns, intents, destinations, provenance and revocation.

The composer is where "publish this" becomes rows. The tests therefore check the rows, not just the
return values: a destination that reports a validation outcome but stores none is a bug, and an
edit that leaves a stale approval hash behind is a security bug.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from publishing_fixtures import (
    make_account,
    make_asset,
    make_composer,
    make_derivative,
    probe_document,
)

from creatoros.db.enums import (
    ApprovalStatus,
    CapabilityState,
    Platform,
    PublishMode,
    PublishState,
)
from creatoros.db.models import (
    AccountCapability,
    AuditEvent,
    ContentProject,
    PlatformVariant,
    PublishJob,
)
from creatoros.services.publishing.composer import ComposerService
from creatoros.services.publishing.errors import PublishingError


def test_campaign_creation_and_update(session):
    composer = make_composer(session)
    campaign = composer.create_campaign(name="  Spring launch  ", goal="awareness", pillars=["tips"])
    assert campaign.name == "Spring launch"
    assert campaign.pillars_json == ["tips"]

    updated = composer.update_campaign(campaign.id, name="Summer launch")
    assert updated.name == "Summer launch"
    with pytest.raises(PublishingError) as raised:
        composer.create_campaign(name="   ")
    assert raised.value.code == "VALIDATION_ERROR"


def test_intent_validation(session):
    composer = make_composer(session)
    with pytest.raises(PublishingError) as raised:
        composer.create_intent(title="  ")
    assert raised.value.code == "VALIDATION_ERROR"

    with pytest.raises(PublishingError) as raised:
        composer.create_intent(title="x", campaign_id="01NOTACAMPAIGN000000000000")
    assert raised.value.http_status == 404

    with pytest.raises(PublishingError) as raised:
        composer.create_intent(title="x", source_asset_id="01NOTANASSET00000000000000")
    assert raised.value.http_status == 404


def test_a_destination_creates_a_variant_and_exactly_one_job(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="Morning routine", source_asset_id=asset.id)

    destination = composer.add_destination(
        project.id,
        platform="facebook",
        account_id=account.id,
        title="Morning routine",
        caption="Coffee, then the gym.",
        hashtags=["#morning", "gym"],
        field_sources={"caption": "human"},
    )
    variant, job = destination.variant, destination.job

    assert variant.project_id == project.id
    assert variant.target_account_id == account.id
    assert variant.hashtags_json == ["morning", "gym"], "hashtags are stored without the hash"
    assert variant.validation_report_json["outcome"] in {"passed", "warning"}

    assert job.variant_id == variant.id
    assert job.intent_key == project.id
    assert job.publish_mode is PublishMode.VIDEO
    assert job.job_state is PublishState.DRAFT
    assert job.asset_id == asset.id
    assert job.asset_sha256 == asset.sha256
    assert job.caption == "Coffee, then the gym."
    assert job.hashtags_json == ["morning", "gym"]
    assert job.field_provenance_json == {"caption": "human"}
    assert job.idempotency_key == f"{project.id}:facebook:{account.id}"
    assert len(job.idempotency_key) <= 64
    assert session.scalar(sa.select(sa.func.count()).select_from(PublishJob)) == 1


def test_the_same_destination_cannot_be_created_twice(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    composer.add_destination(project.id, platform="facebook", account_id=account.id, caption="hi")

    with pytest.raises(PublishingError) as raised:
        composer.add_destination(project.id, platform="facebook", account_id=account.id, caption="hi")
    assert raised.value.code == "DESTINATION_EXISTS"
    assert raised.value.http_status == 409
    assert session.scalar(sa.select(sa.func.count()).select_from(PlatformVariant)) == 1


def test_an_account_that_cannot_publish_is_refused_with_the_reason(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(
        session,
        platform=Platform.TIKTOK,
        capability_state=CapabilityState.REQUIRES_DEVELOPER_APPROVAL,
        missing_scopes=["video.publish"],
    )
    project = composer.create_intent(title="One", source_asset_id=asset.id)

    with pytest.raises(PublishingError) as raised:
        composer.add_destination(project.id, platform="tiktok", account_id=account.id, caption="hi")
    error = raised.value
    assert error.code == "ACCOUNT_NOT_PUBLISH_CAPABLE"
    assert error.user_action_required
    assert error.detail["capability"] == "tiktok.direct_post"
    assert error.detail["state"] == "requires_developer_approval"
    assert error.detail["missing_scopes"] == ["video.publish"]
    assert session.scalar(sa.select(sa.func.count()).select_from(PublishJob)) == 0


def test_an_account_without_a_capability_verdict_is_not_assumed_capable(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.INSTAGRAM)
    session.execute(
        sa.delete(AccountCapability).where(AccountCapability.account_id == account.id)
    )
    session.flush()
    project = composer.create_intent(title="One", source_asset_id=asset.id)

    with pytest.raises(PublishingError) as raised:
        composer.add_destination(project.id, platform="instagram", account_id=account.id, caption="hi")
    assert raised.value.detail["state"] == "unknown"


def test_a_facebook_account_cannot_back_an_instagram_destination(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)

    with pytest.raises(PublishingError) as raised:
        composer.add_destination(project.id, platform="instagram", account_id=account.id, caption="hi")
    assert raised.value.code == "DESTINATION_ACCOUNT_MISMATCH"


def test_each_platform_gets_its_own_mode_and_default_privacy(session):
    composer = make_composer(session)
    asset = make_asset(session)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    instagram = make_account(session, platform=Platform.INSTAGRAM)
    tiktok = make_account(session, platform=Platform.TIKTOK)

    reels = composer.add_destination(
        project.id, platform="instagram", account_id=instagram.id, caption="reel"
    )
    direct = composer.add_destination(
        project.id,
        platform="tiktok",
        account_id=tiktok.id,
        caption="tiktok",
        options={"privacy_level": "SELF_ONLY"},
    )
    assert reels.job.publish_mode is PublishMode.REELS
    assert direct.job.publish_mode is PublishMode.DIRECT_POST
    assert direct.variant.privacy_setting == "SELF_ONLY"
    assert reels.variant.privacy_setting == "public"


def test_a_mode_the_platform_does_not_have_is_refused(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.INSTAGRAM)
    project = composer.create_intent(title="One", source_asset_id=asset.id)

    with pytest.raises(PublishingError) as raised:
        composer.add_destination(
            project.id,
            platform="instagram",
            account_id=account.id,
            mode="direct_post",
            caption="hi",
        )
    assert raised.value.code == "UNKNOWN_PUBLISH_MODE"
    assert raised.value.http_status == 422


def test_an_unknown_timezone_is_refused_before_it_reaches_the_journal(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)

    with pytest.raises(PublishingError) as raised:
        composer.add_destination(
            project.id,
            platform="facebook",
            account_id=account.id,
            caption="hi",
            display_timezone="Mars/Olympus",
        )
    assert raised.value.code == "INVALID_TIMEZONE"


def test_an_unknown_field_source_is_refused(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)

    with pytest.raises(PublishingError) as raised:
        composer.add_destination(
            project.id,
            platform="facebook",
            account_id=account.id,
            caption="hi",
            field_sources={"caption": "vibes"},
        )
    assert raised.value.code == "INVALID_FIELD_SOURCE"


def test_a_portrait_original_is_used_as_is_and_never_re_encoded(session):
    composer = make_composer(session)
    asset = make_asset(session)
    source = composer.select_source(asset.id, platform=Platform.INSTAGRAM, mode=PublishMode.REELS)
    assert source is not None
    assert source.derivative_id is None
    assert source.needs_transcode is False
    assert source.sha256 == asset.sha256


def test_a_landscape_original_asks_for_a_transcode_instead_of_publishing_it(session):
    composer = make_composer(session)
    asset = make_asset(session, probe=probe_document(width=1920, height=1080))
    source = composer.select_source(asset.id, platform=Platform.INSTAGRAM, mode=PublishMode.REELS)
    assert source is not None
    assert source.needs_transcode is True
    assert source.suggested_profile == "publish_reels_1080"
    assert source.media_report is not None and source.media_report.blocking


def test_an_existing_derivative_that_fits_is_preferred_over_a_new_transcode(session):
    composer = make_composer(session)
    asset = make_asset(session, probe=probe_document(width=1920, height=1080))
    derivative = make_derivative(session, asset)

    source = composer.select_source(asset.id, platform=Platform.INSTAGRAM, mode=PublishMode.REELS)
    assert source is not None
    assert source.derivative_id == derivative.id
    assert source.derivative_profile == "normalized"
    assert source.needs_transcode is False
    assert source.sha256 == derivative.sha256


def test_a_derivative_that_does_not_fit_is_skipped(session):
    composer = make_composer(session)
    asset = make_asset(session, probe=probe_document(width=1920, height=1080))
    make_derivative(session, asset, width=480, height=270)

    source = composer.select_source(asset.id, platform=Platform.INSTAGRAM, mode=PublishMode.REELS)
    assert source is not None
    assert source.derivative_id is None
    assert source.needs_transcode is True


def test_a_destination_without_media_is_allowed_but_cannot_validate_cleanly(session):
    composer = make_composer(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="Script first")

    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="draft caption"
    )
    assert destination.job.asset_id is None
    outcomes = {finding["code"] for finding in destination.variant.validation_report_json["findings"]}
    assert "media.probe_missing" in outcomes
    assert destination.variant.validation_report_json["outcome"] == "requires_attention"


def test_editing_approved_content_revokes_the_approval(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="first caption"
    )
    job = destination.job
    job.payload_hash = "f" * 64
    job.approval_payload_hash = "f" * 64
    job.approval_request_id = "01APPROVALREQUEST000000000"
    job.job_state = PublishState.APPROVED
    session.flush()

    composer.update_destination(destination.variant.id, fields={"caption": "second caption"})

    assert job.caption == "second caption"
    assert job.payload_hash is None
    assert job.approval_request_id is None
    assert job.approval_payload_hash is None
    assert job.job_state is PublishState.VALIDATION_PENDING
    assert job.state_reason == "destination_edited"


def test_editing_the_schedule_of_an_approved_destination_also_revokes(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="caption"
    )
    job = destination.job
    job.payload_hash = "a" * 64
    job.job_state = PublishState.SCHEDULED
    session.flush()

    when = datetime.now(UTC) + timedelta(hours=3)
    composer.update_destination(
        destination.variant.id, fields={"scheduled_at_utc": when, "display_timezone": "Europe/Paris"}
    )
    assert job.scheduled_at_utc == when
    assert job.display_timezone == "Europe/Paris"
    assert job.payload_hash is None
    assert job.job_state is PublishState.VALIDATION_PENDING


def test_a_destination_being_uploaded_cannot_be_edited_or_deleted(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="caption"
    )
    destination.job.job_state = PublishState.UPLOADING
    session.flush()

    with pytest.raises(PublishingError) as raised:
        composer.update_destination(destination.variant.id, fields={"caption": "new"})
    assert raised.value.code == "DESTINATION_LOCKED"

    with pytest.raises(PublishingError) as raised:
        composer.delete_destination(destination.variant.id)
    assert raised.value.code == "DESTINATION_LOCKED"


def test_a_queued_destination_is_locked_because_the_worker_may_pick_it_up(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="caption"
    )
    destination.job.job_state = PublishState.READY
    session.flush()

    with pytest.raises(PublishingError) as raised:
        composer.update_destination(destination.variant.id, fields={"caption": "new"})
    assert raised.value.code == "DESTINATION_LOCKED"
    assert raised.value.detail["state"] == "ready"


def test_unknown_edit_fields_are_named_not_ignored(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="caption"
    )
    with pytest.raises(PublishingError) as raised:
        composer.update_destination(destination.variant.id, fields={"platform": "tiktok"})
    assert raised.value.code == "UNKNOWN_FIELD"
    assert raised.value.detail["fields"] == ["platform"]


def test_retargeting_rewrites_the_key_and_keeps_the_payload(session):
    composer = make_composer(session)
    asset = make_asset(session)
    first = make_account(
        session, platform=Platform.FACEBOOK, provider_account_id="page-a", display_name="Page A"
    )
    second = make_account(
        session, platform=Platform.FACEBOOK, provider_account_id="page-b", display_name="Page B"
    )
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=first.id, caption="caption"
    )
    old_key = destination.job.idempotency_key

    composer.update_destination(destination.variant.id, fields={"target_account_id": second.id})
    assert destination.variant.target_account_id == second.id
    assert destination.job.account_id == second.id
    assert destination.job.idempotency_key != old_key
    assert destination.job.idempotency_key == f"{project.id}:facebook:{second.id}"
    assert destination.job.caption == "caption"


def test_deleting_a_draft_destination_removes_its_journal_entry(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="caption"
    )
    variant_id, job_id = destination.variant.id, destination.job.id
    composer.delete_destination(variant_id)
    session.expire_all()
    # Query rather than session.get(): the deleted instance is still in the identity map.
    remaining_variants = session.scalar(
        sa.select(sa.func.count()).select_from(PlatformVariant).where(PlatformVariant.id == variant_id)
    )
    remaining_jobs = session.scalar(
        sa.select(sa.func.count()).select_from(PublishJob).where(PublishJob.id == job_id)
    )
    assert remaining_variants == 0
    assert remaining_jobs == 0, "the journal entry must follow the destination it describes"


def test_a_published_intent_cannot_be_deleted_but_can_be_republished(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="caption"
    )
    destination.job.job_state = PublishState.PUBLISHED
    session.flush()

    with pytest.raises(PublishingError) as raised:
        composer.delete_intent(project.id)
    assert raised.value.code == "DESTINATION_TERMINAL"

    copy = composer.duplicate_intent(project.id)
    assert copy.id != project.id
    assert copy.title.endswith("(again)")
    jobs = list(session.scalars(sa.select(PublishJob).where(PublishJob.intent_key == copy.id)))
    assert len(jobs) == 1
    assert jobs[0].idempotency_key != destination.job.idempotency_key
    assert jobs[0].job_state is PublishState.DRAFT
    # The original stays exactly as it was: republication never rewrites history.
    assert session.get(PublishJob, destination.job.id).job_state is PublishState.PUBLISHED


def test_changing_the_source_asset_invalidates_every_destination_approval(session):
    composer = make_composer(session)
    asset = make_asset(session)
    replacement = make_asset(session, sha256="c" * 64)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="caption"
    )
    destination.job.payload_hash = "d" * 64
    destination.job.job_state = PublishState.APPROVED
    session.flush()

    composer.update_intent(project.id, source_asset_id=replacement.id)
    assert destination.job.payload_hash is None
    assert destination.job.job_state is PublishState.VALIDATION_PENDING


def test_revalidate_stores_the_report_on_the_variant(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.INSTAGRAM)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="instagram", account_id=account.id, caption="fine"
    )
    composer.update_destination(
        destination.variant.id, fields={"caption": "x" * 2_400, "title": "not allowed here"}
    )
    report = composer.revalidate(destination.variant.id)
    assert report.blocking
    stored = destination.variant.validation_report_json
    assert stored["outcome"] == "failed"
    assert {finding["code"] for finding in stored["findings"]} >= {
        "text.caption_too_long",
        "text.title_not_supported",
    }
    assert destination.variant.approval_status is ApprovalStatus.DRAFT


def test_every_write_is_audited_without_content_floods(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="One", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="secret-ish caption"
    )
    actions = [
        row[0]
        for row in session.execute(sa.select(AuditEvent.action).where(AuditEvent.resource_id == destination.job.id))
    ]
    assert "publish.destination.create" in actions
    metadata = session.execute(
        sa.select(AuditEvent.metadata_redacted_json).where(AuditEvent.action == "publish.destination.create")
    ).scalar_one()
    assert "secret-ish caption" not in str(metadata)


def test_two_accounts_on_the_same_platform_are_two_destinations(session):
    composer = make_composer(session)
    asset = make_asset(session)
    first = make_account(session, platform=Platform.FACEBOOK, provider_account_id="page-a")
    second = make_account(session, platform=Platform.FACEBOOK, provider_account_id="page-b")
    project = composer.create_intent(title="One", source_asset_id=asset.id)

    one = composer.add_destination(project.id, platform="facebook", account_id=first.id, caption="a")
    two = composer.add_destination(project.id, platform="facebook", account_id=second.id, caption="a")
    assert one.job.id != two.job.id
    assert one.job.intent_key == two.job.intent_key == project.id
    assert one.job.idempotency_key != two.job.idempotency_key
    assert session.scalar(sa.select(sa.func.count()).select_from(ContentProject)) == 1


def test_a_composer_without_an_audit_service_still_works(session):
    """The queue and the executor use the composer headlessly; audit must be optional."""
    service = ComposerService(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = service.create_intent(title="One", source_asset_id=asset.id)
    destination = service.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="hi"
    )
    assert destination.job.id
