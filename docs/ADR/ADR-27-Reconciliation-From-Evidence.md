# ADR-27 — Amber outcomes are reconciled from evidence, never retried on hope

**Status:** Accepted, implemented and tested (Phase 7)
**Date:** 2026-11-24

## Context

Every provider here can leave a request in an ambiguous position: Facebook accepts the last chunk and
the connection dies before the finish response, Instagram publishes the container and the reply is a
timeout, TikTok's `publish_id` is created but the status call fails. The naive answers are all wrong:

* **Retry** — may publish the same video twice, and spends a second approval.
* **Mark failed** — is a lie; the post may be live.
* **Mark published** — is also a lie; the post may not exist.
* **Poll forever** — hides a real decision behind an infinite spinner.

## Decision

1. **`unknown_remote_state` is a first-class state**, entered when the request may have been processed
   and no answer came back. It carries the provider id, the attempt that produced it and the category.

2. **Reconciliation is a read, then a rule.** `ReconciliationService.check()` asks the provider through
   the *same* publisher the executor used, records what it saw, and applies one of three rules:
   * remote object exists → `published`, with the permalink the provider reported;
   * provider says it does not exist → `requires_user_action` with `reconciled_remote_absent`; the
     destination is **not** resubmitted, because a resubmit re-spends an approval and can double-post;
   * anything else → stays `unknown_remote_state`, case left open for a later check.

3. **Provider vocabularies are read in exactly one place** (`observe()`), so Facebook's
   `video_status`, Instagram's `FINISHED`/`EXPIRED` and TikTok's `PROCESSING_UPLOAD`/`PUBLISH_COMPLETE`
   cannot drift apart in the GUI. An unrecognised state is `undetermined`, not a guess.

4. **Every look is a row** in `publish_reconciliation`, including the ones that decided nothing, so
   "we looked and it was still processing" is visible history rather than a silence. A case is open
   only while the newest row for that destination has no `resolved_by` and the destination is still
   unresolved.

5. **A person can resolve a case by hand**, with a mandatory note; the row records who said so. That is
   the exit when the provider API cannot answer (a post deleted in the app, a revoked app).

6. **Nothing automatic ever retries.** `check_all()` reads and decides; the retry itself is a human
   action (`PublishActions.retry`), which also requires a live approval.

## Consequences

* A crashed executor is recorded as `EXECUTOR_CRASHED` → `unknown_remote_state`, never as a success and
  never as a plain failure.
* An unreachable provider is recorded as `undetermined` with its error code: "we could not ask" is
  different from "it is not there".
* The GUI's attention list is driven by state, not by an error string, and every row carries the reason
  the backend wrote.
* A destination whose approval was revoked mid-upload lands in `requires_user_action` with a readable
  reason instead of a silent stall.
