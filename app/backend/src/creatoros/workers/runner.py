"""Queue worker process loop and job handlers.

Handlers are deliberately small and side-effect-scoped for Phase 2: the queue
must be proven durable before any publishing work is attached to it.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from ..core.paths import ensure_dir
from ..services.backup import AuditService, BackupService
from ..services.queue import QueueWorker, worker_id

log = logging.getLogger("creatoros.worker")


def handle_noop(_session: Any, job: Any) -> dict[str, Any]:
    """Proves the queue works end to end without pretending to do real work."""
    return {"ok": True, "echo": job.payload}


def handle_backup(session: Any, job: Any) -> dict[str, Any]:
    from ..db.session import database_path

    label = str(job.payload.get("label", "queue"))
    result = BackupService(database_path()).create(label=label)
    AuditService(session).record(
        "backup.create",
        resource_type="backup",
        resource_id=result.path.name,
        metadata={"trigger": "queue", "integrity": result.integrity},
    )
    return {"path": str(result.path), "sha256": result.sha256, "integrity": result.integrity}


def handle_cleanup_working(session: Any, job: Any) -> dict[str, Any]:
    """Delete working files older than the retention window (default 14 days)."""
    import datetime as dt
    from pathlib import Path

    days = int(job.payload.get("days", 14))
    cutoff = time.time() - days * 86400
    working = ensure_dir("media_working")
    removed = 0
    for path in Path(working).rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    AuditService(session).record(
        "media.cleanup",
        resource_type="media_working",
        outcome="success",
        metadata={"removed": removed, "older_than_days": days, "at": dt.datetime.now(dt.UTC).isoformat()},
    )
    return {"removed": removed, "days": days}


def build_scheduler(session):
    """One sweep, two dispatchers: publishing first, then analytics.

    The analytics dispatcher does not get its own thread: it runs inside the existing sweeper tick,
    so a collection can never delay a scheduled publish that is due in the same second.
    """
    from ..services.analytics.scheduler import AnalyticsScheduler, CombinedScheduler
    from ..services.publishing.executor import PublishExecutor
    from ..services.publishing.scheduler import PublishScheduler

    return CombinedScheduler(
        PublishScheduler(session, executor=PublishExecutor(session)),
        AnalyticsScheduler(session, enabled=analytics_worker_count() > 0),
    )


def build_worker(session_factory, gateway=None, governor=None) -> BackgroundWorker:
    handlers = {
        "noop": handle_noop,
        "backup.create": handle_backup,
        "media.cleanup_working": handle_cleanup_working,
    }

    # Phase 5: media jobs run on the same durable queue, in their own named queue so a long
    # transcode cannot starve backup/AI work. One media worker means one FFmpeg at a time.
    from ..services.media.jobs import build_media_handlers

    handlers.update(build_media_handlers(session_factory))

    # Phase 7: publishing runs on its own lane for the same reason - an upload is minutes long and
    # network-bound, and must never delay a backup. The handler is the *only* way a publish runs.
    from ..services.publishing.executor import build_publish_handlers

    handlers.update(build_publish_handlers(session_factory))

    # Phase 8: analytics runs on a fourth lane for the same reason publishing has one: a collection
    # is network-bound and must never delay a backup, a transcode or an upload.
    from ..services.analytics.jobs import build_analytics_handlers

    handlers.update(build_analytics_handlers(session_factory))

    # AI jobs are only registered when the gateway/governor exist, so a phase-2
    # database can still be used without an Ollama install.
    if gateway is not None and governor is not None:
        from ..services.ai import build_ai_job_handler
        from ..services.models import ModelRegistry, ModelRouter

        def router_factory(session):
            return ModelRouter(ModelRegistry(session), governor, gateway)

        handlers["ai.generate"] = build_ai_job_handler(session_factory, gateway, governor, router_factory)

        def handle_unload(session, job):
            import asyncio

            tag = str(job.payload.get("model", "")).strip()
            if not tag:
                raise ValueError("ai.unload requires a model tag")
            unloaded = asyncio.run(gateway.unload_model(tag))
            AuditService(session).record(
                "ai.model.unload", resource_type="model", resource_id=tag, metadata={"trigger": "queue"}
            )
            return {"model": tag, "unloaded": unloaded}

        handlers["ai.unload"] = handle_unload

        # Phase 4: the orchestrator runs on the same durable queue and the same
        # AiService, so governor admission and execution history apply unchanged.
        from ..services.orchestrator import build_objective_handler

        def ai_service_factory(session):
            from ..services.ai import AiService

            return AiService(session, gateway, governor, router_factory(session))

        handlers["agents.objective"] = build_objective_handler(session_factory, ai_service_factory)
    return BackgroundWorker(
        QueueWorker(
            session_factory,
            handlers,
            owner=worker_id("creatoros"),
            on_error=lambda job_id, exc: log.error("job %s failed: %s", job_id, exc),
        ),
        media_worker=QueueWorker(
            session_factory,
            handlers,
            owner=worker_id("creatoros-media"),
            queue="media",
            on_error=lambda job_id, exc: log.error("media job %s failed: %s", job_id, exc),
        ),
        publish_workers=[
            QueueWorker(
                session_factory,
                handlers,
                owner=worker_id(f"creatoros-publish-{index}"),
                queue="publish",
                on_error=lambda job_id, exc: log.error("publish job %s failed: %s", job_id, exc),
            )
            for index in range(publish_worker_count())
        ],
        analytics_workers=[
            QueueWorker(
                session_factory,
                handlers,
                owner=worker_id(f"creatoros-analytics-{index}"),
                queue="analytics",
                on_error=lambda job_id, exc: log.error("analytics job %s failed: %s", job_id, exc),
            )
            for index in range(analytics_worker_count())
        ],
        scheduler_factory=build_scheduler,
    )


def scheduler_interval() -> float:
    """Seconds between schedule sweeps. `0` disables the sweeper entirely."""
    import os

    raw = os.environ.get("CREATOROS_SCHEDULER_SECONDS", "15")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 15.0


def analytics_worker_count() -> int:
    """How many analytics workers to run.

    `0` disables the lane completely (and with it the dispatcher), which is the rollback switch:
    analytics then stops collecting but nothing else changes. Clamped to 2 because two 4 GB-VRAM
    machines' worth of provider polling is not a real use case.
    """
    import os

    raw = os.environ.get("CREATOROS_ANALYTICS_WORKERS", "1")
    try:
        return max(0, min(int(raw), 2))
    except (TypeError, ValueError):
        return 1


def publish_worker_count() -> int:
    """How many publish workers to run. One by default: an upload is bandwidth-bound anyway."""
    import os

    raw = os.environ.get("CREATOROS_PUBLISH_WORKERS", "1")
    try:
        return max(1, min(int(raw), 4))
    except (TypeError, ValueError):
        return 1


class BackgroundWorker:
    """Wraps QueueWorker in daemon threads that survive idle periods.

    Two threads, two queues: a multi-minute transcode must not delay a backup or an AI job,
    and the media thread is the single slot that keeps FFmpeg concurrency at one.
    """

    def __init__(
        self,
        worker: QueueWorker,
        *,
        media_worker: QueueWorker | None = None,
        publish_workers: list[QueueWorker] | None = None,
        analytics_workers: list[QueueWorker] | None = None,
        scheduler_factory: Callable[[Session], object] | None = None,
    ) -> None:
        self.worker = worker
        self.media_worker = media_worker
        self.publish_workers = list(publish_workers or [])
        self.analytics_workers = list(analytics_workers or [])
        self.scheduler_factory = scheduler_factory
        self._scheduler_thread: threading.Thread | None = None
        self._thread = None
        self._media_thread = None
        self._publish_threads: list[threading.Thread] = []
        self._analytics_threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.last_error: str | None = None

    def start_background(
        self,
        poll_interval: float = 1.0,
        media_poll_interval: float = 1.5,
        sweep_interval: float | None = None,
    ) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            log.info("worker %s started", self.worker.owner)
            while not self._stop.is_set() and not self.worker.stopped:
                try:
                    ran = self.worker.run_once()
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("worker loop error")
                    ran = False
                if not ran:
                    self._stop.wait(poll_interval)
            log.info("worker %s stopped", self.worker.owner)

        self._thread = threading.Thread(target=loop, name="creatoros-worker", daemon=True)
        self._thread.start()

        if self.media_worker is not None:
            def media_loop() -> None:
                log.info("media worker %s started (queue=media)", self.media_worker.owner)
                while not self._stop.is_set() and not self.media_worker.stopped:
                    try:
                        ran = self.media_worker.run_once()
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        log.exception("media worker loop error")
                        ran = False
                    if not ran:
                        self._stop.wait(media_poll_interval)
                log.info("media worker %s stopped", self.media_worker.owner)

            self._media_thread = threading.Thread(target=media_loop, name="creatoros-media-worker", daemon=True)
            self._media_thread.start()

        for worker in self.publish_workers:
            thread = threading.Thread(
                target=self._publish_loop, args=(worker,), name=f"creatoros-{worker.owner}", daemon=True
            )
            thread.start()
            self._publish_threads.append(thread)

        for worker in self.analytics_workers:
            thread = threading.Thread(
                target=self._queue_loop,
                args=(worker, "analytics", 3.0),
                name=f"creatoros-{worker.owner}",
                daemon=True,
            )
            thread.start()
            self._analytics_threads.append(thread)

        interval = sweep_interval if sweep_interval is not None else scheduler_interval()
        if self.scheduler_factory is not None and interval > 0:
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop, args=(interval,), name="creatoros-scheduler", daemon=True
            )
            self._scheduler_thread.start()

    def _publish_loop(self, worker: QueueWorker) -> None:
        log.info("publish worker %s started (queue=publish)", worker.owner)
        while not self._stop.is_set() and not worker.stopped:
            try:
                ran = worker.run_once()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("publish worker loop error")
                ran = False
            if not ran:
                self._stop.wait(2.0)
        log.info("publish worker %s stopped", worker.owner)

    def _queue_loop(self, worker: QueueWorker, lane: str, poll_interval: float) -> None:
        """Generic lane loop: one claimed job at a time, idle sleep between claims."""
        log.info("%s worker %s started (queue=%s)", lane, worker.owner, worker.queue)
        while not self._stop.is_set() and not worker.stopped:
            try:
                ran = worker.run_once()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("%s worker loop error", lane)
                ran = False
            if not ran:
                self._stop.wait(poll_interval)
        log.info("%s worker %s stopped", lane, worker.owner)

    def _scheduler_loop(self, interval: float) -> None:
        """Sweep scheduled destinations. A missed post must be noticed even if nobody opens the GUI."""
        log.info("schedule sweeper started (every %ss)", interval)
        while not self._stop.is_set():
            session = None
            try:
                # A fresh session per sweep: a long-lived one would pin a snapshot between ticks.
                session = self.worker.session_factory()
                scheduler = self.scheduler_factory(session)
                result = scheduler.tick()
                session.commit()
                if result.count("enqueued") or result.count("missed"):
                    log.info("schedule sweep: %s", result.as_dict())
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("schedule sweeper error")
                if session is not None:
                    with contextlib.suppress(Exception):
                        session.rollback()
            finally:
                if session is not None:
                    # A failed close must not kill the sweeper: the thread outlives this tick.
                    with contextlib.suppress(Exception):
                        session.close()
            self._stop.wait(interval)
        log.info("schedule sweeper stopped")

    def stop(self, timeout: float = 5.0) -> None:
        self.worker.stop()
        if self.media_worker is not None:
            self.media_worker.stop()
        for worker in self.publish_workers:
            worker.stop()
        for worker in self.analytics_workers:
            worker.stop()
        self._stop.set()
        for thread in [
            self._thread,
            self._media_thread,
            self._scheduler_thread,
            *self._publish_threads,
            *self._analytics_threads,
        ]:
            if thread is not None:
                thread.join(timeout=timeout)

    def drain(self, max_jobs: int = 100) -> int:
        return self.worker.drain(max_jobs=max_jobs)
