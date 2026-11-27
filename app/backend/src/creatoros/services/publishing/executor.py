"""The publish executor: the only module allowed to call a publisher.

It is reachable from exactly one place - the `publish` queue lane - and from nowhere else. No route,
no agent tool and no service may import :class:`PublishExecutor` and run a publish inline; a test in
this phase asserts that.

The flow, and why each step is where it is:

    preflight     everything that can be checked locally is checked, and the destination is moved to
                  `blocked`/`requires_user_action` with a reason instead of failing mid-upload
    consume       the human's approval is re-verified against a freshly derived payload and spent, so
                  a reused approval is impossible
    persist       provider ids are written as soon as the provider returns them, *before* the next
                  call, so a crash never loses the anchor of a possibly-created post
    upload/poll   the publisher does the protocol; this module only records what happened
    settle        one of: published, failed, unknown_remote_state, requires_user_action

Exactly-once is not claimed (D7). What is claimed, and tested: one queue job per destination at a
time, one approval per publication, and the provider's own object id persisted before anything else
can be attempted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...db.base import utcnow
from ...db.enums import (
    PUBLISH_TRANSITIONS,
    ApprovalStatus,
    DeliveryAssetState,
    Platform,
    ProjectStatus,
    PublishErrorCategory,
    PublishMode,
    PublishState,
    SecretStatus,
    can_transition,
)
from ...db.models import (
    AccountCapability,
    MediaAsset,
    MediaDerivative,
    PlatformVariant,
    PublishAttempt,
    PublishDelivery,
    PublishJob,
    SecretRef,
    SocialAccount,
)
from ...integrations.social.errors import SocialError
from ...integrations.social.publishing.publishers import (
    FacebookVideoPublisher,
    InstagramReelsPublisher,
    PublishContext,
    Publisher,
    PublisherError,
    PublisherResult,
    TikTokInboxPublisher,
    TikTokPublisher,
)
from ...integrations.social.publishing.security import host_of
from ...services.social.tokens import TokenService
from ...services.social.vault import Vault, VaultError, vault_from_env
from ..backup import AuditService
from ..media.storage import MediaStorage
from ..queue import QueueService
from .approvals import PublishApprovalService
from .composer import PUBLISH_CAPABILITY, ComposerService
from .delivery import DeliveryService
from .errors import PublishingError

log = get_logger(__name__)

JOB_TYPE = "publish.destination"
QUEUE = "publish"
UPLOAD_URL_PURPOSE = "provider_upload_session"

#: How many times a transient failure may be retried before the destination is failed for good.
MAX_PUBLISH_ATTEMPTS = 3
#: Status polls per execution for providers that publish asynchronously.
STATUS_POLL_ATTEMPTS = 20
STATUS_POLL_DELAY_S = 5.0


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    code: str
    message: str
    state: PublishState
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "state": self.state.value,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    job_id: str
    ok: bool
    issues: tuple[PreflightIssue, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "ok": self.ok,
            "issues": [issue.as_dict() for issue in self.issues],
        }


class PublishExecutor:
    def __init__(
        self,
        session: Session,
        *,
        vault: Vault | None = None,
        delivery: DeliveryService | None = None,
        publishers: Mapping[tuple[str, PublishMode], Publisher] | None = None,
        audit: AuditService | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] | None = None,
        status_attempts: int = STATUS_POLL_ATTEMPTS,
        status_delay_s: float = STATUS_POLL_DELAY_S,
    ) -> None:
        self.session = session
        self.vault = vault if vault is not None else vault_from_env()
        self.audit = audit or AuditService(session)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self.status_attempts = status_attempts
        self.status_delay_s = status_delay_s
        self.delivery = delivery or DeliveryService(session, self.vault, audit=self.audit, clock=self.clock)
        self.publishers = dict(publishers or {})
        self.composer = ComposerService(session, self.audit)
        self.approvals = PublishApprovalService(session, self.audit)
        #: Upload URLs handed over by a publisher during this attempt. They are sealed in the vault
        #: once the attempt is over, because sealing is async and the publisher calls its callback
        #: from inside a coroutine.
        self._pending_upload: dict[str, str] = {}

    # ------------------------------------------------------------------ enqueue
    def enqueue(self, job: PublishJob, *, delay_seconds: float = 0.0, priority: int = 40) -> str:
        """Queue one publish attempt. The idempotency key is what stops a second one existing."""
        result = QueueService(self.session).enqueue(
            JOB_TYPE,
            {"job_id": job.id},
            queue=QUEUE,
            priority=priority,
            delay_seconds=delay_seconds,
            max_attempts=MAX_PUBLISH_ATTEMPTS,
            idempotency_key=f"publish:{job.id}",
        )
        return result.job_id

    # ----------------------------------------------------------------- preflight
    def preflight(self, job: PublishJob) -> PreflightReport:
        """Everything that can be known without the provider, in one place, with reasons."""
        issues: list[PreflightIssue] = []
        variant = self.session.get(PlatformVariant, job.variant_id)
        if variant is None:
            issues.append(
                PreflightIssue("DESTINATION_MISSING", "The destination no longer exists.", PublishState.BLOCKED)
            )
        account = self.session.get(SocialAccount, job.account_id)
        if account is None:
            issues.append(
                PreflightIssue("ACCOUNT_MISSING", "The destination's account no longer exists.", PublishState.BLOCKED)
            )
        if account is not None:
            capability_key = PUBLISH_CAPABILITY.get((str(job.platform), _mode(job)))
            capability = (
                self.session.scalar(
                    sa.select(AccountCapability).where(
                        AccountCapability.account_id == account.id,
                        AccountCapability.capability == capability_key,
                    )
                )
                if capability_key
                else None
            )
            if capability is None or str(capability.state) != "available":
                issues.append(
                    PreflightIssue(
                        "ACCOUNT_NOT_PUBLISH_CAPABLE",
                        (capability.reason if capability else f"{capability_key} has not been verified")
                        or "the account cannot publish on this surface",
                        PublishState.BLOCKED,
                        {
                            "capability": capability_key,
                            "state": str(capability.state) if capability else None,
                        },
                    )
                )

        if not job.asset_id:
            issues.append(
                PreflightIssue("NO_MEDIA", "The destination has no media attached.", PublishState.BLOCKED)
            )
        else:
            source = self.composer.select_source(job.asset_id, platform=_platform(job), mode=_mode(job))
            if source is None:
                issues.append(
                    PreflightIssue("NO_MEDIA", "The destination has no media attached.", PublishState.BLOCKED)
                )
            elif source.needs_transcode:
                issues.append(
                    PreflightIssue(
                        "MEDIA_NEEDS_TRANSCODE",
                        "The media does not fit this surface; generate a publishing derivative first.",
                        PublishState.BLOCKED,
                        {"suggested_profile": source.suggested_profile},
                    )
                )

        if not job.approval_request_id:
            issues.append(
                PreflightIssue(
                    "APPROVAL_REQUIRED",
                    "This destination has no approval yet.",
                    PublishState.AWAITING_APPROVAL,
                )
            )

        if (
            _mode(job) is PublishMode.REELS
            and _platform(job) is Platform.INSTAGRAM
            and not self.delivery.configured
        ):
            issues.append(
                PreflightIssue(
                    "DELIVERY_NOT_CONFIGURED",
                    "Instagram fetches the video from a public URL and no delivery bucket is configured.",
                    PublishState.BLOCKED,
                    {"missing": self.delivery.missing()},
                )
            )

        if not _vault_ready(self.vault):
            issues.append(
                PreflightIssue(
                    "VAULT_UNAVAILABLE",
                    "The credential vault is not available, so no provider can be called.",
                    PublishState.BLOCKED,
                )
            )

        report = PreflightReport(job_id=job.id, ok=not issues, issues=tuple(issues))
        if issues:
            self._settle_blocked(job, issues[0])
        return report

    # ------------------------------------------------------------------- execute
    def execute(self, job_id: str) -> dict[str, Any]:
        """Synchronous entry point for the queue handler. Runs the async flow in this thread."""
        return asyncio.run(self.execute_async(job_id))

    async def execute_async(self, job_id: str) -> dict[str, Any]:
        job = self.session.get(PublishJob, job_id)
        if job is None:
            raise PublishingError("JOB_NOT_FOUND", "publish job not found", http_status=404, detail={"id": job_id})
        state = _state(job)
        if state in {PublishState.PUBLISHED, PublishState.CANCELLED}:
            return {"skipped": "terminal", "state": state.value}
        if state in {PublishState.UPLOADING, PublishState.UPLOADED, PublishState.PROCESSING} and not _lease_free(job):
            # Another worker holds the lease: a second concurrent attempt is exactly what must not
            # happen, so this one declines instead of racing.
            return {"skipped": "lease_held", "state": state.value}

        report = self.preflight(job)
        if not report.ok:
            blocker = report.issues[0]
            return {"blocked": blocker.code, "state": str(job.job_state)}

        self._claim(job)
        try:
            return await self._run(job)
        finally:
            self._release(job)

    async def _run(self, job: PublishJob) -> dict[str, Any]:
        variant = self.session.get(PlatformVariant, job.variant_id)
        account = self.session.get(SocialAccount, job.account_id)
        assert variant is not None and account is not None
        mode = _mode(job)
        platform = _platform(job)

        if _state(job) in {PublishState.APPROVED, PublishState.SCHEDULED}:
            self._move(job, PublishState.READY, reason="approved")

        attempt = self._start_attempt(job)
        try:
            record = self.approvals.consume(job)
        except PublishingError as error:
            self._finish_attempt(attempt, outcome="approval_refused", error=error)
            self._settle(job, PublishState.REQUIRES_USER_ACTION, error.code, error.message)
            return {"refused": error.code, "state": str(job.job_state)}

        self._move(job, PublishState.UPLOADING, reason="publishing")
        try:
            token = await TokenService(self.session, self.vault).access_token(account)
        except SocialError as error:
            self._finish_attempt(attempt, outcome="token_failed", code=error.code)
            self._settle(job, PublishState.REQUIRES_USER_ACTION, error.code, error.message)
            return {"failed": error.code, "state": str(job.job_state)}

        source = self.composer.select_source(job.asset_id, platform=platform, mode=mode)
        if source is None:
            self._finish_attempt(attempt, outcome="media_missing", code="NO_MEDIA")
            self._settle(job, PublishState.FAILED, "NO_MEDIA", "The destination has no media.")
            return {"failed": "NO_MEDIA"}

        ctx = self._context(job, account, source, token, mode=mode, platform=platform)
        try:
            if mode is PublishMode.REELS and platform is Platform.INSTAGRAM:
                ctx = await self._deliver(job, ctx, source)
            publisher = self._publisher(job, mode)
            result = await self._publish(publisher, ctx, job, attempt)
        except PublisherError as error:
            await self._seal_pending_upload()
            self._finish_attempt(attempt, outcome="failed", error=error)
            self._settle_from_error(job, error)
            return {"failed": error.code, "state": str(job.job_state), "category": error.category.value}
        except Exception as error:  # noqa: BLE001 - anything unexpected is still recorded, not lost
            self._finish_attempt(attempt, outcome="crashed", code=type(error).__name__)
            self._settle(
                job,
                PublishState.UNKNOWN_REMOTE_STATE,
                "EXECUTOR_CRASHED",
                f"{type(error).__name__}: {error}"[:300],
            )
            return {"crashed": type(error).__name__, "state": str(job.job_state)}

        await self._seal_pending_upload()
        self._finish_attempt(attempt, outcome="published", code=result.state)
        self._settle_published(job, variant, result, record=record)
        return {
            "published": True,
            "state": str(job.job_state),
            "provider_media_id": job.provider_media_id,
            "permalink": job.provider_permalink,
        }

    # -------------------------------------------------------------------- pieces
    def _publisher(self, job: PublishJob, mode: PublishMode) -> Publisher:
        key = (str(job.platform), mode)
        existing = self.publishers.get(key)
        if existing is not None:
            return existing
        if key == ("facebook", PublishMode.VIDEO):
            return FacebookVideoPublisher(sleep=self._sleep)
        if key == ("instagram", PublishMode.REELS):
            return InstagramReelsPublisher(sleep=self._sleep)
        if key == ("tiktok", PublishMode.DIRECT_POST):
            return TikTokPublisher(
                sleep=self._sleep,
                on_upload_session=lambda publish_id, url: self._seal_upload_url(job, publish_id, url),
            )
        if key == ("tiktok", PublishMode.INBOX):
            return TikTokInboxPublisher(
                sleep=self._sleep,
                on_upload_session=lambda publish_id, url: self._seal_upload_url(job, publish_id, url),
            )
        raise PublishingError(
            "NO_PUBLISHER",
            f"there is no publisher for {key[0]}/{mode.value}",
            http_status=409,
            detail={"platform": key[0], "mode": mode.value},
        )

    def _context(
        self,
        job: PublishJob,
        account: SocialAccount,
        source: Any,
        token: str,
        *,
        mode: PublishMode,
        platform: Platform,
    ) -> PublishContext:
        storage = MediaStorage()
        path: str | None = None
        if source.derivative_id:
            derivative = self.session.get(MediaDerivative, source.derivative_id)
            if derivative is not None and derivative.rel_path:
                path = str(storage.resolve_stored(derivative.rel_path))
        else:
            asset = self.session.get(MediaAsset, source.asset_id)
            if asset is not None:
                path = str(storage.resolve_stored(asset.source_path))

        extra_hosts: tuple[str, ...] = ()
        config = self.delivery.load_config()
        if config is not None and config.public_base_url:
            host = host_of(config.public_base_url)
            extra_hosts = (host,) if host else ()
        elif config is not None:
            extra_hosts = (config.host,)

        return PublishContext(
            job_id=job.id,
            account_provider_id=account.provider_account_id,
            access_token=token,
            mode=mode,
            caption=job.caption or "",
            title=job.title,
            hashtags=tuple(job.hashtags_json or []),
            options=dict(job.options_json or {}),
            media_path=path,
            media_sha256=source.sha256,
            media_bytes=source.size_bytes,
            duration_ms=source.probe.get("duration_ms"),
            width=(source.probe.get("video") or {}).get("width"),
            height=(source.probe.get("video") or {}).get("height"),
            api_version=account.api_version or (job.metadata_json or {}).get("api_version"),
            display_timezone=job.display_timezone,
            scheduled_at=job.scheduled_at_utc,
            resume_from=self._resume_state(job),
            allowed_url_hosts=extra_hosts,
            upload_url=self._unseal_upload_url(job),
        )

    async def _deliver(self, job: PublishJob, ctx: PublishContext, source: Any) -> PublishContext:
        """Put the bytes somewhere Instagram can fetch them, and record that fact."""
        if not ctx.media_path:
            raise PublisherError(
                PublishErrorCategory.MEDIA, "The media file is missing on disk.", code="MEDIA_MISSING"
            )
        try:
            receipt, object_key = await self.delivery.ensure_uploaded(
                ctx.media_path, sha256=source.sha256, size_bytes=source.size_bytes
            )
            url = await self.delivery.public_url(object_key)
        except Exception as error:  # DeliveryError and vault failures both end the same way here
            code = getattr(error, "code", "DELIVERY_FAILED")
            message = getattr(error, "message", str(error))
            raise PublisherError(
                PublishErrorCategory.MEDIA
                if str(code).startswith("DELIVERY_") and "NOT_CONFIGURED" not in str(code)
                else PublishErrorCategory.PERMANENT,
                f"The media could not be delivered: {message}"[:300],
                code=str(code),
            ) from error
        self.delivery.record_delivery(job, receipt=receipt, url=url)
        job.upload_bytes_total = receipt.bytes
        job.upload_bytes_done = receipt.bytes
        self.session.flush()
        from dataclasses import replace

        return replace(ctx, media_url=url.url)

    async def _publish(
        self, publisher: Publisher, ctx: PublishContext, job: PublishJob, attempt: PublishAttempt
    ) -> PublisherResult:
        def on_progress(sent: int, total: int) -> None:
            job.upload_bytes_total = total
            job.upload_bytes_done = sent
            self.session.flush()

        result = await publisher.publish(ctx, on_progress=on_progress)
        if result.state in {"processing", "uploaded"} and hasattr(publisher, "poll_until_ready"):
            status = await publisher.poll_until_ready(
                ctx,
                result.provider_object_id,
                attempts=self.status_attempts,
                delay_s=self.status_delay_s,
            )
            if status.failed:
                raise PublisherError(
                    PublishErrorCategory.PROVIDER_PROCESSING,
                    f"The provider could not process the video ({status.state}).",
                    code="PROVIDER_PROCESSING_FAILED",
                    provider_object_id=result.provider_object_id,
                )
            if status.finished:
                from dataclasses import replace

                result = replace(
                    result,
                    permalink=status.permalink or result.permalink,
                    state="published",
                    provider_status=status.state,
                )
        return result

    # ------------------------------------------------------------------- state
    def _settle_published(
        self,
        job: PublishJob,
        variant: PlatformVariant,
        result: PublisherResult,
        *,
        record: Any,
    ) -> None:
        job.provider_media_id = result.provider_object_id
        if result.permalink:
            job.provider_permalink = result.permalink
        job.provider_published_at = result.published_at or self.clock()
        job.remote_checked_at = self.clock()
        job.remote_status_json = {
            "state": result.state,
            "provider_status": result.provider_status,
            "snapshot": dict(result.snapshot),
        }
        job.error_category = None
        job.last_error_code = None
        job.last_error_detail = None
        if result.state == "inbox":
            # The human still has to finish it in the TikTok app: that is not a publication.
            self._advance(job, PublishState.UPLOADED, reason="uploaded_to_inbox")
        else:
            self._advance(job, PublishState.PUBLISHED, reason="published")
            variant.approval_status = ApprovalStatus.APPROVED
            self._complete_project(variant)
        self.audit.record(
            "publish.published",
            resource_type="publish_job",
            resource_id=job.id,
            metadata={
                "provider_media_id": job.provider_media_id,
                "state": result.state,
                "approval_record_id": getattr(record, "id", None),
                "exactly_once": "not claimed",
            },
        )

    def _complete_project(self, variant: PlatformVariant) -> None:
        project = variant.project
        if project is None:
            return
        variants = list(project.variants)
        states = {
            _state(job)
            for job in self.session.scalars(
                sa.select(PublishJob).where(
                    PublishJob.variant_id.in_([item.id for item in variants] or [""])
                )
            )
        }
        if states and states <= {PublishState.PUBLISHED}:
            project.status = ProjectStatus.PUBLISHED
        elif PublishState.PUBLISHED in states:
            project.status = ProjectStatus.PARTIALLY_PUBLISHED

    def _settle_from_error(self, job: PublishJob, error: PublisherError) -> None:
        if error.provider_object_id:
            job.provider_media_id = error.provider_object_id
        job.error_category = error.category
        job.last_error_code = error.code
        job.last_error_detail = error.message[:2000]
        if error.category is PublishErrorCategory.AMBIGUOUS:
            self._settle(job, PublishState.UNKNOWN_REMOTE_STATE, error.code, error.message)
            return
        if error.category in {PublishErrorCategory.AUTHORIZATION, PublishErrorCategory.PERMISSION}:
            self._settle(job, PublishState.REQUIRES_USER_ACTION, error.code, error.message)
            return
        if error.category in {PublishErrorCategory.TRANSIENT, PublishErrorCategory.RATE_LIMIT}:
            if (job.attempts or 0) < MAX_PUBLISH_ATTEMPTS:
                # Stay in `uploading`: the queue retries, and the publisher resumes from the
                # offsets it already reported.
                job.next_attempt_at = self.clock()
                job.state_reason = f"retry_pending:{error.code}"
                self.session.flush()
                return
            self._settle(job, PublishState.FAILED, error.code, error.message)
            return
        self._settle(job, PublishState.FAILED, error.code, error.message)

    def _settle_blocked(self, job: PublishJob, issue: PreflightIssue) -> None:
        if issue.code == "APPROVAL_REQUIRED":
            # Not approved is not the same as blocked: the destination keeps whatever state it had and
            # the report explains what is missing.
            job.state_reason = issue.code[:300]
            self.session.flush()
            return
        self._settle(job, issue.state, issue.code, issue.message)

    def _settle(self, job: PublishJob, state: PublishState, code: str, message: str) -> None:
        job.error_category = job.error_category or None
        job.last_error_code = code[:80]
        job.last_error_detail = message[:2000]
        if _state(job) is state:
            job.state_reason = code[:300]
            self.session.flush()
            return
        if not can_transition(_state(job), state):
            # A state the machine refuses is still recorded, as an observation rather than a claim.
            job.state_reason = f"{code} (from {_state(job).value})"[:300]
            self.session.flush()
            return
        self._move(job, state, reason=code)
        self.audit.record(
            "publish.settled",
            resource_type="publish_job",
            resource_id=job.id,
            outcome="success" if state is PublishState.PUBLISHED else "denied",
            metadata={"state": state.value, "code": code},
        )

    def _move(self, job: PublishJob, state: PublishState, *, reason: str) -> None:
        current = _state(job)
        if current is state:
            return
        if not can_transition(current, state):
            raise PublishingError(
                "ILLEGAL_STATE_TRANSITION",
                f"cannot move a destination from {current.value} to {state.value}",
                http_status=409,
                detail={"from": current.value, "to": state.value},
            )
        job.job_state = state
        job.state_reason = reason[:300]
        self.session.flush()

    def _advance(self, job: PublishJob, target: PublishState, *, reason: str) -> None:
        """Walk the state machine to `target`, one legal step at a time.

        A publisher that finishes in one call still passes through `uploaded` and `processing`, and
        the journal says so: the history of a destination should describe what happened, not just
        where it ended up.
        """
        path = shortest_path(_state(job), target)
        if path is None:
            raise PublishingError(
                "ILLEGAL_STATE_TRANSITION",
                f"cannot move a destination from {_state(job).value} to {target.value}",
                http_status=409,
                detail={"from": _state(job).value, "to": target.value},
            )
        for step in path:
            self._move(job, step, reason=reason)

    def _claim(self, job: PublishJob) -> None:
        job.lease_owner = "publish-worker"
        job.lease_expires_at = self.clock() + _lease_delta()
        self.session.flush()

    def _release(self, job: PublishJob) -> None:
        job.lease_owner = None
        job.lease_expires_at = None
        self.session.flush()

    def _start_attempt(self, job: PublishJob) -> PublishAttempt:
        attempt = PublishAttempt(
            job_id=job.id,
            attempt_no=(job.attempts or 0) + 1,
            started_at=self.clock(),
            outcome="in_progress",
            provider_operation="publish",
            remote_snapshot_json={},
        )
        self.session.add(attempt)
        job.attempts = (job.attempts or 0) + 1
        self.session.flush()
        return attempt

    def _finish_attempt(
        self,
        attempt: PublishAttempt,
        *,
        outcome: str,
        code: str | None = None,
        error: Exception | None = None,
    ) -> None:
        attempt.outcome = outcome[:40]
        attempt.finished_at = self.clock()
        if error is not None:
            attempt.response_code = getattr(error, "code", type(error).__name__)[:120]
            attempt.error_category = getattr(error, "category", None)
            attempt.response_excerpt_redacted = str(
                getattr(error, "message", str(error))
            )[:500]
        elif code:
            attempt.response_code = code[:120]
        self.session.flush()

    # ----------------------------------------------------------------- secrets
    def _seal_upload_url(self, job: PublishJob, publish_id: str, url: str) -> None:
        """Called by the TikTok publisher the moment it receives an upload URL.

        The id goes to the journal immediately (it is the anchor for reconciliation); the URL is only
        held in memory here, because sealing it is an async vault write and this callback runs inside
        a coroutine. The URL is never stored in the journal, never logged and never returned.
        """
        job.provider_upload_session_id = publish_id[:190]
        self._pending_upload = {"publish_id": publish_id, "url": url}
        self.session.flush()

    async def _seal_pending_upload(self) -> None:
        pending = self._pending_upload
        self._pending_upload = {}
        url = pending.get("url")
        if not url:
            return
        if not _vault_ready(self.vault):
            # Without a vault the URL cannot be kept; a resume will start a new TikTok session, which
            # publishes nothing on its own. Leaking it into the journal instead is not an option.
            return
        alias = upload_alias(str(pending.get("publish_id") or url_alias_seed(url)))
        try:
            record = await self.vault.put(
                secret_id=_new_secret_id(),
                value=url,
                purpose=UPLOAD_URL_PURPOSE,
                provider="tiktok",
                alias=alias,
            )
        except VaultError as error:
            log.warning("could not seal an upload url: %s", error.code)
            return
        if self.session.get(SecretRef, record.id) is None:
            self.session.add(
                SecretRef(
                    id=record.id,
                    provider="tiktok",
                    key_alias=alias,
                    fingerprint=record.fingerprint,
                    cipher="dpapi",
                    dpapi_scope="user",
                    purpose=UPLOAD_URL_PURPOSE,
                    schema_version=record.version,
                    status=SecretStatus.ACTIVE,
                    byte_size=record.bytes,
                )
            )
            self.session.flush()

    def _unseal_upload_url(self, job: PublishJob) -> str | None:
        ref = self.session.scalar(
            sa.select(SecretRef).where(
                SecretRef.key_alias == upload_alias(job.provider_upload_session_id or ""),
                SecretRef.purpose == UPLOAD_URL_PURPOSE,
                SecretRef.status == SecretStatus.ACTIVE,
            )
        )
        if ref is None or not _vault_ready(self.vault):
            return None
        try:
            return asyncio.run(self.vault.get(ref.id, UPLOAD_URL_PURPOSE))
        except (VaultError, RuntimeError):
            return None

    def _resume_state(self, job: PublishJob) -> dict[str, str]:
        """What the provider already gave us, so a retry resumes instead of starting over."""
        resume: dict[str, str] = {}
        if job.remote_container_id:
            resume["container_id"] = job.remote_container_id
            resume["video_id"] = job.remote_container_id
            resume["creation_id"] = job.remote_container_id
        if job.provider_upload_session_id:
            resume["upload_session_id"] = job.provider_upload_session_id
            resume["publish_id"] = job.provider_upload_session_id
        if job.provider_media_id:
            resume["media_id"] = job.provider_media_id
        if job.upload_bytes_done:
            resume["upload_offset"] = str(job.upload_bytes_done)
            resume["upload_end_offset"] = str(job.upload_bytes_total or job.upload_bytes_done)
        return resume


# --------------------------------------------------------------------------- helpers


def upload_alias(publish_id: str) -> str:
    """The sealed upload session is found by publish id, which the journal does keep."""
    return f"upload:{publish_id}"[:160]


def url_alias_seed(url: str) -> str:
    """Fallback alias when a publisher reported a URL but no id: never the URL itself."""
    import hashlib

    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _new_secret_id() -> str:
    from ...core.ids import new_ulid

    return new_ulid()


def _lease_delta():
    from datetime import timedelta

    return timedelta(minutes=10)


def shortest_path(start: PublishState, target: PublishState) -> list[PublishState] | None:
    """Breadth-first over the transition table. Returns the states to visit, excluding `start`."""
    if start is target:
        return []
    seen = {start}
    queue: list[tuple[PublishState, list[PublishState]]] = [(start, [])]
    while queue:
        current, trail = queue.pop(0)
        for nxt in sorted(PUBLISH_TRANSITIONS.get(current, frozenset()), key=lambda item: item.value):
            if nxt is current or nxt in seen:
                continue
            if nxt is target:
                return [*trail, nxt]
            seen.add(nxt)
            queue.append((nxt, [*trail, nxt]))
    return None


def _state(job: PublishJob) -> PublishState:
    return job.job_state if isinstance(job.job_state, PublishState) else PublishState(str(job.job_state))


def _mode(job: PublishJob) -> PublishMode:
    return job.publish_mode if isinstance(job.publish_mode, PublishMode) else PublishMode(str(job.publish_mode))


def _platform(job: PublishJob) -> Platform:
    return Platform(str(job.platform))


def _vault_ready(vault: Vault | None) -> bool:
    return bool(vault is not None and getattr(vault, "available", False))


def _lease_free(job: PublishJob) -> bool:
    if not job.lease_owner or job.lease_expires_at is None:
        return True
    expiry = job.lease_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry < utcnow()


# --------------------------------------------------------------------------- queue


def build_publish_handlers(
    session_factory: Callable[[], Session],
    *,
    vault_factory: Callable[[], Vault] | None = None,
    delivery_factory: Callable[[Session, Vault], DeliveryService] | None = None,
    **executor_kwargs: Any,
) -> dict[str, Callable[[Session, Any], Any]]:
    """The queue handler. The only way a publish ever runs."""

    def handle_publish(session: Session, job: Any) -> dict[str, Any]:
        job_id = str((job.payload or {}).get("job_id") or "")
        if not job_id:
            raise ValueError("publish.destination requires a job_id in its payload")
        vault = (vault_factory or vault_from_env)()
        delivery = delivery_factory(session, vault) if delivery_factory else None
        executor = PublishExecutor(
            session, vault=vault, delivery=delivery, **executor_kwargs
        )
        return executor.execute(job_id)

    return {JOB_TYPE: handle_publish}


def delivery_is_recorded(session: Session, job_id: str) -> list[PublishDelivery]:
    return list(
        session.scalars(
            sa.select(PublishDelivery)
            .where(PublishDelivery.job_id == job_id, PublishDelivery.state != DeliveryAssetState.DELETED)
            .order_by(PublishDelivery.created_at)
        )
    )
