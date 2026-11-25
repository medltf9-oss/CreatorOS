"""Choosing a delivery backend from configuration.

There is exactly one implementation - the S3 API - because every provider worth supporting speaks it.
The extra names below are not separate code paths: they are the names a user may choose, all of which
mean "S3 API with a different endpoint". Documenting that here beats four identical classes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import httpx

from .base import DeliveryConfig, DeliveryError, MediaDeliveryProvider, S3Credentials
from .s3 import S3DeliveryProvider

S3_COMPATIBLE_PROVIDERS: frozenset[str] = frozenset({"s3", "r2", "b2", "wasabi", "minio", "spaces"})


def provider_from_config(
    config: DeliveryConfig | Mapping[str, Any],
    credentials: S3Credentials | None,
    *,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MediaDeliveryProvider:
    resolved = config if isinstance(config, DeliveryConfig) else DeliveryConfig.from_mapping(config)
    if resolved.provider not in S3_COMPATIBLE_PROVIDERS:
        raise DeliveryError(
            "DELIVERY_BAD_CONFIG",
            f"{resolved.provider!r} is not a supported delivery provider.",
            http_status=422,
            detail={"supported": sorted(S3_COMPATIBLE_PROVIDERS)},
        )
    if credentials is None:
        raise DeliveryError(
            "DELIVERY_NOT_CONFIGURED",
            "The delivery credentials are missing from the vault.",
            http_status=409,
        )
    return S3DeliveryProvider(resolved, credentials, client=client, clock=clock)
