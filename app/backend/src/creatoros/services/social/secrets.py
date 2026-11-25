"""Secret metadata + vault: the only place a credential value is ever handled.

`secret_ref` rows carry metadata (purpose, fingerprint, state). The value itself only exists
inside the vault, and only for the duration of one adapter call. Nothing in this module logs,
returns or stores a value; every code path that touches one is listed in
`docs/CREDENTIAL_VAULT.md` and covered by `test_social_leaks.py`.

Failure policy, deliberately blunt:

* no vault -> the operation is refused (`VAULT_UNAVAILABLE`), nothing is stored, and the account
  stays unconnected;
* vault says the record is missing or undecryptable -> the `secret_ref` row is marked
  `unavailable`, the account's token state becomes `credential_unavailable`, and the UI explains
  that the user must reconnect. The row is never silently deleted, because the fact that a
  credential *was* lost matters.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.ids import new_ulid
from ...db.base import utcnow
from ...db.enums import SecretStatus
from ...db.models.platform import SecretRef
from ...integrations.social.errors import SocialError
from ..backup import AuditService
from .vault import Vault, VaultError, VaultRecord

SECRET_PURPOSES = {
    "provider_app_secret",
    "access_token",
    "refresh_token",
    "oauth_pkce_verifier",
    "rotation_journal",
    "token_metadata",
    "delivery_credentials",
    "provider_upload_session",
}


@dataclass(slots=True)
class StoredSecret:
    ref: SecretRef
    record: VaultRecord


class SecretStore:
    def __init__(self, session: Session, vault: Vault) -> None:
        self.session = session
        self.vault = vault
        self.audit = AuditService(session)

    # -- availability ------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(getattr(self.vault, "available", False))

    async def health(self) -> dict:
        try:
            health = await self.vault.health()
        except VaultError as error:
            return {"available": False, "backend": "error", "reason": error.message}
        return health

    # -- write -------------------------------------------------------------
    async def create(
        self,
        *,
        value: str,
        purpose: str,
        provider: str,
        alias: str,
        secret_id: str | None = None,
    ) -> StoredSecret:
        if purpose not in SECRET_PURPOSES:
            raise SocialError("VAULT_INVALID_PURPOSE", f"{purpose!r} is not a supported secret purpose")
        if not self.available:
            raise SocialError(
                "VAULT_UNAVAILABLE",
                "the credential vault is not available, so nothing was stored",
                http_status=503,
            )
        secret_id = secret_id or new_ulid()
        try:
            record = await self.vault.put(
                secret_id=secret_id, value=value, purpose=purpose, provider=provider, alias=alias
            )
        except VaultError as error:
            raise SocialError(error.code, error.message, http_status=503 if "UNAVAILABLE" in error.code else 400) from error

        ref = SecretRef(
            id=secret_id,
            provider=provider,
            key_alias=alias[:160],
            fingerprint=record.fingerprint[:64],
            cipher="dpapi" if "dpapi" in (record.alg or "").lower() else (record.alg or "dpapi")[:32],
            dpapi_scope="user",
            purpose=purpose,
            schema_version=record.version,
            status=SecretStatus.ACTIVE,
            byte_size=record.bytes,
            last_verified_at=utcnow(),
        )
        self.session.add(ref)
        self.session.flush()
        self.audit.record(
            "secret.created",
            resource_type="secret_ref",
            resource_id=ref.id,
            metadata={"purpose": purpose, "provider": provider, "fingerprint": ref.fingerprint},
        )
        return StoredSecret(ref=ref, record=record)

    async def rotate(self, ref: SecretRef, value: str) -> StoredSecret:
        if ref.status != SecretStatus.ACTIVE:
            raise SocialError("CREDENTIAL_UNAVAILABLE", "this credential is no longer active", http_status=409)
        try:
            record = await self.vault.rotate(ref.id, value)
        except VaultError as error:
            if error.code in {"VAULT_NOT_FOUND", "VAULT_DECRYPT_FAILED"}:
                self._mark_unavailable(ref, error.message)
                raise SocialError("CREDENTIAL_UNAVAILABLE", error.message, http_status=409) from error
            raise SocialError(error.code, error.message, http_status=503) from error
        ref.fingerprint = record.fingerprint[:64]
        ref.byte_size = record.bytes
        ref.rotated_at = utcnow()
        ref.last_verified_at = utcnow()
        self.session.flush()
        self.audit.record(
            "secret.rotated",
            resource_type="secret_ref",
            resource_id=ref.id,
            metadata={"purpose": ref.purpose, "fingerprint": ref.fingerprint},
        )
        return StoredSecret(ref=ref, record=record)

    async def adopt(
        self,
        secret_id: str,
        *,
        purpose: str,
        provider: str,
        alias: str,
    ) -> SecretRef:
        """Register a record the desktop shell already sealed as a `secret_ref` row.

        The renderer writes app secrets straight into the DPAPI vault, so the value never crosses
        this API at all. Adoption records the metadata: it asks the vault whether the record exists
        and whether its stored purpose matches, then links it. A record that is missing is refused
        rather than trusted, so a typo cannot produce a credential that looks configured.
        """
        if purpose not in SECRET_PURPOSES:
            raise SocialError("VAULT_INVALID_PURPOSE", f"{purpose!r} is not a supported secret purpose")
        if not self.available:
            raise SocialError(
                "VAULT_UNAVAILABLE",
                "the credential vault is not available, so nothing can be adopted",
                http_status=503,
            )
        existing = self.session.get(SecretRef, secret_id)
        if existing is not None:
            if existing.status == SecretStatus.ACTIVE and existing.purpose == purpose:
                return existing
            raise SocialError(
                "VAULT_EXISTS",
                "a different secret already uses this id",
                http_status=409,
                detail={"status": existing.status.value, "purpose": existing.purpose},
            )
        try:
            report = await self.vault.verify(secret_id)
        except VaultError as error:
            raise SocialError(error.code, error.message, http_status=503) from error
        if not report.get("present") or not report.get("ok", report.get("present")):
            raise SocialError(
                "VAULT_NOT_FOUND",
                "the vault has no readable record with that id; seal the value before adopting it",
                http_status=404,
                detail={"code": report.get("code")},
            )
        record_purpose = report.get("purpose")
        if record_purpose and record_purpose != purpose:
            raise SocialError(
                "VAULT_PURPOSE_MISMATCH",
                f"the sealed record is a {record_purpose}, not a {purpose}",
                http_status=409,
            )
        ref = SecretRef(
            id=secret_id,
            provider=provider,
            key_alias=alias[:160],
            fingerprint=str(report.get("fingerprint") or "")[:64],
            cipher=("dpapi" if "dpapi" in str(report.get("alg") or "").lower() else str(report.get("alg") or "dpapi"))[:32],
            dpapi_scope="user",
            purpose=purpose,
            schema_version=int(report.get("version") or 1),
            status=SecretStatus.ACTIVE,
            byte_size=int(report.get("bytes") or 0),
            last_verified_at=utcnow(),
        )
        self.session.add(ref)
        self.session.flush()
        self.audit.record(
            "secret.adopted",
            resource_type="secret_ref",
            resource_id=ref.id,
            metadata={"purpose": purpose, "provider": provider, "fingerprint": ref.fingerprint},
        )
        return ref

    # -- read --------------------------------------------------------------
    async def read(self, ref: SecretRef | str, purpose: str) -> str:
        row = ref if isinstance(ref, SecretRef) else self.session.get(SecretRef, ref)
        if row is None:
            raise SocialError("CREDENTIAL_UNAVAILABLE", "the referenced credential no longer exists", http_status=409)
        if row.status != SecretStatus.ACTIVE:
            raise SocialError(
                "CREDENTIAL_UNAVAILABLE",
                f"the credential is {row.status.value}: {row.unavailable_reason or 'no reason recorded'}",
                http_status=409,
            )
        if row.purpose and row.purpose != purpose:
            raise SocialError(
                "CREDENTIAL_UNAVAILABLE",
                f"the stored credential is a {row.purpose}, not a {purpose}",
                http_status=409,
            )
        try:
            value = await self.vault.get(row.id, purpose)
        except VaultError as error:
            if error.code in {"VAULT_NOT_FOUND", "VAULT_DECRYPT_FAILED", "VAULT_CORRUPT", "VAULT_PURPOSE_MISMATCH"}:
                self._mark_unavailable(row, error.message)
                raise SocialError(
                    "CREDENTIAL_UNAVAILABLE",
                    "the stored credential could not be read from the vault; reconnect the account",
                    http_status=409,
                    detail={"vault_code": error.code},
                ) from error
            raise SocialError(error.code, error.message, http_status=503) from error
        row.last_used_at = utcnow()
        self.session.flush()
        return value

    # -- delete ------------------------------------------------------------
    async def revoke(self, ref: SecretRef, *, reason: str = "disconnected by user") -> bool:
        removed = False
        if self.available:
            try:
                removed = await self.vault.delete(ref.id)
            except VaultError as error:
                # The metadata still gets revoked: refusing to mark it revoked because the blob is
                # already gone would leave a credential that looks alive.
                self.audit.record(
                    "secret.delete_failed",
                    resource_type="secret_ref",
                    resource_id=ref.id,
                    outcome="failure",
                    metadata={"code": error.code},
                )
        ref.status = SecretStatus.REVOKED
        ref.revoked_at = utcnow()
        ref.unavailable_reason = reason[:300]
        self.session.flush()
        self.audit.record(
            "secret.revoked",
            resource_type="secret_ref",
            resource_id=ref.id,
            metadata={"purpose": ref.purpose, "blob_removed": removed, "reason": reason},
        )
        return removed

    def mark_revoked(self, ref: SecretRef | None, *, reason: str) -> None:
        if ref is None:
            return
        ref.status = SecretStatus.REVOKED
        ref.revoked_at = ref.revoked_at or utcnow()
        ref.unavailable_reason = reason[:300]
        self.session.flush()

    # -- integrity ---------------------------------------------------------
    async def verify(self, ref: SecretRef) -> dict:
        if not self.available:
            return {"id": ref.id, "ok": False, "present": False, "code": "VAULT_UNAVAILABLE"}
        try:
            result = await self.vault.verify(ref.id)
        except VaultError as error:
            return {"id": ref.id, "ok": False, "present": False, "code": error.code}
        if result.get("ok"):
            ref.last_verified_at = utcnow()
            if ref.status != SecretStatus.ACTIVE:
                ref.status = SecretStatus.ACTIVE
                ref.unavailable_reason = None
            self.session.flush()
        elif result.get("code") in {"VAULT_NOT_FOUND", "VAULT_CORRUPT"}:
            self._mark_unavailable(ref, f"vault verification failed: {result.get('code')}")
        return result

    def _mark_unavailable(self, ref: SecretRef, reason: str) -> None:
        ref.status = SecretStatus.UNAVAILABLE
        ref.unavailable_reason = reason[:300]
        self.session.flush()

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict:
        rows = self.session.execute(
            sa.select(SecretRef.status, sa.func.count(SecretRef.id)).group_by(SecretRef.status)
        ).all()
        by_status = {status.value if hasattr(status, "value") else str(status): count for status, count in rows}
        total = sum(by_status.values())
        return {
            "total": total,
            "by_status": by_status,
            "active": by_status.get("active", 0),
            "unavailable": by_status.get("unavailable", 0),
            "revoked": by_status.get("revoked", 0),
        }
