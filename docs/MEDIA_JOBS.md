# Media jobs on the durable queue (Phase 5)

Phase 5 does **not** add a queue. It adds media handlers and a second queue *name* on the
Phase 2 `queue_job` table, leased and retried by the same `QueueWorker`.

## Why a second queue name

AI work and FFmpeg work compete for different resources and have different latencies. A
20-minute transcode must never sit in front of a 3-second agent task. Jobs declare
`queue="media"`, and the worker started in Phase 5 drains only that name
(`workers/runner.py` builds two `QueueWorker` instances from the same table).

The property that keeps this honest: `QueueWorker.run_once()` on `queue="default"` returns
`False` when only media jobs are queued — asserted by
`test_worker_does_not_pick_media_jobs_from_the_default_queue`.

## Job types

| Job type | Payload | Effect |
|---|---|---|
| `media.import` | `{source_path, original_filename?}` | copy → hash → probe → register → thumbnail + waveform |
| `media.probe` | `{asset_id}` | re-probe an asset, update facts and `probe_version` |
| `media.thumbnail.generate` | `{asset_id, size?, timeline?, force?}` | poster frame (and optional timeline) |
| `media.preview.generate` | `{asset_id, height?}` | `fast_preview` derivative |
| `media.proxy.generate` | `{asset_id, height?}` | `editing_proxy` derivative |
| `media.transcode` | `{asset_id, profile, height?}` | any video profile |
| `media.audio.extract` | `{asset_id, format}` | `audio_wav` / `audio_aac` |
| `media.waveform.generate` | `{asset_id, buckets?}` | peaks JSON + waveform PNG |
| `media.recovery.scan` | `{older_than_minutes?}` | staging sweep, quarantine flags, missing-file detection |

All handler names live in `services/media/jobs.py` as constants; nothing string-matches a
job type elsewhere.

## Lifecycle

```
queued ──claim──> running ──success──> succeeded
   ▲                 │
   │                 ├──cancel requested──> cancelled   (derivative row back to queued)
   │                 └──error──> queued (retry with backoff + jitter) ──> dead_letter
   └───────────────────────────────── retry (manual, resets attempts)
```

* **Lease + heartbeat**: a claimed job holds a lease; the handler heartbeats every few
  seconds (on its own session) so a long encode is not stolen by another worker.
* **Idempotency**: open jobs dedupe by `idempotency_key` (`media:import:<hash>`), so a crash
  and retry do not enqueue a second copy of the same import.
* **Backoff**: exponential with jitter, capped; `max_attempts` per job type.
* **Dead letter**: after the final attempt the row keeps `last_error` verbatim, and the GUI
  offers *retry*, which re-queues with `attempts = 0` and `result_json.stage = "requeued"`.
* **Cancellation**: `POST /library/jobs/{id}/cancel` sets `cancel_requested`. The handler
  polls it between chunks (import) or between progress lines (FFmpeg), kills the child, and
  marks the derivative `QUEUED` — a cancelled job is not a failed job.
* **Progress**: `result_json.progress` (0..1) and `result_json.stage` are written directly on
  the job row, so any client can show truth without a websocket.

## Restart behaviour

Jobs live in SQLite, so a restart loses nothing. `test_job_survives_a_restart_and_then_runs`
resets the engine (simulating a new process) and proves the queued import still executes.
On startup the worker also runs a recovery sweep: staging files from the previous process are
removed, assets stuck in a non-terminal intake state are quarantined with a reason, and
derivative rows whose file disappeared are marked `FAILED` / `DERIVATIVE_MISSING`.

## Recovery is not a delete

`run_recovery` reports what it did:

```json
{"staging_removed": 2, "bytes_freed": 1048576,
 "assets_quarantined": 1, "derivatives_missing": 3, "older_than_minutes": 0}
```

Originals are never removed by recovery. A missing derivative is a fact to surface, not a row
to hide.

## Tests

`tests/test_media_queue.py` (19 tests) covers: real execution, idempotency keys, restart,
cancel-before-run, cancel-mid-job, progress writes, verified derivatives, dead letter, manual
retry, retry refusal for a missing asset, handler routing, progress shape, staging sweep,
missing-derivative detection, priority order, queue stats, audit events, queue isolation, and
that a real preview encode measurably takes time.
