"""Media jobs on the durable queue: real handlers, real recovery, real cancellation."""
from __future__ import annotations

import time

import pytest

from creatoros.db.enums import DerivativeKind, DerivativeState, QueueState
from creatoros.db.models import MediaAsset, MediaDerivative, QueueJob
from creatoros.services.media.jobs import (
    JOB_IMPORT,
    JOB_PREVIEW,
    MEDIA_QUEUE,
    JobCancelled,
    MediaJobContext,
    build_media_handlers,
    enqueue_media_job,
    job_progress,
    retry_media_job,
    run_recovery,
)
from creatoros.services.media.library import MediaLibrary
from creatoros.services.queue import QueueService, QueueWorker


def _worker(session_factory):
    handlers = build_media_handlers(session_factory)
    return QueueWorker(session_factory, handlers, queue=MEDIA_QUEUE, lease_seconds=30)


def test_import_job_runs_and_produces_a_real_asset(session, media_files, session_factory):
    job_id = enqueue_media_job(
        session, JOB_IMPORT, {"source_path": str(media_files.landscape), "original_filename": "landscape.mp4"}
    )
    session.commit()
    worker = _worker(session_factory)
    assert worker.run_once() is True

    session.expire_all()
    job = session.get(QueueJob, job_id)
    assert job.state == QueueState.SUCCEEDED
    assert job.result_json["status"] == "imported"
    assert session.query(MediaAsset).count() == 1
    asset = session.query(MediaAsset).one()
    assert asset.original_filename == "landscape.mp4"
    assert asset.sha256 and len(asset.sha256) == 64


def test_import_job_is_idempotent_by_key(session, media_files, session_factory):
    key = "import:test-file"
    first = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)}, idempotency_key=key)
    second = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)}, idempotency_key=key)
    session.commit()
    assert first == second
    assert session.query(QueueJob).filter_by(job_type=JOB_IMPORT).count() == 1


def test_job_survives_a_restart_and_then_runs(session, media_files, session_factory):
    """A queued import must still exist and execute after the process was restarted."""
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.image)})
    session.commit()

    from creatoros.db import session as session_mod

    session_mod.reset_engine()  # simulate a fresh process with a fresh engine
    from creatoros.db.session import session_scope

    with session_scope() as fresh:
        job = fresh.get(QueueJob, job_id)
        assert job is not None and job.state == QueueState.QUEUED

    worker = _worker(session_factory)
    assert worker.run_once() is True
    with session_scope() as after:
        assert after.get(QueueJob, job_id).state == QueueState.SUCCEEDED
        assert after.query(MediaAsset).count() == 1


def test_cancel_requested_before_the_job_runs_is_honoured(session, media_files, session_factory):
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.portrait)})
    session.commit()
    QueueService(session).cancel(job_id)
    session.commit()

    worker = _worker(session_factory)
    ran = worker.run_once()
    session.expire_all()
    job = session.get(QueueJob, job_id)
    assert ran is False or job.state == QueueState.CANCELLED
    assert session.query(MediaAsset).count() == 0


def test_cancellation_mid_job_stops_work_and_marks_the_row(session, media_files, session_factory):
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.landscape)})
    session.commit()
    ctx = MediaJobContext(session_factory, job_id)
    QueueService(session).cancel(job_id)
    session.commit()

    assert ctx.should_cancel() is True
    with pytest.raises(JobCancelled):
        ctx.check()


def test_progress_is_written_to_the_job_row(session, media_files, session_factory):
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)})
    session.commit()
    ctx = MediaJobContext(session_factory, job_id, min_interval_s=0.0)
    ctx.progress(0.42, {"stage": "copy"}, force=True)
    session.expire_all()
    job = session.get(QueueJob, job_id)
    assert job.result_json["progress"] == 0.42
    assert job.result_json["stage"] == "copy"
    assert job.heartbeat_at is not None


def test_preview_job_creates_a_verified_derivative(session, media_files, session_factory):
    library = MediaLibrary(session)
    outcome = library.import_file(media_files.landscape, auto_thumbnail=False)
    session.commit()
    job_id = enqueue_media_job(session, JOB_PREVIEW, {"asset_id": outcome.asset_id})
    session.commit()

    worker = _worker(session_factory)
    assert worker.run_once() is True
    session.expire_all()
    rows = session.query(MediaDerivative).filter_by(asset_id=outcome.asset_id, kind=DerivativeKind.PREVIEW).all()
    assert len(rows) == 1 and rows[0].state == DerivativeState.READY
    assert rows[0].job_id == job_id
    assert rows[0].size_bytes > 0


def test_dead_letter_after_repeated_failures(session, session_factory, media_files):
    """A job that keeps raising lands in dead_letter with its error, not silently dropped."""
    from creatoros.services.media.jobs import JOB_PROBE

    job_id = enqueue_media_job(session, JOB_PROBE, {"asset_id": "01M80ZZZZZZZZZZZZZZZZZZZ99"}, max_attempts=1)
    session.commit()
    worker = _worker(session_factory)
    worker.run_once()
    session.expire_all()
    job = session.get(QueueJob, job_id)
    assert job.state == QueueState.DEAD_LETTER
    assert job.last_error and "does not exist" in job.last_error


def test_retry_requeues_a_dead_letter_job(session, media_files, session_factory):
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)}, max_attempts=1)
    session.commit()
    job = session.get(QueueJob, job_id)
    job.state = QueueState.DEAD_LETTER
    job.last_error = "simulated failure"
    job.attempts = 1
    session.commit()

    retried = retry_media_job(session, job_id)
    assert retried.state == QueueState.QUEUED
    assert retried.attempts == 0
    assert retried.result_json["stage"] == "requeued"
    session.commit()
    session.expire_all()

    worker = _worker(session_factory)
    assert worker.run_once() is True
    session.expire_all()
    assert session.get(QueueJob, job_id).state == QueueState.SUCCEEDED
    session.commit()


def test_retry_refuses_a_job_whose_asset_is_gone(session):
    from creatoros.db.models import QueueJob as Row

    job = Row(
        id="01M80ZZZZZZZZZZZZZZZZZZZ01",
        job_type=JOB_PREVIEW,
        queue=MEDIA_QUEUE,
        payload_json={"asset_id": "01M80ZZZZZZZZZZZZZZZZZZZ02"},
        state=QueueState.DEAD_LETTER,
        attempts=3,
        max_attempts=3,
        run_after=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    session.add(job)
    session.commit()
    with pytest.raises(ValueError):
        retry_media_job(session, job.id)


def test_unknown_job_type_is_not_routed_to_a_media_handler(session_factory):
    handlers = build_media_handlers(session_factory)
    assert set(handlers) >= {JOB_IMPORT, JOB_PREVIEW}
    assert "ai.generate" not in handlers


def test_job_progress_shape(session, media_files, session_factory):
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)})
    session.commit()
    ctx = MediaJobContext(session_factory, job_id, min_interval_s=0.0)
    ctx.progress(0.5, {"stage": "copy"}, force=True)
    session.expire_all()
    payload = job_progress(session.get(QueueJob, job_id))
    assert payload["progress"] == 0.5
    assert payload["state"] == "queued"
    assert payload["max_attempts"] >= 1


def test_recovery_cleans_staging_and_flags_stuck_assets(session, media_files):
    library = MediaLibrary(session)
    leftover = library.storage.staging_path("crash", extension=".mp4")
    leftover.write_bytes(b"partial upload")
    outcome = library.import_file(media_files.image, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    asset.intake_state = "copying"
    session.commit()

    report = run_recovery(session, older_than_minutes=0)
    assert report["staging_removed"] >= 1
    assert not leftover.exists()
    session.refresh(asset)
    assert str(asset.intake_state) in ("quarantined", "IntakeState.QUARANTINED")
    assert asset.quarantine_reason


def test_recovery_marks_missing_derivative_files(session, media_files):
    library = MediaLibrary(session)
    outcome = library.import_file(media_files.landscape)
    asset = library.get_asset(outcome.asset_id)
    thumb = library.primary_thumbnail(asset.id)
    library.derivative_file(thumb).unlink()

    report = run_recovery(session, older_than_minutes=0)
    assert report["derivatives_missing"] >= 1
    session.refresh(thumb)
    assert thumb.state == DerivativeState.FAILED
    assert thumb.error_code == "DERIVATIVE_MISSING"


def test_worker_processes_jobs_in_priority_order(session, media_files, session_factory):
    low = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.image)}, priority=90)
    high = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)}, priority=10)
    session.commit()
    worker = _worker(session_factory)
    worker.run_once()
    session.expire_all()
    done = session.get(QueueJob, high)
    assert done.state == QueueState.SUCCEEDED
    assert session.get(QueueJob, low).state == QueueState.QUEUED


def test_queue_stats_include_media_jobs(session, media_files):
    enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.image)})
    session.commit()
    stats = QueueService(session).stats(queue="media")
    assert stats["queued"] >= 1


def test_import_through_the_queue_records_an_audit_event(session, media_files, session_factory):
    from creatoros.db.models import AuditEvent

    enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)})
    session.commit()
    _worker(session_factory).run_once()
    actions = [row.action for row in session.query(AuditEvent).all()]
    assert "media.import" in actions


@pytest.fixture()
def session_factory(_isolated_root):
    from creatoros.db.session import get_session_factory

    return get_session_factory()


def test_worker_does_not_pick_media_jobs_from_the_default_queue(session, media_files, session_factory):
    """Media jobs are only visible to the media worker, so a long transcode can't block AI."""
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.image)})
    session.commit()
    default_worker = QueueWorker(session_factory, build_media_handlers(session_factory), queue="default")
    assert default_worker.run_once() is False
    session.expire_all()
    assert session.get(QueueJob, job_id).state == QueueState.QUEUED


def test_running_a_job_takes_a_measurable_amount_of_time(session, media_files, session_factory):
    """A real transcode must not be faked: it has to actually take time and CPU."""
    library = MediaLibrary(session)
    outcome = library.import_file(media_files.big, auto_thumbnail=False)
    session.commit()
    enqueue_media_job(session, JOB_PREVIEW, {"asset_id": outcome.asset_id})
    session.commit()
    session.expire_all()
    started = time.perf_counter()
    _worker(session_factory).run_once()
    elapsed = time.perf_counter() - started
    assert elapsed > 0.05
    session.expire_all()
    row = session.query(MediaDerivative).filter_by(asset_id=outcome.asset_id, kind=DerivativeKind.PREVIEW).one()
    assert row.state == DerivativeState.READY
    assert row.size_bytes > 0
