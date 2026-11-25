# Analytics API (`/analytics`)

P8.17 adds a token-authenticated `/analytics` router (53 endpoints) on top of the analytics services
from P8.3–P8.16. It is a **read-first projection of the observation ledger**: no route recomputes
collection, aggregation, comparison or recommendation logic, and no route can start a provider
request inline.

## Conventions

* **Auth:** every route requires the per-launch session token (`Authorization: Bearer …`); the router
  is mounted with the same `require_token` dependency as every other non-public router. An
  unauthenticated request is `401 AUTH_REQUIRED` before the body is validated.
* **Errors:** the existing envelope `{code, message, correlation_id, details?}`. Service errors are
  mapped through it (`NO_PUBLICATIONS`, `METRIC_NOT_FOUND`, `REPORT_FILE_MISSING`, …).
* **Lists:** `Page[T] = {items, total, limit, offset}`, `limit ≤ 200`.
* **Time:** UTC on the way out; `start`/`end` accept ISO-8601 and must be ordered (`422` otherwise).
* **Values:** every metric payload carries `availability`. `value` is `NULL` unless the state is
  `observed`; `value_json` is the untouched structured payload. A metric that was not collected is
  `not_collected` — never `0`.
* **Writes that queue:** `sync`, `backfill` and `workflows` return `202` with a real job id and the
  real state (`queued`/`already_queued`/`pending`). Nothing returns "completed" because a job exists.

## Endpoints

### Overview, freshness, capability

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/overview` | counts of accounts, publications, observations, aggregations, recommendations by status |
| GET | `/analytics/freshness` | per account: last collection, age, stale flag, next due, blocked reason |
| GET | `/analytics/capabilities` | per platform: connected accounts, metric total, planned account metrics from the granted scopes |
| GET | `/analytics/versions` | configured vs latest documented API version, retirement, metrics above the version |
| GET | `/analytics/windows` | the six documented performance windows and their tolerance |

### Registry and availability

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/metrics` | filter `platform`, `object_type`, `preferred_for`, `include_deprecated`; paged |
| GET | `/analytics/metrics/{metric_key}` | one definition (class, kind, unit, periods, scopes, deprecation) |
| GET | `/analytics/availability?account_id=` | `registry.plan(...)` for an account: one row per metric with the reason |
| GET | `/analytics/accounts/{id}/capabilities` | same, scoped to one account |

### Accounts

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/accounts` | accounts with observation/publication counts; paged |
| GET | `/analytics/accounts/{id}` | account + per-object-type sync state |
| GET | `/analytics/accounts/{id}/growth` | follower series from the registry's `preferred_for=followers` metrics |
| GET | `/analytics/accounts/{id}/metrics` | account-scope aggregations |

### Publications

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/publications` | published objects with attribution and `has_metrics`; filter account/platform/period |
| GET | `/analytics/publications/{id}` | publication + its aggregations |
| GET | `/analytics/publications/{id}/metrics` | aggregations for one publication, optional window/metric |
| GET | `/analytics/publications/{id}/retention` | stored retention curve(s), or `available: false` with a reason |
| GET | `/analytics/publications/{id}/timeline` | observations ordered by collection time |

### Series, platforms, raw ledger

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/series` | one metric, `bucket=hour|day|week`; a bucket with no observation is `not_collected`, not `0` |
| GET | `/analytics/platforms` | per-platform counts and last collection |
| GET | `/analytics/snapshots` | the observation ledger; filter account/metric/publication/object/period |
| GET | `/analytics/aggregations` | rollups; filter account/scope/metric/window/version |

### Campaigns

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/campaigns` | campaigns with project/publication/observation counts |
| GET | `/analytics/campaigns/{id}` | campaign + publications + campaign-scope metrics |

### Comparison, hashtags, timing

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/analytics/compare` | deterministic group comparison (medians, intervals, refusals); body is strict |
| GET | `/analytics/hashtags` | the same comparison grouped by hashtag |
| GET | `/analytics/timing` | publish-time buckets that pass the detector's sample gate |

### Recommendations

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/recommendations` | filter status/account/kind; paged |
| GET | `/analytics/recommendations/{id}` | recommendation + evidence measurements |
| POST | `/analytics/recommendations/{id}/accept` | records a hash-bound **approval request**; never publishes |
| POST | `/analytics/recommendations/{id}/reject` | stores the reason |
| POST | `/analytics/recommendations/refresh` | re-runs detection; idempotent (an identical open draft is not duplicated) |

### Reports and exports

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/reports` | stored reports; paged |
| POST | `/analytics/reports` | builds a JSON report from stored aggregations + recommendations, records sha256 |
| GET | `/analytics/reports/{id}` | report metadata (no path leak) |
| GET | `/analytics/reports/{id}/file` | the stored file, only if it is inside the data root |
| DELETE | `/analytics/reports/{id}` | deletes the row and the file |
| GET | `/analytics/export/metrics.csv` | aggregations matching the same filters as `/aggregations` |
| GET | `/analytics/export/publications.csv` | publications matching the list filters |

### Collection control

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/analytics/sync/{account_id}` | enqueue `analytics.account_sync` on the `analytics` lane; `202` + job id |
| POST | `/analytics/backfill/{account_id}` | enqueue `analytics.backfill`; `202` + job id |
| POST | `/analytics/prune` | **preview only** in this phase: counts what the retention job will remove; deletes nothing |
| GET | `/analytics/runs` | collection runs; paged |
| GET | `/analytics/runs/{run_id}` | one run |
| GET | `/analytics/sync-state` | per account/object sync state |

### Settings, workflows, diagnostics

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/settings` | bounded preferences + their bounds |
| PUT | `/analytics/settings` | validates keys and ranges; unknown keys are `422` |
| GET | `/analytics/workflows` | the W1–W3 definitions (stages, agents) |
| POST | `/analytics/workflows` | creates a deterministic workflow objective and enqueues it |
| GET | `/analytics/diagnostics` | row counts, blocked sync states, stale runs, version reports, warnings |

### Consent (P8.20)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/analytics/consent` | accounts whose analytics is blocked by something a user can act on |
| GET | `/analytics/consent/{account_id}` | the `ConsentPlan`: required / granted / missing / requested scopes, Page tasks, app mode, blockers and the blocked metrics |
| POST | `/analytics/consent/{account_id}` | begins an account-bound OAuth permission upgrade; returns `authorization_started` + the provider URL, or `consent_not_needed` / `page_task_required` / `unsupported` |

The POST accepts only `{"intent": "..."}` — a client can never name a scope. The backend computes
`requested_scopes` from the capability catalogue and the account's login path. Existing account
identity and credentials are preserved: the callback adopts the new credential only for the same
provider identity and only after re-verifying it, and a denial or failure leaves the account
connected with its previous scopes. See `docs/OAUTH_ENGINE.md` § permission upgrade and
`docs/ANALYTICS_ARCHITECTURE.md` § consent.

The authorization itself completes on the existing loopback listener
(`http://127.0.0.1:47821/oauth/callback/<route>`), not a new public route. Poll
`GET /social/oauth/transactions/{session_id}` while the browser is open.

## Security

* No token, refresh token, vault value, `secret_ref` id or client secret is ever selected or
  returned. Account payloads expose only display/connection facts and `token_expires_at`.
* Provider error bodies are not returned; only classified codes and redacted details.
* Report files are served only when the stored path is inside the configured data root.
* No route accepts an arbitrary path, SQL fragment or provider URL.
* Analytics agents reach data through their tools, not this API, so API access grants them nothing
  new.

## Desktop consumption (P8.23)

The three P8.23 pages add no route. `services/apiAnalytics.ts` gained the typed methods below, each of
which maps 1:1 onto a route above:

| Client method | Route | Used by |
| --- | --- | --- |
| `recommendations({status, account_id, kind, limit, offset})` | `GET /analytics/recommendations` | Recommendations filter list |
| `recommendation(id)` | `GET /analytics/recommendations/{id}` | evidence panel |
| `acceptRecommendation(id, {note})` | `POST …/accept` | acceptance confirmation |
| `rejectRecommendation(id, {reason})` | `POST …/reject` | rejection confirmation |
| `refreshRecommendations({account_id, window_kind, limit})` | `POST /analytics/recommendations/refresh` | "Generate drafts" |
| `reports({kind, limit, offset})`, `createReport(body)`, `report(id)`, `deleteReport(id)` | `/analytics/reports…` | Reports library + creation |
| `downloadReport(id)` | `GET /analytics/reports/{id}/file` | verified download (bytes, unparsed) |
| `settings()`, `updateSettings({...})` | `GET`/`PUT /analytics/settings` | Analytics settings |

`downloadReport` deliberately returns the raw bytes as well as the decoded text: the page hashes the
bytes and compares them with `sha256` from `GET /analytics/reports/{id}`. Parsing the document and
re-serialising it before hashing would change the digest and defeat the check.

### Contract gaps the pages report instead of inventing

* `RecommendationOut` carries **no** `title`, `priority`, `category` or expiry field. The category is
  `kind`, the narrative is `observation`, and the page says so where a reader would look for them.
* `ReportOut` carries no `warnings` array and no `path`: limits are recorded inside the section bodies,
  and the file is identified by `sha256` + `bytes`. Only `json` is generated — there is no `format`
  field to advertise another.
* `GET /analytics/settings` returns stored values and their bounds but **not** the defaults, so there
  is no honest reset-to-defaults control. Reporting defaults and per-run budgets are not stored
  settings at all.

## Reports, retention and maintenance (P8.24)

| Route | What it does |
| --- | --- |
| `POST /analytics/reports` | creates a report in `json`, `csv` or `pdf`; unknown format is 422 `REPORT_FORMAT_UNSUPPORTED` |
| `GET /analytics/reports` | library, newest first, filter by `kind` |
| `GET /analytics/reports/{id}` | metadata: `sha256`, `bytes`, `format`, `status`, sections |
| `GET /analytics/reports/{id}/file` | the raw bytes, `Content-Type` per format, only from inside the data root |
| `DELETE /analytics/reports/{id}` | removes the row and its file |
| `POST /analytics/prune` | **preview only**: per-category plan, deletes nothing (`dry_run` forced true) |
| `POST /analytics/retention/execute` | applies the plan in bounded batches; `dry_run` defaults to `true` |
| `GET /analytics/versions` | pinned version, latest documented version, observation age, sunset dates, stale metrics |
| `POST /analytics/versions/check` | records a fresh version observation (honours `CREATOROS_ANALYTICS_VERSION_FETCH`) |
| `GET /analytics/diagnostics` | collection, reports, retention, version watch and availability facts |

Two route-level rules are worth stating because a client can depend on them:

* A report's `sha256` is the digest of the bytes the file endpoint returns. A client that parses the
  document and re-serialises it before hashing will compute a different value — that is a client bug,
  not a server one.
* `POST /analytics/prune` can never delete. `dry_run` is fixed to `true` on that path; a caller that
  wants deletion must use `POST /analytics/retention/execute`.

### P8.24 client methods

`services/apiAnalytics.ts` gained `retentionPreview({older_than_days, batch_limit})`,
`retentionExecute({older_than_days, batch_limit, dry_run})` and `ReportFormat`/`RetentionPreview`/
`RetentionExecution` types. `retentionPreview()` does not accept a `dry_run` override from a caller —
the preview path always sends `true`.

## Known limitations (P8.17/P8.24)

* Settings are stored preferences; the sweeper applies them on its next tick (not mid-run).
* Retention only touches the observation ledger and its derived rows: `docs/ANALYTICS_RETENTION.md`
  lists exactly what is eligible and what is protected.
* `POST /analytics/prune` remains a preview by design, not by omission.
* Nothing here has been exercised against a live provider: this machine has no connected Facebook
  Page, Instagram professional account or approved TikTok app. See `docs/evidence/P8.md`.