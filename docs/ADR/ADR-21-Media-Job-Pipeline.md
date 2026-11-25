# ADR-21 — Media work on the existing durable queue

* Status: Accepted
* Date: 2026-11-22
* Phase: 5

## Context

Phase 2 already ships a durable queue: atomic claim with `RETURNING`, leases and heartbeats,
exponential backoff with jitter, dead-letter, idempotency keys, cancellation, and a worker that
runs in the API process. Phase 5 needs nine new job types, some of which run for minutes.

The tempting shortcuts are a thread per encode, an in-memory worker, or a second job table.
All three lose work on restart and duplicate the semantics Phase 2 already tests.

## Decision

**Extend the existing queue with a second lane name, not a second implementation.**

* Job types are constants in `services/media/jobs.py`; handlers register on the same
  `QueueWorker` class.
* Media jobs declare `queue="media"`; `workers/runner.py` starts a second `QueueWorker` on that
  name. `QueueWorker.run_once()` on `queue="default"` must not claim media jobs — asserted by
  `test_worker_does_not_pick_media_jobs_from_the_default_queue`.
* Rationale for the lane: a 20-minute transcode must not delay a 3-second agent task, and the
  two workloads have different failure economics (FFmpeg errors are usually permanent,
  inference errors are often transient).
* **Progress on the job row.** `result_json.progress` and `result_json.stage` are written on a
  separate session with a minimum interval, so a long encode never holds a read transaction and
  any client can render truth without a websocket.
* **Cancellation is cooperative and non-punitive.** `cancel_requested` is polled between
  chunks/lines; the child process tree is killed; the derivative returns to `queued` rather
  than `failed`, and the job is `cancelled`, not a burned retry.
* **Idempotency for imports only.** `media:import:<sha256>` prevents a duplicate job after a
  crash. Derivative jobs are already idempotent through the unique
  `(asset_id, kind, profile, version)` key, so they need no key.
* **Recovery is explicit.** `run_recovery()` (startup + `POST /library/recovery/scan`) sweeps
  stale staging files, quarantines assets stuck mid-intake with a reason, and marks derivative
  rows whose file vanished as failed. It never deletes an original.

## Consequences

**Good**

* One queue implementation to reason about, test and back up; Phase 2's 30+ queue tests keep
  their value.
* Two lanes keep interactive agent work responsive during long encodes.
* Restart safety is inherited, not re-implemented
  (`test_job_survives_a_restart_and_then_runs`).
* Dead-letter plus manual retry gives an operator a real recovery path with the original error
  preserved verbatim.

**Costs**

* Two workers in one process means two polling loops; they are cheap (1 s interval) but the
  process now has two background threads.
* `queue` is a string column, so a typo could strand a job. Mitigated by exported constants
  (`MEDIA_QUEUE`) used everywhere and a stats endpoint that reports per-lane counts.
* Cancellation depends on the handler polling; a handler that ignores `should_cancel` would
  run to completion. Every media handler polls (chunk loop, progress line, per timeline frame),
  and cancellation is tested at the library level and at the job level.
