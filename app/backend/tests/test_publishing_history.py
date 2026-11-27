"""History read-model tests.

What these protect: the projections are complete enough to answer "why is this stuck?" from the GUI
alone, and narrow enough that a credential, a signed URL or a local file path can never be in them.
"""
from __future__ import annotations

from datetime import timedelta

from publishing_fixtures import bind_token, make_account, make_asset, make_composer
from social_fixtures import InMemoryVault

from creatoros.db.base import utcnow
from creatoros.db.enums import (
    DeliveryAssetState,
    PublishErrorCategory,
    PublishState,
    ReconciliationDecision,
)
from creatoros.db.models import PublishAttempt, PublishDelivery, PublishReconciliation
from creatoros.services.publishing.approvals import PublishApprovalService
from creatoros.services.publishing.history import JobQuery, PublishHistory, redact

VAULT = InMemoryVault()


def published_destination(session, *, caption="Morning routine", platform="facebook"):
    """Returns (project, destination, job, approval record), all committed through real services."""
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=platform)
    bind_token(session, account, VAULT)
    project = composer.create_intent(title="Morning routine", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform=platform, account_id=account.id, caption=caption
    )
    job = destination.job
    approvals = PublishApprovalService(session)
    approvals.request(job)
    record = approvals.decide(job, approve=True, decided_by="local-user")
    approvals.consume(job)
    job.job_state = PublishState.PUBLISHED
    job.provider_media_id = "video-1"
    job.provider_permalink = "https://fb.example/v/1"
    job.provider_published_at = utcnow()
    job.state_reason = "published"
    session.flush()
    return project, destination, job, record


def add_attempt(session, job, *, no=1, outcome="published", **fields):
    attempt = PublishAttempt(
        job_id=job.id,
        attempt_no=no,
        started_at=utcnow(),
        finished_at=utcnow(),
        outcome=outcome,
        **fields,
    )
    session.add(attempt)
    session.flush()
    return attempt


def test_a_published_destination_is_projected_with_its_facts(session):
    project, destination, job, record = published_destination(session)
    detail = PublishHistory(session).job(job.id)
    assert detail["state"] == "published"
    assert detail["platform"] == "facebook"
    assert detail["provider_permalink"] == "https://fb.example/v/1"
    assert detail["project"]["id"] == project.id
    assert detail["account_name"]
    # `decide` appends the consent row; the destination points at it.
    assert detail["approval_record"]["id"] == job.approval_record_id
    assert detail["approval_record"]["id"] != record.id
    assert detail["approval_record"]["actor"] == "local-user"
    assert detail["approval_record"]["result"] == "granted"
    assert detail["media"]["asset_sha256"]
    assert detail["variant"]["id"] == destination.variant.id
    assert "media_path" not in str(detail)


def test_the_detail_carries_the_attempt_delivery_and_case_ledgers(session):
    _, _, job, _ = published_destination(session)
    add_attempt(
        session,
        job,
        http_status=502,
        request_id="req-1",
        response_code="1363030",
        error_category=PublishErrorCategory.TRANSIENT,
        response_excerpt_redacted="upstream timeout",
        remote_snapshot_json={"stage": "finish"},
    )
    session.add(
        PublishDelivery(
            job_id=job.id,
            provider="s3",
            bucket="creatoros-media",
            object_key="objects/ab/abcdef.mp4",
            sha256="a" * 64,
            bytes=1024,
            state=DeliveryAssetState.UPLOADED,
            url_expires_at=utcnow() + timedelta(hours=1),
        )
    )
    session.add(
        PublishReconciliation(
            job_id=job.id,
            checked_at=utcnow(),
            provider_state="processing",
            decision=ReconciliationDecision.UNDETERMINED,
            evidence_json={"detail": "encoding"},
        )
    )
    session.flush()

    detail = PublishHistory(session).job(job.id)
    assert detail["attempts_log"][0]["http_status"] == 502
    assert detail["attempts_log"][0]["error_category"] == "transient"
    assert detail["attempts_log"][0]["remote_snapshot"] == {"stage": "finish"}
    assert detail["deliveries"][0]["object_key"] == "objects/ab/abcdef.mp4"
    assert detail["deliveries"][0]["expired"] is False
    assert detail["reconciliations"][0]["decision"] == "undetermined"


def test_an_unknown_job_has_no_detail(session):
    assert PublishHistory(session).job("does-not-exist") is None


def test_filters_select_by_platform_state_account_and_text(session):
    project, _, job, _ = published_destination(session, caption="Morning routine")
    published_destination(session, caption="Evening walk", platform="facebook")

    history = PublishHistory(session)
    rows, total = history.jobs(JobQuery(platform="facebook", limit=10))
    assert total == 2
    rows, total = history.jobs(JobQuery(state=(PublishState.PUBLISHED,), limit=10))
    assert total == 2

    other = make_account(session, platform="facebook", provider_account_id="page-2")
    bind_token(session, other, VAULT)
    rows, total = history.jobs(JobQuery(account_id=other.id, limit=10))
    assert total == 0, "an account with nothing published has an empty history"

    rows, total = history.jobs(JobQuery(search="Evening", limit=10))
    assert total == 1
    assert rows[0]["caption"] == "Evening walk"

    rows, total = history.jobs(JobQuery(project_id=project.id, limit=10))
    assert total == 1 and rows[0]["id"] == job.id

    rows, total = history.jobs(JobQuery(since=utcnow() + timedelta(days=1), limit=10))
    assert total == 0


def test_summary_counts_states_and_attention(session):
    published_destination(session)
    _, _, blocked_job, _ = published_destination(session, caption="Second")
    blocked_job.job_state = PublishState.REQUIRES_USER_ACTION
    blocked_job.state_reason = "approval_revoked"
    session.flush()

    summary = PublishHistory(session).summary()
    assert summary["total"] == 2
    assert summary["by_state"]["published"] == 1
    assert summary["by_state"]["requires_user_action"] == 1
    assert summary["needs_attention"] == 1
    assert summary["published_last_7d"] == 1
    assert summary["platforms"][0]["platform"] == "facebook"


def test_the_attention_list_contains_only_destinations_a_person_must_touch(session):
    _, _, failing, _ = published_destination(session)
    failing.job_state = PublishState.FAILED
    failing.state_reason = "upload_failed"
    _, _, unknown, _ = published_destination(session, caption="Unknown")
    unknown.job_state = PublishState.UNKNOWN_REMOTE_STATE
    published_destination(session, caption="Fine")
    session.flush()

    rows = PublishHistory(session).attention()
    assert {row["state"] for row in rows} == {"failed", "unknown_remote_state"}


def test_the_queue_snapshot_separates_running_from_unknown(session):
    _, _, running, _ = published_destination(session)
    running.job_state = PublishState.UPLOADING
    running.lease_owner = "creatoros-publish-0"
    running.lease_expires_at = utcnow() + timedelta(seconds=60)
    running.upload_bytes_total = 1000
    running.upload_bytes_done = 250
    session.flush()

    snapshot = PublishHistory(session).queue_snapshot()
    assert snapshot["running_total"] == 1
    assert snapshot["leases_held"] == 1
    detail = PublishHistory(session).job(running.id)
    assert detail["upload"]["percent"] == 25
    assert detail["lease"]["held"] is True


def test_a_scheduled_destination_appears_in_the_schedule(session):
    _, _, job, _ = published_destination(session)
    job.job_state = PublishState.SCHEDULED
    job.scheduled_at_utc = utcnow() + timedelta(days=1)
    job.display_timezone = "Europe/Paris"
    session.flush()

    schedule = PublishHistory(session).schedule()
    assert [row["id"] for row in schedule["scheduled"]] == [job.id]
    assert schedule["scheduled"][0]["display_timezone"] == "Europe/Paris"


def test_a_missed_destination_is_reported_separately(session):
    _, _, job, _ = published_destination(session)
    job.job_state = PublishState.REQUIRES_USER_ACTION
    job.missed_at = utcnow() - timedelta(hours=2)
    job.state_reason = "missed_schedule"
    session.flush()

    schedule = PublishHistory(session).schedule()
    assert [row["id"] for row in schedule["missed"]] == [job.id]


def test_the_published_feed_only_contains_published_destinations(session):
    published_destination(session)
    _, _, draft, _ = published_destination(session, caption="Draft")
    draft.job_state = PublishState.DRAFT
    session.flush()

    rows, total = PublishHistory(session).published(JobQuery(limit=10))
    assert total == 1
    assert rows[0]["state"] == "published"


def test_redaction_removes_anything_credential_shaped():
    payload = {
        "access_token": "EAAB",
        "upload_url": "https://x/y?sig=1",
        "nested": {"signed_url": "https://x", "detail": "fine"},
        "items": [{"url": "https://x", "state": "uploaded"}],
    }
    cleaned = redact(payload)
    assert cleaned == {
        "nested": {"detail": "fine"},
        "items": [{"state": "uploaded"}],
    }


def test_the_audit_trail_for_a_destination_is_readable(session):
    _, _, job, _ = published_destination(session)
    history = PublishHistory(session)
    entries = history.audit_trail(job.id)
    assert isinstance(entries, list), "an empty trail is a list, never an error"
