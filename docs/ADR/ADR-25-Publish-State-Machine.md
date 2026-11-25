# ADR-25 — The publish destination is a state machine, not a set of booleans

**Status:** Accepted, implemented and tested (Phase 7)
**Date:** 2026-11-24

## Context

Phase 2 left `publish_job` with a loose `job_state` string and the Phase 4/6 code wrote
`queued`, `needs_attention`, `manual_publish_required`, `validation` next to values that Phase 7
needed to distinguish. Publishing has genuinely different situations that a boolean
(`published: yes/no`) collapses into one:

* nothing has been sent yet, and something local is wrong → `blocked`;
* the provider may or may not have the object because the answer was lost → `unknown_remote_state`;
* the provider definitely does not have it, and a human must decide to resend → `requires_user_action`;
* it is at the provider and confirmed → `published`.

Those need different UI, different retry rules and different audit language. A single "error" column
produces a GUI that cannot tell a person what to do.

## Decision

1. **Eighteen canonical states** (`PublishState`), one lifecycle per *destination*:

   `draft`, `preparing`, `validation_pending`, `awaiting_approval`, `approved`, `scheduled`, `ready`,
   `uploading`, `uploaded`, `processing`, `published`, `failed`, `blocked`, `requires_user_action`,
   `unknown_remote_state`, `needs_reconciliation`, `cancelled`, `partially_published`.

2. **`partially_published` is a campaign rollup, never a destination state.** One destination is
   either published or it is not; calling it "partially" would be the exact lie this project forbids.

3. **One transition table** (`PUBLISH_TRANSITIONS`) is the only authority. `can_transition()` answers;
   services decide how to fail. Every module that moves a destination reads this table, including the
   executor, the scheduler, reconciliation and the API's actions.

4. **`published` and `cancelled` are the only terminal states.** `failed` is human-retryable
   (`failed → {ready, draft, cancelled}`) because "Retry eligible job" is a real action a person takes,
   while a queue sweeper must never resurrect a failure on its own.

5. **Legacy strings stay readable, never written.** `LEGACY_PUBLISH_STATES` maps
   `validation→validation_pending`, `queued→ready`, `needs_attention→requires_user_action`,
   `manual_publish_required→requires_user_action`.

6. **Two parsers, deliberately.** `normalize_publish_state()` is a boundary normaliser for *stored*
   values and falls back to `draft`; `parse_publish_state()` is for *caller input* and raises. A filter
   typo must not silently select drafts.

## Consequences

* The executor settles by **walking** the table (`shortest_path`), so a publisher that finishes in one
  call still records `uploading → uploaded → processing → published`. The history describes what
  happened, not just where it ended up.
* Two genuine gaps were found by tests and fixed rather than papered over: `uploading →
  requires_user_action` (a token can be revoked mid-upload) and `ready → requires_user_action` (an
  approval can be revoked between queueing and uploading), plus `failed → draft` (a failed attempt
  spends its approval, and consent is per payload).
* `ready` is not editable: the publish worker may already own the destination. `EDITABLE_JOB_STATES`
  excludes it.
* Downgrading to Phase 6 maps the new states back through `DOWNGRADE_STATE_CASE`, so the migration is
  reversible without inventing data.
