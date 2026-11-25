"""The media delivery interface.

Instagram does not accept an upload: it fetches the video from a public URL. A local-first desktop
application therefore needs somewhere to put the bytes where Meta can reach them, and that places
four hard requirements on whatever provides it:

* the bytes are addressable by a URL that expires (so it cannot be guessed later),
* the upload is verifiable (size and checksum), because "the PUT returned 200" is not the same as
  "the object Meta will fetch is the file we meant to send",
* the URL can be deleted after the post exists, so a dead link does not linger,
* and none of it may be the user's own machine. There is no localhost, no tunnel and no
  "Meta can reach 127.0.0.1" in this project; if no delivery provider is configured, Instagram
  destinations are blocked with that reason and say so before a caption is written.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,120}$")

MIN_PART_SIZE = 5 * 1024 * 1024
MAX_PART_SIZE = 512 * 1024 * 1024
DEFAULT_PART_SIZE = 8 * 1024 * 1024
DEFAULT_URL_TTL_S = 900
MAX_URL_TTL_S = 7 * 24 * 3600

#: Codes that mean "this will fail the same way next time", used by the executor to stop retrying.
PERMANENT_DELIVERY_CODES = frozenset(
    {"DELIVERY_NOT_CONFIGURED", "DELIVERY_BAD_CONFIG", "DELIVERY_VERIFY_FAILED", "DELIVERY_FORBIDDEN"}
)


class DeliveryError(Exception):
    """A delivery failure with a stable code and a message that is safe to store."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 502,
        detail: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}
        self.retryable = (code not in PERMANENT_DELIVERY_CODES) if retryable is None else retryable


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    """Everything needed to reach the bucket, minus the credentials."""

    bucket: str
    region: str = "us-east-1"
    provider: str = "s3"
    endpoint_url: str | None = None
    public_base_url: str | None = None
    path_style: bool = False
    part_size: int = DEFAULT_PART_SIZE
    url_ttl_s: int = DEFAULT_URL_TTL_S
    prefix: str = "creatoros/publish"

    def __post_init__(self) -> None:
        if not BUCKET_RE.match(self.bucket or ""):
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG",
                "The bucket name is not a valid S3 bucket name.",
                http_status=422,
                detail={"bucket": self.bucket},
            )
        if not REGION_RE.match(self.region or ""):
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG",
                "The region must look like eu-west-3.",
                http_status=422,
                detail={"region": self.region},
            )
        for label, url in (("endpoint_url", self.endpoint_url), ("public_base_url", self.public_base_url)):
            if url and not str(url).startswith("https://"):
                raise DeliveryError(
                    "DELIVERY_BAD_CONFIG",
                    f"{label} must be an https URL.",
                    http_status=422,
                    detail={label: url},
                )
        if not (MIN_PART_SIZE <= int(self.part_size) <= MAX_PART_SIZE):
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG",
                f"part_size must be between {MIN_PART_SIZE} and {MAX_PART_SIZE} bytes.",
                http_status=422,
            )
        if not (60 <= int(self.url_ttl_s) <= MAX_URL_TTL_S):
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG",
                f"url_ttl_s must be between 60 and {MAX_URL_TTL_S} seconds.",
                http_status=422,
            )
        if not PREFIX_RE.match(self.prefix or ""):
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG", "The object prefix contains unsupported characters.", http_status=422
            )

    @property
    def host(self) -> str:
        if self.endpoint_url:
            return str(self.endpoint_url).split("://", 1)[1].rstrip("/")
        if self.path_style:
            return f"s3.{self.region}.amazonaws.com"
        return f"{self.bucket}.s3.{self.region}.amazonaws.com"

    @property
    def scheme(self) -> str:
        return "https"

    def object_key(self, sha256: str, *, suffix: str = ".mp4") -> str:
        """Content-addressed: the same bytes never occupy two objects, and a wrong file is obvious."""
        digest = str(sha256).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG", "object keys are derived from a sha256 digest", http_status=422
            )
        return f"{self.prefix}/{digest[:2]}/{digest}{suffix}"

    def url_path(self, object_key: str) -> str:
        if self.path_style:
            return f"/{self.bucket}/{object_key}"
        return f"/{object_key}"

    def bucket_path(self) -> str:
        return f"/{self.bucket}" if self.path_style else "/"

    def with_overrides(self, **overrides: Any) -> DeliveryConfig:
        return DeliveryConfig(**{**_fields(self), **{k: v for k, v in overrides.items() if v is not None}})

    def summary(self) -> dict[str, Any]:
        """Safe to show in the GUI and to store in audit metadata: no credentials, no signed URL."""
        return {
            "provider": self.provider,
            "bucket": self.bucket,
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "public_base_url": self.public_base_url,
            "path_style": self.path_style,
            "part_size": self.part_size,
            "url_ttl_s": self.url_ttl_s,
            "prefix": self.prefix,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> DeliveryConfig:
        try:
            return cls(
                bucket=str(raw["bucket"]),
                region=str(raw.get("region") or "us-east-1"),
                provider=str(raw.get("provider") or "s3"),
                endpoint_url=(str(raw["endpoint_url"]) if raw.get("endpoint_url") else None),
                public_base_url=(str(raw["public_base_url"]) if raw.get("public_base_url") else None),
                path_style=bool(raw.get("path_style") or False),
                part_size=int(raw.get("part_size") or DEFAULT_PART_SIZE),
                url_ttl_s=int(raw.get("url_ttl_s") or DEFAULT_URL_TTL_S),
                prefix=str(raw.get("prefix") or "creatoros/publish"),
            )
        except KeyError as exc:
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG", "The delivery configuration is missing its bucket.", http_status=422
            ) from exc
        except (TypeError, ValueError) as exc:
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG", "The delivery configuration is not readable.", http_status=422
            ) from exc


def _fields(config: DeliveryConfig) -> dict[str, Any]:
    return {
        "bucket": config.bucket,
        "region": config.region,
        "provider": config.provider,
        "endpoint_url": config.endpoint_url,
        "public_base_url": config.public_base_url,
        "path_style": config.path_style,
        "part_size": config.part_size,
        "url_ttl_s": config.url_ttl_s,
        "prefix": config.prefix,
    }


@dataclass(frozen=True, slots=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str

    def __repr__(self) -> str:  # never print the secret
        return f"S3Credentials(access_key_id='{self.access_key_id[:4]}…')"

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> S3Credentials:
        access = str(raw.get("access_key_id") or "").strip()
        secret = str(raw.get("secret_access_key") or "").strip()
        if not access or not secret:
            raise DeliveryError(
                "DELIVERY_BAD_CONFIG",
                "The delivery credentials are incomplete.",
                http_status=422,
            )
        return cls(access_key_id=access, secret_access_key=secret)


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    bucket: str
    object_key: str
    bytes: int
    sha256: str
    etag: str | None
    uploaded_at: datetime
    parts: int = 1


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    object_key: str
    bytes: int
    sha256: str | None
    etag: str | None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class SignedUrl:
    """A short-lived public URL. It carries its own credentials in the query string."""

    url: str = field(repr=False)
    expires_at: datetime
    object_key: str

    def __repr__(self) -> str:  # a signed URL must not reach a log because someone printed an object
        return f"SignedUrl(object_key='{self.object_key}', expires_at='{self.expires_at.isoformat()}')"

    @property
    def seconds_left(self) -> int:
        delta = self.expires_at - datetime.now(UTC)
        return max(0, int(delta.total_seconds()))


@dataclass(frozen=True, slots=True)
class DeliveryHealth:
    available: bool
    provider: str
    detail: str
    bucket: str | None = None
    reason_code: str | None = None


class MediaDeliveryProvider(ABC):
    """What the executor and the composer are allowed to assume about a delivery backend."""

    name: str = "abstract"
    configured: bool = False

    @abstractmethod
    async def health(self) -> DeliveryHealth: ...

    @abstractmethod
    async def upload(
        self,
        path: Any,
        *,
        object_key: str,
        sha256: str,
        size_bytes: int,
        content_type: str = "video/mp4",
    ) -> DeliveryReceipt: ...

    @abstractmethod
    async def head(self, object_key: str) -> ObjectInfo | None: ...

    @abstractmethod
    async def sign_get(self, object_key: str, *, ttl_s: int | None = None) -> SignedUrl: ...

    @abstractmethod
    async def delete(self, object_key: str) -> bool: ...

    async def aclose(self) -> None:
        return None
