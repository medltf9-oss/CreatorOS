"""First-run setup routes: detection, pinned prerequisites and completion.

The routes are read-mostly on purpose. `status` and `prerequisites` observe the machine; `verify`
compares one file with the pinned manifest; `complete` records the user decision. Nothing here
downloads or executes a third-party installer, so the user keeps the consent step and the vendor
keeps its own elevated prompt.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from ...core.settings import Settings, get_settings
from ...schemas.setup import (
    CompleteRequest,
    CompleteResponse,
    ConsentRequest,
    ConsentResponse,
    PrerequisiteCatalog,
    PrerequisiteState,
    SetupCheck,
    SetupDisk,
    SetupFfmpeg,
    SetupFirstRun,
    SetupModelFact,
    SetupOllama,
    SetupStatus,
    SetupVerification,
    VerifyCheck,
    VerifyRequest,
    VerifyResponse,
)
from ...services.setup import detection
from ...services.setup import state as state_service
from ...services.setup import verify as verify_service
from ...services.setup.manifest import Component, Manifest, ManifestError, load_manifest
from ..deps import Audit, DbSession

router = APIRouter(prefix="/setup", tags=["setup"])

# Check ids that gate completion. They come from the same `_checks` report the status endpoint
# returns, so a 409 can never disagree with what the wizard displays. The AI check is a warning by
# design (the app opens without a model runtime), and `skipped_ai` relaxes any future AI gate.
REQUIRED_CHECK_IDS = frozenset({"data.root", "media.ffmpeg", "prerequisites.manifest"})


def _manifest() -> tuple[Manifest | None, str | None]:
    """A broken manifest must not hide the rest of the machine report, so the error travels with it."""
    try:
        return load_manifest(), None
    except ManifestError as exc:
        return None, str(exc)


def _search_dirs(root: Path) -> list[Path]:
    candidates = [Path.home() / "Downloads", Path.home() / "Desktop", root / "temp" / "downloads"]
    return [directory for directory in candidates if directory.is_dir()]


def _local_path(component: Component, settings: Settings) -> Path:
    """Where this component is expected on disk, using the same resolution the backend really uses."""
    if component.id in {"ffmpeg", "ffprobe"}:
        return settings.ffmpeg_bin / component.filename
    relative = str(component.acquisition.get("relative_to") or "")
    return settings.root / relative / component.filename if relative else settings.root / component.filename


def _pinned_version_note(component: Component, installed: str | None) -> str:
    """State the pinned-version comparison honestly: "matches" only when the strings are equal."""
    pinned = component.version
    if not installed:
        return f"installed version is unreadable; the pinned version is {pinned}"
    if installed == pinned:
        return f"version {installed} matches the pinned {pinned}"
    return f"version {installed} does not match the pinned {pinned}"


def _pinned_local_presence(component: Component, path: Path, version: str | None) -> tuple[str, str, str | None]:
    """A size match is never a verification; only /verify reads bytes and records a hash."""
    expected = component.size_bytes
    try:
        actual = path.stat().st_size
    except OSError as exc:
        return "unverified", f"could not read the size of {path}: {type(exc).__name__}: {exc}", str(path)
    if expected is not None and actual != expected:
        return (
            "unverified",
            f"{actual} bytes at {path}, but the pinned build is {expected} bytes",
            str(path),
        )
    return (
        "ok",
        (
            f"present at {path} ({version or 'not executed'}); the size matches the pinned build, "
            "but integrity is unverified and the binary is not run until /verify succeeds"
        ),
        str(path),
    )


def _presence(
    component: Component,
    *,
    ollama: detection.OllamaFacts,
    ffmpeg: detection.FfmpegFacts,
    settings: Settings,
) -> tuple[str, str, str | None]:
    """Presence-only state: cheap, honest, and never a substitute for `verify`."""
    if component.id == "ollama":
        if ollama.installed:
            where = ollama.install_path or ollama.endpoint
            version = _pinned_version_note(component, ollama.version)
            return "ok", f"installed at {where}, {version}; presence is not byte verification", ollama.install_path
        candidate = verify_service.find_candidate(component, _search_dirs(settings.root))
        if candidate is not None:
            return "unverified", f"installer found at {candidate}; verify it before running it", str(candidate)
        return "missing", "not installed, and no downloaded installer was found", None

    if component.id == "ffmpeg":
        if not ffmpeg.present:
            return "missing", ffmpeg.error or "ffmpeg.exe is not present", None
        return _pinned_local_presence(component, Path(str(ffmpeg.path)), ffmpeg.version)

    if component.id == "ffprobe":
        path = _local_path(component, settings)
        if not path.is_file():
            return "missing", f"ffprobe.exe is not present at {path}", None
        return _pinned_local_presence(component, path, None)

    if component.acquisition.get("mode") == "local":
        path = _local_path(component, settings)
        if path.is_file():
            return "ok", f"present at {path}", str(path)
        return "missing", f"missing at {path}", None

    return "unknown", "no detector is implemented for this component", None


def _component_state(
    component: Component,
    *,
    ollama: detection.OllamaFacts,
    ffmpeg: detection.FfmpegFacts,
    settings: Settings,
    verification_record: dict | None = None,
    consent_record: dict | None = None,
) -> PrerequisiteState:
    state, detail, local = _presence(component, ollama=ollama, ffmpeg=ffmpeg, settings=settings)
    verification: SetupVerification | None = None
    verified = False
    if verification_record is not None:
        payload, stale_reason = verify_service.verification_view(verification_record)
        verification = SetupVerification(**payload)
        if verification.ok and verification.current:
            verified = True
            state = "verified"
            detail = (
                f"byte-verified at {verification.path} on {verification.verified_at}; the recorded SHA-256 "
                "is unchanged and the size and modified time still match. This is a recorded verification, "
                "not a live re-hash by this status call."
            )
        else:
            state = "unverified"
            if not verification.current:
                detail = (
                    f"the verification recorded at {verification.verified_at} is stale: "
                    f"{stale_reason or 'the file no longer matches its recorded fingerprint'}"
                )
            else:
                failure = str(verification_record.get("detail") or "the recorded checks did not pass")
                detail = f"the last verification at {verification.verified_at} failed: {failure}"
        if local is None:
            local = verification.path
    return PrerequisiteState(
        id=component.id,
        kind=component.kind,
        display_name=component.display_name,
        version=component.version,
        required=component.required,
        state=state,
        verified=verified,
        verification=verification,
        detail=detail,
        local_path=local,
        redistribution_approved=component.redistribution_approved,
        redistribution_detail=str(component.redistribution.get("detail") or ""),
        download_url=component.download_url,
        release_page=component.release_page,
        source_url=component.acquisition.get("source"),
        consent_required=component.needs_consent,
        consent_summary=str(component.consent.get("summary") or ""),
        consent_accepted=bool(consent_record and consent_record.get("accepted")),
        consent_recorded_at=(str(consent_record.get("recorded_at")) if consent_record else None),
    )


def _checks(
    *,
    manifest: Manifest | None,
    manifest_error: str | None,
    disk: detection.DiskFacts,
    ffmpeg: detection.FfmpegFacts,
    ollama: detection.OllamaFacts,
    completed_version: str | None = None,
    current_version: str | None = None,
) -> list[SetupCheck]:
    checks: list[SetupCheck] = []
    checks.append(
        SetupCheck(
            id="data.root",
            state="ok" if disk.exists and disk.writable else "fail",
            detail=f"{disk.root} is writable"
            if disk.writable
            else f"{disk.root}: {disk.error or 'not writable'}",
        )
    )
    if not ffmpeg.present:
        checks.append(SetupCheck(id="media.ffmpeg", state="fail", detail="no ffmpeg.exe; media features cannot run"))
    elif not ffmpeg.ffprobe_present:
        # ffprobe is required for duration/probe facts; a media check that passes without it is
        # exactly the presence-only claim D-7 rejects. Completion can still be overridden explicitly.
        checks.append(
            SetupCheck(
                id="media.ffmpeg",
                state="fail",
                detail="ffmpeg.exe is present but ffprobe.exe is missing; verify both pinned binaries",
            )
        )
    else:
        checks.append(
            SetupCheck(
                id="media.ffmpeg",
                state="ok",
                detail="ffmpeg.exe and ffprobe.exe are present but not executed; verify both pinned binaries before use",
            )
        )

    checks.append(
        SetupCheck(
            id="ai.ollama",
            state="ok" if ollama.installed else "warn",
            detail=(
                f"installed at {ollama.install_path or ollama.endpoint}"
                if ollama.installed
                else "not installed; CreatorOS opens without it and AI features stay unavailable"
            ),
        )
    )
    pinned_ollama = None
    if manifest is not None:
        try:
            pinned_ollama = manifest.get("ollama").version
        except KeyError:
            pinned_ollama = None
    if ollama.installed and ollama.version and pinned_ollama:
        matches = ollama.version == pinned_ollama
        checks.append(
            SetupCheck(
                id="ai.ollama.version",
                state="ok" if matches else "warn",
                detail=(
                    f"installed {ollama.version} matches the pinned {pinned_ollama}"
                    if matches
                    else f"installed {ollama.version} does not match the pinned {pinned_ollama}; "
                    "AI features may behave differently"
                ),
            )
        )
    if manifest_error is not None:
        checks.append(SetupCheck(id="prerequisites.manifest", state="fail", detail=manifest_error))
    elif manifest is not None:
        checks.append(
            SetupCheck(
                id="prerequisites.manifest",
                state="ok",
                detail=f"{len(manifest.components)} pinned components, updated {manifest.updated or 'unknown'}",
            )
        )
        for component in manifest.components:
            if component.redistribution_approved:
                continue
            checks.append(
                SetupCheck(
                    id=f"license.{component.id}",
                    state="warn",
                    detail=str(component.redistribution.get("detail") or "redistribution is not approved"),
                )
            )
    if completed_version and current_version and completed_version != current_version:
        checks.append(
            SetupCheck(
                id="setup.version_changed",
                state="warn",
                detail=(
                    f"setup was completed for {completed_version} and the running version is "
                    f"{current_version}; the recorded decision is kept and the wizard does not reopen "
                    "on upgrade"
                ),
            )
        )
    return checks


def _required_check_failures(checks: list[SetupCheck], *, skipped_ai: bool) -> list[str]:
    """Failing ids from the `_checks` report that gate completion. Never a second check list."""
    failing: list[str] = []
    for check in checks:
        if check.state != "fail" or check.id not in REQUIRED_CHECK_IDS:
            continue
        if skipped_ai and check.id.startswith("ai."):
            continue
        failing.append(check.id)
    return sorted(failing)


@router.get("/status", response_model=SetupStatus, summary="First-run detection report")
def setup_status(session: DbSession) -> SetupStatus:
    settings = get_settings()
    manifest, manifest_error = _manifest()
    ollama = detection.detect_ollama(settings.ollama_url)
    ffmpeg = detection.detect_ffmpeg(settings)
    disk = detection.disk_facts(settings.root)
    recorded = state_service.read_state(session)
    verifications = state_service.read_verifications(session)
    consents = state_service.read_consents(session)
    completed = bool(recorded and recorded.get("completed"))
    completed_version = (
        str(recorded.get("completed_version") or recorded.get("version"))
        if completed and recorded and (recorded.get("completed_version") or recorded.get("version"))
        else None
    )
    version_changed = bool(completed_version and completed_version != settings.version)
    first_run = SetupFirstRun(
        completed=completed,
        completed_at=recorded.get("completed_at") if recorded else None,
        version=completed_version,
        skipped_ai=bool(recorded.get("skipped_ai")) if recorded else False,
        version_changed=version_changed,
    )
    prerequisites = (
        [
            _component_state(
                component,
                ollama=ollama,
                ffmpeg=ffmpeg,
                settings=settings,
                verification_record=verifications.get(component.id),
                consent_record=consents.get(component.id),
            )
            for component in manifest.components
        ]
        if manifest is not None
        else []
    )
    return SetupStatus(
        version=settings.version,
        data_root=str(settings.root),
        setup_complete=first_run.completed,
        first_run=first_run,
        system=detection.system_facts(),
        disk=SetupDisk(
            root=disk.root,
            exists=disk.exists,
            writable=disk.writable,
            free_bytes=disk.free_bytes,
            total_bytes=disk.total_bytes,
            error=disk.error,
        ),
        ollama=SetupOllama(
            installed=ollama.installed,
            install_path=ollama.install_path,
            version=ollama.version,
            endpoint=ollama.endpoint,
            reachable=ollama.reachable,
            models=[
                SetupModelFact(name=model.name, size_bytes=model.size_bytes, modified_at=model.modified_at)
                for model in ollama.models
            ],
            error=ollama.error,
        ),
        ffmpeg=SetupFfmpeg(
            present=ffmpeg.present,
            ffprobe_present=ffmpeg.ffprobe_present,
            path=ffmpeg.path,
            version=ffmpeg.version,
            error=ffmpeg.error,
        ),
        prerequisites=prerequisites,
        checks=_checks(
            manifest=manifest,
            manifest_error=manifest_error,
            disk=disk,
            ffmpeg=ffmpeg,
            ollama=ollama,
            completed_version=completed_version,
            current_version=settings.version,
        ),
        manifest_updated=manifest.updated if manifest else None,
        manifest_error=manifest_error,
    )


@router.get("/prerequisites", response_model=PrerequisiteCatalog, summary="Pinned prerequisite catalog")
def prerequisites(session: DbSession) -> PrerequisiteCatalog:
    settings = get_settings()
    manifest, manifest_error = _manifest()
    if manifest is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"MANIFEST_INVALID: {manifest_error}")
    ollama = detection.detect_ollama(settings.ollama_url)
    ffmpeg = detection.detect_ffmpeg(settings)
    verifications = state_service.read_verifications(session)
    consents = state_service.read_consents(session)
    return PrerequisiteCatalog(
        manifest_schema=manifest.schema,
        updated=manifest.updated,
        source=manifest.source,
        components=[
            _component_state(
                component,
                ollama=ollama,
                ffmpeg=ffmpeg,
                settings=settings,
                verification_record=verifications.get(component.id),
                consent_record=consents.get(component.id),
            )
            for component in manifest.components
        ],
        manifest_error=None,
    )


@router.post(
    "/prerequisites/{component_id}/consent",
    response_model=ConsentResponse,
    summary="Record the user's explicit consent decision before a download",
)
def record_prerequisite_consent(
    component_id: str,
    body: ConsentRequest,
    session: DbSession,
    audit: Audit,
) -> ConsentResponse:
    manifest, manifest_error = _manifest()
    if manifest is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"MANIFEST_INVALID: {manifest_error}")
    try:
        component = manifest.get(component_id)
    except KeyError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"UNKNOWN_COMPONENT: no prerequisite named {component_id!r}",
        ) from None
    if not component.needs_consent:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"NO_CONSENT_REQUIRED: {component_id} is acquired without a network download",
        )
    record = state_service.record_consent(
        session,
        component.id,
        accepted=body.accepted,
        version=component.version,
        sha256=component.sha256,
    )
    audit.record(
        "setup.prerequisite.consent",
        resource_type="prerequisite",
        resource_id=component.id,
        outcome="accepted" if body.accepted else "declined",
        metadata={
            "accepted": body.accepted,
            "version": component.version,
            "sha256": component.sha256,
            "download_url": component.download_url,
        },
    )
    return ConsentResponse(
        component_id=component.id,
        accepted=bool(record["accepted"]),
        recorded_at=str(record["recorded_at"]),
    )


@router.post(
    "/prerequisites/{component_id}/verify",
    response_model=VerifyResponse,
    summary="Verify a local file against the pinned manifest",
)
def verify_prerequisite(
    component_id: str,
    session: DbSession,
    audit: Audit,
    body: VerifyRequest | None = None,
) -> VerifyResponse:
    manifest, manifest_error = _manifest()
    if manifest is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"MANIFEST_INVALID: {manifest_error}")
    try:
        component = manifest.get(component_id)
    except KeyError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"UNKNOWN_COMPONENT: no prerequisite named {component_id!r}",
        ) from None

    settings = get_settings()
    requested = (body.path if body else None) or None
    if requested is not None:
        target = Path(requested)
        if not target.is_absolute():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "PATH_NOT_ABSOLUTE: pass a full path")
    else:
        found = verify_service.find_candidate(component, _search_dirs(settings.root))
        candidate = _local_path(component, settings) if component.acquisition.get("mode") == "local" else None
        if found is None and candidate is not None and candidate.is_file():
            found = candidate
        if found is None:
            detail = f"NO_LOCAL_FILE: expected {candidate or component.filename or component.id}"
            source = str(component.acquisition.get("source") or "").strip()
            if source:
                detail += f"; pinned official source: {source}"
            detail += "; download it, then choose that file to verify its pinned SHA-256"
            raise HTTPException(status.HTTP_409_CONFLICT, detail)
        target = found

    try:
        outcome = verify_service.verify_target(component, target)
    except verify_service.VerifyRejected as exc:
        # A refused target never became a verification: no bytes were read, so no record is written.
        raise HTTPException(exc.http_status, f"{exc.code}: {exc}") from None
    except verify_service.VerifyTimeout as exc:
        # The deadline is enforced before the record is written, so a timeout cannot leave a partial one.
        raise HTTPException(
            verify_service.VerifyTimeout.http_status,
            f"{verify_service.VerifyTimeout.code}: {exc}",
        ) from None

    record = verify_service.verification_record(outcome)
    payload, _ = verify_service.verification_view(record)
    report = outcome.report
    state_service.record_verification(session, component.id, record)
    audit.record(
        "setup.prerequisite.verify",
        resource_type="prerequisite",
        resource_id=component.id,
        outcome="success" if report.ok else "failure",
        metadata={
            "filename": target.name,
            "ok": report.ok,
            "sha256": report.sha256,
            "size_bytes": report.size_bytes,
            "current": payload["current"],
        },
    )
    return VerifyResponse(
        component_id=report.component_id,
        path=report.path,
        ok=report.ok,
        sha256=report.sha256,
        size_bytes=report.size_bytes,
        signer_subject=report.signer_subject,
        timestamped=report.timestamped,
        verification=SetupVerification(**payload),
        checks=[VerifyCheck(id=check.id, state=check.state, detail=check.detail) for check in report.checks],
    )


@router.post("/complete", response_model=CompleteResponse, summary="Record the first-run decision")
def complete_setup(body: CompleteRequest, session: DbSession, audit: Audit) -> CompleteResponse:
    settings = get_settings()
    manifest, manifest_error = _manifest()
    disk = detection.disk_facts(settings.root)
    ffmpeg = detection.detect_ffmpeg(settings)
    ollama = detection.detect_ollama(settings.ollama_url)
    checks = _checks(
        manifest=manifest,
        manifest_error=manifest_error,
        disk=disk,
        ffmpeg=ffmpeg,
        ollama=ollama,
        current_version=settings.version,
    )
    failures = _required_check_failures(checks, skipped_ai=body.skipped_ai)
    if failures and not body.override_required_checks:
        # Keep the machine-readable check-id list in "message": the desktop parser splits it on
        # commas/whitespace. Put the repair guidance in "details.guidance" instead of appending
        # prose to the id list, which would otherwise become fake check ids in the wizard.
        guidance: list[str] = []
        if "media.ffmpeg" in failures:
            media_dir = settings.root / "tools" / "ffmpeg" / "bin"
            guidance.append(
                f"Place both pinned binaries under {media_dir}, then run Verify; "
                "or override with a recorded reason."
            )
            if manifest is not None:
                try:
                    source = str(manifest.get("ffmpeg").acquisition.get("source") or "").strip()
                except KeyError:
                    source = ""
                if source:
                    guidance.append(f"Pinned official source: {source}")
        detail: dict[str, object] = {
            "code": "REQUIRED_CHECKS_FAILING",
            "message": ", ".join(failures),
        }
        if guidance:
            detail["guidance"] = " ".join(guidance)
        raise HTTPException(status.HTTP_409_CONFLICT, detail)

    existing = state_service.read_state(session)
    recorded = state_service.mark_complete(
        session,
        version=settings.version,
        skipped_ai=body.skipped_ai,
        actor="local-user",
        notes=body.notes,
        override_required_checks=body.override_required_checks,
    )
    completed_version = str(recorded.get("version") or "")
    version_changed = bool(completed_version and completed_version != settings.version)
    audit.record(
        "setup.first_run.complete",
        resource_type="setup",
        resource_id="first_run",
        metadata={
            "skipped_ai": body.skipped_ai,
            "version": settings.version,
            "completed_version": completed_version,
            "override_required_checks": bool(recorded.get("override_required_checks")),
            "failed_checks": failures,
            "consents": state_service.read_consents(session),
        },
    )
    return CompleteResponse(
        setup_complete=True,
        first_run=SetupFirstRun(
            completed=True,
            completed_at=recorded.get("completed_at"),
            version=completed_version,
            skipped_ai=bool(recorded.get("skipped_ai")),
            version_changed=version_changed,
        ),
        already_completed=bool(existing and existing.get("completed")),
    )
