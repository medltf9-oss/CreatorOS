# ADR-30 — Analytics history expires; business history does not

**Status:** Accepted, **implemented + tested** (P8.24 — per-category policy, capped batches, audit
records, `PRAGMA foreign_key_check` after every execution).

## Context

Phase 8 writes one row per metric per object per run (`analytics_snapshot`), plus derived rollups
(`analytics_aggregation`), run records, report records and recommendations. A daily sweep on three
accounts with a dozen objects produces thousands of rows a day; left alone, the ledger grows without
bound and the desktop app slows down for no benefit — Meta's own media-insight window is two years,
Instagram's account window ninety days, and TikTok exposes current state only.

P8.17 shipped `POST /analytics/prune` as a **preview that deleted nothing**. Something had to make the
decision about what may be deleted, and that decision is the interesting part.

## Decision

**A per-category policy, not one duration.** Every category states its eligibility rule and its
justification, and the preview shows both.

| Category | Eligible when | Why |
| --- | --- | --- |
| `aggregations` | every input observation is also eligible, or already gone | a rollup whose inputs are gone cannot be recomputed or verified |
| `observations` | `collected_at` older than `retention_days` **and** the run that wrote it has finished | the provider's retention window is the ceiling; a finished run is required so an in-flight retry can still write |
| `collection_runs` | `finished_at` older than the cutoff and no observation refers to it | an execution record with no data left is history, but `finished_at` (not `outcome`) is what proves closure |
| `reports` | older than `max(retention_days, 730)` or explicitly archived | a report is the only record of what a number looked like on a given day |
| `superseded_recommendations` | `status = superseded` and older than the cutoff | a superseded draft was replaced; it is not a decision |
| `orphan_files` | a report file no row points at, older than 24 h | a crash between write and flush leaves an unreachable file |

Four rules make the policy load-bearing:

1. **Business history is never in scope.** Publications, campaigns, content projects, platform
   variants, social accounts, media assets, metric definitions, sync states and *decided*
   recommendations (`accepted`/`rejected`) are counted and reported as protected, and no deletion
   statement in this module names their tables.
2. **Deletion order follows the foreign keys.** Aggregations before the observations they cite, runs
   after the observations that cascade from them. `execute()` asserts `PRAGMA foreign_key_check` is
   empty and reports `integrity_ok`; a non-empty result is a hard error in the result, not a warning.
3. **Bounded batches.** `DEFAULT_BATCH_LIMIT = 2000` rows per category per execution. `handle_retention`
   returns `work_remaining`, and the job re-enqueues itself on the next scheduler tick while work
   remains. No single tick can hold the write lock for minutes.
4. **Every execution is audited.** `analytics.retention.preview` and `analytics.retention.execute`
   are written to the audit log with the policy, the cutoff, per-category counts and the deleted
   rows; a preview is audited too, because "someone asked what would be deleted" is a fact worth
   keeping.

The policy is derived from the stored `analytics.settings` value `retention_days` (7–3650, default
365). `report_retention_days` is `max(retention_days, 730)` — a person may shorten history, but not
below the two-year floor for reports.

## Consequences

* `POST /analytics/prune` stays a preview and now returns the full per-category plan; actual deletion
  moved to `POST /analytics/retention/execute` with `dry_run` defaulting to `true`.
* The desktop settings page shows the plan, requires the word `DELETE` typed into a field, and then
  displays the execution result including the audit id and `foreign_key_violations`. It does not
  offer a "select all" — a bulk delete with no category view is the failure mode this ADR exists to
  prevent.
* Retention runs daily (`RETENTION_INTERVAL_HOURS = 24`) on the existing `analytics` lane, through the
  existing sweeper. A capped run is not a failure; it reports remaining work.
* Shortening `retention_days` cannot recover data that was already deleted. The settings note in the
  UI says the setting applies to the next retention job, not to data already gone.
