"""Facebook Page video publishing (chunked, resumable, status-polled).

The Graph video API is a three-phase session rather than a single upload:

    POST /{page-id}/videos  upload_phase=start      -> video_id, upload_session_id, end_offset
    POST /{page-id}/videos  upload_phase=transfer   -> new start_offset / end_offset  (repeat)
    POST /{page-id}/videos  upload_phase=finish     -> the video is created

Two properties are load-bearing here:

* **`video_id` exists from the first call.** Meta allocates the object before the bytes arrive, so
  the id is persisted immediately. If a later step fails ambiguously, the executor has something to
  reconcile against instead of guessing whether a video was created.
* **A transfer failure is resumable.** The offsets come back from the provider, so a retry continues
  from `start_offset` rather than re-sending gigabytes. A failure during `finish` is *not* resumable
  in the same way: the request may have created the video, so it is classified as ambiguous.

We never ask Meta to schedule the post (`scheduled_publish_time`). A provider-side schedule cannot be
cancelled from this application, and the local scheduler already decides when to call. Sending both
would create two sources of truth for "when".
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from .....db.enums import PublishErrorCategory, PublishMode
from ....social import config as social_config
from ....social.base import HttpResponse, HttpxTransport, Transport
from ....social.errors import SocialError
from .base import (
    PublishContext,
    Publisher,
    PublisherError,
    PublisherResult,
    RemoteStatus,
)

CHUNK_SIZE = 4 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024**3
DEFAULT_POLL_ATTEMPTS = 20
DEFAULT_POLL_DELAY_S = 3.0

#: Graph error codes and what they mean for the journal.
_CATEGORY_BY_CODE = {
    "RATE_LIMITED": PublishErrorCategory.RATE_LIMIT,
    "PROVIDER_UNAVAILABLE": PublishErrorCategory.TRANSIENT,
    "NETWORK_ERROR": PublishErrorCategory.TRANSIENT,
    "PROVIDER_TIMEOUT": PublishErrorCategory.TRANSIENT,
    "TOKEN_EXPIRED": PublishErrorCategory.AUTHORIZATION,
    "TOKEN_REVOKED": PublishErrorCategory.AUTHORIZATION,
    "REAUTHORIZATION_REQUIRED": PublishErrorCategory.AUTHORIZATION,
    "SCOPE_NOT_GRANTED": PublishErrorCategory.PERMISSION,
    "APP_REVIEW_REQUIRED": PublishErrorCategory.PERMISSION,
    "ACCOUNT_TYPE_UNSUPPORTED": PublishErrorCategory.PERMISSION,
    "PROVIDER_ERROR": PublishErrorCategory.VALIDATION,
}


def _category(code: str) -> PublishErrorCategory:
    return _CATEGORY_BY_CODE.get(code, PublishErrorCategory.PERMANENT)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _read_chunk(path: str, offset: int, length: int) -> bytes:
    """Slice of the file, read off the event loop: a 4 MiB read is fast but not free."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        return handle.read(length)


class FacebookVideoPublisher(Publisher):
    platform = "facebook"
    mode = PublishMode.VIDEO

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        chunk_size: int = CHUNK_SIZE,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.transport: Transport = transport or HttpxTransport("facebook")
        self.chunk_size = chunk_size
        self._sleep = sleep

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _graph(version: str | None) -> str:
        return social_config.FACEBOOK_GRAPH_URL.format(version=version or social_config.DEFAULT_GRAPH_VERSION)

    def _url(self, ctx: PublishContext, *, path: str = "videos") -> str:
        return f"{self._graph(ctx.api_version)}/{ctx.account_provider_id}/{path}"

    def _raise(self, operation: str, error: SocialError, **kwargs: Any) -> PublisherError:
        category = _category(error.code)
        if category is PublishErrorCategory.TRANSIENT and operation == "finish":
            # A timeout while finishing may still have created the video: nothing about that is
            # safe to retry blindly.
            category = PublishErrorCategory.AMBIGUOUS
        return PublisherError(
            category,
            error.message,
            code=error.code,
            http_status=error.http_status,
            retry_after_s=error.retry_after_s,
            detail={"operation": operation, **kwargs},
        )

    # ------------------------------------------------------------------ publish
    async def publish(
        self,
        ctx: PublishContext,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> PublisherResult:
        if not ctx.media_path or not os.path.exists(ctx.media_path):
            raise PublisherError(
                PublishErrorCategory.MEDIA, "The video file is not available on disk.", code="MEDIA_MISSING"
            )
        total = ctx.media_bytes or os.path.getsize(ctx.media_path)
        if total > MAX_FILE_BYTES:
            raise PublisherError(
                PublishErrorCategory.VALIDATION,
                "Facebook accepts at most 4 GB per video.",
                code="FILE_TOO_LARGE",
                detail={"bytes": total, "max": MAX_FILE_BYTES},
            )

        video_id = ctx.object_id("video_id")
        session_id = ctx.object_id("upload_session_id")
        start_offset = _int(ctx.object_id("upload_offset") or 0)
        end_offset = _int(ctx.object_id("upload_end_offset") or 0)

        if not session_id or not video_id:
            started = await self._start(ctx, total)
            video_id = str(started.get("video_id") or "")
            session_id = str(started.get("upload_session_id") or "")
            start_offset = _int(started.get("start_offset"), 0)
            end_offset = _int(started.get("end_offset"), 0)
            if not video_id or not session_id:
                raise PublisherError(
                    PublishErrorCategory.PROVIDER_PROCESSING,
                    "Facebook did not return a video id and upload session.",
                    code="UPLOAD_SESSION_MISSING",
                    detail={"video_id_present": bool(video_id)},
                )

        # The provider's offset is the loop condition, not a local counter: after a resumed job they
        # differ, and trusting the local one would re-send bytes Meta already has.
        while start_offset < total:
            if end_offset <= start_offset:
                raise PublisherError(
                    PublishErrorCategory.PROVIDER_PROCESSING,
                    "Facebook returned an empty upload window.",
                    code="UPLOAD_WINDOW_INVALID",
                    provider_object_id=video_id,
                    detail={"start_offset": start_offset, "end_offset": end_offset},
                )
            chunk_start = start_offset
            chunk_end = min(end_offset, total)
            try:
                chunk = await asyncio.to_thread(
                    _read_chunk, ctx.media_path, chunk_start, max(1, chunk_end - chunk_start)
                )
                transferred = await self._transfer(
                    ctx,
                    session_id=session_id,
                    start_offset=chunk_start,
                    chunk=chunk,
                    video_id=video_id,
                )
            except SocialError as error:
                # Resumable: the provider keeps the session, and the caller retries from here.
                raise self._raise(
                    "transfer", error, video_id=video_id, sent=chunk_start, total=total
                ) from error
            start_offset = _int(transferred.get("start_offset"), chunk_end)
            end_offset = _int(transferred.get("end_offset"), chunk_end)
            if on_progress is not None:
                on_progress(min(chunk_end, total), total)

        try:
            await self._finish(ctx, session_id=session_id, video_id=video_id)
        except SocialError as error:
            raise self._raise("finish", error, video_id=video_id) from error

        snapshot = {"video_id": video_id, "upload_session_id": session_id, "bytes_sent": total}
        return PublisherResult(
            provider_object_id=video_id,
            permalink=None,
            state="processing",
            provider_status="processing",
            snapshot=snapshot,
        )

    async def _start(self, ctx: PublishContext, total: int) -> dict:
        response = await self.transport.request(
            "POST",
            self._url(ctx),
            data={
                "upload_phase": "start",
                "file_size": str(total),
                "access_token": ctx.access_token,
            },
        )
        return self._body("start", response)

    async def _transfer(
        self, ctx: PublishContext, *, session_id: str, start_offset: int, chunk: bytes, video_id: str
    ) -> dict:
        response = await self.transport.request(
            "POST",
            self._url(ctx),
            data={
                "upload_phase": "transfer",
                "upload_session_id": session_id,
                "start_offset": str(start_offset),
                "access_token": ctx.access_token,
            },
            files={"video_file_chunk": ("chunk.bin", chunk, "application/octet-stream")},
            timeout=180.0,
        )
        return self._body("transfer", response, video_id=video_id)

    async def _finish(self, ctx: PublishContext, *, session_id: str, video_id: str) -> dict:
        data: dict[str, Any] = {
            "upload_phase": "finish",
            "upload_session_id": session_id,
            "access_token": ctx.access_token,
        }
        if ctx.title:
            data["title"] = ctx.title
        if ctx.text:
            data["description"] = ctx.text
        response = await self.transport.request("POST", self._url(ctx), data=data)
        body = self._body("finish", response, video_id=video_id)
        if body.get("success") is False:
            raise PublisherError(
                PublishErrorCategory.PROVIDER_PROCESSING,
                "Facebook refused to finish the upload.",
                code="UPLOAD_FINISH_FAILED",
                provider_object_id=video_id,
            )
        return body

    def _body(self, operation: str, response: HttpResponse, **context: Any) -> dict:
        if response.status in (200, 201):
            return response.body
        raise self._http_error(operation, response, **context)

    def _http_error(self, operation: str, response: HttpResponse, **context: Any) -> PublisherError:
        error = response.body.get("error") if isinstance(response.body.get("error"), dict) else {}
        message = str(error.get("message") or f"Facebook returned HTTP {response.status}")[:300]
        numeric = _int(error.get("code"))
        category = self._numeric_category(numeric, response.status)
        if category is PublishErrorCategory.TRANSIENT and operation == "finish":
            category = PublishErrorCategory.AMBIGUOUS
        return PublisherError(
            category,
            message,
            code=f"GRAPH_{numeric or response.status}",
            http_status=response.status,
            retry_after_s=response.retry_after(),
            provider_object_id=context.get("video_id"),
            detail={
                "operation": operation,
                "graph_code": numeric,
                "graph_subcode": error.get("error_subcode"),
                **{key: value for key, value in context.items() if key != "video_id"},
            },
        )

    @staticmethod
    def _numeric_category(code: int, status: int) -> PublishErrorCategory:
        if status == 429 or code in {4, 17, 32, 613}:
            return PublishErrorCategory.RATE_LIMIT
        if code in {1, 2} or status >= 500:
            return PublishErrorCategory.TRANSIENT
        if code in {190, 102, 463, 467, 460}:
            return PublishErrorCategory.AUTHORIZATION
        if code in {10, 200, 458, 459}:
            return PublishErrorCategory.PERMISSION
        if code in {100, 9007, 1363030, 1363037, 1363042}:
            return PublishErrorCategory.VALIDATION
        return PublishErrorCategory.PERMANENT

    # ------------------------------------------------------------------- status
    async def check_status(self, ctx: PublishContext, provider_object_id: str) -> RemoteStatus:
        response = await self.transport.request(
            "GET",
            f"{self._graph(ctx.api_version)}/{provider_object_id}",
            params={
                "fields": "status,permalink_url,published",
                "access_token": ctx.access_token,
            },
        )
        if response.status != 200:
            raise self._http_error("status", response, video_id=provider_object_id)
        body = response.body or {}
        status_block = body.get("status") if isinstance(body.get("status"), dict) else {}
        state = str(status_block.get("video_status") or "unknown").lower()
        permalink = body.get("permalink_url")
        failed = state in {"error", "failed"}
        return RemoteStatus(
            provider_object_id=provider_object_id,
            state=state,
            detail=str(status_block.get("processing_phase") or "") or None,
            permalink=permalink,
            finished=state == "ready",
            failed=failed,
        )

    async def poll_until_ready(
        self,
        ctx: PublishContext,
        provider_object_id: str,
        *,
        attempts: int = DEFAULT_POLL_ATTEMPTS,
        delay_s: float = DEFAULT_POLL_DELAY_S,
    ) -> RemoteStatus:
        """Bounded polling. Never loops forever on a provider that forgot to finish processing."""
        status = await self.check_status(ctx, provider_object_id)
        remaining = max(0, attempts - 1)
        while not status.finished and not status.failed and remaining > 0:
            if self._sleep is None:
                await asyncio.sleep(delay_s)
            else:
                await self._sleep(delay_s)
            status = await self.check_status(ctx, provider_object_id)
            remaining -= 1
        return status
