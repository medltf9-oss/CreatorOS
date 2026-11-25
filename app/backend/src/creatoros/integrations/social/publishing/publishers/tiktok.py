"""TikTok publishing: Direct Post and Inbox.

TikTok's API allocates a `publish_id` and hands back a one-time `upload_url`; the bytes go to that
URL, not to the API host. Three consequences shape this module:

* **The upload URL is a credential.** It is a signed URL with its authorisation in the query string.
  It is never returned in a result, never put in an error detail, and never written to the journal; the
  executor seals it in the vault through `on_upload_session` and hands it back in
  `PublishContext.upload_url` when a job resumes.
* **Chunking is decided by the documented ladder**, not by our own preference: one chunk under 64 MB,
  ten-megabyte chunks above it, with the last chunk absorbing the remainder. Resuming recomputes the
  same boundaries from the file size, so the ranges stay consistent.
* **Direct Post and Inbox are different products.** Direct Post publishes when the upload completes and
  needs an explicit `privacy_level` the creator actually has. Inbox uploads a draft the human finishes
  in the TikTok app, so it deliberately carries no privacy level and reports `inbox`, not `published`.

The creator's own `creator_info` is queried before every direct post: it is the only authority on
which privacy levels exist for this account and on the maximum duration it may post.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .....db.enums import PublishErrorCategory, PublishMode
from ....social import config as social_config
from ....social.base import HttpResponse, HttpxTransport, Transport
from ..security import validate_provider_url
from .base import PublishContext, Publisher, PublisherError, PublisherResult, RemoteStatus

MIN_CHUNK_BYTES = 5 * 1024 * 1024
SINGLE_CHUNK_LIMIT = 64 * 1024 * 1024
LARGE_CHUNK_BYTES = 10 * 1024 * 1024
MAX_CHUNK_BYTES = 128 * 1024 * 1024

STATUS_ATTEMPTS = 30
STATUS_DELAY_S = 5.0

#: post_info keys that TikTok understands, per mode.
DIRECT_POST_INFO_KEYS = (
    "disable_comment",
    "disable_duet",
    "disable_stitch",
    "video_cover_timestamp_ms",
    "is_aigc",
    "brand_content_toggle",
    "brand_organic_toggle",
)

_TIKTOK_CATEGORY = {
    "rate_limit_exceeded": PublishErrorCategory.RATE_LIMIT,
    "internal_error": PublishErrorCategory.TRANSIENT,
    "access_token_invalid": PublishErrorCategory.AUTHORIZATION,
    "access_token_expired": PublishErrorCategory.AUTHORIZATION,
    "scope_not_authorized": PublishErrorCategory.PERMISSION,
    "scope_permission_missed": PublishErrorCategory.PERMISSION,
    "spam_risk_too_many_posts": PublishErrorCategory.RATE_LIMIT,
    "spam_risk_user_banned_from_posting": PublishErrorCategory.PERMISSION,
    "spam_risk_text": PublishErrorCategory.VALIDATION,
    "invalid_params": PublishErrorCategory.VALIDATION,
    "invalid_request": PublishErrorCategory.VALIDATION,
    "video_pull_failed": PublishErrorCategory.MEDIA,
    "video_format_check_failed": PublishErrorCategory.MEDIA,
    "duration_check_failed": PublishErrorCategory.VALIDATION,
    "frame_rate_check_failed": PublishErrorCategory.VALIDATION,
    "picture_size_check_failed": PublishErrorCategory.MEDIA,
    "unaudited_client_can_only_post_to_private_accounts": PublishErrorCategory.PERMISSION,
}


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    chunk_size: int
    count: int

    def bounds(self, index: int, size: int) -> tuple[int, int]:
        start = index * self.chunk_size
        end = size - 1 if index == self.count - 1 else start + self.chunk_size - 1
        return start, end


def plan_chunks(size: int) -> ChunkPlan:
    """TikTok's documented ladder: one chunk under 64 MB, ten-megabyte chunks above it."""
    if size <= SINGLE_CHUNK_LIMIT:
        return ChunkPlan(chunk_size=max(1, size), count=1)
    count = max(1, size // LARGE_CHUNK_BYTES)
    chunk_size = size // count
    if chunk_size > MAX_CHUNK_BYTES:  # pragma: no cover - size // count cannot exceed this
        count = size // MAX_CHUNK_BYTES + 1
        chunk_size = size // count
    return ChunkPlan(chunk_size=chunk_size, count=count)


def _read_range(path: str, start: int, end: int) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(start)
        return handle.read(end - start + 1)


class TikTokPublisher(Publisher):
    platform = "tiktok"
    mode = PublishMode.DIRECT_POST

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        on_upload_session: Callable[[str, str], None] | None = None,
        sleep: Callable[[float], Any] | None = None,
        status_attempts: int = STATUS_ATTEMPTS,
        status_delay_s: float = STATUS_DELAY_S,
    ) -> None:
        self.transport: Transport = transport or HttpxTransport("tiktok")
        self._on_upload_session = on_upload_session
        self._sleep = sleep
        self.status_attempts = max(1, status_attempts)
        self.status_delay_s = status_delay_s

    # ------------------------------------------------------------------ helpers
    @property
    def post_mode(self) -> str:
        return "DIRECT_POST" if self.mode is PublishMode.DIRECT_POST else "MEDIA_UPLOAD"

    def _base(self, ctx: PublishContext) -> str:
        return social_config.TIKTOK_API_URL.format(version=ctx.api_version or social_config.DEFAULT_TIKTOK_VERSION)

    def _version(self, ctx: PublishContext) -> str:
        return ctx.api_version or social_config.DEFAULT_TIKTOK_VERSION

    def _headers(self, ctx: PublishContext) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ctx.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    @staticmethod
    def _envelope(response: HttpResponse, operation: str, **context: Any) -> dict:
        body = response.body or {}
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = str(error.get("code") or "").lower()
        if response.status == 200 and code in {"", "ok"}:
            return body.get("data") if isinstance(body.get("data"), dict) else {}
        raise TikTokPublisher._error(code or f"http_{response.status}", error, response, operation, **context)

    @staticmethod
    def _error(
        code: str, error: dict, response: HttpResponse, operation: str, **context: Any
    ) -> PublisherError:
        category = _TIKTOK_CATEGORY.get(
            code, PublishErrorCategory.RATE_LIMIT if response.status == 429 else PublishErrorCategory.PERMANENT
        )
        if response.status >= 500:
            category = PublishErrorCategory.TRANSIENT
        return PublisherError(
            category,
            str(error.get("message") or f"TikTok returned HTTP {response.status}")[:300],
            code=f"TIKTOK_{code}",
            http_status=response.status,
            retry_after_s=response.retry_after(),
            provider_object_id=context.get("publish_id"),
            detail={"operation": operation, "tiktok_code": code, "log_id": error.get("log_id")},
        )

    # -------------------------------------------------------------- creator info
    async def creator_info(self, ctx: PublishContext) -> dict:
        response = await self.transport.request(
            "POST",
            social_config.TIKTOK_CREATOR_INFO_URL.format(version=self._version(ctx)),
            headers=self._headers(ctx),
            json={},
        )
        data = self._envelope(response, "creator_info")
        if not data.get("privacy_level_options"):
            raise PublisherError(
                PublishErrorCategory.PERMISSION,
                "TikTok did not report any privacy level for this creator; Direct Post cannot proceed.",
                code="CREATOR_INFO_INCOMPLETE",
            )
        return data

    # ------------------------------------------------------------------ publish
    async def publish(
        self,
        ctx: PublishContext,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> PublisherResult:
        publish_id = ctx.object_id("publish_id")
        upload_url = ctx.upload_url
        info = await self.creator_info(ctx)

        if not publish_id or not upload_url:
            publish_id, upload_url = await self._init(ctx, info)
        if upload_url is not None:
            self._check_upload_url(upload_url)
        await self._upload(ctx, publish_id, upload_url, on_progress=on_progress)
        status = await self._wait_for_status(ctx, publish_id)
        return self._result(ctx, publish_id, status, info)

    async def _init(self, ctx: PublishContext, info: dict) -> tuple[str, str]:
        post_info: dict[str, Any] = {}
        if self.mode is PublishMode.DIRECT_POST:
            privacy = str(ctx.options.get("privacy_level") or "")
            allowed = list(info.get("privacy_level_options") or [])
            if not privacy:
                raise PublisherError(
                    PublishErrorCategory.VALIDATION,
                    "Direct Post requires an explicit privacy level.",
                    code="PRIVACY_LEVEL_REQUIRED",
                )
            if allowed and privacy not in allowed:
                raise PublisherError(
                    PublishErrorCategory.VALIDATION,
                    "This creator cannot publish with the requested privacy level.",
                    code="PRIVACY_LEVEL_NOT_ALLOWED",
                    detail={"requested": privacy, "allowed": allowed},
                )
            post_info["privacy_level"] = privacy
            for key in DIRECT_POST_INFO_KEYS:
                if key in ctx.options and ctx.options[key] is not None:
                    post_info[key] = ctx.options[key]
        post_info["title"] = ctx.text

        size = ctx.media_bytes or (os.path.getsize(ctx.media_path) if ctx.media_path else 0)
        if size <= 0:
            raise PublisherError(
                PublishErrorCategory.MEDIA, "The video is empty or missing.", code="MEDIA_MISSING"
            )
        plan = plan_chunks(size)
        payload = {
            "post_info": post_info,
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": plan.chunk_size,
                "total_chunk_count": plan.count,
            },
            "post_mode": self.post_mode,
            "media_type": "VIDEO",
        }
        response = await self.transport.request(
            "POST",
            f"{self._base(ctx)}/post/publish/video/init/",
            headers=self._headers(ctx),
            json=payload,
        )
        data = self._envelope(response, "init")
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise PublisherError(
                PublishErrorCategory.PROVIDER_PROCESSING,
                "TikTok did not return a publish id and upload url.",
                code="PUBLISH_ID_MISSING",
            )
        if self._on_upload_session is not None:
            # Hand the URL to the caller to seal; it never appears in a result or an error.
            self._on_upload_session(publish_id, upload_url)
        return publish_id, upload_url

    def _check_upload_url(self, url: str) -> None:
        verdict = validate_provider_url(url)
        if not verdict:
            raise PublisherError(
                PublishErrorCategory.PROVIDER_PROCESSING,
                f"TikTok returned an upload URL that is not acceptable: {verdict.reason}.",
                code="UPLOAD_URL_REJECTED",
                detail={"reason": verdict.reason},
            )

    async def _upload(
        self,
        ctx: PublishContext,
        publish_id: str,
        upload_url: str | None,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if not upload_url:
            raise PublisherError(
                PublishErrorCategory.PROVIDER_PROCESSING,
                "The upload URL for this publish session is no longer available; start a new session.",
                code="UPLOAD_URL_MISSING",
                provider_object_id=publish_id,
            )
        if not ctx.media_path or not os.path.exists(ctx.media_path):
            raise PublisherError(
                PublishErrorCategory.MEDIA, "The video file is not available on disk.", code="MEDIA_MISSING"
            )
        size = ctx.media_bytes or os.path.getsize(ctx.media_path)
        plan = plan_chunks(size)
        start_index = int(ctx.object_id("upload_chunk_index") or 0)
        for index in range(start_index, plan.count):
            start_byte, end_byte = plan.bounds(index, size)
            chunk = await asyncio.to_thread(_read_range, ctx.media_path, start_byte, end_byte)
            response = await self.transport.request(
                "PUT",
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start_byte}-{end_byte}/{size}",
                },
                content=chunk,
            )
            if response.status not in (200, 201, 206):
                if response.status in (401, 403):
                    raise PublisherError(
                        PublishErrorCategory.TRANSIENT,
                        "The upload URL expired or was refused; a new session is required.",
                        code="UPLOAD_URL_EXPIRED",
                        http_status=response.status,
                        provider_object_id=publish_id,
                        detail={"chunk_index": index},
                    )
                raise PublisherError(
                    PublishErrorCategory.TRANSIENT,
                    f"TikTok's upload endpoint returned HTTP {response.status}.",
                    code="UPLOAD_FAILED",
                    http_status=response.status,
                    provider_object_id=publish_id,
                    detail={"chunk_index": index},
                )
            if on_progress is not None:
                on_progress(min(end_byte + 1, size), size)

    async def _wait_for_status(self, ctx: PublishContext, publish_id: str) -> dict:
        data = await self._fetch_status(ctx, publish_id)
        remaining = self.status_attempts - 1
        while _state_of(data) in {"processing_upload", "processing_download"} and remaining > 0:
            await self._sleep_for(self.status_delay_s)
            data = await self._fetch_status(ctx, publish_id)
            remaining -= 1
        state = _state_of(data)
        if state == "failed":
            reason = str(data.get("fail_reason") or "unknown")
            raise PublisherError(
                _TIKTOK_CATEGORY.get(reason, PublishErrorCategory.PROVIDER_PROCESSING),
                f"TikTok failed to publish the video ({reason}).",
                code="PUBLISH_FAILED",
                provider_object_id=publish_id,
                detail={"fail_reason": reason},
            )
        if state not in {"published", "inbox"}:
            raise PublisherError(
                PublishErrorCategory.TRANSIENT,
                "TikTok is still processing the upload; the same publish id can be polled again.",
                code="PUBLISH_NOT_FINISHED",
                provider_object_id=publish_id,
            )
        return data

    async def _fetch_status(self, ctx: PublishContext, publish_id: str) -> dict:
        response = await self.transport.request(
            "POST",
            f"{self._base(ctx)}/post/publish/status/fetch/",
            headers=self._headers(ctx),
            json={"publish_id": publish_id},
        )
        return self._envelope(response, "status", publish_id=publish_id)

    def _result(self, ctx: PublishContext, publish_id: str, data: dict, info: dict) -> PublisherResult:
        state = _state_of(data)
        post_ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
        post_id = str(post_ids[0]) if isinstance(post_ids, list) and post_ids else None
        username = info.get("creator_username")
        permalink = (
            f"https://www.tiktok.com/@{username}/video/{post_id}" if post_id and username else None
        )
        return PublisherResult(
            provider_object_id=post_id or publish_id,
            permalink=permalink,
            state="published" if state == "published" else "inbox",
            provider_status=state,
            snapshot={
                "publish_id": publish_id,
                "post_id": post_id,
                "post_mode": self.post_mode,
                "creator_username": username,
                "in_inbox": state == "inbox",
            },
        )

    async def _sleep_for(self, seconds: float) -> None:
        if self._sleep is None:
            await asyncio.sleep(seconds)
        else:
            await self._sleep(seconds)

    # ------------------------------------------------------------------- status
    async def check_status(self, ctx: PublishContext, provider_object_id: str) -> RemoteStatus:
        publish_id = ctx.object_id("publish_id") or provider_object_id
        data = await self._fetch_status(ctx, publish_id)
        state = _state_of(data)
        return RemoteStatus(
            provider_object_id=publish_id,
            state=state,
            detail=str(data.get("fail_reason") or "") or None,
            finished=state in {"published", "inbox"},
            failed=state == "failed",
        )


class TikTokInboxPublisher(TikTokPublisher):
    """Inbox upload: the video lands as a draft the human finishes in the app."""

    mode = PublishMode.INBOX

    async def publish(
        self,
        ctx: PublishContext,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> PublisherResult:
        return await super().publish(ctx, on_progress=on_progress)


def _state_of(data: dict) -> str:
    raw = str(data.get("status") or "").upper()
    return {
        "PROCESSING_UPLOAD": "processing_upload",
        "PROCESSING_DOWNLOAD": "processing_download",
        "PUBLISH_COMPLETE": "published",
        "SEND_TO_USER_INBOX": "inbox",
        "FAILED": "failed",
    }.get(raw, raw.lower() or "unknown")
