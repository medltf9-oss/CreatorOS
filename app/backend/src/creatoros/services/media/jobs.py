"""Media jobs on the existing Phase 2 durable queue.

There is no second queue. Every job below is a ``queue_job`` row with ``queue="media"``, so
it inherits atomic claiming, leases, heartbeats, backoff, dead-letter and crash recovery
that Phase 2 tests already pin down.

What this module adds is the two things the queue alone cannot do for media work:

* **real progress** — the handler writes ``result_json.progress`` and the current stage while
  it runs, so the Processing center shows measured numbers instead of a spinner;
* **real cancellation** — ``cancel_requested`` is polled through a short-lived session (the
  handler's own session holds a transaction snapshot, so it would never see another
  connection's write) and the FFmpeg child is killed.

Progress writes happen on a separate session on purpose: the worker's session must not hold
a write transaction open for the minutes an encode takes, or the API and the UI would block.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...db.enums import (
    DerivativeKind,
    DerivativeState,
    IntakeState,
    MediaKind,
    QueueState,
)
from ...db.models import MediaAsset, MediaDerivative, QueueJob
from ..backup import AuditService
from ..queue import QueueService
from .library import (
    AssetNotReady,
    MediaError,
    MediaLibrary,
)
from .profiles import ProfileError, ProfileNotApplicable, get_profile

log = get_logger(__name__)

MEDIA_QUEUE = "media"

JOB_IMPORT = "media.import"
JOB_PROBE = "media.probe"
JOB_THUMBNAIL = "media.thumbnail.generate"
JOB_TIMELINE = "media.timeline.generate"
JOB_PREVIEW = "media.preview.generate"
JOB_PROXY = "media.proxy.generate"
JOB_TRANSCODE = "media.transcode"
JOB_AUDIO = "media.audio.extract"
JOB_WAVEFORM = "media.waveform.generate"
JOB_RECOVERY = "media.recovery.scan"

MEDIA_JOB_TYPES = (
    JOB_IMPORT,
    JOB_PROBE,
    JOB_THUMBNAIL,
    JOB_TIMELINE,
    JOB_PREVIEW,
    JOB_PROXY,
    JOB_TRANSCODE,
    JOB_AUDIO,
    JOB_WAVEFORM,
    JOB_RECOVERY,
)


class JobCancelled(ValueError):
    """Raised when the user cancelled. ValueError makes the queue treat it as non-retryable."""

    code = "MEDIA_JOB_CANCELLED"


class MediaJobContext:
    """Progress + cancellation for one running job, without holding a transaction."""

    def __init__(self, session_factory: Callable[[], Session], job_id: str, *, min_interval_s: float = 0.35) -> None:
        self.session_factory = session_factory
        self.job_id = job_id
        self.min_interval_s = min_interval_s
        self._last_write = 0.0
        self._cancelled = False
        self._last_stage: str | None = None

    def should_cancel(self) -> bool:
        if self._cancelled:
            return True
        session = self.session_factory()
        try:
            flag = session.scalar(sa.select(QueueJob.cancel_requested).where(QueueJob.id == self.job_id))
            self._cancelled = bool(flag)
        except sa.exc.SQLAlchemyError:  # pragma: no cover - database unavailable
            self._cancelled = False
        finally:
            session.close()
        return self._cancelled

    def check(self) -> None:
        if self.should_cancel():
            raise JobCancelled("cancelled by the user")

    def progress(self, fraction: float, info: dict[str, Any] | None = None, *, force: bool = False) -> None:
        now = time.time()
        stage = str((info or {}).get("stage") or self._last_stage or "")
        if not force and now - self._last_write < self.min_interval_s and stage == self._last_stage:
            return
        self._last_write = now
        self._last_stage = stage
        payload = {
            "progress": max(0.0, min(1.0, round(float(fraction), 4))),
            "stage": stage or "working",
            "updated_at": datetime.now(UTC).isoformat(),
            **(info or {}),
        }
        session = self.session_factory()
        try:
            session.execute(
                sa.update(QueueJob)
                .where(QueueJob.id == self.job_id)
                .values(result_json=payload, heartbeat_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            )
            session.commit()
        except sa.exc.SQLAlchemyError:  # pragma: no cover - progress is best effort
            session.rollback()
        finally:
            session.close()


# ------------------------------------------------------------------ registration
def enqueue_media_job(
    session: Session,
    job_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    priority: int = 50,
    delay_seconds: float = 0.0,
    max_attempts: int = 3,
) -> str:
    """Enqueue a media job. The queue's own dedupe handles duplicate open jobs."""
    result = QueueService(session).enqueue(
        job_type,
        payload,
        queue=MEDIA_QUEUE,
        priority=priority,
        delay_seconds=delay_seconds,
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
    )
    return result.job_id


# ---------------------------------------------------------------------- handlers
def _asset(session: Session, asset_id: str) -> MediaAsset:
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise MediaError(f"media asset {asset_id} does not exist")
    return asset


def handle_import(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Import one file: copy → hash → probe → register → thumbnail/waveform."""
    from pathlib import Path

    source = payload.get("source_path")
    if not source:
        raise ValueError("media.import requires source_path")
    asset_id = payload.get("asset_id")
    library = MediaLibrary(session)
    ctx.progress(0.0, {"stage": "copy"}, force=True)
    outcome = library.import_file(
        Path(source),
        progress=ctx.progress,
        should_cancel=ctx.should_cancel,
        auto_thumbnail=payload.get("auto_thumbnail"),
        original_filename=payload.get("original_filename"),
    )
    ctx.progress(1.0, {"stage": outcome.status}, force=True)

    audit = AuditService(session)
    audit.record(
        "media.import",
        resource_type="media_asset",
        resource_id=outcome.asset_id or asset_id or "unknown",
        outcome="success" if outcome.status == "imported" else "skipped",
        metadata={
            "status": outcome.status,
            "sha256": outcome.sha256,
            "original_filename": outcome.original_filename,
            "reason": outcome.reason,
            "elapsed_ms": outcome.elapsed_ms,
        },
    )
    return outcome.as_dict()


def handle_probe(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
    asset = _asset(session, str(payload.get("asset_id") or ""))
    library = MediaLibrary(session)
    ctx.progress(0.2, {"stage": "probe"}, force=True)
    probe = library.reprobe(asset)
    ctx.progress(1.0, {"stage": "probe"}, force=True)
    return {"asset_id": asset.id, "probe_version": probe.probe_version, "kind": probe.kind}


def handle_thumbnail(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
    asset = _asset(session, str(payload.get("asset_id") or ""))
    library = MediaLibrary(session)
    rows = library.ensure_thumbnail(
        asset,
        size=payload.get("size"),
        job_id=ctx.job_id,
        progress=ctx.progress,
        should_cancel=ctx.should_cancel,
        force=bool(payload.get("force")),
    )
    if payload.get("timeline"):
        rows += library.ensure_timeline(
            asset,
            count=int(payload.get("timeline_count") or 10),
            job_id=ctx.job_id,
            progress=ctx.progress,
            should_cancel=ctx.should_cancel,
        )
    ctx.progress(1.0, {"stage": "thumbnail"}, force=True)
    return {"asset_id": asset.id, "derivatives": [row.id for row in rows]}


def handle_timeline(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
    asset = _asset(session, str(payload.get("asset_id") or ""))
    library = MediaLibrary(session)
    rows = library.ensure_timeline(
        asset,
        count=int(payload.get("count") or 10),
        job_id=ctx.job_id,
        progress=ctx.progress,
        should_cancel=ctx.should_cancel,
    )
    return {"asset_id": asset.id, "derivatives": [row.id for row in rows]}


def _transcode_handler(profile_id: str | None = None):
    def handler(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
        asset = _asset(session, str(payload.get("asset_id") or ""))
        target_profile = profile_id or str(payload.get("profile") or "")
        if not target_profile:
            raise ValueError("a transcode job requires a profile")
        profile = get_profile(target_profile)
        library = MediaLibrary(session)
        ctx.progress(0.02, {"stage": profile.id}, force=True)
        row = library.transcode(
            asset,
            profile.id,
            height=payload.get("height"),
            crf=payload.get("crf"),
            fps=payload.get("fps"),
            job_id=ctx.job_id,
            progress=ctx.progress,
            should_cancel=ctx.should_cancel,
        )
        ctx.progress(1.0, {"stage": profile.id}, force=True)
        return {
            "asset_id": asset.id,
            "derivative_id": row.id,
            "profile": profile.id,
            "rel_path": row.rel_path,
            "size_bytes": row.size_bytes,
            "width": row.width,
            "height": row.height,
            "duration_ms": row.duration_ms,
        }

    return handler


def handle_audio(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
    fmt = str(payload.get("format") or "wav").lower()
    profile = "audio_wav" if fmt in ("wav", "pcm") else "audio_aac"
    return _transcode_handler(profile)(session, ctx, payload)


def handle_waveform(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
    asset = _asset(session, str(payload.get("asset_id") or ""))
    library = MediaLibrary(session)
    rows = library.ensure_waveform(
        asset,
        buckets=int(payload.get("buckets") or 800),
        job_id=ctx.job_id,
        progress=ctx.progress,
        should_cancel=ctx.should_cancel,
    )
    if payload.get("image"):
        rows += library.ensure_waveform_image(asset, job_id=ctx.job_id, should_cancel=ctx.should_cancel)
    return {"asset_id": asset.id, "derivatives": [row.id for row in rows]}


def handle_recovery(session: Session, ctx: MediaJobContext, payload: dict[str, Any]) -> dict[str, Any]:
    return run_recovery(session, older_than_minutes=float(payload.get("older_than_minutes") or 15))


# ---------------------------------------------------------------------- recovery
def run_recovery(session: Session, *, older_than_minutes: float = 15) -> dict[str, Any]:
    """Clean incomplete work and reconcile rows with the filesystem.

    Called at boot and from ``media.recovery.scan``. Three cases:

    * staging files left by a crash (older than the window) are deleted;
    * assets marked ``copying``/``probing`` without a job still running become quarantined
      with a reason, so nothing looks half-imported forever;
    * ready derivatives whose file vanished are marked failed, so the UI can regenerate.
    """
    library = MediaLibrary(session)
    swept = library.storage.sweep_staging(older_than_seconds=max(0.0, older_than_minutes * 60))

    active_jobs = set(
        session.scalars(
            sa.select(QueueJob.payload_json).where(
                QueueJob.job_type.in_(MEDIA_JOB_TYPES),
                QueueJob.state.in_([QueueState.QUEUED, QueueState.CLAIMED, QueueState.RUNNING]),
            )
        ).all()
    )
    active_asset_ids = {
        str((payload or {}).get("asset_id"))
        for payload in active_jobs
        if isinstance(payload, dict) and payload.get("asset_id")
    }

    stuck = session.scalars(
        sa.select(MediaAsset).where(MediaAsset.intake_state.in_([IntakeState.COPYING, IntakeState.PROBING]))
    ).all()
    quarantined = 0
    for asset in stuck:
        if asset.id in active_asset_ids:
            continue
        asset.intake_state = IntakeState.QUARANTINED
        asset.quarantine_reason = "the import was interrupted before it finished; re-import the file"
        quarantined += 1

    missing = 0
    for row in session.scalars(
        sa.select(MediaDerivative).where(MediaDerivative.state == DerivativeState.READY)
    ).all():
        if not row.rel_path:
            row.state = DerivativeState.FAILED
            row.error_code = "DERIVATIVE_MISSING"
            missing += 1
            continue
        try:
            if not library.storage.resolve_stored(row.rel_path).exists():
                row.state = DerivativeState.FAILED
                row.error_code = "DERIVATIVE_MISSING"
                missing += 1
        except Exception:  # noqa: BLE001 - an unsafe path is also a missing derivative
            row.state = DerivativeState.FAILED
            row.error_code = "DERIVATIVE_MISSING"
            missing += 1

    session.commit()
    report = {
        "staging_removed": swept["removed"],
        "bytes_freed": swept["bytes_freed"],
        "assets_quarantined": quarantined,
        "derivatives_missing": missing,
        "older_than_minutes": older_than_minutes,
    }
    AuditService(session).record("media.recovery.scan", resource_type="media_library", metadata=report)
    return report


def retry_media_job(session: Session, job_id: str) -> QueueJob:
    """Requeue a dead-letter media job, but only if its target still exists."""
    job = session.get(QueueJob, job_id)
    if job is None:
        raise KeyError(job_id)
    if job.state not in (QueueState.DEAD_LETTER, QueueState.FAILED):
        raise ValueError(f"job {job_id} is {job.state}, not retryable")
    payload = dict(job.payload_json or {})
    asset_id = payload.get("asset_id")
    if asset_id and session.get(MediaAsset, str(asset_id)) is None:
        raise ValueError("the media asset for this job no longer exists")
    job.state = QueueState.QUEUED
    job.attempts = 0
    job.cancel_requested = False
    job.run_after = datetime.now(UTC)
    job.last_error = None
    job.result_json = {"progress": 0.0, "stage": "requeued"}
    job.finished_at = None
    session.commit()
    AuditService(session).record(
        "media.job.retry", resource_type="queue_job", resource_id=job.id, metadata={"job_type": job.job_type}
    )
    return job


def job_progress(job: QueueJob) -> dict[str, Any]:
    result = job.result_json or {}
    progress = result.get("progress") if isinstance(result, dict) else None
    return {
        "job_id": job.id,
        "job_type": job.job_type,
        "state": str(job.state),
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "progress": float(progress) if isinstance(progress, (int, float)) else None,
        "stage": result.get("stage") if isinstance(result, dict) else None,
        "asset_id": (job.payload_json or {}).get("asset_id"),
        "last_error": job.last_error,
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "result": {k: v for k, v in result.items() if k not in ("progress", "stage", "updated_at")}
        if isinstance(result, dict)
        else {},
    }


# --------------------------------------------------------------- handler factory
def build_media_handlers(session_factory: Callable[[], Session]) -> dict[str, Callable[..., Any]]:
    """Wrap each handler so the queue passes a job, not a job plus console.

    Handlers take ``(session, ctx, payload)``; the queue calls ``(session, job_view)``. This
    adapter keeps the two contracts apart and gives every handler the same progress surface.
    """

    def adapt(session: Session, job: Any) -> dict[str, Any]:
        ctx = MediaJobContext(session_factory, job.id)
        ctx.check()
        return _HANDLERS[job.job_type](session, ctx, dict(job.payload or {}))

    return {job_type: adapt for job_type in _HANDLERS}


_HANDLERS: dict[str, Callable[[Session, MediaJobContext, dict[str, Any]], dict[str, Any]]] = {
    JOB_IMPORT: handle_import,
    JOB_PROBE: handle_probe,
    JOB_THUMBNAIL: handle_thumbnail,
    JOB_TIMELINE: handle_timeline,
    JOB_PREVIEW: _transcode_handler("fast_preview"),
    JOB_PROXY: lambda session, ctx, payload: _transcode_handler("editing_proxy")(
        session,
        ctx,
        {**payload, "height": payload.get("height") or 480},
    ),
    JOB_TRANSCODE: _transcode_handler(),
    JOB_AUDIO: handle_audio,
    JOB_WAVEFORM: handle_waveform,
    JOB_RECOVERY: handle_recovery,
}

__all__ = [
    "JOB_AUDIO",
    "JOB_IMPORT",
    "JOB_PREVIEW",
    "JOB_PROBE",
    "JOB_PROXY",
    "JOB_RECOVERY",
    "JOB_THUMBNAIL",
    "JOB_TIMELINE",
    "JOB_TRANSCODE",
    "JOB_WAVEFORM",
    "MEDIA_JOB_TYPES",
    "MEDIA_QUEUE",
    "AssetNotReady",
    "DerivativeKind",
    "JobCancelled",
    "MediaJobContext",
    "MediaKind",
    "ProfileError",
    "ProfileNotApplicable",
    "build_media_handlers",
    "enqueue_media_job",
    "handle_import",
    "job_progress",
    "retry_media_job",
    "run_recovery",
]
