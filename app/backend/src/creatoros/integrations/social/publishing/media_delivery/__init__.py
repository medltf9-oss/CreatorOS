"""Media delivery: somewhere the provider can fetch the bytes from.

Only S3-compatible object storage is implemented (see `s3.py`). `unconfigured.py` is the honest
answer when a user has not set one up.
"""

from __future__ import annotations

from .base import (
    DEFAULT_PART_SIZE,
    DEFAULT_URL_TTL_S,
    PERMANENT_DELIVERY_CODES,
    DeliveryConfig,
    DeliveryError,
    DeliveryHealth,
    DeliveryReceipt,
    MediaDeliveryProvider,
    ObjectInfo,
    S3Credentials,
    SignedUrl,
)
from .cloud import provider_from_config
from .s3 import S3DeliveryProvider
from .unconfigured import REASON as DELIVERY_UNCONFIGURED_REASON
from .unconfigured import UnconfiguredDelivery, unconfigured

__all__ = [
    "DEFAULT_PART_SIZE",
    "DEFAULT_URL_TTL_S",
    "DELIVERY_UNCONFIGURED_REASON",
    "PERMANENT_DELIVERY_CODES",
    "DeliveryConfig",
    "DeliveryError",
    "DeliveryHealth",
    "DeliveryReceipt",
    "MediaDeliveryProvider",
    "ObjectInfo",
    "S3Credentials",
    "S3DeliveryProvider",
    "SignedUrl",
    "UnconfiguredDelivery",
    "provider_from_config",
    "unconfigured",
]
