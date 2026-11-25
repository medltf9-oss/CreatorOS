"""The publishing API: envelopes, affordances, and what it refuses.

Two things are asserted on every response in this file: the shape the GUI depends on, and the
absence of anything that must not leave the backend. The "no secrets" check is deliberately blunt -
it looks for the token and the delivery credential values in the whole serialised body - because a
leak through a nested JSON column would not be caught by field-by-field assertions.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from publishing_fixtures import bind_token, make_account, make_asset

from creatoros.db.enums import PublishState
from creatoros.db.models import QueueJob

TOKEN = "test-session-token"
SECRET = "SUPER-SECRET-DELIVERY-KEY"


@pytest.fixture()
def media(session):
    asset = make_asset(session, size_bytes=7_000_000)
    # The API reads through its own session, so the row has to be committed, not merely flushed.
    session.commit()
    return asset


@pytest.fixture()
def account(session, vault):
    account = make_account(session, platform="facebook")
    bind_token(session, account, vault)
    session.commit()
    return account


def create_intent(client, session, media, *, title="Morning routine"):
    response = client.post("/publishing/intents", json={"title": title, "source_asset_id": media.id})
    assert response.status_code == 201, response.text
    return response.json()["project"]["id"]


def create_destination(client, session, media, account, *, caption="Morning routine"):
    project_id = create_intent(client, session, media)
    response = client.post(
        f"/publishing/intents/{project_id}/destinations",
        json={
            "platform": "facebook",
            "account_id": account.id,
            "caption": caption,
            "scheduled_at_utc": None,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["variant"]["id"], body["variant"]["job_id"]


def approve(client, variant_id):
    assert client.post(f"/publishing/destinations/{variant_id}/approval", json={}).status_code == 200
    decided = client.post(
        f"/publishing/destinations/{variant_id}/approval/decide", json={"approve": True}
    )
    assert decided.status_code == 200, decided.text
    return decided.json()


def assert_clean(body: str) -> None:
    assert TOKEN not in body
    assert SECRET not in body
    assert "access_token" not in body
    assert "upload_url" not in body


# ------------------------------------------------------------------------ auth
def test_publishing_routes_require_the_session_token(unauth_client):
    assert unauth_client.get("/publishing/overview").status_code == 401
    assert unauth_client.get("/publishing/jobs").status_code == 401


# -------------------------------------------------------------------- overview
def test_overview_reports_the_honest_state_of_every_part(client, session, media, account):
    response = client.get("/publishing/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["total"] == 0
    assert body["delivery"]["configured"] is False, "no bucket is configured on a fresh install"
    assert body["delivery"]["missing"]
    assert body["schedule"]["policy"]["policy"] == "ask"
    assert [item["id"] for item in body["capabilities"]] == [account.id]
    assert_clean(response.text)


# ------------------------------------------------------------------- composer
def test_a_campaign_intent_and_destination_can_be_created_and_read_back(client, session, media, account):
    campaign = client.post("/publishing/campaigns", json={"name": "Launch", "objective": "awareness"})
    assert campaign.status_code == 201, campaign.text
    campaign_id = campaign.json()["items"][0]["id"]

    intent = client.post(
        "/publishing/intents",
        json={"title": "First cut", "campaign_id": campaign_id, "source_asset_id": media.id},
    )
    assert intent.status_code == 201
    project_id = intent.json()["project"]["id"]

    added = client.post(
        f"/publishing/intents/{project_id}/destinations",
        json={"platform": "facebook", "account_id": account.id, "caption": "Hello"},
    )
    assert added.status_code == 201, added.text
    variant_id = added.json()["variant"]["id"]

    detail = client.get(f"/publishing/intents/{project_id}").json()
    assert detail["project"]["destinations"] == 1
    assert detail["destinations"][0]["state"] == "draft"
    assert detail["destinations"][0]["validation"]["outcome"] in {"passed", "warning", "requires_attention"}

    listing = client.get("/publishing/intents", params={"campaign_id": campaign_id}).json()
    assert listing["total"] == 1

    destination = client.get(f"/publishing/destinations/{variant_id}").json()
    assert destination["variant"]["caption"] == "Hello"
    assert destination["job"]["media"]["asset_id"] == media.id


def test_a_destination_can_be_edited_until_it_is_queued(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    response = client.patch(
        f"/publishing/destinations/{variant_id}", json={"fields": {"caption": "New caption"}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["variant"]["caption"] == "New caption"

    refused = client.patch(
        f"/publishing/destinations/{variant_id}", json={"fields": {"media_path": "D:/x.mp4"}}
    )
    assert refused.status_code == 422, "a local path is not an editable field"


def test_duplicating_an_intent_makes_a_new_one_and_leaves_the_old(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    project_id = client.get(f"/publishing/destinations/{variant_id}").json()["variant"]["project_id"]

    copy = client.post(f"/publishing/intents/{project_id}/duplicate", json={})
    assert copy.status_code == 201, copy.text
    assert copy.json()["project"]["id"] != project_id
    assert copy.json()["project"]["title"].endswith("(again)")
    assert cube_destinations(client, project_id) == 1


def cube_destinations(client, project_id) -> int:
    return client.get(f"/publishing/intents/{project_id}").json()["project"]["destinations"]


# ------------------------------------------------------------------ affordances
def test_the_gui_is_told_why_an_action_is_unavailable(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    body = client.get(f"/publishing/destinations/{variant_id}/actions").json()
    assert body["state"] == "draft"
    assert body["publish_now"]["enabled"] is False
    assert "approval" in body["publish_now"]["reason"]
    assert body["cancel"]["enabled"] is True
    assert body["reconcile"]["enabled"] is False
    assert body["reconcile"]["reason"]


def test_a_draft_cannot_be_published_at_all_and_says_so(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    response = client.post(f"/publishing/destinations/{variant_id}/publish-now")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "NOT_PUBLISHABLE_NOW"


def test_a_queued_destination_without_a_live_approval_reports_the_reason(client, session, media, account):
    from creatoros.db.models import PublishJob

    variant_id, job_id = create_destination(client, session, media, account)
    job = session.get(PublishJob, job_id)
    job.job_state = PublishState.READY
    session.commit()

    response = client.post(f"/publishing/destinations/{variant_id}/publish-now")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is False
    assert body["reason"] != "queued"
    assert any(issue["code"] == "APPROVAL_REQUIRED" for issue in body["detail"]["issues"])


def test_an_approved_destination_is_queued_rather_than_published_inline(client, session, media, account):
    variant_id, job_id = create_destination(client, session, media, account)
    approve(client, variant_id)

    response = client.post(f"/publishing/destinations/{variant_id}/publish-now")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is True
    assert body["state"] == "ready"

    session.expire_all()
    queued = session.scalars(sa.select(QueueJob)).all()
    assert [row.idempotency_key for row in queued] == [f"publish:{job_id}"]


def test_an_unapproved_destination_cannot_be_approved_twice_or_decided_without_a_request(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    missing = client.post(f"/publishing/destinations/{variant_id}/approval/decide", json={"approve": True})
    assert missing.status_code == 409
    assert missing.json()["code"] == "APPROVAL_MISSING"


def test_cancel_is_allowed_on_a_draft_and_refused_once_remote(client, session, media, account):
    variant_id, _job_id = create_destination(client, session, media, account)
    cancelled = client.post(f"/publishing/destinations/{variant_id}/cancel", json={"reason": "not today"})
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"

    other_id, other_job = create_destination(client, session, media, account, caption="Second")
    from creatoros.db.models import PublishJob

    job = session.get(PublishJob, other_job)
    job.job_state = PublishState.UNKNOWN_REMOTE_STATE
    job.provider_media_id = "video-9"
    session.commit()
    refused = client.post(f"/publishing/destinations/{other_id}/cancel", json={})
    assert refused.status_code == 409
    assert refused.json()["code"] == "REMOTE_STATE_IN_PLAY"


def test_retry_on_a_failed_destination_asks_for_a_fresh_approval(client, session, media, account):
    variant_id, job_id = create_destination(client, session, media, account)
    approve(client, variant_id)
    from creatoros.db.models import PublishJob
    from creatoros.services.publishing.approvals import PublishApprovalService

    job = session.get(PublishJob, job_id)
    # The failed attempt spent the consent, which is exactly what the executor does before uploading.
    PublishApprovalService(session).consume(job)
    job.job_state = PublishState.FAILED
    job.state_reason = "upload_failed"
    session.commit()

    response = client.post(f"/publishing/destinations/{variant_id}/retry")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is False
    assert body["reason"] == "approval_required"
    assert body["state"] == "draft", "the destination goes back to draft so consent can be granted again"


def test_retrying_something_that_did_not_fail_is_refused(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    response = client.post(f"/publishing/destinations/{variant_id}/retry")
    assert response.status_code == 409
    assert response.json()["code"] == "NOT_RETRYABLE"


# --------------------------------------------------------------------- approval
def test_the_approval_payload_is_the_thing_being_approved(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    status = client.get(f"/publishing/destinations/{variant_id}/approval").json()
    assert status["payload_hash"] is None
    assert status["can_request"] is True
    assert status["payload"]["content"]["caption"] == "Morning routine"
    assert status["payload"]["account"]["display_name"] == account.display_name
    assert status["consent_text_version"]

    approved = approve(client, variant_id)
    assert approved["state"] == "approved"
    assert approved["payload_hash"] and approved["approved_hash"] == approved["payload_hash"]
    assert approved["can_request"] is False, "an approved destination is past the approval stage"


# --------------------------------------------------------------------- schedule
def test_scheduling_moves_the_destination_and_shows_up_in_the_schedule(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    approve(client, variant_id)
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    response = client.post(
        f"/publishing/destinations/{variant_id}/schedule",
        json={"scheduled_at_utc": when, "display_timezone": "Europe/Paris"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == "scheduled"

    schedule = client.get("/publishing/schedule").json()
    assert [row["id"] for row in schedule["scheduled"]]
    assert schedule["scheduled"][0]["display_timezone"] == "Europe/Paris"


def test_a_bad_timezone_is_refused_by_name(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    approve(client, variant_id)
    response = client.post(
        f"/publishing/destinations/{variant_id}/schedule",
        json={"scheduled_at_utc": datetime.now(UTC).isoformat(), "display_timezone": "Mars/Olympus"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_TIMEZONE"


def test_the_late_policy_is_readable_and_settable(client):
    assert client.get("/publishing/schedule/policy").json()["policy"] == "ask"
    updated = client.put("/publishing/schedule/policy", json={"policy": "publish_now", "grace_seconds": 60})
    assert updated.status_code == 200
    assert updated.json()["policy"] == "publish_now"
    bad = client.put("/publishing/schedule/policy", json={"policy": "eventually"})
    assert bad.status_code == 422


def test_a_sweep_queues_a_due_destination_and_reports_what_it_did(client, session, media, account):
    variant_id, job_id = create_destination(client, session, media, account)
    approve(client, variant_id)
    from creatoros.db.models import PublishJob

    job = session.get(PublishJob, job_id)
    job.job_state = PublishState.SCHEDULED
    job.scheduled_at_utc = datetime.now(UTC) - timedelta(seconds=5)
    session.commit()

    result = client.post("/publishing/schedule/tick").json()
    assert result["enqueued"] == 1
    assert result["decisions"][0]["action"] == "enqueued"
    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 1


def test_a_missed_schedule_is_reported_and_can_be_published_by_hand(client, session, media, account):
    variant_id, job_id = create_destination(client, session, media, account)
    approve(client, variant_id)
    from creatoros.db.models import PublishJob

    job = session.get(PublishJob, job_id)
    job.job_state = PublishState.SCHEDULED
    job.scheduled_at_utc = datetime.now(UTC) - timedelta(hours=10)
    session.commit()

    result = client.post("/publishing/schedule/tick").json()
    assert result["missed"] == 1
    schedule = client.get("/publishing/schedule").json()
    assert [row["id"] for row in schedule["missed"]] == [job_id]

    published = client.post(f"/publishing/destinations/{variant_id}/publish-missed-now")
    assert published.status_code == 200, published.text
    assert published.json()["queued"] is True


# --------------------------------------------------------------- reconciliation
def test_reconciling_a_destination_without_a_provider_id_is_refused(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    response = client.post(f"/publishing/destinations/{variant_id}/reconcile")
    assert response.status_code == 409
    assert response.json()["code"] == "NOTHING_TO_RECONCILE"


def test_an_undetermined_case_cannot_be_resolved_by_hand(client, session, media, account):
    variant_id, _ = create_destination(client, session, media, account)
    response = client.post(
        f"/publishing/destinations/{variant_id}/reconcile/resolve",
        json={"decision": "undetermined", "note": "no idea"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "UNDETERMINED_IS_NOT_A_RESOLUTION"


def test_a_person_can_resolve_a_case_and_the_destination_moves(client, session, media, account):
    variant_id, job_id = create_destination(client, session, media, account)
    from creatoros.db.models import PublishJob

    job = session.get(PublishJob, job_id)
    job.job_state = PublishState.UNKNOWN_REMOTE_STATE
    job.provider_media_id = "video-9"
    session.commit()

    response = client.post(
        f"/publishing/destinations/{variant_id}/reconcile/resolve",
        json={"decision": "remote_exists", "note": "visible on the Page", "permalink": "https://fb.example/v/9"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "published"
    detail = client.get(f"/publishing/jobs/{job_id}").json()["job"]
    assert detail["provider_permalink"] == "https://fb.example/v/9"
    assert detail["reconciliations"][0]["resolved_by"] == "local-user"


# ---------------------------------------------------------------------- history
def test_history_filters_and_ledgers_are_readable(client, session, media, account):
    variant_id, job_id = create_destination(client, session, media, account)
    approve(client, variant_id)
    from creatoros.db.base import utcnow
    from creatoros.db.models import PublishJob

    job = session.get(PublishJob, job_id)
    job.job_state = PublishState.PUBLISHED
    job.provider_published_at = utcnow()
    job.provider_permalink = "https://fb.example/v/1"
    session.commit()

    listing = client.get("/publishing/jobs", params={"platform": "facebook", "state": "published"}).json()
    assert listing["total"] == 1
    assert listing["items"][0]["provider_permalink"] == "https://fb.example/v/1"

    feed = client.get("/publishing/history").json()
    assert feed["total"] == 1

    attempts = client.get(f"/publishing/jobs/{job_id}/attempts").json()
    assert attempts == {"items": [], "total": 0}
    deliveries = client.get(f"/publishing/jobs/{job_id}/deliveries").json()
    assert deliveries["total"] == 0
    audit = client.get(f"/publishing/jobs/{job_id}/audit").json()
    assert isinstance(audit["items"], list)

    missing = client.get("/publishing/jobs/nope")
    assert missing.status_code == 404


def test_the_attention_and_queue_views_only_contain_what_they_promise(client, session, media, account):
    _variant_id, job_id = create_destination(client, session, media, account)
    from creatoros.db.models import PublishJob

    job = session.get(PublishJob, job_id)
    job.job_state = PublishState.UPLOADING
    session.commit()

    attention = client.get("/publishing/attention").json()
    assert attention["total"] == 0, "an upload in progress is not an attention item"

    queue = client.get("/publishing/queue").json()
    assert queue["running_total"] == 1
    assert queue["unknown_total"] == 0


# --------------------------------------------------------------------- delivery
def test_delivery_is_reported_as_unconfigured_with_a_reason(client):
    body = client.get("/publishing/delivery").json()
    assert body["configured"] is False
    assert body["reason"]
    assert body["credentials_present"] is False
    assert set(body["supported_providers"]) == {"s3", "r2", "b2", "wasabi", "minio", "spaces"}


def test_configuring_delivery_without_a_vault_secret_is_refused(client):
    response = client.put(
        "/publishing/delivery",
        json={
            "provider": "s3",
            "bucket": "creatoros-media",
            "region": "eu-west-3",
            "credentials_secret_id": "01ZZZZZZZZZZZZZZZZZZZZZZZ",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "DELIVERY_CREDENTIALS_REQUIRED"


def test_delivery_check_reports_the_unconfigured_state(client):
    response = client.post("/publishing/delivery/check")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["reason"]


# ------------------------------------------------------------------- validation
def test_limits_are_published_per_platform(client):
    body = client.get("/publishing/limits", params={"platform": "tiktok"}).json()
    assert body["mode"] == "direct_post"
    assert body["rules"]["text"]["caption_max"] is not None
    assert body["rules"]["media"]["max_bytes"] > 0
    assert body["rules"]["docs_url"]


def test_validation_reports_findings_without_saving_anything(client, media):
    # TikTok's documented caption cap is 2200 characters, so 2500 is over the line without having to
    # pretend the API accepts a 70 000-character body.
    response = client.post(
        "/publishing/validate",
        json={"platform": "tiktok", "caption": "x" * 2500, "asset_id": media.id},
    )
    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert report["outcome"] == "failed"
    assert any(finding["code"].startswith("text.") for finding in report["findings"])


def test_capabilities_come_from_the_capability_rows(client, session, account):
    session.flush()
    body = client.get("/publishing/capabilities").json()
    entry = next(item for item in body["accounts"] if item["id"] == account.id)
    assert entry["platform"] == "facebook"
    assert isinstance(entry["capabilities"], dict)


def test_no_response_in_the_whole_surface_leaks_a_secret(client, session, media, account, vault):
    variant_id, job_id = create_destination(client, session, media, account)
    approve(client, variant_id)
    calls = [
        client.get("/publishing/overview"),
        client.get("/publishing/jobs"),
        client.get(f"/publishing/jobs/{job_id}"),
        client.get(f"/publishing/destinations/{variant_id}/actions"),
        client.get(f"/publishing/destinations/{variant_id}/approval"),
        client.get("/publishing/delivery"),
        client.get("/publishing/capabilities"),
        client.get("/publishing/queue"),
        client.get("/publishing/schedule"),
    ]
    for response in calls:
        assert response.status_code == 200, response.text
        assert_clean(json.dumps(response.json()))
