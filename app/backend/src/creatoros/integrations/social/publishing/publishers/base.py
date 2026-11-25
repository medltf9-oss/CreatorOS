"""The publisher contract: what "publish this" means to a provider client.

Every publisher answers three questions and nothing else:

    publish(ctx)        -> the provider's own object id, persisted before the next call
    check_status(ctx)   -> what the provider says about that object now
    delete(ctx)         -> ask the provider to remove it (used only by explicit user action)

The contract exists so the executor never branches on a platform name. It also fixes the shape of
failure: a publisher raises :class:`PublisherError` with one of the canonical categories, and the
executor decides whether that means "retry", "ask the user" or "reconcile" from the category, never
from the message text.

What is deliberately *not* here: retries, scheduling, database writes, approvals. A publisher that
retried internally would make the job journal a lie about how many times the provider was called.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .....db.enums import PublishErrorCategory, PublishMode


class PublisherError(Exception):
    """A provider failure with a canonical category and no secret material.

    `provider_object_id` is set when the provider told us what it created *and* then failed; the
    executor persists it so a retry can never produce a second post.
    """

    def __init__(
        self,
        category: PublishErrorCategory,
        message: str,
        *,
        code: str = "PUBLISH_FAILED",
        http_status: int | None = None,
        retry_after_s: float | None = None,
        provider_object_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.code = code
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        self.provider_object_id = provider_object_id
        self.detail = dict(detail or {})

    @property
    def retryable(self) -> bool:
        return self.category in {PublishErrorCategory.TRANSIENT, PublishErrorCategory.RATE_LIMIT}

    @property
    def ambiguous(self) -> bool:
        return self.category is PublishErrorCategory.AMBIGUOUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "retry_after_s": self.retry_after_s,
            "provider_object_id": self.provider_object_id,
        }


@dataclass(frozen=True, slots=True)
class PublishContext:
    """Everything a publisher needs, already resolved by the executor.

    The access token is present in memory for the length of one call. No publisher may log it, store
    it, put it in an error message or return it.
    """

    job_id: str
    account_provider_id: str
    access_token: str = field(repr=False)
    mode: PublishMode
    caption: str = ""
    title: str | None = None
    hashtags: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)
    media_path: str | None = None
    media_url: str | None = None
    media_sha256: str | None = None
    media_bytes: int = 0
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    api_version: str | None = None
    display_timezone: str = "UTC"
    scheduled_at: datetime | None = None
    #: Provider objects already created for this attempt, so a resumed job does not create a second.
    resume_from: Mapping[str, str] = field(default_factory=dict)
    #: Extra hosts the media URL may live on (the configured delivery bucket), for the SSRF check.
    allowed_url_hosts: tuple[str, ...] = ()
    #: A provider upload URL (TikTok). It carries its own authorisation in the query string, so it is
    #: kept out of reprs, out of results and out of the journal - the executor seals it in the vault.
    upload_url: str | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        """Caption with hashtags appended once, the way the provider will show it."""
        tags = [f"#{tag}" for tag in self.hashtags]
        parts = [self.caption.strip(), " ".join(tags).strip()]
        return "\n\n".join(part for part in parts if part)

    def object_id(self, key: str) -> str | None:
        value = self.resume_from.get(key)
        return str(value) if value else None


@dataclass(frozen=True, slots=True)
class PublisherResult:
    """What the provider created, in the terms the journal records."""

    provider_object_id: str
    permalink: str | None = None
    state: str = "published"
    provider_status: str | None = None
    published_at: datetime | None = None
    snapshot: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RemoteStatus:
    """A provider's current opinion about an object: the input to reconciliation."""

    provider_object_id: str
    state: str
    detail: str | None = None
    permalink: str | None = None
    finished: bool = False
    failed: bool = False
    retry_after_s: float | None = None


class Publisher(ABC):
    """One platform, one surface."""

    platform: str
    mode: PublishMode

    @abstractmethod
    async def publish(
        self,
        ctx: PublishContext,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> PublisherResult:
        """Send the media. `on_progress(sent_bytes, total_bytes)` is how the journal learns how far
        an interrupted upload got, so a retry can resume instead of starting over."""

    @abstractmethod
    async def check_status(self, ctx: PublishContext, provider_object_id: str) -> RemoteStatus: ...

    async def delete(self, ctx: PublishContext, provider_object_id: str) -> bool:
        raise PublisherError(
            PublishErrorCategory.PERMANENT,
            f"{self.platform} does not expose deletion through this API.",
            code="DELETE_NOT_SUPPORTED",
        )
