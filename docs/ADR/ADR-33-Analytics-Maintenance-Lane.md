# ADR-33 — Maintenance work shares the analytics lane, and never blocks collection

**Status:** Accepted, **implemented + tested** (P8.24 — two job types, two scheduler gates, idempotent
enqueue, 9 tests).

## Context

P8.11 established the fourth queue lane (`analytics`) on the existing `queue_job` table, dispatched by
the existing sweeper thread; ADR-31 of the phase plan forbids a second queue and a fifth thread.

P8.24 adds two recurring maintenance jobs — retention (`analytics_retention`) and the version watch
(`analytics_version_check`). They are not collection work: nothing a person is waiting for depends on
them, they can take seconds, and a capped retention pass intentionally continues on the next tick.

The obvious mistakes were available:

* give them their own lane, which would have meant a fifth worker thread to isolate them from
  collection;
* put them in the same job type with a `mode` field, which would have made "one job, one handler"
  untrue;
* enqueue them on every tick with no idempotency, which would have produced a queue that grows by one
  job per second when a job fails.

## Decision

**Two job types on the existing `analytics` lane, gated before they are enqueued.**

* `JOB_RETENTION` (priority 70) and `JOB_VERSION_CHECK` (priority 75) sit *behind* the collection
  priorities, so a due account sync is picked first when a worker is free.
* `AnalyticsScheduler.tick()` calls `_maintenance()` **after** collection dispatch, and only enqueues
  when due: `RETENTION_INTERVAL_HOURS = 24`, `VERSION_CHECK_INTERVAL_DAYS = 7`. "Due" is computed from
  the last *audited* execution / observation, so a restart does not re-run either job.
* Enqueue goes through the queue's existing idempotency key, so two ticks in the same second produce
  one job; a test asserts a second tick enqueues nothing.
* `handle_retention` returns `work_remaining` when the batch limit was hit; the job then re-enqueues
  itself rather than deleting unbounded rows in one lease.
* `AnalyticsTickResult` gained `retention_queued` / `version_check_queued`, and the combined result
  reports them separately from `enqueued`. This is why `tests/test_analytics_queue.py` now filters
  maintenance job types out of `jobs_in_lane()`: the tests' invariant is "one collection job per due
  account", and counting a retention pass in that number would have made a passing scheduler look
  broken.

## Consequences

* No new thread, no new lane, no new table. The retention and version-watch jobs appear in the same
  queue view as everything else and are visible in `/diagnostics`.
* A machine that is left running accumulates version observations weekly and prunes daily, both
  audited.
* A retention pass that hits the batch limit is not an error; it reports remaining work and continues.
* Because maintenance is queued after collection and at lower priority, analytics collection latency is
  unaffected on a single-worker machine (the default `CREATOROS_ANALYTICS_WORKERS = 1`).
