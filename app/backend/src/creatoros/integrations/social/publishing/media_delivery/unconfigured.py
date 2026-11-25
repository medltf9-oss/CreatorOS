"""What the engine uses when no delivery provider is configured.

This is not a stub for tests. It is the state a real installation is in until the user configures a
bucket, and it exists so that state is *stated* rather than crashing: Instagram destinations are
blocked with `DELIVERY_NOT_CONFIGURED`, health says exactly why, and nothing pretends a local file
server could substitute. There is deliberately no localhost implementation of this interface.
"""

from __future__ import annotations

from typing import Any

from .base import (
    DeliveryConfig,
    DeliveryError,
    DeliveryHealth,
    DeliveryReceipt,
    MediaDeliveryProvider,
    ObjectInfo,
    SignedUrl,
)

REASON = (
    "No S3-compatible media delivery provider is configured. Instagram fetches videos from a public "
    "URL, so it cannot publish until a bucket is set up in Settings; Facebook and TikTok upload "
    "directly and are unaffected."
)


class UnconfiguredDelivery(MediaDeliveryProvider):
    name = "unconfigured"
    configured = False

    def __init__(self, reason: str = REASON, *, missing: list[str] | None = None) -> None:
        self.reason = reason
        self.missing = list(missing or [])

    def _fail(self, action: str) -> DeliveryError:
        return DeliveryError(
            "DELIVERY_NOT_CONFIGURED",
            self.reason,
            http_status=409,
            detail={"action": action, "missing": self.missing},
        )

    async def health(self) -> DeliveryHealth:
        return DeliveryHealth(
            available=False,
            provider=self.name,
            detail=self.reason,
            bucket=None,
            reason_code="DELIVERY_NOT_CONFIGURED",
        )

    async def upload(
        self,
        path: Any,
        *,
        object_key: str,
        sha256: str,
        size_bytes: int,
        content_type: str = "video/mp4",
    ) -> DeliveryReceipt:
        raise self._fail("upload")

    async def head(self, object_key: str) -> ObjectInfo | None:
        raise self._fail("head")

    async def sign_get(self, object_key: str, *, ttl_s: int | None = None) -> SignedUrl:
        raise self._fail("sign")

    async def delete(self, object_key: str) -> bool:
        raise self._fail("delete")


def unconfigured(missing: list[str] | None = None) -> UnconfiguredDelivery:
    return UnconfiguredDelivery(missing=missing)


def config_from_dict(raw: dict[str, Any]) -> DeliveryConfig:
    """Kept here so callers do not import the base module for one helper."""
    return DeliveryConfig.from_mapping(raw)
