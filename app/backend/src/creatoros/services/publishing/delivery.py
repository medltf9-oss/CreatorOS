"""Delivery service: configuration, credentials, uploads and retention.

This is the piece the rest of the engine talks to. It owns three things the integrations layer must
not know about:

* where the configuration lives (`app_setting`, key `publish.delivery`),
* where the credentials live (the DPAPI vault, through `SecretStore` with purpose
  `delivery_credentials`), and
* the `publish_delivery` ledger, so a dead public URL can be deleted and the fact recorded.

When either the configuration or the credentials are missing, the service returns
:class:`UnconfiguredDelivery` rather than raising from a constructor: the composer and the `/version`
map need to *ask* about delivery without an exception, and Instagram needs to be blocked with a
reason a user can read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import DeliveryAssetState, PublishState, SecretStatus
from ...db.models import AppSetting, PublishDelivery, PublishJob, SecretRef
from ...integrations.social.errors import SocialError
from ...integrations.social.publishing.media_delivery import (
    PERMANENT_DELIVERY_CODES,
    DeliveryConfig,
    DeliveryError,
    DeliveryHealth,
    DeliveryReceipt,
    MediaDeliveryProvider,
    S3Credentials,
    SignedUrl,
    UnconfiguredDelivery,
    unconfigured,
)
from ...integrations.social.publishing.media_delivery.cloud import provider_from_config
from ..backup import AuditService
from ..social.secrets import SECRET_PURPOSES, SecretStore, StoredSecret
from ..social.vault import Vault, VaultError
from .errors import PublishingError

SETTING_KEY = "publish.delivery"
CREDENTIAL_PURPOSE = "delivery_credentials"
CREDENTIAL_ALIAS = "delivery:s3"

#: Objects are deleted once every destination that uses them is finished *and* this long has passed.
DEFAULT_RETENTION_HOURS = 24


class DeliveryService:
    def __init__(
        self,
        session: Session,
        vault: Vault | None = None,
        *,
        audit: AuditService | None = None,
        client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.vault = vault
        self.audit = audit or AuditService(session)
        self.secrets = SecretStore(session, vault) if vault is not None else None
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---------------------------------------------------------------- read side
    def load_config(self) -> DeliveryConfig | None:
        row = self.session.get(AppSetting, SETTING_KEY)
        if row is None:
            return None
        try:
            return DeliveryConfig.from_mapping(dict(row.value_json or {}))
        except DeliveryError:
            # A stored configuration that no longer parses is not a configuration.
            return None

    def secret_ref(self) -> SecretRef | None:
        row = self.session.get(AppSetting, SETTING_KEY)
        if row is None:
            return None
        ref_id = (row.value_json or {}).get("secret_ref_id")
        return self.session.get(SecretRef, ref_id) if ref_id else None

    def missing(self) -> list[str]:
        missing: list[str] = []
        if self.load_config() is None:
            missing.append("bucket configuration")
        if self.secret_ref() is None:
            missing.append("credentials")
        elif self.vault is None or not getattr(self.vault, "available", False):
            missing.append("credential vault")
        return missing

    @property
    def configured(self) -> bool:
        return not self.missing()

    def object_key(self, sha256: str, *, suffix: str = ".mp4") -> str:
        config = self.load_config()
        if config is None:
            raise DeliveryError("DELIVERY_NOT_CONFIGURED", _unconfigured_reason(["bucket configuration"]))
        return config.object_key(sha256, suffix=suffix)

    # ------------------------------------------------------------------ provider
    async def provider(self) -> MediaDeliveryProvider:
        """Build the provider, or say precisely what is missing.

        Deliberately not cached: a rotated credential, a revoked secret or a changed bucket has to
        take effect on the next call, and reading the vault costs one local round trip. A cache here
        would keep publishing against credentials the user already replaced.
        """
        missing = self.missing()
        if missing:
            return UnconfiguredDelivery(_unconfigured_reason(missing), missing=missing)
        assert self.secrets is not None
        ref = self.secret_ref()
        assert ref is not None
        try:
            raw = await self.secrets.read(ref, CREDENTIAL_PURPOSE)
        except (SocialError, VaultError) as exc:
            # A revoked, unreadable or unreachable credential is not a crash: it is the same state as
            # "not configured yet", and the reason travels to the GUI and /version verbatim.
            return UnconfiguredDelivery(
                f"The delivery credentials could not be read from the vault: {_short(exc)}",
                missing=["credentials"],
            )
        try:
            credentials = S3Credentials.from_json(json.loads(raw))
        except (ValueError, DeliveryError):
            return UnconfiguredDelivery(
                "The stored delivery credentials are not readable; enter them again.",
                missing=["credentials"],
            )
        return provider_from_config(
            self.load_config(), credentials, client=self._client, clock=self._clock
        )

    async def health(self) -> DeliveryHealth:
        provider = await self.provider()
        return await provider.health()

    async def status(self) -> dict[str, Any]:
        config = self.load_config()
        health = await self.health()
        return {
            "configured": self.configured,
            "missing": self.missing(),
            "provider": config.provider if config else None,
            "bucket": config.bucket if config else None,
            "region": config.region if config else None,
            "public_base_url": config.public_base_url if config else None,
            "url_ttl_s": config.url_ttl_s if config else None,
            "part_size": config.part_size if config else None,
            "health": {
                "available": health.available,
                "detail": health.detail,
                "reason_code": health.reason_code,
            },
            "credential_ref": (self.secret_ref().id if self.secret_ref() else None),
        }

    # ------------------------------------------------------------------ write side
    async def configure(
        self,
        *,
        fields: dict[str, Any],
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        secret_ref_id: str | None = None,
        actor: str = "local-user",
    ) -> dict[str, Any]:
        """Store the configuration, and the credentials if they were supplied.

        Three ways to end up with credentials, in order of preference: a `secret_ref_id` the desktop
        shell already sealed (the API then never sees a value at all), a value pair handed to the
        vault here, or the reference that is already configured.
        """
        config = DeliveryConfig.from_mapping(fields)
        existing_ref = self.secret_ref()

        if secret_ref_id:
            adopted = self.session.get(SecretRef, secret_ref_id)
            if adopted is None or adopted.status != SecretStatus.ACTIVE:
                raise PublishingError(
                    "DELIVERY_CREDENTIALS_REQUIRED",
                    "That secret id is not an active secret in this vault. Seal the credentials first.",
                    http_status=422,
                    detail={"secret_ref_id": secret_ref_id},
                )
            if adopted.purpose != CREDENTIAL_PURPOSE:
                raise PublishingError(
                    "DELIVERY_CREDENTIAL_PURPOSE",
                    f"The secret is a {adopted.purpose}, not a {CREDENTIAL_PURPOSE}.",
                    http_status=422,
                )
            ref_id = adopted.id
        elif access_key_id and secret_access_key:
            if self.secrets is None or not getattr(self.vault, "available", False):
                raise PublishingError(
                    "VAULT_UNAVAILABLE",
                    "The credential vault is not available, so the delivery credentials were not stored.",
                    http_status=503,
                )
            payload = json.dumps(
                {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
            )
            if existing_ref is not None and existing_ref.purpose == CREDENTIAL_PURPOSE:
                await self.secrets.rotate(existing_ref, payload)
                ref = existing_ref
            else:
                stored: StoredSecret = await self.secrets.create(
                    value=payload,
                    purpose=CREDENTIAL_PURPOSE,
                    provider=config.provider,
                    alias=CREDENTIAL_ALIAS,
                )
                ref = stored.ref
            ref_id = ref.id
        elif existing_ref is not None:
            ref_id = existing_ref.id
        else:
            raise PublishingError(
                "DELIVERY_CREDENTIALS_REQUIRED",
                "Configure the bucket together with its access key id and secret access key.",
                http_status=422,
            )

        value = {**config.summary(), "secret_ref_id": ref_id}
        row = self.session.get(AppSetting, SETTING_KEY)
        if row is None:
            row = AppSetting(key=SETTING_KEY, value_json=value, scope="app")
            self.session.add(row)
        else:
            row.value_json = value
        self.session.flush()
        self.audit.record(
            "publish.delivery.configure",
            resource_type="app_setting",
            resource_id=SETTING_KEY,
            actor=actor,
            metadata={"bucket": config.bucket, "provider": config.provider, "credential_ref": ref_id},
        )
        return await self.status()

    async def clear(self, *, actor: str = "local-user") -> None:
        ref = self.secret_ref()
        if ref is not None and self.secrets is not None and getattr(self.vault, "available", False):
            await self.secrets.revoke(ref, reason="delivery provider removed")
        row = self.session.get(AppSetting, SETTING_KEY)
        if row is not None:
            self.session.delete(row)
        self.session.flush()
        self.audit.record(
            "publish.delivery.clear", resource_type="app_setting", resource_id=SETTING_KEY, actor=actor
        )

    # --------------------------------------------------------------------- upload
    async def ensure_uploaded(
        self,
        path: str | Path,
        *,
        sha256: str,
        size_bytes: int,
        content_type: str = "video/mp4",
    ) -> tuple[DeliveryReceipt, str]:
        provider = await self.provider()
        object_key = self.object_key(sha256)
        receipt = await provider.upload(
            path, object_key=object_key, sha256=sha256, size_bytes=size_bytes, content_type=content_type
        )
        return receipt, object_key

    async def public_url(self, object_key: str, *, ttl_s: int | None = None) -> SignedUrl:
        provider = await self.provider()
        return await provider.sign_get(object_key, ttl_s=ttl_s)

    # --------------------------------------------------------------------- ledger
    def record_delivery(
        self,
        job: PublishJob,
        *,
        receipt: DeliveryReceipt,
        url: SignedUrl | None,
    ) -> PublishDelivery:
        row = self.session.scalar(
            sa.select(PublishDelivery).where(
                PublishDelivery.job_id == job.id, PublishDelivery.object_key == receipt.object_key
            )
        )
        config = self.load_config()
        if row is None:
            row = PublishDelivery(
                job_id=job.id,
                provider=config.provider if config else "s3",
                bucket=receipt.bucket,
                object_key=receipt.object_key,
                sha256=receipt.sha256,
                bytes=receipt.bytes,
                state=DeliveryAssetState.UPLOADED,
                url_expires_at=url.expires_at if url else None,
                uploaded_at=receipt.uploaded_at,
            )
            self.session.add(row)
        else:
            row.state = DeliveryAssetState.VERIFIED
            row.bytes = receipt.bytes
            row.sha256 = receipt.sha256
            row.url_expires_at = url.expires_at if url else row.url_expires_at
            row.uploaded_at = row.uploaded_at or receipt.uploaded_at
        if url is not None:
            row.state = DeliveryAssetState.VERIFIED
            row.verified_at = utcnow()
        self.session.flush()
        return row

    async def release(self, object_key: str, *, reason: str = "published") -> int:
        """Delete the object and mark every ledger row that referenced it."""
        provider = await self.provider()
        rows = list(
            self.session.scalars(sa.select(PublishDelivery).where(PublishDelivery.object_key == object_key))
        )
        removed = False
        try:
            removed = await provider.delete(object_key)
        except DeliveryError as error:
            for row in rows:
                row.state = DeliveryAssetState.FAILED
                row.error_detail = f"{error.code}: {error.message}"[:500]
                row.delete_attempts = (row.delete_attempts or 0) + 1
            self.session.flush()
            raise
        now = utcnow()
        for row in rows:
            row.state = DeliveryAssetState.DELETED
            row.deleted_at = now
            row.delete_attempts = (row.delete_attempts or 0) + (0 if removed else 1)
            row.error_detail = None if removed else "object was already absent"
        self.audit.record(
            "publish.delivery.release",
            resource_type="publish_delivery",
            resource_id=object_key,
            metadata={"reason": reason, "rows": len(rows), "removed": removed},
        )
        self.session.flush()
        return len(rows)

    async def cleanup(self, *, retention_hours: int = DEFAULT_RETENTION_HOURS, limit: int = 50) -> dict[str, int]:
        """Delete delivered objects whose destinations are finished.

        Nothing is deleted while a destination might still need the URL, and nothing is deleted that
        a non-terminal job still points at - a cleanup that races an upload would be worse than a
        stale object.
        """
        cutoff = (self._clock() - timedelta(hours=max(1, retention_hours))).astimezone(UTC)
        rows = list(
            self.session.scalars(
                sa.select(PublishDelivery)
                .where(PublishDelivery.state != DeliveryAssetState.DELETED)
                .order_by(PublishDelivery.created_at)
                .limit(max(1, min(limit, 200)))
            )
        )
        keys: dict[str, bool] = {}
        for row in rows:
            job = self.session.get(PublishJob, row.job_id)
            terminal = job is None or PublishState(job.job_state) in {
                PublishState.PUBLISHED,
                PublishState.FAILED,
                PublishState.CANCELLED,
            }
            old_enough = _aware(row.created_at) is not None and _aware(row.created_at) < cutoff
            if terminal and old_enough:
                keys[row.object_key] = True
        released = 0
        failed = 0
        for key in keys:
            try:
                released += await self.release(key, reason="retention")
            except DeliveryError:
                failed += 1
        return {"objects": len(keys), "rows_released": released, "failed": failed}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _short(error: Exception) -> str:
    text = getattr(error, "message", None) or str(error)
    return str(text)[:200]


def _unconfigured_reason(missing: list[str]) -> str:
    return (
        "No S3-compatible media delivery provider is configured (missing: "
        + ", ".join(missing)
        + "). Instagram fetches videos from a public URL, so it cannot publish until a bucket is set "
        "up in Settings; Facebook and TikTok upload directly and are unaffected."
    )


def is_permanent(error: DeliveryError) -> bool:
    return error.code in PERMANENT_DELIVERY_CODES


__all__ = [
    "CREDENTIAL_ALIAS",
    "CREDENTIAL_PURPOSE",
    "DEFAULT_RETENTION_HOURS",
    "SECRET_PURPOSES",
    "SETTING_KEY",
    "DeliveryService",
    "is_permanent",
    "unconfigured",
]
