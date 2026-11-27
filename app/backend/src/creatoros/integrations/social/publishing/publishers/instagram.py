"""Instagram Reels publishing, on both login paths.

Instagram republishes nothing: it *fetches* the video from a URL you give it. The flow is therefore
container-based:

    POST /{ig-user-id}/media          media_type=REELS, video_url, caption  -> creation_id
    GET  /{creation_id}               status_code = IN_PROGRESS | FINISHED | ERROR | EXPIRED
    POST /{ig-user-id}/media_publish  creation_id                           -> media_id
    GET  /{media_id}                  permalink, media_type, timestamp      (verification)

The two login paths are kept apart the way Phase 6 kept them apart: the Instagram Login route talks to
`graph.instagram.com` with a directly obtained token, the Facebook Login route talks to
`graph.facebook.com` with a Page-derived token. The host is the only difference in this flow, but it
is a difference that decides which token is valid, so it is explicit rather than inferred.

`media_publish` is the irreversible step. A lost response there means the post may exist, so the
failure is classified ambiguous and the creation id is preserved - the executor reconciles instead of
publishing twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .....db.enums import OAuthRoute, PublishErrorCategory, PublishMode
from ....social import config as social_config
from ....social.base import HttpResponse, HttpxTransport, Transport
from ..security import validate_provider_url
from .base import PublishContext, Publisher, PublisherError, PublisherResult, RemoteStatus

CONTAINER_ATTEMPTS = 20
CONTAINER_DELAY_S = 10.0
MEDIA_FIELDS = "permalink,media_type,timestamp,status_code,caption"

#: Options the Reels API understands. Anything else is dropped by the validator long before this.
PASSTHROUGH_OPTIONS = ("share_to_feed", "thumb_offset", "collaborators", "audio_name")


class InstagramReelsPublisher(Publisher):
    platform = "instagram"
    mode = PublishMode.REELS

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        route: OAuthRoute = OAuthRoute.INSTAGRAM_LOGIN,
        sleep: Callable[[float], Any] | None = None,
        container_attempts: int = CONTAINER_ATTEMPTS,
        container_delay_s: float = CONTAINER_DELAY_S,
    ) -> None:
        self.transport: Transport = transport or HttpxTransport("instagram")
        self.route = route
        self._sleep = sleep
        self.container_attempts = max(1, container_attempts)
        self.container_delay_s = container_delay_s

    # ------------------------------------------------------------------ helpers
    def _base(self, ctx: PublishContext) -> str:
        version = ctx.api_version or social_config.DEFAULT_IG_VERSION
        if self.route is OAuthRoute.FACEBOOK_LOGIN:
            return social_config.FACEBOOK_GRAPH_URL.format(version=version)
        return social_config.INSTAGRAM_GRAPH_URL.format(version=version)

    def _url(self, ctx: PublishContext, suffix: str) -> str:
        return f"{self._base(ctx)}/{ctx.account_provider_id}/{suffix}"

    def _node(self, ctx: PublishContext, node_id: str) -> str:
        return f"{self._base(ctx)}/{node_id}"

    async def _sleep_for(self, seconds: float) -> None:
        if self._sleep is None:
            await asyncio.sleep(seconds)
        else:
            await self._sleep(seconds)

    def _error(self, operation: str, response: HttpResponse, **context: Any) -> PublisherError:
        error = response.body.get("error") if isinstance(response.body.get("error"), dict) else {}
        numeric = _int(error.get("code"))
        message = str(error.get("message") or f"Instagram returned HTTP {response.status}")[:300]
        category = _category(numeric, response.status)
        if operation == "media_publish" and category in {
            PublishErrorCategory.TRANSIENT,
            PublishErrorCategory.RATE_LIMIT,
        }:
            # The publish call may have succeeded before the connection broke.
            category = PublishErrorCategory.AMBIGUOUS
        return PublisherError(
            category,
            message,
            code=f"IG_{numeric or response.status}",
            http_status=response.status,
            retry_after_s=response.retry_after(),
            provider_object_id=context.get("provider_object_id"),
            detail={
                "operation": operation,
                "igraph_code": numeric,
                "igraph_subcode": error.get("error_subcode"),
            },
        )

    def _check_media_url(self, ctx: PublishContext) -> str:
        url = ctx.media_url
        verdict = validate_provider_url(url, extra_hosts=ctx.allowed_url_hosts)
        if not verdict:
            raise PublisherError(
                PublishErrorCategory.MEDIA,
                f"Instagram will not be given this media URL: {verdict.reason}.",
                code="MEDIA_URL_REJECTED",
                detail={"reason": verdict.reason},
            )
        assert url is not None  # narrowed by the verdict
        return url

    # ------------------------------------------------------------------ publish
    async def publish(
        self,
        ctx: PublishContext,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> PublisherResult:
        # A previous attempt may already have published: never create a second post.
        published_id = ctx.object_id("media_id")
        if published_id:
            return await self._verify(ctx, published_id, resumed=True)

        media_url = self._check_media_url(ctx)
        creation_id = ctx.object_id("creation_id")
        if not creation_id:
            creation_id = await self._create_container(ctx, media_url)
        if on_progress is not None:
            on_progress(0, ctx.media_bytes or 0)

        await self._wait_for_container(ctx, creation_id)
        media_id = await self._publish_container(ctx, creation_id)
        return await self._verify(ctx, media_id)

    async def _create_container(self, ctx: PublishContext, media_url: str) -> str:
        data: dict[str, Any] = {
            "media_type": "REELS",
            "video_url": media_url,
            "caption": ctx.text,
            "access_token": ctx.access_token,
        }
        for key in PASSTHROUGH_OPTIONS:
            if key in ctx.options and ctx.options[key] is not None:
                value = ctx.options[key]
                if key == "collaborators" and isinstance(value, (list, tuple)):
                    value = ",".join(str(item) for item in value)
                data[key] = "true" if value is True else "false" if value is False else str(value)
        response = await self.transport.request("POST", self._url(ctx, "media"), data=data)
        if response.status not in (200, 201):
            raise self._error("media", response)
        creation_id = str(response.body.get("id") or "")
        if not creation_id:
            raise PublisherError(
                PublishErrorCategory.PROVIDER_PROCESSING,
                "Instagram accepted the request but returned no container id.",
                code="CONTAINER_ID_MISSING",
            )
        return creation_id

    async def _container_status(self, ctx: PublishContext, creation_id: str) -> str:
        response = await self.transport.request(
            "GET",
            self._node(ctx, creation_id),
            params={"fields": "status_code,status", "access_token": ctx.access_token},
        )
        if response.status != 200:
            raise self._error("container_status", response, provider_object_id=creation_id)
        return str(response.body.get("status_code") or "UNKNOWN").upper()

    async def _wait_for_container(
        self, ctx: PublishContext, creation_id: str, *, attempts: int | None = None
    ) -> str:
        budget = attempts if attempts is not None else self.container_attempts
        state = await self._container_status(ctx, creation_id)
        remaining = max(0, budget - 1)
        while state == "IN_PROGRESS" and remaining > 0:
            await self._sleep_for(self.container_delay_s)
            state = await self._container_status(ctx, creation_id)
            remaining -= 1

        if state == "FINISHED":
            return state
        if state in {"ERROR", "EXPIRED"}:
            raise PublisherError(
                PublishErrorCategory.PROVIDER_PROCESSING,
                f"Instagram could not process the video (container {state.lower()}).",
                code=f"CONTAINER_{state}",
                provider_object_id=creation_id,
            )
        raise PublisherError(
            PublishErrorCategory.TRANSIENT,
            "Instagram is still processing the video; the container can be polled again.",
            code="CONTAINER_NOT_READY",
            provider_object_id=creation_id,
        )

    async def _publish_container(self, ctx: PublishContext, creation_id: str) -> str:
        response = await self.transport.request(
            "POST",
            self._url(ctx, "media_publish"),
            data={"creation_id": creation_id, "access_token": ctx.access_token},
        )
        if response.status not in (200, 201):
            raise self._error("media_publish", response)
        media_id = str(response.body.get("id") or "")
        if not media_id:
            raise PublisherError(
                PublishErrorCategory.AMBIGUOUS,
                "Instagram published but returned no media id; reconcile before retrying.",
                code="MEDIA_ID_MISSING",
                provider_object_id=creation_id,
            )
        return media_id

    async def _verify(self, ctx: PublishContext, media_id: str, *, resumed: bool = False) -> PublisherResult:
        response = await self.transport.request(
            "GET",
            self._node(ctx, media_id),
            params={"fields": MEDIA_FIELDS, "access_token": ctx.access_token},
        )
        if response.status == 404:
            raise PublisherError(
                PublishErrorCategory.AMBIGUOUS,
                "Instagram has no record of the media id it returned.",
                code="MEDIA_NOT_FOUND",
                provider_object_id=media_id,
            )
        if response.status != 200:
            raise self._error("verify", response, provider_object_id=media_id)
        body = response.body or {}
        permalink = body.get("permalink")
        snapshot = {
            "media_id": media_id,
            "media_type": body.get("media_type"),
            "timestamp": body.get("timestamp"),
            "permalink_present": bool(permalink),
            "resumed": resumed,
        }
        return PublisherResult(
            provider_object_id=media_id,
            permalink=permalink,
            state="published",
            provider_status=str(body.get("status_code") or "PUBLISHED"),
            snapshot=snapshot,
        )

    # ------------------------------------------------------------------- status
    async def check_status(self, ctx: PublishContext, provider_object_id: str) -> RemoteStatus:
        response = await self.transport.request(
            "GET",
            self._node(ctx, provider_object_id),
            params={"fields": MEDIA_FIELDS, "access_token": ctx.access_token},
        )
        if response.status == 404:
            return RemoteStatus(
                provider_object_id=provider_object_id,
                state="missing",
                detail="Instagram has no such creation or media id",
                failed=False,
            )
        if response.status != 200:
            raise self._error("status", response, provider_object_id=provider_object_id)
        body = response.body or {}
        permalink = body.get("permalink")
        state = str(body.get("status_code") or ("PUBLISHED" if permalink else "UNKNOWN")).upper()
        return RemoteStatus(
            provider_object_id=provider_object_id,
            state=state.lower(),
            detail=body.get("status"),
            permalink=permalink,
            finished=bool(permalink) or state == "FINISHED",
            failed=state in {"ERROR", "EXPIRED"},
        )


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _category(numeric: int, status: int) -> PublishErrorCategory:
    if status == 429 or numeric in {4, 17, 32, 613}:
        return PublishErrorCategory.RATE_LIMIT
    if status >= 500 or numeric in {1, 2}:
        return PublishErrorCategory.TRANSIENT
    if numeric in {190, 102, 463, 467}:
        return PublishErrorCategory.AUTHORIZATION
    if numeric in {10, 200, 458, 459}:
        return PublishErrorCategory.PERMISSION
    if numeric == 803:
        return PublishErrorCategory.PERMISSION
    if numeric == 9007:
        return PublishErrorCategory.PROVIDER_PROCESSING
    if numeric == 100:
        return PublishErrorCategory.VALIDATION
    return PublishErrorCategory.PERMANENT
