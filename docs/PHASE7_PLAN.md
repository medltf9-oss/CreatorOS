# Phase 7 — Unified social media publishing engine

Status: **plan, not code.** Every decision below is made against the code that exists today, after
inspecting it. Tasks 7.1–7.22 are the work; §11 is what will be blocked and why.

Pinned provider versions for this phase: **Graph v23.0**, **graph.instagram.com v23.0**,
**TikTok API v2**. They are configurable per app record and are never bumped silently.

---

## 1. What already exists (inspected, not rebuilt)

| Area | Reality found | Consequence for Phase 7 |
| --- | --- | --- |
| Publishing tables | `publish_job`, `publish_attempt`, `approval_record`, `webhook_event`, `platform_variant`, `campaign`, `content_project` — from Phase 2 | **Reuse all of them.** No parallel publishing tables. |
| `publish_job` columns | `variant_id`, `platform`, `account_id`, `scheduled_at_utc`, `display_timezone`, `job_state`, `attempts`, `idempotency_key` (UNIQUE), `remote_container_id`, `remote_post_id`, `remote_status_json`, `last_error_*`, `next_attempt_at`, lease fields | Add the Phase 7 fields (§4). The lease columns are redundant with the queue and will be documented as unused rather than dropped in the same migration. |
| `publish_attempt` | attempt_no, started/finished, http_status, request_id, response_code, `response_excerpt_redacted`, outcome | Already the ledger the spec asks for. Extend `outcome` vocabulary, add `error_category`, `provider_operation`. |
| `PublishState` | 13 values (`draft`, `validation`, `awaiting_approval`, `scheduled`, `queued`, `uploading`, `processing`, `published`, `needs_reconciliation`, `needs_attention`, `manual_publish_required`, `failed`, `cancelled`) | Spec wants 18. Extend + normalise legacy values (same technique as ADR-24), do not rewrite stored rows. |
| Publishing service / API / GUI | **None.** Zero routes, zero services; `publish_job` has never been written outside one agent tool | Everything in Parts 3–14 is new code on top of existing tables. |
| `publish.plan.generate` agent tool | Creates draft `PublishJob` rows from a project's variants, picking *any* connected account, with an idempotency key over (variant, platform, account, schedule) | Replace the "any account" behaviour with explicit targets; keep the idempotency-key recipe as the basis of the publishing-intent key. |
| Approvals | Two systems: `ApprovalRequest` + `ApprovalService` (hash-bound, single-use, TTL, `verify_for_use`/`consume`) **and** `approval_record` (immutable consent ledger: `payload_hash`, `consent_text_version`, `expires_at`, `result`) with no service | Use **both, for their intended roles** — `ApprovalService` is the gate, `approval_record` is the ledger written at grant time. No third approval store. |
| Queue | `queue_job` with named lanes: `default` (worker) and `media` (second thread, concurrency 1) | Add a third lane `publish`, same table, same `QueueWorker`. No second queue. |
| Media | `media_asset` (sha256, probe facts), `media_derivative` (kind/profile/version, `rel_path`, `sha256`), `PROFILES` = fast_preview / editing_proxy / normalized / audio_* , `MediaStorage.resolve_stored` refuses paths outside the media root | Publishing reads derivatives through `resolve_stored`; new platform profiles only where an existing one fails a limit (§8). |
| Social layer (P6) | `social_account` (+`meta_json` with page `tasks`/`can_post`, IG path, creator info), `account_capability`, `capability_snapshot`, `TokenService.access_token(account)`, `ProviderAppService.resolve_credentials`, `AdapterRegistry`, `SecretStore`/broker vault | Preflight reads these directly. No new credential path. |
| `/version` | `publishing: not_implemented`; `_phase7_readiness` already reports publish-capable accounts and blockers | Becomes a real per-platform map (§9). |
| Webhooks | `webhook_event` table exists; **no receiver, no public HTTPS endpoint** | Reconciliation is polling-based. `webhook_event` is used to de-duplicate provider status observations. A public callback is out of scope (§11). |
| Electron | main-process vault + broker, `safeStorage`, renderer has no decrypt handle, `openAuthUrl` host allowlist | Media-delivery credentials go through the same sealed-secret path. |
| Tests | 742 passed / 9 skipped; every provider HTTP shape is a recorded `httpx.MockTransport` fixture | Publishing fixtures extend the same pattern; no live calls in the default suite. |

Git state at planning time: branch `main`, working tree clean, HEAD `83a9e35` (Phase 6).

---

## 2. Architecture

```
 desktop (renderer)                    backend (loopback, token-authed)
┌──────────────────────┐              ┌────────────────────────────────────────────┐
│ Publishing Center    │  REST        │ /publishing/*  (≈40 routes)                │
│  composer · calendar │─────────────▶│                                            │
│  monitor · history   │              │ services/publishing/                       │
└──────────┬───────────┘              │  campaigns.py   destinations.py            │
           │ IPC (vault only)         │  content.py     validation.py              │
           ▼                          │  approvals.py   preflight.py               │
┌──────────────────────┐              │  scheduler.py   executor.py                │
│ Electron main        │              │  reconciliation.py  history.py             │
│ safeStorage vault    │◀────────────▶│  delivery.py (provider selection)          │
└──────────────────────┘   broker     │                                            │
                                      │ integrations/social/publishing/            │
                                      │  base.py        (Publisher ABC, results)   │
                                      │  facebook/      instagram/      tiktok/    │
                                      │  media_delivery/ (s3 adapter)              │
                                      │  metadata/      (limits + validators)      │
                                      └───────────┬────────────────────────────────┘
                                                  │ queue lane "publish"
                                      ┌───────────▼────────────────────────────────┐
                                      │ publish_job · publish_attempt              │
                                      │ publish_delivery · publish_reconciliation  │
                                      │ approval_request · approval_record         │
                                      │ queue_job (lane=publish)                   │
                                      └───────────┬────────────────────────────────┘
                                                  │ https, allowlisted hosts
                                Facebook Graph · graph.instagram.com · open.tiktokapis.com
                                                  │ + S3-compatible bucket (Instagram only)
```

**Separation enforced by module boundaries, not convention:**

* `integrations/social/publishing/*` performs HTTP and knows nothing about SQLAlchemy sessions.
* `services/publishing/executor.py` is the **only** module allowed to call a publisher, and it is
  reachable only from the queue handler — no route, no agent tool.
* Agents get metadata tools only. A test asserts no tool handler imports the executor or a publisher.

---

## 3. Decisions

**D1 — One publishing intent = one `content_project`.** `campaign` → `content_project` → one
`platform_variant` per (platform, target account) → one `publish_job` per variant. The unique
constraint on the variant is what makes accidental duplicates impossible; intentional republication
creates a **new project** ("intent #2"), which is exactly what Part 11 demands. No new grouping
table, no new unique rule.

**D2 — `publish_job` stays the destination ledger.** The spec's "publishing_jobs" is this table.
Renaming it would break Phase 2 foreign keys for a cosmetic match; the mapping is documented in
`PUBLISHING_ARCHITECTURE.md` instead.

**D3 — 18 canonical states, legacy values normalised at the boundary.** `validation`→
`validation_pending`, `queued`→`ready`, `needs_attention`→`requires_user_action`,
`manual_publish_required`→`requires_user_action` (with `state_reason="manual handoff"`). Stored rows
are not rewritten. `partially_published` exists for **campaign rollups only**; a destination is never
"partially" published and the API says so.

**D4 — Approval is `ApprovalService`, recorded in `approval_record`.** Request → human decision →
`verify_for_use(payload)` at execution → `consume` + `approval_record` row (with
`consent_text_version`). The bound payload is canonical JSON of: intent id, destination id, platform,
account id, asset sha256, derived sha256 + profile, caption, hashtags, title, privacy/options,
schedule (UTC + display timezone), capability snapshot id. Any change ⇒ new hash ⇒ old approval
refused with `APPROVAL_HASH_MISMATCH`. Nothing else in the system can mark a destination approved.

**D5 — Media delivery is a provider interface with an S3-compatible implementation, and Instagram is
blocked without one.** `MediaDeliveryProvider` (upload / sign / verify / checksum / renew / delete /
health). S3 adapter implements SigV4 locally (no new dependency), uses `x-amz-checksum-sha256`, and
signs short-lived GET URLs. **No localhost, no tunnel, no "Meta can reach 127.0.0.1"** anywhere in
the code or the UI. If no provider is configured: `delivery_not_configured`, Instagram destinations
are `blocked` with that reason, and the composer shows it before the user writes a caption.

**D6 — Third queue lane `publish`, one worker by default.** Uploads are minutes-long and
network-bound; they must not starve backups, AI or FFmpeg. `CREATOROS_PUBLISH_WORKERS` (default 1)
allows more without touching the durability model.

**D7 — Exactly-once is not claimed.** Local: `publish_job.idempotency_key` UNIQUE + one in-flight
queue job per destination (`idempotency_key="publish:{destination_id}"`). Remote: the provider's own
object id is the anchor — IG container id, TikTok `publish_id`, Facebook returned video id —
persisted **before** the next call. On a timeout after a request that may have been accepted, the
destination goes `unknown_remote_state` and only reconciliation may move it; blind resubmission is
impossible because the retry path requires either a terminal provider error or a reconciliation that
found nothing.

**D8 — Polling, not webhooks, for remote state.** A public HTTPS receiver is out of scope for a
local-first app (Part 13 says "available provider APIs and webhooks"; only the former is available
here). `webhook_event` stores de-duplicated status observations keyed by provider event/publish id
with `signature_valid=false` and the reason, so the table's meaning is not quietly redefined.

**D9 — Upload URLs are secrets.** Provider upload URLs carry credentials in the query string.
They are stored in the DPAPI vault (`purpose="upload_url"`… rejected by the current allowlist, so
`provider_upload_session`) and never in `publish_job`, logs, audit metadata or error strings.
Redaction patterns gain signed-URL rules.

**D10 — SSRF is a validation step, not a hope.** Every provider-returned URL (TikTok `upload_url`, IG
container `video_url` we create ourselves) is checked: `https`, host in an allowlist
(`open.tiktokapis.com`, `*.tiktokapis.com`, `*.tiktokcdn.com`, `<configured bucket host>`), no IP
literal, no private/loopback range, no redirects followed. Tests attempt each violation.

**D11 — Missed schedules never publish silently.** Default policy `ask`: a destination whose
`scheduled_at_utc` passed while the app was closed becomes `requires_user_action` with
`missed_schedule`, showing intended vs. current time; the user publishes now, reschedules or cancels.
Setting `late_policy = publish_now | ask | skip` is per-workspace and recorded in the audit trail.

---

## 4. Database changes (migration `a7b8c9d0e1f2`, after `f6a7b8c9d0e1`)

ALTER-only for `publish_job`, `publish_attempt`, `platform_variant`; two new tables; nothing dropped.

`publish_job` (new columns)

| Column | Type | Why |
| --- | --- | --- |
| `intent_key` | str(64), indexed | stable hash of (project, platform, account) — the publishing intent |
| `asset_id`, `asset_sha256` | str | the original that was approved |
| `derivative_id`, `derivative_sha256`, `derivative_profile`, `derivative_version` | str/int | the exact file that will be uploaded |
| `publish_mode` | enum | `video` · `reels` · `direct_post` · `inbox` |
| `title` | str(500) | TikTok title / FB title, separate from caption |
| `caption`, `hashtags_json`, `metadata_json`, `options_json` | JSON | platform-specific payload, exactly as approved |
| `field_provenance_json` | JSON | which agent wrote which field (Part 7.1) |
| `approval_request_id`, `approval_payload_hash`, `approval_record_id` | str | the bound approval |
| `payload_hash` | str(64) | hash of the exact outbound payload (reconciliation anchor) |
| `provider_upload_session_id`, `provider_media_id`, `provider_permalink` | str | remote facts |
| `provider_published_at`, `remote_checked_at` | datetime | timing facts |
| `upload_bytes_total`, `upload_bytes_done`, `upload_expires_at`, `upload_started_at` | int/datetime | real progress only |
| `error_category` | enum | permanent · transient · authorization · permission · rate_limit · validation · media · provider_processing · ambiguous |
| `state_reason` | str(300) | plain-language reason shown in the UI |
| `missed_at` | datetime | set when a schedule was missed |
| `reconcile_attempts` | int | bounded reconciliation |

`publish_attempt` (new columns): `provider_operation` (str), `error_category` (enum),
`remote_snapshot_json` (JSON) — the ledger keeps what was observed, not just what failed.

`platform_variant` (new columns): `metadata_json`, `options_json`, `title`, `source_derivative_id` —
so the composer's approved content lives where Phase 2 put variants.

New tables

* `publish_delivery` — one row per temporary cloud copy: `job_id`, `provider`, `bucket`, `object_key`,
  `sha256`, `bytes`, `url_expires_at`, `uploaded_at`, `verified_at`, `deleted_at`, `delete_attempts`.
  Needed because a retry may need a second copy and retention must be provable.
* `publish_reconciliation` — `job_id`, `attempt_id`, `checked_at`, `provider_state`, `decision`
  (`no_remote_object` · `remote_exists` · `undetermined`), `evidence_json`, `resolved_by`.

Unique constraints: `publish_job.intent_key` is **not** globally unique (republication is legal);
`(idempotency_key)` stays unique; `publish_delivery (job_id, object_key)` unique; queue lane jobs get
`idempotency_key="publish:{job_id}"` so a second enqueue is refused while one is open.

Backup first (`data/backups/manual-p7-*.db`), migrate a **checkpointed copy** (`-wal`/`-shm`
included), `PRAGMA foreign_key_check` (must be `[]`), `PRAGMA integrity_check` (must be `ok`),
downgrade/upgrade round trip on an isolated `CREATOROS_ROOT`, and 111 agents + 40→42 tables verified.

---

## 5. API surface (new `/publishing` router, token-authenticated)

| Group | Routes |
| --- | --- |
| Overview | `GET /publishing/overview` |
| Campaigns | `POST/GET /publishing/campaigns` · `GET/PATCH /publishing/campaigns/{id}` · `POST /publishing/campaigns/{id}/cancel` · `GET /publishing/campaigns/{id}/summary` |
| Destinations / composer | `POST /publishing/campaigns/{id}/destinations` · `GET/PATCH/DELETE /publishing/destinations/{id}` · `POST /publishing/destinations/{id}/generate` (agent-assisted fields) · `POST /publishing/destinations/{id}/validate` · `GET /publishing/destinations/{id}/preview` |
| Publishing | `POST /publishing/destinations/{id}/request-approval` · `POST /publishing/destinations/{id}/publish` (needs `approval_id`) · `POST /publishing/destinations/{id}/schedule` · `POST /publishing/destinations/{id}/cancel` · `POST /publishing/destinations/{id}/retry` · `GET /publishing/destinations/{id}/upload` · `POST /publishing/destinations/{id}/republish` (new intent) |
| Reconciliation / history | `POST /publishing/destinations/{id}/reconcile` · `GET /publishing/history` · `GET /publishing/history/{destination_id}` · `GET /publishing/attempts/{attempt_id}` · `GET /publishing/reconciliations` |
| Scheduler | `GET /publishing/calendar` · `POST /publishing/schedule/sweep` (manual trigger, same code as the sweeper) |
| Media delivery | `GET /publishing/delivery/health` · `PUT /publishing/delivery/config` · `PUT /publishing/delivery/secret-ref` · `POST /publishing/delivery/verify` · `GET /publishing/delivery/assets` · `POST /publishing/delivery/assets/cleanup` |
| Settings | `GET/PUT /publishing/settings` |
| Approvals | reuses `GET/POST /agents/approvals*` (no parallel approval API) |

Conventions: existing error envelope `{code, message, detail?, correlation_id}`; `Page[T]` paging;
no response contains a token, an upload URL, a signed URL or a filesystem path outside the media root
(paths are returned as `media://` ids plus a signed media-access URL).

---

## 6. GUI — Publishing Center (9 pages)

| Page | Content |
| --- | --- |
| Overview | counts straight from SQL: published, scheduled, drafts, awaiting approval, uploading, processing, failed, blocked accounts, active campaigns |
| Composer | left: asset picker (library), video facts, thumbnail, destination accounts; centre: per-platform editors (caption/title/hashtags/options) + previews; right: capabilities, validation report, approval state, schedule, publish action |
| Account selector | avatar, platform, name, connection status, publish capability, missing scopes, blocked reason, selection |
| Platform previews | Facebook video card, Instagram Reel phone frame, TikTok card — each labelled "approximate representation", real account identity, real caption and hashtags |
| Calendar | day/week/month, scheduled campaigns, per-destination status, drag-to-reschedule only for `scheduled`, timezone shown |
| Upload monitor | live bytes from `publish_job.upload_*`, speed computed from two observations, ETA only when stable, processing state, retries — nothing synthesised |
| History | search/filter by platform, account, date, campaign, status, asset; attempt drill-down |
| Campaign detail | every destination independently, partial-success summary, per-destination retry/reconcile |
| Settings | delivery provider, upload limits, retry policy, late-publication policy, defaults — **no secrets in renderer** |

---

## 7. Agents

Existing: `publish.plan.generate`, `caption.compose`, `content.readiness.check`, `social.accounts.list`,
`social.capabilities.read`, `human.approval.request`.

Add (all read-only or plan-only): `publishing.destinations.read` (status/validation/approval state),
`publishing.preview.read` (rendered payload an agent may critique), and extend `publish.plan.generate`
to accept explicit `account_ids` + per-destination content. **No tool may publish, upload, schedule an
execution, or approve.** Grants: `publishing_operations`, `creative_writing`, `platform_adaptation`,
`quality_governance`, `analytics_growth`. A test asserts the executor and publisher modules are unreachable from the tool
registry.

---

## 8. Media selection and validation

Per platform, from `media_asset.probe_json` (never filenames):

| Platform | Container / codec | Frame | Duration | Size | Notes |
| --- | --- | --- | --- | --- | --- |
| Facebook Page video | MP4/MOV, H.264 + AAC | ≥ 240p, ≤ 1080p, 16:9 to 9:16 | ≤ 240 min | ≤ 4 GB | chunked resumable |
| Instagram Reels | MP4/MOV, H.264 + AAC | 9:16, ≥ 540×960 | 3 s – 15 min (API max), Reels surface 90 s | ≤ 1 GB | needs public URL |
| TikTok Direct Post | MP4/MOV, H.264 + AAC | 9:16 or 1:1, 360–4096 px | 3 s – 10 min, also `creator_info.max_video_post_duration_sec` | ≤ 4 GB | chunk size from init response |
| TikTok Inbox | same as Direct Post | | | | separate scope |

Profile choice: reuse `normalized` (1080p30 H.264/AAC) when it satisfies the platform; otherwise
generate a **new** derivative with a new profile (`publish_reels_1080`, `publish_tiktok_1080`) —
never mutate the original, never re-encode an acceptable file. Validation reports are
`passed | warning | failed | unsupported | requires_attention` with per-rule detail, stored in
`platform_variant.validation_report_json`.

---

## 9. `/version` after Phase 7

```json
{"publishing": "available",
 "publishing_facebook": "…", "publishing_instagram": "…", "publishing_tiktok_direct": "…",
 "publishing_tiktok_inbox": "…", "publishing_media_delivery": "…",
 "publishing_scheduler": "available", "publishing_reconciliation": "available"}
```

Each value is derived from facts: app configured, account connected, capability `available`,
scopes granted, delivery provider configured, broadcaster approved. Values like
`no_eligible_account`, `delivery_not_configured`, `needs_developer_approval`,
`app_review_required` are the honest answers until they change.

---

## 10. Tasks

| # | Task | Done when | Verified by |
| --- | --- | --- | --- |
| 7.1 | This plan | committed before code | — |
| 7.2 | Enums + state machine | 18 canonical states, normaliser, terminal sets, per-state allowed transitions in one table | `test_publishing_state.py` |
| 7.3 | Migration `a7b8c9d0e1f2` | ALTER-only + 2 tables; copy test; fk + integrity; downgrade/upgrade | `test_publishing_migration.py` |
| 7.4 | Provider limits + metadata validators | per-platform rule tables with docs URLs; validators return typed findings | `test_publishing_validation.py` |
| 7.5 | Composer services | campaign/project/variant/destination CRUD, field provenance, variant regeneration | `test_publishing_composer.py` |
| 7.6 | Approval binding | canonical payload, hash, request/verify/consume, ledger row, tamper matrix | `test_publishing_approvals.py` |
| 7.7 | Media delivery provider | interface + S3 SigV4 adapter + unconfigured double; checksum verify; retention cleanup | `test_publishing_delivery.py` |
| 7.8 | Facebook publisher | chunked start/transfer/finish, offset recovery, processing poll, permalink | `test_publishing_facebook.py` |
| 7.9 | Instagram publisher | delivery upload → container → status poll → media_publish → verify; both paths | `test_publishing_instagram.py` |
| 7.10 | TikTok publishers | creator_info, Direct Post init/upload/status, inbox path, rate limits, upload-URL expiry | `test_publishing_tiktok.py` |
| 7.11 | Executor + queue lane | preflight → approval → upload → poll → persist; one in-flight job per destination | `test_publishing_executor.py` |
| 7.12 | Scheduler + missed runs | due/future/missed/restart/timezone-change policies | `test_publishing_scheduler.py` |
| 7.13 | Reconciliation | ambiguous outcomes, provider polling, bounded retries, manual decision | `test_publishing_reconciliation.py` |
| 7.14 | History + attempts API | filters, attempt drill-down, remote facts | `test_publishing_history.py` |
| 7.15 | API router | ≈40 routes, envelopes, no secrets/URLs/paths | `test_publishing_api.py` |
| 7.16 | Agent tools + grants | read/plan only; no executor reachability | `test_agent_publishing_tools.py` |
| 7.17 | GUI: 9 pages | real data, disabled-with-reason, screenshots | `docs/evidence/P7.md` |
| 7.18 | Security hardening | SSRF allowlists, upload-URL sealing, redaction patterns, leak scan | `test_publishing_security.py` |
| 7.19 | `diagnose.ps1` + `/version` | honest capability map; publishing readiness in diagnostics | `docs/evidence/P7.md` |
| 7.20 | Docs | 11 documents + ADR-25…28 + evidence | reviewed |
| 7.21 | Opt-in live harness | `scripts/live_publish.py`, refuses without flag + account + explicit confirmation | `LIVE_PUBLISHING_TESTS.md` |
| 7.22 | Acceptance | full suite, ruff, tsc, build, diagnose, migration, recovery, shutdown | `docs/evidence/P7.md` |

All 22 tasks are complete. Evidence: `docs/evidence/P7.md` (categories A/B/C/D). Live publication
remains unverified because this machine has no Meta app, no TikTok app, no connected account and no
delivery bucket — that is category D in the evidence file, not an oversight.

---

## 11. Honest limits and blockers, stated up front

1. **No live publication can be verified on this machine.** There is no Meta app, no TikTok app, no
   connected account. Everything will be tested against recorded HTTP fixtures; the report will say
   "implemented and fixture-tested", never "verified live".
2. **Instagram needs a configured S3-compatible bucket** (user-supplied, credentials in DPAPI). Until
   then Instagram publishing is `blocked: delivery_not_configured` — not "coming soon", not a
   localhost workaround.
3. **TikTok Direct Post needs the Content Posting API add-on**; without it that path is
   `needs_developer_approval` and the inbox path remains available.
4. **Facebook publishing in `live` mode needs app review** for `pages_manage_posts`; in development
   mode only app testers can authorize. The UI states which mode is in force.
5. **No public webhook receiver.** A local-only app cannot receive provider webhooks without exposing
   an endpoint; reconciliation is polling, and `webhook_event` records observations, not signatures.
6. **Exactly-once is impossible** with these providers. CreatorOS guarantees: no accidental duplicate
   within an intent, no blind resubmission after an ambiguous outcome, and reconciliation before any
   retry. Intentional republication is a new intent, explicitly requested.
7. **Remote media processing time is the provider's.** Progress for that phase is a status string, not
   a percentage; the UI will not invent one.
8. **Upload resumption is provider-specific**: Facebook supports chunked offsets, TikTok supports
   chunked upload with an expiring URL, Instagram has no resumable upload (its input is a URL, so the
   retry unit is the delivery upload + container creation).

---

## 12. Acceptance mapping (Part 20 → evidence)

Every item in the spec's Part 20 list maps to a task above and to a line in `docs/evidence/P7.md`,
with the four-way split the spec demands: **A. implemented and tested**, **B. verified with real
provider accounts**, **C. blocked by developer approval**, **D. blocked by missing credentials or
media delivery setup**. Section B will be empty on this machine, and the report will say so in the
first paragraph rather than in a footnote.
