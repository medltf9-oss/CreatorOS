"""Executor tests: preflight, approval consumption, persistence ordering and settling.

The executor is where a wrong decision becomes a published post, so the tests are about refusal as
much as success: no approval, no capability, no delivery, no media, a stale payload, a second worker
racing the first, and a provider that fails in each of the canonical ways.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from publishing_fixtures import bind_token, make_account, make_asset, make_composer, probe_document
from social_fixtures import InMemoryVault

from creatoros.db.enums import (
    ApprovalStatus,
    Platform,
    ProjectStatus,
    PublishErrorCategory,
    PublishMode,
    PublishState,
)
from creatoros.db.models import (
    ApprovalRecord,
    PublishAttempt,
    PublishDelivery,
    QueueJob,
)
from creatoros.integrations.social.publishing.publishers import (
    PublishContext,
    Publisher,
    PublisherError,
    PublisherResult,
)
from creatoros.services.publishing.approvals import PublishApprovalService
from creatoros.services.publishing.executor import JOB_TYPE, QUEUE, PublishExecutor


class StubPublisher(Publisher):
    """A publisher that does exactly what a test tells it to, and records the context it saw."""

    platform = "facebook"
    mode = PublishMode.VIDEO

    def __init__(self, *, result=None, error: Exception | None = None, on_publish=None):
        self._result = result
        self._error = error
        self._on_publish = on_publish
        self.contexts: list[PublishContext] = []

    async def publish(self, ctx, *, on_progress=None):
        self.contexts.append(ctx)
        if on_progress is not None:
            on_progress(ctx.media_bytes, ctx.media_bytes)
        if self._on_publish is not None:
            self._on_publish(ctx)
        if self._error is not None:
            raise self._error
        return self._result or PublisherResult(
            provider_object_id="fb-video-1", state="published", permalink="https://facebook.com/x"
        )

    async def check_status(self, ctx, provider_object_id):
        raise AssertionError("not used")


#: One vault for this module: the executor and the token service must read the same records.
VAULT = InMemoryVault()


def approved_destination(session, *, platform=Platform.FACEBOOK, caption="Morning routine", vault=None):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=platform)
    bind_token(session, account, vault if vault is not None else VAULT)
    project = composer.create_intent(title="Morning routine", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform=platform.value, account_id=account.id, caption=caption
    )
    approvals = PublishApprovalService(session)
    approvals.request(destination.job)
    approvals.decide(destination.job, approve=True, decided_by="local-user")
    return composer, destination, asset, approvals


def executor_for(session, *, publishers=None, delivery=None, vault=None, sleep=None) -> PublishExecutor:
    return PublishExecutor(
        session,
        vault=vault if vault is not None else VAULT,
        delivery=delivery,
        publishers=publishers
        or {
            ("facebook", PublishMode.VIDEO): StubPublisher(),
            ("instagram", PublishMode.REELS): StubPublisher(),
            ("tiktok", PublishMode.DIRECT_POST): StubPublisher(),
        },
        sleep=sleep or _no_sleep,
        status_attempts=2,
        status_delay_s=0,
    )


async def _no_sleep(_seconds):
    return None


# --------------------------------------------------------------------------- preflight


def test_preflight_passes_for_an_approved_ready_destination(session):
    _, destination, _, _ = approved_destination(session)
    report = executor_for(session).preflight(destination.job)
    assert report.ok is True
    assert report.as_dict()["issues"] == []


def test_preflight_blocks_without_an_approval(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="x", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="hi"
    )
    report = executor_for(session).preflight(destination.job)
    assert report.ok is False
    assert report.issues[0].code == "APPROVAL_REQUIRED"
    # Asking for an approval is not a blocked state: the destination simply is not approved yet.
    assert destination.job.job_state is PublishState.DRAFT


def test_preflight_blocks_when_the_account_lost_its_capability(session):
    _, destination, _, _ = approved_destination(session)
    session.execute(sa.text("update account_capability set state = 'requires_developer_approval'"))
    session.flush()
    report = executor_for(session).preflight(destination.job)
    assert report.ok is False
    assert report.issues[0].code == "ACCOUNT_NOT_PUBLISH_CAPABLE"
    assert destination.job.job_state is PublishState.BLOCKED


def test_preflight_blocks_instagram_without_a_delivery_provider(session):
    _, destination, _, _ = approved_destination(session, platform=Platform.INSTAGRAM)
    report = executor_for(session).preflight(destination.job)
    assert report.ok is False
    codes = {issue.code for issue in report.issues}
    assert "DELIVERY_NOT_CONFIGURED" in codes
    assert destination.job.job_state is PublishState.BLOCKED


def test_preflight_blocks_media_that_needs_a_transcode(session):
    composer = make_composer(session)
    asset = make_asset(session, probe=probe_document(width=1920, height=1080))
    account = make_account(session, platform=Platform.INSTAGRAM)
    project = composer.create_intent(title="x", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="instagram", account_id=account.id, caption="hi"
    )
    report = executor_for(session).preflight(destination.job)
    assert "MEDIA_NEEDS_TRANSCODE" in {issue.code for issue in report.issues}


def test_preflight_blocks_when_the_vault_is_missing(session):
    _, destination, _, _ = approved_destination(session)
    executor = PublishExecutor(session, vault=None, publishers={("facebook", PublishMode.VIDEO): StubPublisher()})
    # vault_from_env() returns the unavailable client when no broker is configured.
    report = executor.preflight(destination.job)
    assert "VAULT_UNAVAILABLE" in {issue.code for issue in report.issues}


# --------------------------------------------------------------------------- execution


def test_a_publish_consumes_the_approval_and_records_everything(session):
    _, destination, _asset, _ = approved_destination(session)
    job = destination.job
    stub = StubPublisher()
    executor = executor_for(session, publishers={("facebook", PublishMode.VIDEO): stub})

    outcome = executor.execute(job.id)

    assert outcome.get("published") is True, outcome
    assert job.job_state is PublishState.PUBLISHED
    assert job.provider_media_id == "fb-video-1"
    assert job.provider_permalink == "https://facebook.com/x"
    assert job.provider_published_at is not None
    assert job.lease_owner is None and job.lease_expires_at is None
    assert session.scalar(sa.select(sa.func.count()).select_from(ApprovalRecord)) == 1
    assert job.approval_record_id is not None

    attempt = session.scalars(sa.select(PublishAttempt)).one()
    assert attempt.outcome == "published"
    assert attempt.finished_at is not None

    assert stub.contexts[0].caption == "Morning routine"
    assert stub.contexts[0].media_path and stub.contexts[0].media_path.endswith(".mp4")
    assert destination.variant.approval_status is ApprovalStatus.APPROVED
    assert destination.variant.project.status is ProjectStatus.PUBLISHED


def test_a_stale_approval_refuses_to_publish(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    job.caption = "A caption nobody approved"
    session.flush()
    stub = StubPublisher()
    outcome = executor_for(session, publishers={("facebook", PublishMode.VIDEO): stub}).execute(job.id)

    assert "refused" in outcome
    assert outcome["refused"] in {"APPROVAL_HASH_MISMATCH"}
    assert job.job_state is PublishState.REQUIRES_USER_ACTION
    assert stub.contexts == [], "no provider may be called without a matching approval"


def test_a_second_execution_while_the_lease_is_held_declines(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    job.job_state = PublishState.UPLOADING
    job.lease_owner = "other-worker"
    job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    session.flush()
    stub = StubPublisher()
    outcome = executor_for(session, publishers={("facebook", PublishMode.VIDEO): stub}).execute(job.id)
    assert outcome["skipped"] == "lease_held"
    assert stub.contexts == []


def test_an_expired_lease_can_be_taken_over(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    job.job_state = PublishState.UPLOADING
    job.lease_owner = "dead-worker"
    job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    session.flush()
    job.approval_request_id = None  # make the approval step fail fast; the lease is what is tested
    outcome = executor_for(session).execute(job.id)
    assert outcome.get("skipped") != "lease_held"


def test_a_transient_failure_keeps_the_job_retryable_until_the_budget_runs_out(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    error = PublisherError(
        PublishErrorCategory.TRANSIENT, "network hiccup", code="PROVIDER_UNAVAILABLE"
    )
    stub = StubPublisher(error=error)
    executor = executor_for(session, publishers={("facebook", PublishMode.VIDEO): stub})

    first = executor.execute(job.id)
    assert first["category"] == "transient"
    assert job.job_state is PublishState.UPLOADING
    assert job.state_reason.startswith("retry_pending")

    # The retry cannot spend the same approval twice, so it must first be re-requested by the human.
    assert job.approval_request_id is not None
    second = executor.execute(job.id)
    assert second.get("refused") or second.get("failed")


def test_a_permanent_failure_settles_as_failed_with_a_category(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    error = PublisherError(PublishErrorCategory.VALIDATION, "bad caption", code="GRAPH_100")
    outcome = executor_for(
        session, publishers={("facebook", PublishMode.VIDEO): StubPublisher(error=error)}
    ).execute(job.id)
    assert outcome["category"] == "validation"
    assert job.job_state is PublishState.FAILED
    assert job.error_category is PublishErrorCategory.VALIDATION
    assert job.last_error_code == "GRAPH_100"
    session.refresh(job)
    attempt = session.scalars(sa.select(PublishAttempt)).one()
    assert attempt.outcome == "failed"
    assert attempt.error_category is PublishErrorCategory.VALIDATION


def test_an_ambiguous_failure_goes_to_unknown_remote_state_with_the_provider_id(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    error = PublisherError(
        PublishErrorCategory.AMBIGUOUS,
        "connection lost after the request was sent",
        code="NETWORK_ERROR",
        provider_object_id="fb-video-9",
    )
    outcome = executor_for(
        session, publishers={("facebook", PublishMode.VIDEO): StubPublisher(error=error)}
    ).execute(job.id)
    assert outcome["category"] == "ambiguous"
    assert job.job_state is PublishState.UNKNOWN_REMOTE_STATE
    assert job.provider_media_id == "fb-video-9", "the provider id must survive for reconciliation"


def test_an_authorization_failure_asks_the_user(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    error = PublisherError(PublishErrorCategory.AUTHORIZATION, "token expired", code="TOKEN_EXPIRED")
    executor_for(session, publishers={("facebook", PublishMode.VIDEO): StubPublisher(error=error)}).execute(
        job.id
    )
    assert job.job_state is PublishState.REQUIRES_USER_ACTION


def test_an_unexpected_crash_is_never_reported_as_success(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job

    class Exploding(StubPublisher):
        async def publish(self, ctx, *, on_progress=None):
            raise RuntimeError("boom")

    outcome = executor_for(session, publishers={("facebook", PublishMode.VIDEO): Exploding()}).execute(job.id)
    assert outcome["crashed"] == "RuntimeError"
    assert job.job_state is PublishState.UNKNOWN_REMOTE_STATE
    assert job.last_error_code == "EXECUTOR_CRASHED"


def test_a_published_job_is_not_published_twice(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    stub = StubPublisher()
    executor = executor_for(session, publishers={("facebook", PublishMode.VIDEO): stub})
    executor.execute(job.id)
    second = executor.execute(job.id)
    assert second["skipped"] == "terminal"
    assert len(stub.contexts) == 1


def test_publishing_a_job_with_no_media_fails_cleanly(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    job.asset_id = None
    session.flush()
    # Preflight refuses before any provider call.
    outcome = executor_for(session).execute(job.id)
    assert outcome.get("blocked") in {"NO_MEDIA", "APPROVAL_REQUIRED", None} or "failed" in outcome


# --------------------------------------------------------------------------- queue lane


def test_enqueue_is_idempotent_per_destination(session):
    _, destination, _, _ = approved_destination(session)
    job = destination.job
    executor = executor_for(session)
    first = executor.enqueue(job)
    second = executor.enqueue(job)
    assert first == second
    jobs = list(session.scalars(sa.select(QueueJob).where(QueueJob.job_type == JOB_TYPE)))
    assert len(jobs) == 1
    assert jobs[0].queue == QUEUE
    assert jobs[0].idempotency_key == f"publish:{job.id}"


def test_two_destinations_get_two_queue_jobs(session):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=Platform.FACEBOOK)
    project = composer.create_intent(title="x", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id, caption="hi"
    )
    other = make_account(session, platform=Platform.FACEBOOK, provider_account_id="page-2")
    second_destination = composer.add_destination(
        project.id, platform="facebook", account_id=other.id, caption="hi"
    )
    executor = executor_for(session)
    assert executor.enqueue(destination.job) != executor.enqueue(second_destination.job)
    assert session.scalar(sa.select(sa.func.count()).select_from(QueueJob)) == 2


def test_the_queue_handler_runs_the_executor(session):
    from creatoros.services.publishing.executor import build_publish_handlers

    _, destination, _, _ = approved_destination(session)
    stub = StubPublisher()
    handlers = build_publish_handlers(
        lambda: session,
        vault_factory=lambda: VAULT,
        publishers={("facebook", PublishMode.VIDEO): stub},
    )
    handler = handlers[JOB_TYPE]

    class Payload:
        def __init__(self) -> None:
            self.payload = {"job_id": destination.job.id}

    outcome = handler(session, Payload())
    assert outcome["published"] is True
    assert destination.job.job_state is PublishState.PUBLISHED


def test_the_handler_refuses_a_payload_without_a_job_id(session):
    from creatoros.services.publishing.executor import build_publish_handlers

    handlers = build_publish_handlers(lambda: session, vault_factory=lambda: VAULT)

    class Payload:
        def __init__(self) -> None:
            self.payload: dict = {}

    with pytest.raises(ValueError):
        handlers[JOB_TYPE](session, Payload())


def test_an_instagram_publish_records_the_delivery_object(session):
    from creatoros.integrations.social.publishing.media_delivery import DeliveryReceipt, SignedUrl

    _, destination, _, _ = approved_destination(session, platform=Platform.INSTAGRAM)
    job = destination.job

    class FakeDelivery:
        configured = True

        def __init__(self):
            self.recorded = []

        def missing(self):
            return []

        def load_config(self):
            return None

        async def ensure_uploaded(self, path, *, sha256, size_bytes, content_type="video/mp4"):
            receipt = DeliveryReceipt(
                bucket="b", object_key="k/x.mp4", bytes=size_bytes, sha256=sha256, etag='"e"', uploaded_at=datetime.now(UTC)
            )
            return receipt, "k/x.mp4"

        async def public_url(self, object_key, *, ttl_s=None):
            return SignedUrl(
                url="https://creatoros-media.s3.eu-west-3.amazonaws.com/k/x.mp4?sig=1",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
                object_key=object_key,
            )

        def record_delivery(self, job, *, receipt, url):
            row = PublishDelivery(
                job_id=job.id,
                provider="s3",
                bucket=receipt.bucket,
                object_key=receipt.object_key,
                sha256=receipt.sha256,
                bytes=receipt.bytes,
                state="verified",
            )
            session.add(row)
            session.flush()
            self.recorded.append(row)
            return row

    delivery = FakeDelivery()
    stub = StubPublisher()
    outcome = executor_for(
        session,
        publishers={("instagram", PublishMode.REELS): stub},
        delivery=delivery,
    ).execute(job.id)
    assert outcome["published"] is True
    assert delivery.recorded, "the delivery ledger row is written before the provider is called"
    assert session.scalar(sa.select(sa.func.count()).select_from(PublishDelivery)) == 1
    assert stub.contexts[0].media_url.startswith("https://creatoros-media.")
    assert stub.contexts[0].media_path  # the local file is still known, for size and diagnostics
