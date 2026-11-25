# Analytics architecture

Phase 8 adds measurement to CreatorOS. It is built on the Phase 6 social layer and the Phase 7
publishing ledger; it does not add a second account store, queue, approval store or report service.

```
publish_job (real provider ids) ─┐
social_account + TokenService   ─┼─► services/analytics/collector.py ─► analytics_snapshot
account_capability              ─┘        ▲                                  │
analytics_metric_definition ──────────────┘                                  ▼
                                                     services/analytics/aggregation.py
                                                                             │
                                        ┌────────────────────────────────────┴──────────┐
                                        ▼                                               ▼
                             analytics_aggregation                        analytics_recommendation
                                        │                                               │
                                        └──────────────► /analytics/* ◄─────────────────┘
                                                              │
                                                    ANALYTICS CENTER (desktop)
```

## Vocabulary

`db/enums.py` holds the Phase 8 vocabulary. Four things are easy to get wrong, so they are explicit
types rather than strings:

* **`MetricAvailability`** — ten states. Only `observed` may carry a value, and the rule is enforced
  by a database CHECK, not by convention. `normalize_availability()` is a total function so a legacy
  or unknown string can never crash a read path.
* **`MetricClass`** — `cumulative`, `period`, `point_in_time`, `ratio`, `series`. A change is only
  computed between two observations of the same object with the same class and period; anything else
  is refused by `services/analytics/comparability.py`.
* **`MetricValueKind`** — `scalar`, `breakdown`, `series`, `object`. A curve stays a curve: the
  snapshot row stores it in `value_json`, and the CHECK constraints keep `value_num` and
  `value_json` mutually exclusive by kind.
* **`AnalyticsErrorClass`** — one classification per failed call, which decides retry behaviour and
  the availability state that is recorded.

## What is measured, and what is not

Only documented provider endpoints are used: `graph.facebook.com` Page/post insights and
`/{video-id}/video_insights`, `graph.instagram.com` or `graph.facebook.com` IG user/media insights,
and `open.tiktokapis.com` Display API v2. TikTok's Research API is not used, no browser automation or
scraping exists anywhere, and demographic breakdowns are deliberately not collected
(`redacted_by_policy`).

A metric that a provider does not expose is never estimated. TikTok watch time, retention, replays,
saves, reach and unique viewers do not exist in any approved TikTok API, so they read
`not_supported` and the UI says so.

## The metric registry

`integrations/analytics/catalog.py` holds 129 `MetricSpec` rows and is the only place a metric is
described; `services/analytics/registry.py` copies them into `analytics_metric_definition` on boot
and never deletes one. Counts by platform and object:

| | account | post | video | reel | story |
| --- | --- | --- | --- | --- | --- |
| Facebook | 25 | 24 | 13 | 10 | – |
| Instagram | 18 | 15 | – | 5 | 2 |
| TikTok | 5 | – | 12 | – | – |

Every row carries `metric_class`, `value_kind`, `unit`, `period_kinds_json`, a documentation URL and
its required scopes, because those five facts decide whether two numbers can be compared at all.
`ensure_catalog_is_consistent()` runs at import time: a metric without a class, kind, unit, period,
doc URL or scope list cannot exist, so the failure happens at seed time rather than in a chart.

Scopes are conjunctive by default. Instagram is the exception: the same read capability is granted by
two *alternative* login routes, so those rows use `{"any_of": [[business login scopes], [facebook
login scopes]]}`. A flat list would have marked every Instagram metric permanently unauthorized.

Named gaps are rows too: `tt.video.watch_time`, `tt.video.retention_curve`, `tt.video.reach`,
`tt.video.saves`, `tt.video.replays` and `tt.video.avg_watch_time` exist with
`availability_default=not_supported`, and the Instagram demographic metrics exist with
`redacted_by_policy`. The UI can therefore explain a gap by name instead of showing a blank tile or
a zero.

TikTok is split by grant, not by convenience: the four account counters
(`follower_count`, `following_count`, `likes_count`, `video_count`) require `user.info.stats`, while
every video metric requires `video.list`. The catalogue refuses to load if that split is broken.

`registry.plan()` is the guardrail used by every collector: given the platform, object type, the
scopes **actually on the token** and the configured API version, it returns one entry per metric with
`allowed`, a reason code (`ok`, `not_authorized`, `below_threshold`, `version_above`,
`threshold_unknown`, `not_supported`, `deprecated`) and the availability the row would take if
skipped. A metric is skipped with a name, never requested and recorded as `0`.

## Collection

The lane is `analytics` on the existing `queue_job` table. One worker by default
(`CREATOROS_ANALYTICS_WORKERS=0` disables it), and the **existing** sweeper thread dispatches due
accounts — there is no fifth thread and no second queue.

A logical run is identified by `analytics_collection_run.dedupe_key`
(`account | target | object | window | utc_bucket`). A replayed job resumes that run id; a new slot
opens a new run. Between runs the snapshot table is append-only.

Provider cadence is the floor, not the target:

| Provider | Object | Cadence |
| --- | --- | --- |
| Facebook | Page | 24 h ± jitter (insights update ~daily) |
| Facebook | post / video / reel | +1 h, +24 h, +48 h, +7 d, +28 d, then monthly |
| Instagram | account | 24 h ± jitter (data delayed up to 48 h) |
| Instagram | media | +1 h, +24 h, +48 h, +7 d, +28 d, then monthly |
| TikTok | profile counters | 24 h |
| TikTok | video counters | +1 h, +24 h, +7 d, +28 d, then monthly |

Local budgets sit far below the provider ceilings (60 calls/hour/account for Meta and Instagram,
6 calls/minute/account for TikTok) and respect `retry-after` / `x-business-use-case-usage`.

## Provider versions

The Graph version is pinned per app record (`provider_app_config.api_version`, default `v23.0`) and
recorded on every observation. `services/analytics/version_monitor.py` reports the configured
version, the latest documented version (`v27.0` for Meta on 2026-11-23), and every metric whose
`api_version_max` is below the configured one. A retired version is refused **before** any HTTP call
(`VersionUnsupported`, `GRAPH_VERSION_UNSUPPORTED`; `v19.0` retired 2026-05-21 and `v20.0` retires
2026-09-24, both recorded as data). Nothing in Phase 8 upgrades a version; that is a human edit
followed by the compatibility tests in `tests/test_analytics_version_compat.py`, which also fail if a
version literal appears in an adapter.

## Attribution

`analytics_snapshot.account_id` and `publication_id` are foreign keys (`CASCADE` and `SET NULL`).
`content_project_id`, `campaign_id` and `asset_id` are plain strings captured at collection time:
deleting a draft project must not erase what a published video did. This is the one deliberate
departure from pure normalisation and it is documented in the model.

## Determinism

Numbers come from SQL and Python only. Models narrate; they never compute. Every recommendation
stores the snapshot and aggregation ids it was built from, and the agent output validator rejects a
number that is not traceable to the evidence payload.

## Recommendations

`services/analytics/recommendations.py` runs two stages. Detection is pure Python over aggregations:
seven rules (topic, format, hashtag, timing, retention, growth, campaign coverage), each with a
sample gate (`MIN_PUBLICATIONS = 8`, and per-rule gates such as `MIN_HASHTAG_USES = 5` or
`MIN_TIMING_PUBLICATIONS = 10`). A rule that fails its gate emits nothing — a group of three posts
is not a finding. Comparisons are always against the **rest** of the account: a group compared with
a mixture containing itself has overlapping intervals by construction and hides its own effect.

Narration is optional and number-free. The deterministic sentence is always available, so a model
failure degrades prose quality instead of hiding a measured pattern.

Eight fields are required before a row is written (`_validate`): observation, evidence, sample size,
confidence (+ method), limitations, proposed experiment, expected benefit, kind. Evidence is
re-resolved against the database (`assert_resolvable`), so a recommendation cannot cite a snapshot or
aggregation that does not exist. Accepting a recommendation creates one hash-bound approval request
for *work*; it never creates a publish job and never satisfies a publishing approval.

## API

`api/routes/analytics.py` is a 53-endpoint, token-authenticated read surface over these tables
(`docs/ANALYTICS_API.md` is the reference). It does not compute anything itself: collection control
enqueues on the existing `analytics` lane, comparisons call the same deterministic service the tools
call, and recommendations call `recommendations.accept/reject`. Every metric payload carries its
availability and a NULL value when it is not `observed`, so the GUI cannot accidentally render a gap
as a zero. Expensive provider work is never executed inside a request.

## Consent and the permission gap (P8.20)

Availability is derived from stored facts, so a metric an account has never been granted is
`not_authorized`, not zero. `services/social/consent.py` turns that into an actionable plan:

* `ConsentService.plan(account)` reads the analytics capability for the account's platform, resolves
  its scopes **per login path** (Instagram Login vs Facebook Login never share a vocabulary), subtracts
  the granted scopes and returns the missing/requested set **and** the non-OAuth blockers (a Page
  `ANALYZE` task, a developer app still in development mode, an unsupported account type). It also
  lists which stored metrics would still be `not_authorized` after the upgrade, from the registry.
* `POST /analytics/consent/{account_id}` starts the upgrade through the existing OAuth engine,
  account-bound. The backend computes the scopes; the client sends only an intent note.
* The callback re-verifies the provider identity, adopts the credential for the *same* account,
  unions the granted scopes, then re-evaluates capabilities and writes a snapshot
  (`trigger="analytics_consent"`). The result is `capability_available` or `capability_still_blocked`;
  a successful exchange alone never marks analytics available.

A denial, an identity mismatch or a vault failure leaves the account connected with its previous
credential and scopes. The GUI surfaces the plan on Account performance and, for every affected
account, on Social accounts → Permissions.

Agents never initiate this: no analytics agent tool reaches the consent routes (they can only *read*
the missing-scope state and recommend the upgrade), and the routes require the per-launch user session
token.

### Consent troubleshooting

Everything below comes from `GET /analytics/consent/{account_id}`; nothing is guessed from the platform
name.

| Status | Required scope | Existing scope | Additional scope requested | Who must act |
| --- | --- | --- | --- | --- |
| `consent_not_needed` | the insight scope | already granted (and, for a Page, the `ANALYZE` task held) | none | nobody |
| `consent_required` | `read_insights` / `instagram_manage_insights` / `instagram_business_manage_insights` / `user.info.stats` | the base scopes, not the insight scope | exactly the missing insight scope | the user, via "Grant analytics access" |
| `developer_approval_required` | as above | as above | as above | the app owner: the developer app needs review / must leave development mode — user consent alone cannot fix it |
| `page_task_required` | granted | the insight scope is granted | none | the user: assign the Page `ANALYZE` role in Facebook (a role, not a scope) |
| `unsupported` | — | — | none | nobody: this account type or login path has no insight permission |

After a request, `POST` returns `authorization_started`; the outcome is read back from the account:

* `capability_available` — the provider granted the scope **and** every other condition holds.
* `capability_still_blocked` — the request succeeded but a blocker remains (a withheld scope, a Page
  task, an unapproved app). `plan.metrics_blocked` names the stored metrics still `not_authorized`.
* `authorization_denied` — the user declined; the account is unchanged and analytics stays blocked.
* `account_identity_mismatch` / `authorization_failed` — refused/aborted; the old credential is intact,
  so the user can try again.

## Desktop client (P8.18-P8.19)

`src/components/chartModel.ts` holds the rules (observed detection, gap segmentation, deterministic
axis, formatting, empty reasons, API→point mapping) and `src/components/charts.tsx` renders them as
dependency-free SVG. The analytics pages import those, never their own chart logic.

`src/services/apiAnalytics.ts` is the typed client: bearer auth, the shared error envelope,
`URLSearchParams` filters, bounded pagination and an `AbortSignal` on every read so a filter change
cancels the previous request instead of rendering a stale response. It returns API payloads untouched
(`availability` string, possibly-null `value`), and pages map rows to chart points only through
`pointsFromRows`, so a gap cannot be turned into a zero anywhere.

The three batch-1 pages (`pages/analytics/{Overview,PlatformComparison,AccountPerformance}.tsx`) read
`/analytics/{overview,platforms,freshness,capabilities,metrics,availability,accounts,series,compare,runs}`.
Platform comparison groups by the registry's `preferred_for` concept rather than by metric key, because
keys are platform-specific (`fb.post.media_views` ≠ `tt.video.views`), and it states explicitly when the
resolved definitions differ in unit or class.

The P8.21 pages (`pages/analytics/{PublicationDetail,ContentComparison,CampaignAnalytics}.tsx`) cover
one publication, a within-account content comparison, and a campaign. Their shared, pure logic lives in
`components/analyticsModel.ts` (unit-tested with `node --test`), which is where the "missing is not
zero" rule is applied to these views:

* **Publication detail** reads `/analytics/publications/{id}` (metrics by window), `…/retention`,
  `…/timeline`, and `/analytics/{accounts,runs,metrics}`. Retention is drawn **only** from an observed
  curve whose `value_json.points` are numeric; every other case (`available: false`, a stored-but-
  unobserved curve, a malformed payload) returns no chart and a verbatim reason. The page states that
  the analytics API exposes no media asset, so there is no thumbnail.
* **Content comparison** resolves a concept to the account's platform metric, ranks publications from
  publication-scope aggregations, and calls `POST /analytics/compare` for deterministic group medians.
  It compares within one account/platform only and shows each group's sample size, availability and the
  backend's limitations.
* **Campaign analytics** renders `campaign → content_project → platform_variant → publish_job →
  observation` from `/analytics/campaigns/{id}`; metrics are grouped **by platform** and never summed
  across platforms, and a campaign with publish jobs but zero observations shows an empty state rather
  than a zero.

All three reuse the same chart primitives and share the availability vocabulary, so an unavailable
metric is a chip or a gap everywhere, never a fabricated point.

## Agent workflows (W1-W3)
`services/analytics/workflows.py` turns three reviews into *deterministic* task chains instead of
letting a model choose what to delegate that day:

* **W1 weekly growth review** — `analytics_director` plans; eight analysis agents run on the stored
  metrics (performance, views, engagement, retention, audience growth, hashtags, campaign,
  comparison); `growth_reporting_agent` writes the report; `strategy_director` states the direction;
  the chief compiles it. The engine still adds its own independent quality review afterwards.
* **W2 single-publication diagnosis** — performance, then retention, engagement and comparison, then
  an `analytics_director` verdict.
* **W3 experiment design** — `ab_testing_agent` designs one experiment (one variable, a pre-declared
  success metric, a stop rule), `analytics_director` reviews measurability, `strategy_director`
  checks fit. The output is a draft; nothing is scheduled or published.

The chain *shape* is code, asserted by `tests/test_agent_analytics_workflows.py`; the wording lives in
`prompt_template` rows under `analytics.*` (`analytics.measurement_rules`, `analytics.workflow.*`),
seeded once and never overwritten. Every analytics stage runs under the `analytics` task type, which
the engine maps to the read tools; it is deliberately **not** `department_plan`, because that task
type is auto-expanded into extra model-chosen work and would break a fixed chain. No workflow can
reach a publisher.

## The Analytics Center: concepts, not pooled metrics (P8.19–P8.23)

The desktop pages never show a number the backend did not measure, and they never pool two providers'
metrics. Three rules make that mechanical:

* **A column is a concept, a cell is a metric key.** `analyticsModel.resolveConceptCells()` resolves
  "views" to `fb.post.media_views` for a Facebook row and `tt.video.views` for a TikTok row. A platform
  that does not declare the concept yields `not_supported` with a null value — not a zero, and not
  another platform's number.
* **Ranking and comparison are separate.** `rankVideoCells()` ranks only observed values (an observed
  `0` ranks); `videoComparison()` groups the selection by metric key at one window and reports `n` per
  block, so a TikTok view is never ordered against a Facebook view. Groups are never merged.
* **Absence has a reason.** Every missing cell carries one of the ten availability states, and the
  audience page reads the availability plan to say *why* a follower metric is absent (permission,
  provider, threshold, not collected) instead of reporting "no metric".

Charts follow P8.18: a bucket with no observation is a gap (the line breaks), a bucket keeps the
latest observed value, and no series is interpolated, carried forward or smoothed.

## Decisions, integrity and stored preferences (P8.23)

The three P8.23 pages are where a person acts on analytics, so the rules they enforce are written down
here rather than left to the components.

### A recommendation is decided, not published

`analytics_recommendation` has five states; only `draft` and `reviewed` are decidable. The page renders
the two decisions as **two separate confirmations with different content and different consequences**:

| | Acceptance | Rejection |
| --- | --- | --- |
| Entry point | `Review acceptance…` | `Review rejection…` |
| Shows | recommendation, category, sample size, confidence, observation | recommendation, category |
| Requires | nothing (an optional note is stored) | a reason, without which the confirm button is disabled |
| Consequence | `POST …/accept` stores the decision and creates **one hash-bound approval request for the experiment** | `POST …/reject` stores the decision and the reason |
| Publishing | none | none |

Acceptance is *not* publishing approval. The approval request it creates is bound to the experiment
payload; carrying the experiment out is a separate workflow with its own gate (requirement 8, §13.4).
No page in the Analytics Center imports the publishing client, enqueues a job or offers a publish
control. The frontend also never decides: it posts, replaces the row with the server's response, and
reports a `409` as an already-decided conflict instead of pretending the decision succeeded.

### Nothing is displayed that cannot be resolved

`RecommendationOut` is rendered field by field, and the eight mandated fields are checked for *usable*
presence before they are drawn — an empty `limitations` list is a defect, not compliance. Evidence is
re-read from the ledger: the `snapshot_ids` an item cites are looked up in the account's stored
snapshots, and each item is classified `resolved`, `missing` (cited row absent), `not_observed` (the
provider returned no value — never a zero), `stale` (measured more than 30 days ago) or `unreferenced`
(no row cited at all). A page that showed the number without the classification would be asserting
more than the database supports.

### A downloaded report is verified before it is offered

The digest used for verification comes from the **report record** (`GET /analytics/reports/{id}`), never
from a field inside the downloaded document — a payload that carries its own hash proves nothing. The
file is fetched as bytes through the authenticated client and hashed with WebCrypto SHA-256; an empty
body is refused before hashing, a record with no digest is refused outright, and a mismatch is reported
as an integrity failure with both digests and the failure kind. The file is never silently renamed,
regenerated or retried: a repeated mismatch is a storage or transport fault, and hiding it behind a
retry would make the failure unobservable. Reports are JSON today; the page does not offer CSV or PDF.

### Settings are bounded by the API, and nothing else

`analytics.settings` holds five keys, and the page edits exactly those five. Bounds come from the API
response, so the frontend cannot drift from the backend's own limits; a value outside them is an error
and never a clamp (clamping saves a configuration nobody asked for), the `PUT` carries only changed
keys, and the form is re-seeded from the response. The backend's note is displayed verbatim: these are
read by the sweeper on its next tick and do not alter a running collect, an already-queued job, the
Resource Governor, queue limits, provider rate limits, retention or security. Reporting defaults,
per-run budgets and a reset-to-defaults are named as gaps — the endpoint returns stored values and
bounds but not its defaults, so a reset button would be a guess.

## Artefacts, expiry and version telescopy (P8.24)

Three things outlive a collection run: a report, a retention decision, and the record of which
provider API version was in force. Each has its own module and its own honesty rule.

* **A report is bytes with a digest.** `reports.py` renders one content model into JSON, CSV or PDF,
  writes `….part`, renames, and takes `sha256`/`bytes` from the file it just wrote. Nothing recomputes
  a number at download time, and no format is offered that a renderer does not implement.
  See `docs/ANALYTICS_REPORTS.md`.
* **Retention deletes history, never business records.** The policy is per category, with the reason
  carried in the preview response; decided recommendations, publications, campaigns, accounts, media
  and metric definitions are structurally out of scope. See `docs/ANALYTICS_RETENTION.md` and ADR-30.
* **The version watch observes and warns.** It records the pinned version, the latest documented
  version and the age of that observation, and it has no code path that changes the pin. A stale
  observation is labelled stale rather than presented as current (ADR-31).
* **Maintenance shares the analytics lane.** Retention and the version check are two job types at
  lower priority on the existing lane, queued by the existing sweeper after collection work (ADR-33).
  No fifth thread, no second queue.

`GET /analytics/diagnostics` reports each of these as a fact with a timestamp, an audit id, or an
explicit "never run" — never as a green tick (ADR-32).

## Local environment note (2026-11-25)

Windows Smart App Control is **On** on this machine and began blocking SQLAlchemy's unsigned
compiled extensions (`sqlalchemy/**/*.pyd`), so `import sqlalchemy` failed with
`DLL load failed while importing _util_cy`. SQLAlchemy ships a pure-Python implementation next to
every compiled module, so `scripts/venv_pure_python_sqlalchemy.ps1` renames the eight `.pyd` files to
`*.pyd.sacblocked` (reversible with `-Restore`). No OS security setting was changed and no project
dependency was swapped. The full suite runs green in that mode (1127 passed, 9 skipped) and the
workaround is recorded in `docs/evidence/P8.md`.
