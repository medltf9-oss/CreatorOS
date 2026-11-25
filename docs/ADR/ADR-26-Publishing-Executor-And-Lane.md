# ADR-26 — Publishing is one path from approval to provider, and it runs on its own queue lane

**Status:** Accepted, implemented and tested (Phase 7)
**Date:** 2026-11-24

## Context

Publishing is the first thing in this project that changes the outside world. Three failure modes
matter more than throughput:

1. **Two code paths that publish.** A GUI button that posts inline plus a worker that also posts is
   how duplicate publications happen.
2. **A long upload blocking everything else.** Facebook accepts 4 GB, TikTok up to 4 GB in 10 MB
   chunks; a publish can take minutes and is bandwidth-bound. The media lane already exists because a
   transcode must not delay a backup; publishing has the same problem.
3. **A crashed upload that starts over.** Re-uploading from zero after 900 MB is a waste and, worse,
   can create a second remote object.

## Decision

1. **`PublishExecutor` is the only module that calls a publisher.** It is reachable from exactly two
   places: the `publish.destination` queue handler, and `PublishActions`/`PublishScheduler`, which can
   only *enqueue*. No API route, agent tool or GUI component holds a publisher. A test scans the
   package for publisher imports outside `executor.py` and `reconciliation.py`.

2. **A third queue lane.** `queue="publish"`, `CREATOROS_PUBLISH_WORKERS` (default 1, clamped 1–4),
   `CREATOROS_SCHEDULER_SECONDS` (default 15) for the sweeper. Same durable queue, same lease
   machinery as Phase 2 — no second queue system (ADR-08/ADR-21 hold).

3. **Order that matters, enforced and tested:**
   * preflight turns local facts into reasons *before* any upload;
   * the approval is re-derived from current rows, compared to the stored hash, and **spent** before
     the first provider call;
   * provider ids are written as they arrive, so a resumed destination continues instead of restarting;
   * a lease stops two workers racing one destination;
   * a published destination is never published again.

4. **The queue's idempotency key is per destination** (`publish:<job_id>`), which is what makes a
   restart-then-sweep safe and what stops an accidental duplicate within an intent.

5. **Ambiguity is a state, not an error string.** A timeout after the request left the machine settles
   as `unknown_remote_state` with the provider id kept. Nothing is resubmitted until reconciliation has
   looked (ADR-27).

## Consequences

* Progress comes from the provider's own offsets (`start_offset` on Facebook, `upload_chunk_index` on
  TikTok), not a local byte counter, so a resume is correct rather than approximate.
* Preflight writes `blocked`/`requires_user_action` with a readable reason instead of failing halfway
  through an upload after 300 MB.
* An `APPROVAL_REQUIRED` preflight result does **not** move the destination out of `ready`: the user's
  intent to publish stands, and the missing consent is reported as the reason.
* Exactly-once is impossible against these providers and is not claimed. The guarantees are: no
  accidental duplicate within an intent, no blind resubmission after an ambiguous outcome, and
  reconciliation before any retry.
