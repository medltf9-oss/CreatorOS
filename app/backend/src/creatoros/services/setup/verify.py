"""Byte and signature verification for a pinned prerequisite.

A pass here means one thing only: the file on disk is byte-identical to the pinned record and,
where the manifest demands one, carries a valid Authenticode signature from the expected publisher.
It does not mean the component is licensed for redistribution, and it does not mean the vendor
approved bundling. Those are separate decisions recorded in the manifest and the release blockers.

Every check has one of four states. `unknown` is not a pass: if the platform cannot read a
signature, the report is not ok, because "could not verify" and "verified" must never look alike.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .manifest import Component

CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Check:
    """One verification step and what it observed."""

    id: str
    state: str  # pass | fail | unknown | skipped
    detail: str

    @property
    def ok(self) -> bool:
        return self.state in ("pass", "skipped")


@dataclass(frozen=True)
class SignatureInfo:
    """What Windows reported about one file. `None` means the field was not readable."""

    status: str | None
    signer_subject: str | None
    thumbprint: str | None
    timestamped: bool | None
    timestamp_subject: str | None
    error: str | None = None


@dataclass(frozen=True)
class VerificationReport:
    """The result the API returns; `ok` is false unless every check passed or was inapplicable."""

    component_id: str
    path: str
    ok: bool
    sha256: str | None
    size_bytes: int | None
    signer_subject: str | None
    timestamped: bool | None
    checks: tuple[Check, ...]


def sha256_file(path: Path | str) -> str:
    """Hash a file in fixed-size chunks so a 1.8 GiB installer does not sit in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _powershell_host() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def read_authenticode_signature(path: Path | str) -> SignatureInfo:
    """Ask Windows for the Authenticode signature of a file.

    This is deliberately read-only: `Get-AuthenticodeSignature` validates the chain and reports the
    timestamp countersignature, and it is the same API a user would see in the file properties sheet.
    """
    host = _powershell_host()
    if host is None:
        return SignatureInfo(None, None, None, None, None, "no PowerShell host is available")

    literal = str(path).replace("'", "''")
    script = (
        f"$sig = Get-AuthenticodeSignature -LiteralPath '{literal}'; "
        "if ($null -eq $sig) { throw 'no signature object' }; "
        "[pscustomobject]@{ status = [string]$sig.Status; "
        "signer = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { $null }; "
        "thumbprint = if ($sig.SignerCertificate) { $sig.SignerCertificate.Thumbprint } else { $null }; "
        "timestamped = [bool]($null -ne $sig.TimeStamperCertificate); "
        "timestamp_subject = if ($sig.TimeStamperCertificate) { $sig.TimeStamperCertificate.Subject } else { $null }; "
        "message = [string]$sig.StatusMessage } | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [host, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SignatureInfo(None, None, None, None, None, f"{type(exc).__name__}: {exc}")

    if completed.returncode != 0 or not completed.stdout.strip():
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = detail[0] if detail else f"PowerShell exited {completed.returncode}"
        return SignatureInfo(None, None, None, None, None, message)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return SignatureInfo(None, None, None, None, None, f"unparseable signature payload: {exc}")

    return SignatureInfo(
        status=str(payload.get("status") or "") or None,
        signer_subject=payload.get("signer") or None,
        thumbprint=payload.get("thumbprint") or None,
        timestamped=payload.get("timestamped"),
        timestamp_subject=payload.get("timestamp_subject") or None,
        error=(payload.get("message") or None) if str(payload.get("status")) != "Valid" else None,
    )


def _subject_parts(subject: str) -> list[str]:
    """Split a distinguished name on commas that are outside quoted values."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in subject:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            current.append(char)
            continue
        if char == '"':
            quoted = not quoted
            current.append(char)
            continue
        if char == "," and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return ["".join(part.split()).lower() for part in parts if part.strip()]


def subject_matches(expected: str, actual: str | None) -> bool:
    """Compare a manifest subject with what Windows printed.

    The order of relative distinguished names is not part of the identity, so every component of the
    expected subject must equal one component of the actual subject. A component that merely starts
    with the expected value ("CN=Ollama Inc. Support" for "CN=Ollama Inc.") is a different publisher
    and must not pass.
    """
    if not actual:
        return False
    expected_parts = _subject_parts(expected)
    if not expected_parts:
        return False
    actual_parts = set(_subject_parts(actual))
    return all(part in actual_parts for part in expected_parts)


def find_candidate(component: Component, directories: list[Path]) -> Path | None:
    """Look for the pinned file name in the directories a browser would have used."""
    name = component.filename
    if not name:
        return None
    for directory in directories:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def verify_file(
    component: Component,
    path: Path | str,
    *,
    hasher: Callable[[Path], str] | None = None,
    signature_reader: Callable[[Path], SignatureInfo] | None = None,
) -> VerificationReport:
    """Verify one file against the manifest entry. Never raises for a missing or locked file.

    The collaborators are resolved per call so a test (or a platform without PowerShell) can replace
    them without editing the default arguments.
    """
    hash_file = hasher or sha256_file
    read_signature = signature_reader or read_authenticode_signature
    target = Path(path)
    checks: list[Check] = []

    if not target.is_file():
        checks.append(Check("file.exists", "fail", f"no file at {target}"))
        return VerificationReport(component.id, str(target), False, None, None, None, None, tuple(checks))
    checks.append(Check("file.exists", "pass", str(target)))

    try:
        size = target.stat().st_size
        actual_hash = hash_file(target)
    except OSError as exc:
        checks.append(Check("file.readable", "fail", f"{type(exc).__name__}: {exc}"))
        return VerificationReport(component.id, str(target), False, None, None, None, None, tuple(checks))
    checks.append(Check("file.readable", "pass", "read without error"))

    expected_size = component.size_bytes
    if expected_size is None:
        checks.append(Check("file.size", "unknown", "the manifest pins no size for this component"))
    elif size == expected_size:
        checks.append(Check("file.size", "pass", f"{size} bytes, as pinned"))
    else:
        checks.append(Check("file.size", "fail", f"{size} bytes on disk, {expected_size} pinned"))

    expected_hash = component.sha256
    if expected_hash is None:
        checks.append(Check("file.sha256", "unknown", "the manifest pins no hash for this component"))
    elif actual_hash.lower() == expected_hash:
        checks.append(Check("file.sha256", "pass", actual_hash.lower()))
    else:
        checks.append(Check("file.sha256", "fail", f"{actual_hash.lower()} does not match the pinned {expected_hash}"))

    signature = SignatureInfo(None, None, None, None, None, None)
    if component.requires_signature:
        signature = read_signature(target)
        if signature.error or signature.status is None:
            checks.append(Check("signature.read", "unknown", signature.error or "no signature status"))
        elif signature.status != "Valid":
            checks.append(Check("signature.status", "fail", f"Windows reports {signature.status}"))
        else:
            checks.append(Check("signature.status", "pass", "Valid"))
            if subject_matches(component.expected_subject or "", signature.signer_subject):
                checks.append(Check("signature.signer", "pass", signature.signer_subject or ""))
            else:
                checks.append(
                    Check(
                        "signature.signer",
                        "fail",
                        f"signed by {signature.signer_subject or 'an unknown subject'}, "
                        f"expected {component.expected_subject}",
                    )
                )
            if component.signature.get("timestamp_required"):
                if signature.timestamped:
                    checks.append(Check("signature.timestamp", "pass", signature.timestamp_subject or "present"))
                else:
                    checks.append(Check("signature.timestamp", "fail", "the signature carries no RFC 3161 timestamp"))
    else:
        # A component that is not required to be signed may still be signed, but reading a signature
        # costs a PowerShell process per call and must not turn "unsigned" into a failure here.
        checks.append(Check("signature.status", "skipped", "the manifest does not require a signature"))

    ok = all(check.ok for check in checks)
    return VerificationReport(
        component_id=component.id,
        path=str(target),
        ok=ok,
        sha256=actual_hash.lower(),
        size_bytes=size,
        signer_subject=signature.signer_subject,
        timestamped=signature.timestamped,
        checks=tuple(checks),
    )
