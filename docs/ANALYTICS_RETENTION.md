# Analytics retention (P8.24)

**Analytics history expires. Business history does not.**

Retention is the only analytics feature that deletes data, so it is the one feature where the reason
for every deletion is written down, shown before it happens, and recorded afterwards.

| | |
| --- | --- |
| Service | `app/backend/src/creatoros/services/analytics/retention.py` |
| Policy source | `analytics.settings` → `retention_days` (7–3650, default 365) |
| Report floor | `REPORT_MIN_RETENTION_DAYS = 730` — reports are kept at least two years |
| Batch | `DEFAULT_BATCH_LIMIT = 2000` rows per category per execution |
| Schedule | daily (`RETENTION_INTERVAL_HOURS = 24`) on the `analytics` queue lane |
| Audit | `analytics.retention.preview`, `analytics.retention.execute` |
| Routes | `POST /analytics/prune` (preview, read-only), `POST /analytics/retention/execute` |

## What is eligible, and why

| Category | Eligible when | The reason, in the API response |
| --- | --- | --- |
| `aggregations` | every input observation is also eligible, or already gone | "no rollup is redundant" / "every observation this rollup cites is expiring or already gone" |
| `observations` | `collected_at` older than the cutoff **and** the writing run is closed | "collected before YYYY-MM-DD by a finished run" |
| `collection_runs` | `finished_at` older than the cutoff, no observation refers to it | "finished before the cutoff and no observation still refers to it" |
| `reports` | older than `max(retention_days, 730)` or archived | "generated before YYYY-MM-DD or explicitly archived" |
| `superseded_recommendations` | `status = superseded` and older than the cutoff | "replaced by a newer recommendation before the cutoff; decisions are not deleted" |
| `orphan_files` | a report file no row points at, older than 24 h | "a report file with no record pointing at it, abandoned for over a day" |

The preview response carries the reason **per category**, not only a count, because "3 214 rows" with
no explanation is a warning label rather than a preview.

## What is never deleted

Counted and reported as `protected` in every preview:

* publications, campaigns, content projects, platform variants
* social accounts and their sync state
* media assets and metric definitions
* **decided** recommendations — `accepted` and `rejected` are decisions, not drafts
* runs still referenced by a surviving observation

No deletion statement in `retention.py` names any of those tables. The protection is structural, not a
filter that could be forgotten.

## Preview first, then arm, then delete

```
POST /analytics/prune            {"dry_run": true}                    -> per-category plan, deletes nothing
POST /analytics/retention/execute {"dry_run": false, "batch_limit": 2000} -> counts, bytes freed, audit id
```

`dry_run` defaults to `true`, and `preview()` and `execute()` share `plan()`, so a preview cannot
disagree with an execution about eligibility.

The desktop page (`pages/analytics/AnalyticsSettings.tsx`, "Retention" card) shows the preview table,
then requires the word `DELETE` to be typed into a field before the execute button arms. After an
execution it shows what was deleted, the bytes freed, `foreign_key_violations`, `integrity_ok` and the
audit id, and re-reads the preview so the page shows what is *left* rather than what the response
counted.

## Batched, ordered, and integrity-checked

1. **Files before rows**, so a failure leaves a reachable file rather than a row pointing at nothing.
2. **Aggregations before observations**, so a rollup never survives its inputs.
3. **Observations before runs**, so a run's cascade cannot remove rows a policy kept.
4. `PRAGMA foreign_key_check` runs at the end; a non-empty result is reported as `foreign_key_violations`
   and `integrity_ok: false` in the result — a failure, not a warning.

A pass that hits `batch_limit` returns `work_remaining`, and `handle_retention` re-enqueues the job so
the next tick continues. No single tick holds the write lock for minutes.

## Changing the policy

`retention_days` is a stored preference (`PUT /analytics/settings`), bounded 7–3650. Lowering it makes
more rows eligible at the **next** retention run; it can never recover rows already deleted. The
desktop page says exactly that, and states that the sweeper reads the setting on its next tick, not
mid-run.

`report_retention_days` is derived (`max(retention_days, 730)`) and is not separately settable: a
report is the only record of what a number looked like on the day it was generated.

## Verified on this machine

`tests/test_analytics_retention_reports.py`:

* per-category eligibility (aggregation kept while its inputs live, superseded draft deleted, decided
  recommendation kept, orphan file removed, report floor enforced)
* a batch limit caps the batch, reports `work_remaining`, and a second execution finishes the work
* `dry_run` deletes nothing while reporting the same plan
* `foreign_key_check` empty and `integrity_ok` true after an execution
* both audit events written; previews are audited too
* the scheduled job is queued once a day, and a second tick in the same second queues nothing

Screenshots: `docs/evidence/p8-analytics-settings-retention{,-2,-3}.png` (preview + armed button),
`docs/evidence/p8-analytics-settings-retention-executed{,-2,-3}.png` (execution result with audit id).

**Not verified here:** no execution has been run against live provider data, because there is none on
this machine (no connected Page, Instagram professional account or approved TikTok app). The policy is
exercised against fixture data only (category B).
