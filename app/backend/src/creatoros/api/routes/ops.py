"""Operational routes: queue inspection, audit trail, notifications, backups."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...db.enums import QueueState
from ...db.models import Notification, QueueJob
from ...db.session import database_path
from ...schemas.api import AuditOut, NotificationOut, Page, QueueEnqueueRequest, QueueJobOut
from ...services.backup import BackupService
from ...services.queue import QueueService
from ..deps import Audit, DbSession, Paging, paginate

router = APIRouter(tags=["ops"])


@router.get("/queue/stats")
def queue_stats(session: DbSession) -> dict[str, Any]:
    service = QueueService(session)
    return {"counts": service.stats(), "due_now": service.due_count()}


@router.post("/queue/jobs", response_model=QueueJobOut, status_code=status.HTTP_201_CREATED)
def enqueue_job(body: QueueEnqueueRequest, session: DbSession, audit: Audit) -> QueueJob:
    service = QueueService(session)
    result = service.enqueue(
        body.job_type,
        body.payload,
        queue=body.queue,
        priority=body.priority,
        delay_seconds=body.delay_seconds,
        max_attempts=body.max_attempts,
        idempotency_key=body.idempotency_key,
    )
    job = session.get(QueueJob, result.job_id)
    if job is None:  # pragma: no cover - defensive
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "job vanished after enqueue")
    audit.record(
        "queue.enqueue",
        resource_type="queue_job",
        resource_id=job.id,
        metadata={"job_type": body.job_type, "created": result.created, "reason": result.reason},
    )
    return job


@router.get("/queue/jobs", response_model=Page[QueueJobOut])
def list_jobs(session: DbSession, paging: Paging, state: QueueState | None = None) -> Page[QueueJobOut]:
    where = [QueueJob.state == state] if state else []
    items, total = paginate(session, QueueJob, paging, *where)
    return Page(items=[QueueJobOut.model_validate(i) for i in items], total=total, limit=paging.limit, offset=paging.offset)


@router.post("/queue/jobs/{job_id}/cancel", response_model=QueueJobOut)
def cancel_job(job_id: str, session: DbSession, audit: Audit) -> QueueJob:
    job = session.get(QueueJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.state in (QueueState.SUCCEEDED, QueueState.DEAD_LETTER):
        raise HTTPException(status.HTTP_409_CONFLICT, f"job already terminal ({job.state.value})")
    QueueService(session).cancel(job_id)
    session.refresh(job)
    audit.record("queue.cancel", resource_type="queue_job", resource_id=job_id)
    return job


@router.get("/audit", response_model=Page[AuditOut])
def list_audit(
    session: DbSession,
    paging: Paging,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> Page[AuditOut]:
    from ...db.models import AuditEvent

    where = []
    if action:
        where.append(AuditEvent.action == action)
    if resource_type:
        where.append(AuditEvent.resource_type == resource_type)
    if resource_id:
        where.append(AuditEvent.resource_id == resource_id)
    items, total = paginate(session, AuditEvent, paging, *where)
    return Page(items=[AuditOut.model_validate(i) for i in items], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/notifications", response_model=Page[NotificationOut])
def list_notifications(session: DbSession, paging: Paging, unread_only: bool = False) -> Page[NotificationOut]:
    where = [Notification.read_at.is_(None)] if unread_only else []
    items, total = paginate(session, Notification, paging, *where)
    return Page(items=[NotificationOut.model_validate(i) for i in items], total=total, limit=paging.limit, offset=paging.offset)


@router.post("/backups")
def create_backup(session: DbSession, audit: Audit) -> dict[str, Any]:
    service = BackupService(database_path())
    result = service.create(label="api")
    audit.record(
        "backup.create",
        resource_type="backup",
        resource_id=result.path.name,
        metadata={"sha256": result.sha256, "integrity": result.integrity},
    )
    return {
        "path": str(result.path),
        "sha256": result.sha256,
        "size_bytes": result.size_bytes,
        "integrity": result.integrity,
        "tables": result.tables,
    }


@router.get("/backups")
def list_backups() -> dict[str, Any]:
    service = BackupService(database_path())
    files = sorted(service.backup_dir.glob("creatoros-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "directory": str(service.backup_dir),
        "items": [
            {"name": f.name, "size_bytes": f.stat().st_size, "verified": service.verify(f)["ok"]} for f in files
        ],
    }
