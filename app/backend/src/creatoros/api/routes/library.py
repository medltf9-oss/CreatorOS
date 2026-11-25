"""Content library API.

All routes are authenticated with the session token like every other route. Media bytes are
served through short-lived signed URLs (:mod:`creatoros.api.media_access`) because ``<img>``
and ``<video>`` cannot set an Authorization header.

No route accepts a filesystem path except ``POST /library/import``, and that one requires the
desktop shell's HMAC signature (:mod:`creatoros.api.ipc_auth`) so the renderer cannot use it
to read arbitrary files. Every served file is resolved and proven to be inside the media
root before a byte is sent.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from ...core.paths import is_inside_root
from ...core.settings import get_settings
from ...db.enums import (
    ArchiveState,
    DerivativeKind,
    DerivativeState,
    IntakeState,
    MediaKind,
    QueueState,
    RightsStatus,
)
from ...db.models import ContentProject, MediaAsset, MediaDerivative, QueueJob, Transcript
from ...schemas.library import (
    AssetDetail,
    AssetPage,
    AssetSummary,
    AudioRequest,
    DerivativeOut,
    ImportAccepted,
    ImportDuplicate,
    ImportRequest,
    JobOut,
    LibraryStats,
    MediaJobPage,
    MetadataUpdate,
    ProxyRequest,
    RecoveryReport,
    StorageReport,
    ThumbnailRequest,
    TranscodeRequest,
    WaveformRequest,
)
from ...services.media import jobs as media_jobs
from ...services.media.jobs import MEDIA_JOB_TYPES, MEDIA_QUEUE, job_progress, retry_media_job
from ...services.media.library import (
    AssetNotFound,
    MediaLibrary,
    library_supports,
)
from ...services.media.probe import ProbeError
from ...services.media.profiles import ProfileError, ProfileNotApplicable, list_profiles, recommend_profile
from ...services.media.storage import available_bytes
from ..deps import Audit, DbSession, Paging
from ..ipc_auth import require_ipc_signature
from ..media_access import sign_media_url

log = logging.getLogger("creatoros.library")
router = APIRouter(tags=["library"])

# Media bytes cannot carry an Authorization header: <img> and <video> send a plain GET.
# These four routes are therefore mounted without the session-token dependency and are
# authorized solely by the HMAC-signed URL (asset id + resource + expiry). Everything that
# can mutate state stays on `router`, which always requires the token.
media_router = APIRouter(tags=["library-media"])

SORTS = {
    "created_at": MediaAsset.created_at,
    "size": MediaAsset.size_bytes,
    "duration": MediaAsset.duration_ms,
    "name": MediaAsset.original_filename,
    "kind": MediaAsset.kind,
}


def _http(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "MEDIA_ERROR")
    status_code = getattr(exc, "status", 422)
    detail = getattr(exc, "detail", None)
    message = str(exc)
    if isinstance(detail, dict) and detail:
        extra = ", ".join(f"{key}={value}" for key, value in detail.items() if key != "code")
        if extra:
            message = f"{message} ({extra})"
    return HTTPException(int(status_code), f"{code}: {message}")


def _library(session, request: Request | None = None) -> MediaLibrary:
    return MediaLibrary(session)


def _summary(asset: MediaAsset, request: Request) -> AssetSummary:
    summary = AssetSummary.model_validate(asset)
    if asset.kind in (MediaKind.VIDEO, MediaKind.IMAGE) and asset.thumbnail_path:
        summary.thumbnail_url = sign_media_url(request, asset.id, resource="thumbnail")
    if asset.has_audio or asset.kind == MediaKind.AUDIO:
        summary.poster_url = sign_media_url(request, asset.id, resource="poster")
    summary.stream_url = sign_media_url(request, asset.id, resource="stream")
    return summary


def _derivative_out(row: MediaDerivative, request: Request) -> DerivativeOut:
    payload = DerivativeOut.model_validate(row)
    if row.state == DerivativeState.READY and row.rel_path:
        resource = {
            DerivativeKind.THUMBNAIL: "thumbnail",
            DerivativeKind.TIMELINE: "thumbnail",
            DerivativeKind.WAVEFORM_IMAGE: "poster",
        }.get(row.kind, "derivative")
        extra = row.profile if resource == "thumbnail" else ""
        payload.urls = {"file": sign_media_url(request, row.asset_id, resource=resource, extra=extra)}
    return payload


# ------------------------------------------------------------------------- stats
@router.get("/library/stats", response_model=LibraryStats)
def library_stats(session: DbSession, request: Request) -> LibraryStats:
    library = MediaLibrary(session)
    by_kind = {kind.value: 0 for kind in MediaKind}
    bytes_by_kind = {kind.value: 0 for kind in MediaKind}
    for kind, count, total in session.execute(
        sa.select(MediaAsset.kind, sa.func.count(), sa.func.coalesce(sa.func.sum(MediaAsset.size_bytes), 0))
        .where(MediaAsset.deleted_at.is_(None))
        .group_by(MediaAsset.kind)
    ):
        key = kind.value if hasattr(kind, "value") else str(kind)
        by_kind[key] = count
        bytes_by_kind[key] = int(total or 0)

    by_state = {state.value: 0 for state in IntakeState}
    for state, count in session.execute(
        sa.select(MediaAsset.intake_state, sa.func.count()).group_by(MediaAsset.intake_state)
    ):
        by_state[state.value if hasattr(state, "value") else str(state)] = count

    derivative_counts = {kind.value: 0 for kind in DerivativeKind}
    for kind, count in session.execute(
        sa.select(MediaDerivative.kind, sa.func.count())
        .where(MediaDerivative.state == DerivativeState.READY)
        .group_by(MediaDerivative.kind)
    ):
        derivative_counts[kind.value if hasattr(kind, "value") else str(kind)] = count

    job_counts = dict(
        session.execute(
            sa.select(QueueJob.state, sa.func.count())
            .where(QueueJob.job_type.in_(MEDIA_JOB_TYPES))
            .group_by(QueueJob.state)
        ).all()
    )
    normalised = {state.value: 0 for state in QueueState}
    for state, count in job_counts.items():
        normalised[state.value if hasattr(state, "value") else str(state)] = count

    disk = _disk_report()
    recent = list(
        session.scalars(
            sa.select(MediaAsset)
            .where(MediaAsset.deleted_at.is_(None))
            .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
            .limit(6)
        )
    )
    return LibraryStats(
        assets_total=sum(by_kind.values()),
        by_kind=by_kind,
        by_state=by_state,
        bytes_total=sum(bytes_by_kind.values()),
        bytes_by_kind=bytes_by_kind,
        derivatives_total=sum(derivative_counts.values()),
        derivatives_by_kind=derivative_counts,
        jobs_active=normalised.get("queued", 0) + normalised.get("claimed", 0) + normalised.get("running", 0),
        jobs_failed=normalised.get("failed", 0) + normalised.get("dead_letter", 0),
        jobs_completed=normalised.get("succeeded", 0),
        storage=library.storage.usage(),
        disk=disk,
        recent=[_summary(asset, request) for asset in recent],
        capabilities=library_supports(),
    )


def _disk_report() -> dict[str, float]:
    import shutil

    settings = get_settings()
    root = settings.root
    try:
        usage = shutil.disk_usage(str(root))
    except OSError:  # pragma: no cover - drive missing
        return {"free_gb": 0.0, "total_gb": 0.0, "used_gb": 0.0, "free_bytes": 0.0}
    return {
        "free_gb": round(usage.free / 1024**3, 2),
        "total_gb": round(usage.total / 1024**3, 2),
        "used_gb": round(usage.used / 1024**3, 2),
        "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        "free_bytes": float(usage.free),
        "min_free_gb": settings.media_min_free_gb,
        "below_minimum": usage.free < settings.media_min_free_gb * 1024**3,
    }


@router.get("/library/storage", response_model=StorageReport)
def library_storage(session: DbSession) -> StorageReport:
    library = MediaLibrary(session)
    return StorageReport(
        areas=library.storage.usage(),
        disk=_disk_report(),
        spacing_note="originals are never deleted by any automatic job",
    )


# ------------------------------------------------------------------------ assets
@router.get("/library/assets", response_model=AssetPage)
def list_assets(
    session: DbSession,
    request: Request,
    paging: Paging,
    kind: MediaKind | None = None,
    state: IntakeState | None = None,
    q: str | None = Query(default=None, max_length=120),
    rights: RightsStatus | None = None,
    archived: bool = False,
    sort: str = Query(default="created_at", pattern="^(created_at|size|duration|name|kind)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> AssetPage:
    where: list[Any] = [MediaAsset.deleted_at.is_(None)]
    if kind:
        where.append(MediaAsset.kind == kind)
    if state:
        where.append(MediaAsset.intake_state == state)
    if rights:
        where.append(MediaAsset.rights_status == rights)
    if not archived:
        where.append(MediaAsset.archive_state != ArchiveState.DELETED)
    if q:
        needle = f"%{q.strip()}%"
        where.append(
            sa.or_(
                MediaAsset.original_filename.ilike(needle),
                MediaAsset.format_name.ilike(needle),
                MediaAsset.mime_type.ilike(needle),
            )
        )
    column = SORTS[sort]
    stmt = sa.select(MediaAsset).where(*where)
    count = session.scalar(sa.select(sa.func.count()).select_from(MediaAsset).where(*where)) or 0
    stmt = stmt.order_by(column.desc() if order == "desc" else column.asc(), MediaAsset.id.desc())
    items = list(session.scalars(stmt.limit(paging.limit).offset(paging.offset)))
    return AssetPage(
        items=[_summary(asset, request) for asset in items],
        total=count,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/library/assets/{asset_id}", response_model=AssetDetail)
def get_asset(asset_id: str, session: DbSession, request: Request) -> AssetDetail:
    library = MediaLibrary(session)
    try:
        asset = library.get_asset(asset_id)
    except AssetNotFound as exc:
        raise _http(exc) from exc
    derivatives = library.derivatives(asset.id)
    project_count = session.scalar(
        sa.select(sa.func.count()).select_from(ContentProject).where(ContentProject.source_asset_id == asset.id)
    ) or 0
    jobs = list(
        session.scalars(
            sa.select(QueueJob)
            .where(QueueJob.job_type.in_(MEDIA_JOB_TYPES))
            .where(QueueJob.payload_json["asset_id"].as_string() == asset.id)
            .order_by(QueueJob.created_at.desc())
            .limit(25)
        )
    )
    detail = AssetDetail.model_validate(asset)
    base = _summary(asset, request)
    for field, value in base.model_dump().items():
        setattr(detail, field, value)
    detail.probe = dict(asset.probe_json or {})
    detail.derivatives = [_derivative_out(row, request) for row in derivatives]
    detail.project_count = int(project_count)
    detail.jobs = [job_progress(job) for job in jobs]
    detail.storage = {
        "original_bytes": asset.size_bytes,
        "derivative_bytes": sum(row.size_bytes for row in derivatives if row.state == DerivativeState.READY),
        "derivative_count": len([row for row in derivatives if row.state == DerivativeState.READY]),
    }
    return detail


@router.get("/library/assets/{asset_id}/probe")
def get_probe(asset_id: str, session: DbSession, request: Request) -> dict[str, Any]:
    library = MediaLibrary(session)
    try:
        asset = library.get_asset(asset_id)
    except AssetNotFound as exc:
        raise _http(exc) from exc
    return {
        "asset_id": asset.id,
        "probe_version": asset.probe_version,
        "stored": dict(asset.probe_json or {}),
        "recommendation": recommend_profile(
            kind=str(asset.kind),
            width=asset.width,
            height=asset.height,
            duration_ms=asset.duration_ms,
            has_audio=asset.has_audio,
        ),
        "urls": {
            "thumbnail": sign_media_url(request, asset.id, resource="thumbnail")
            if asset.thumbnail_path
            else None,
            "stream": sign_media_url(request, asset.id, resource="stream"),
        },
    }


@router.post("/library/assets/{asset_id}/inspect")
def inspect_asset(asset_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    library = MediaLibrary(session)
    try:
        asset = library.get_asset(asset_id)
        probe = library.reprobe(asset)
    except (AssetNotFound, ProbeError) as exc:
        raise _http(exc) from exc
    audit.record("media.inspect", resource_type="media_asset", resource_id=asset.id)
    return {"asset_id": asset.id, "kind": probe.kind, "probe": probe.as_dict(), "warnings": probe.warnings}


@router.patch("/library/assets/{asset_id}")
def update_metadata(asset_id: str, body: MetadataUpdate, session: DbSession, audit: Audit) -> dict[str, Any]:
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ASSET_NOT_FOUND: media asset does not exist")
    changes: dict[str, Any] = {}
    if body.rights_status is not None:
        try:
            asset.rights_status = RightsStatus(body.rights_status)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: unknown rights status") from exc
        changes["rights_status"] = body.rights_status
    if body.rights_notes is not None:
        asset.rights_notes = body.rights_notes
        changes["rights_notes"] = "updated"
    if body.tags_json is not None:
        asset.tags_json = [str(tag)[:60] for tag in body.tags_json][:50]
        changes["tags"] = len(asset.tags_json)
    if body.archive_state is not None:
        try:
            asset.archive_state = ArchiveState(body.archive_state)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: unknown archive state") from exc
        changes["archive_state"] = body.archive_state
    session.commit()
    audit.record(
        "media.metadata.update", resource_type="media_asset", resource_id=asset.id, metadata=changes
    )
    return {"asset_id": asset.id, "changes": changes}


@router.delete("/library/assets/{asset_id}")
def delete_asset(
    asset_id: str,
    session: DbSession,
    audit: Audit,
    confirm: str = Query(default="", description="must equal the asset id"),
    force: bool = False,
) -> dict[str, Any]:
    """Delete an asset and its derived files. Originals are never removed implicitly."""
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ASSET_NOT_FOUND: media asset does not exist")
    if confirm != asset_id:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "CONFIRMATION_REQUIRED: pass confirm=<asset id> to delete an original and its derivatives",
        )
    project_count = session.scalar(
        sa.select(sa.func.count()).select_from(ContentProject).where(ContentProject.source_asset_id == asset.id)
    ) or 0
    transcript_count = session.scalar(
        sa.select(sa.func.count()).select_from(Transcript).where(Transcript.asset_id == asset.id)
    ) or 0
    if (project_count or transcript_count) and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ASSET_IN_USE: {project_count} project(s) and {transcript_count} transcript(s) use this asset; pass force=true to delete anyway",
        )
    library = MediaLibrary(session)
    removed_files = 0
    for row in list(asset.derivatives):
        if row.rel_path:
            try:
                path = library.storage.resolve_stored(row.rel_path)
                if is_inside_root(path) and path.exists():
                    path.unlink()
                    removed_files += 1
            except Exception as exc:  # noqa: BLE001 - a missing derivative must not block deletion
                log.warning("could not delete derivative file for %s: %s", asset.id, exc)
    original_removed = False
    try:
        original = library.storage.resolve_stored(asset.source_path)
        if is_inside_root(original) and original.exists():
            original.unlink()
            original_removed = True
    except Exception as exc:  # noqa: BLE001 - a missing original must not block the delete
        log.warning("could not delete original for %s: %s", asset.id, exc)
    audit.record(
        "media.delete",
        resource_type="media_asset",
        resource_id=asset.id,
        outcome="success",
        metadata={
            "original_removed": original_removed,
            "derivative_files_removed": removed_files,
            "forced": force,
            "original_filename": asset.original_filename,
        },
    )
    session.delete(asset)
    session.commit()
    return {"asset_id": asset_id, "original_removed": original_removed, "derivatives_removed": removed_files}


# ---------------------------------------------------------------------- media bytes
def _serve(session, asset_id: str, kind: str, profile: str | None) -> FileResponse:
    library = MediaLibrary(session)
    try:
        asset = library.get_asset(asset_id)
    except AssetNotFound as exc:
        raise _http(exc) from exc
    if kind == "stream":
        path = library.asset_file(asset)
        media_type = asset.mime_type or "application/octet-stream"
    else:
        derivative_kind = {
            "primary": DerivativeKind.THUMBNAIL,
            "timeline": DerivativeKind.TIMELINE,
            "poster": DerivativeKind.WAVEFORM_IMAGE,
        }[kind]
        stmt = sa.select(MediaDerivative).where(
            MediaDerivative.asset_id == asset.id,
            MediaDerivative.kind == derivative_kind,
            MediaDerivative.state == DerivativeState.READY,
        )
        if profile:
            stmt = stmt.where(MediaDerivative.profile == profile)
        row = session.scalars(stmt.order_by(MediaDerivative.created_at.asc())).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "DERIVATIVE_NOT_FOUND: generate it first")
        path = library.derivative_file(row)
        media_type = row.mime_type or "application/octet-stream"
    if not is_inside_root(path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PERMISSION_DENIED: path outside the media root")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=60",
            "X-Content-Type-Options": "nosniff",
            # The renderer runs on the Vite origin in development and file:// when packaged,
            # so a same-site resource policy would block every <img>/<video>. These responses
            # are already gated by the HMAC signature and a 10-minute expiry.
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Access-Control-Allow-Origin": "*",
        },
    )


@media_router.get("/library/assets/{asset_id}/thumbnail")
def asset_thumbnail(
    asset_id: str,
    session: DbSession,
    request: Request,
    profile: str | None = Query(default=None, max_length=60),
) -> FileResponse:
    # Signed URL is verified by the dependency in the route signature below in practice;
    # see _signed() wrapper used by the router-level check.
    from ..media_access import verify_media_url

    verify_media_url(
        request,
        asset_id,
        resource="thumbnail",
        exp=request.query_params.get("exp"),
        sig=request.query_params.get("sig"),
        extra=profile or request.query_params.get("i", ""),
    )
    return _serve(session, asset_id, "primary", profile or request.query_params.get("i"))


@media_router.get("/library/assets/{asset_id}/poster")
def asset_poster(asset_id: str, session: DbSession, request: Request) -> FileResponse:
    from ..media_access import verify_media_url

    verify_media_url(
        request,
        asset_id,
        resource="poster",
        exp=request.query_params.get("exp"),
        sig=request.query_params.get("sig"),
    )
    return _serve(session, asset_id, "poster", None)


@media_router.get("/library/assets/{asset_id}/stream")
def asset_stream(asset_id: str, session: DbSession, request: Request) -> FileResponse:
    """Serve the original with HTTP Range support so the player can seek."""
    from ..media_access import verify_media_url

    verify_media_url(
        request,
        asset_id,
        resource="stream",
        exp=request.query_params.get("exp"),
        sig=request.query_params.get("sig"),
    )
    return _serve(session, asset_id, "stream", None)


@media_router.get("/library/assets/{asset_id}/derivative/{derivative_id}")
def derivative_file(asset_id: str, derivative_id: str, session: DbSession, request: Request) -> FileResponse:
    from ...services.media.library import MediaError
    from ..media_access import verify_media_url

    verify_media_url(
        request,
        asset_id,
        resource="derivative",
        exp=request.query_params.get("exp"),
        sig=request.query_params.get("sig"),
        extra=derivative_id,
    )
    row = session.get(MediaDerivative, derivative_id)
    if row is None or row.asset_id != asset_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "DERIVATIVE_NOT_FOUND: no such derivative")
    library = MediaLibrary(session)
    try:
        path = library.derivative_file(row)
    except MediaError as exc:
        raise _http(exc) from exc
    if not is_inside_root(path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PERMISSION_DENIED: path outside the media root")
    return FileResponse(path, media_type=row.mime_type or "application/octet-stream")


# ------------------------------------------------------------------------- import
@router.post("/library/import/preview")
async def preview_import(request: Request, session: DbSession) -> dict[str, Any]:
    """Cheap duplicate pre-check before copying anything (name + size), for the dialog."""
    body = await request.body()
    require_ipc_signature(request, body)
    import json

    try:
        payload = json.loads(body or b"{}")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: invalid JSON body") from exc
    paths = payload.get("paths") or []
    if not isinstance(paths, list) or not paths:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_ERROR: paths is required")
    results = []
    library = MediaLibrary(session)
    for entry in paths[:200]:
        raw = entry.get("path") if isinstance(entry, dict) else str(entry)
        if not raw:
            continue
        path = Path(str(raw))
        try:
            size = path.stat().st_size
        except OSError:
            results.append({"path": str(path), "status": "unreadable", "reason": "cannot read the file"})
            continue
        existing = library._existing_by_size_and_name(path, path.name)
        results.append(
            {
                "path": str(path),
                "name": path.name,
                "size_bytes": size,
                "status": "probable_duplicate" if existing else "new",
                "existing_asset_id": existing.id if existing else None,
            }
        )
    return {"results": results}


@router.post("/library/import", response_model=ImportAccepted, status_code=status.HTTP_202_ACCEPTED)
async def import_media(request: Request, session: DbSession, audit: Audit) -> ImportAccepted:
    body = await request.body()
    require_ipc_signature(request, body)
    import json


    try:
        payload = ImportRequest.model_validate(json.loads(body or b"{}"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"VALIDATION_ERROR: {exc}") from exc

    library = MediaLibrary(session)
    accepted: list[dict[str, Any]] = []
    duplicates: list[ImportDuplicate] = []
    rejected: list[dict[str, str]] = []

    for entry in payload.paths:
        path = Path(entry.path)
        name = entry.original_filename or path.name
        if not path.exists() or not path.is_file():
            rejected.append({"path": str(path), "reason": "file does not exist"})
            continue
        if path.is_symlink():
            rejected.append({"path": str(path), "reason": "symbolic links are not accepted"})
            continue
        try:
            size = library.storage.check_source(path)
        except Exception as exc:  # noqa: BLE001 - policy rejections are reported per file
            rejected.append({"path": str(path), "reason": str(exc)})
            continue
        probable = library._existing_by_size_and_name(path, name)
        if probable is not None and probable.size_bytes == size:
            duplicates.append(
                ImportDuplicate(
                    path=str(path),
                    existing_asset_id=probable.id,
                    original_filename=probable.original_filename,
                    sha256=probable.sha256,
                    reason="same name and size as an asset already in the library",
                )
            )
            continue
        job_id = media_jobs.enqueue_media_job(
            session,
            media_jobs.JOB_IMPORT,
            {
                "source_path": str(path),
                "original_filename": name,
                "auto_thumbnail": payload.auto_thumbnail,
                "auto_preview": payload.auto_preview,
            },
            priority=payload.priority,
        )
        accepted.append({"job_id": job_id, "path": str(path), "filename": name, "size_bytes": size})

    audit.record(
        "media.import.request",
        resource_type="media_library",
        metadata={"accepted": len(accepted), "duplicates": len(duplicates), "rejected": len(rejected)},
    )
    return ImportAccepted(jobs=accepted, duplicates=duplicates, rejected=rejected)


# -------------------------------------------------------------- derivative requests
def _enqueue_asset_job(
    session,
    job_type: str,
    asset_id: str,
    payload: dict[str, Any],
    *,
    idempotency: str | None = None,
    audit: Audit | None = None,
) -> dict[str, Any]:
    library = MediaLibrary(session)
    try:
        asset = library.get_asset(asset_id)
    except AssetNotFound as exc:
        raise _http(exc) from exc
    if asset.intake_state != IntakeState.READY:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ASSET_NOT_READY: asset is {asset.intake_state}",
        )
    job_id = media_jobs.enqueue_media_job(
        session,
        job_type,
        {"asset_id": asset.id, **payload},
        idempotency_key=idempotency,
    )
    if audit is not None:
        audit.record(
            "media.job.request",
            resource_type="media_asset",
            resource_id=asset.id,
            metadata={"job_type": job_type, "job_id": job_id, **payload},
        )
    job = session.get(QueueJob, job_id)
    return job_progress(job) if job is not None else {"job_id": job_id}


@router.post("/library/assets/{asset_id}/thumbnail")
def request_thumbnail(
    asset_id: str, body: ThumbnailRequest, session: DbSession, audit: Audit
) -> dict[str, Any]:
    return _enqueue_asset_job(
        session,
        media_jobs.JOB_THUMBNAIL,
        asset_id,
        {"size": body.size, "timeline": body.timeline, "timeline_count": body.timeline or 10, "force": body.force},
        audit=audit,
    )


@router.post("/library/assets/{asset_id}/preview")
def request_preview(asset_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    return _enqueue_asset_job(session, media_jobs.JOB_PREVIEW, asset_id, {}, audit=audit)


@router.post("/library/assets/{asset_id}/proxy")
def request_proxy(asset_id: str, body: ProxyRequest, session: DbSession, audit: Audit) -> dict[str, Any]:
    return _enqueue_asset_job(
        session,
        media_jobs.JOB_PROXY,
        asset_id,
        {"height": body.height, "crf": body.crf},
        idempotency=f"proxy:{asset_id}:{body.height}:{body.crf}",
        audit=audit,
    )


@router.post("/library/assets/{asset_id}/transcode")
def request_transcode(asset_id: str, body: TranscodeRequest, session: DbSession, audit: Audit) -> dict[str, Any]:
    try:
        profile = body.profile
        from ...services.media.profiles import get_profile

        get_profile(profile)
    except ProfileError as exc:
        raise _http(exc) from exc
    return _enqueue_asset_job(
        session,
        media_jobs.JOB_TRANSCODE,
        asset_id,
        {"profile": profile, "height": body.height, "crf": body.crf, "fps": body.fps},
        idempotency=f"transcode:{asset_id}:{profile}:{body.height}:{body.crf}:{body.fps}",
        audit=audit,
    )


@router.post("/library/assets/{asset_id}/audio")
def request_audio(asset_id: str, body: AudioRequest, session: DbSession, audit: Audit) -> dict[str, Any]:
    return _enqueue_asset_job(
        session,
        media_jobs.JOB_AUDIO,
        asset_id,
        {"format": body.format},
        idempotency=f"audio:{asset_id}:{body.format}",
        audit=audit,
    )


@router.post("/library/assets/{asset_id}/waveform")
def request_waveform(asset_id: str, body: WaveformRequest, session: DbSession, audit: Audit) -> dict[str, Any]:
    return _enqueue_asset_job(
        session,
        media_jobs.JOB_WAVEFORM,
        asset_id,
        {"buckets": body.buckets, "image": body.image},
        idempotency=f"waveform:{asset_id}:{body.buckets}:{int(body.image)}",
        audit=audit,
    )


@router.get("/library/profiles")
def transcode_profiles() -> dict[str, Any]:
    return {"profiles": list_profiles()}


@router.delete("/library/derivatives/{derivative_id}")
def delete_derivative(
    derivative_id: str,
    session: DbSession,
    audit: Audit,
    confirm: bool = Query(default=False),
) -> dict[str, Any]:
    row = session.get(MediaDerivative, derivative_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "DERIVATIVE_NOT_FOUND: no such derivative")
    if not confirm:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "CONFIRMATION_REQUIRED: pass confirm=true to delete this derived file",
        )
    library = MediaLibrary(session)
    library.delete_derivative(row)
    if row.kind == DerivativeKind.THUMBNAIL:
        asset = session.get(MediaAsset, row.asset_id)
        if asset is not None and asset.thumbnail_path == row.rel_path:
            asset.thumbnail_path = None
            session.commit()
    audit.record(
        "media.derivative.delete",
        resource_type="media_derivative",
        resource_id=row.id,
        metadata={"kind": str(row.kind), "profile": row.profile},
    )
    return {"derivative_id": derivative_id, "deleted": True}


# --------------------------------------------------------------------------- jobs
@router.get("/library/jobs", response_model=MediaJobPage)
def list_media_jobs(
    session: DbSession,
    paging: Paging,
    state: QueueState | None = None,
    job_type: str | None = Query(default=None, max_length=60),
    asset_id: str | None = Query(default=None, max_length=26),
) -> MediaJobPage:
    where: list[Any] = [QueueJob.job_type.in_(MEDIA_JOB_TYPES)]
    if state:
        where.append(QueueJob.state == state)
    if job_type:
        where.append(QueueJob.job_type == job_type)
    if asset_id:
        where.append(QueueJob.payload_json["asset_id"].as_string() == asset_id)
    stmt = sa.select(QueueJob).where(*where)
    total = session.scalar(sa.select(sa.func.count()).select_from(QueueJob).where(*where)) or 0
    items = list(
        session.scalars(
            stmt.order_by(QueueJob.created_at.desc(), QueueJob.id.desc())
            .limit(paging.limit)
            .offset(paging.offset)
        )
    )
    return MediaJobPage(
        items=[JobOut(**job_progress(job)) for job in items],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/library/jobs/{job_id}", response_model=JobOut)
def get_media_job(job_id: str, session: DbSession) -> JobOut:
    job = session.get(QueueJob, job_id)
    if job is None or job.job_type not in MEDIA_JOB_TYPES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "JOB_NOT_FOUND: no such media job")
    return JobOut(**job_progress(job))


@router.post("/library/jobs/{job_id}/cancel")
def cancel_media_job(job_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    job = session.get(QueueJob, job_id)
    if job is None or job.job_type not in MEDIA_JOB_TYPES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "JOB_NOT_FOUND: no such media job")
    if job.state in (QueueState.SUCCEEDED, QueueState.DEAD_LETTER, QueueState.CANCELLED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"JOB_NOT_CANCELLABLE: job is already {job.state}")
    from ...services.queue import QueueService

    state_after = QueueService(session).cancel(job_id)
    audit.record("media.job.cancel", resource_type="queue_job", resource_id=job.id)
    return {"job_id": job_id, "state": str(state_after), "cancel_requested": True}


@router.post("/library/jobs/{job_id}/retry")
def retry_job(job_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    try:
        job = retry_media_job(session, job_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "JOB_NOT_FOUND: no such media job") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"JOB_NOT_RETRYABLE: {exc}") from exc
    audit.record("media.job.retry", resource_type="queue_job", resource_id=job.id)
    return job_progress(job)


@router.post("/library/recovery/scan", response_model=RecoveryReport)
def recovery_scan(
    session: DbSession,
    audit: Audit,
    older_than_minutes: float = Query(default=0, ge=0, le=10080),
) -> RecoveryReport:
    report = media_jobs.run_recovery(session, older_than_minutes=older_than_minutes)
    audit.record("media.recovery.scan", resource_type="media_library", metadata=report)
    return RecoveryReport(**report)


# ------------------------------------------------------------- media capabilities
@router.get("/library/capabilities")
def capabilities() -> dict[str, Any]:
    return library_supports()


__all__ = ["MEDIA_QUEUE", "MediaLibrary", "ProfileNotApplicable", "available_bytes", "list_profiles", "router"]
