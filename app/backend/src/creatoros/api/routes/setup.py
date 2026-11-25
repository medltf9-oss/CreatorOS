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
    PrerequisiteCatalog,
    PrerequisiteState,
    SetupCheck,
    SetupDisk,
    SetupFfmpeg,
    SetupFirstRun,
    SetupModelFact,
    SetupOllama,
    SetupStatus,
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
    if component.id == "ffmpeg":
        return settings.ffmpeg_bin / component.filename
    relative = str(component.acquisition.get("relative_to") or "")
    return settings.root / relative / component.filename if relative else settings.root / component.filename


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
            version = f", version {ollama.version}" if ollama.version else ""
            return "ok", f"installed at {where}{version}", ollama.install_path
        candidate = verify_service.find_candidate(component, _search_dirs(settings.root))
        if candidate is not None:
            return "unverified", f"installer found at {candidate}; verify it before running it", str(candidate)
        return "missing", "not installed, and no downloaded installer was found", None

    if component.id == "ffmpeg":
        if not ffmpeg.present:
            return "missing", ffmpeg.error or "ffmpeg.exe is not present", None
        expected = component.size_bytes
        actual = Path(str(ffmpeg.path)).stat().st_size
        if expected is not None and actual != expected:
            return (
                "unverified",
                f"{actual} bytes at {ffmpeg.path}, but the pinned build is {expected} bytes",
                ffmpeg.path,
            )
        return "ok", f"present at {ffmpeg.path} ({ffmpeg.version or 'version unreadable'})", ffmpeg.path

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
) -> PrerequisiteState:
    state, detail, local = _presence(component, ollama=ollama, ffmpeg=ffmpeg, settings=settings)
    return PrerequisiteState(
        id=component.id,
        kind=component.kind,
        display_name=component.display_name,
        version=component.version,
        required=component.required,
        state=state,
        verified=False,
        detail=detail,
        local_path=local,
        redistribution_approved=component.redistribution_approved,
        redistribution_detail=str(component.redistribution.get("detail") or ""),
        download_url=component.download_url,
        release_page=component.release_page,
        consent_required=component.needs_consent,
    )


def _checks(
    *,
    manifest: Manifest | None,
    manifest_error: str | None,
    disk: detection.DiskFacts,
    ffmpeg: detection.FfmpegFacts,
    ollama: detection.OllamaFacts,
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
        checks.append(SetupCheck(id="media.ffmpeg", state="warn", detail="ffmpeg is present but ffprobe is missing"))
    else:
        checks.append(SetupCheck(id="media.ffmpeg", state="ok", detail=ffmpeg.version or "present"))

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
    return checks


@router.get("/status", response_model=SetupStatus, summary="First-run detection report")
def setup_status(session: DbSession) -> SetupStatus:
    settings = get_settings()
    manifest, manifest_error = _manifest()
    ollama = detection.detect_ollama(settings.ollama_url)
    ffmpeg = detection.detect_ffmpeg(settings)
    disk = detection.disk_facts(settings.root)
    recorded = state_service.read_state(session)
    first_run = SetupFirstRun(
        completed=bool(recorded and recorded.get("completed")),
        completed_at=recorded.get("completed_at") if recorded else None,
        version=recorded.get("version") if recorded else None,
        skipped_ai=bool(recorded.get("skipped_ai")) if recorded else False,
    )
    prerequisites = (
        [
            _component_state(component, ollama=ollama, ffmpeg=ffmpeg, settings=settings)
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
        checks=_checks(manifest=manifest, manifest_error=manifest_error, disk=disk, ffmpeg=ffmpeg, ollama=ollama),
        manifest_updated=manifest.updated if manifest else None,
        manifest_error=manifest_error,
    )


@router.get("/prerequisites", response_model=PrerequisiteCatalog, summary="Pinned prerequisite catalog")
def prerequisites() -> PrerequisiteCatalog:
    settings = get_settings()
    manifest, manifest_error = _manifest()
    if manifest is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"MANIFEST_INVALID: {manifest_error}")
    ollama = detection.detect_ollama(settings.ollama_url)
    ffmpeg = detection.detect_ffmpeg(settings)
    return PrerequisiteCatalog(
        manifest_schema=manifest.schema,
        updated=manifest.updated,
        source=manifest.source,
        components=[
            _component_state(component, ollama=ollama, ffmpeg=ffmpeg, settings=settings)
            for component in manifest.components
        ],
        manifest_error=None,
    )


@router.post(
    "/prerequisites/{component_id}/verify",
    response_model=VerifyResponse,
    summary="Verify a local file against the pinned manifest",
)
def verify_prerequisite(
    component_id: str,
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
        if found is None and component.acquisition.get("mode") == "local":
            candidate = _local_path(component, settings)
            found = candidate if candidate.is_file() else None
        if found is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"NO_LOCAL_FILE: {component.filename or component.id} was not found in Downloads, Desktop or the data root",
            )
        target = found

    report = verify_service.verify_file(component, target)
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
        checks=[VerifyCheck(id=check.id, state=check.state, detail=check.detail) for check in report.checks],
    )


@router.post("/complete", response_model=CompleteResponse, summary="Record the first-run decision")
def complete_setup(body: CompleteRequest, session: DbSession, audit: Audit) -> CompleteResponse:
    settings = get_settings()
    existing = state_service.read_state(session)
    recorded = state_service.mark_complete(
        session,
        version=settings.version,
        skipped_ai=body.skipped_ai,
        actor="local-user",
        notes=body.notes,
    )
    audit.record(
        "setup.first_run.complete",
        resource_type="setup",
        resource_id="first_run",
        metadata={"skipped_ai": body.skipped_ai, "version": settings.version},
    )
    return CompleteResponse(
        setup_complete=True,
        first_run=SetupFirstRun(
            completed=True,
            completed_at=recorded.get("completed_at"),
            version=recorded.get("version"),
            skipped_ai=bool(recorded.get("skipped_ai")),
        ),
        already_completed=bool(existing and existing.get("completed")),
    )
