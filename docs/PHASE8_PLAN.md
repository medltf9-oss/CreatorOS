# Phase 8 — Analytics and performance intelligence

Status: **plan, not code.** Nothing in this document has been implemented. It is written after
inspecting the repository at `d7706fc` (Phase 7 complete) and after reading the current official
provider documentation on 2026-11-23. Every claim about the existing system below was checked
against the code or the live database, and every provider metric name was read from the vendor's
own documentation page — sources are cited per row in §5.

Pinned provider versions for planning: **Graph v23.0** (`provider_app_config.api_version`, currently
the default in `integrations/social/config.py`), **graph.instagram.com v23.0**, **TikTok API v2**.
Documentation now publishes **Graph v27.0**; the migration consequences of moving are in §4.4 and
must not be applied silently.

### Approval amendments (round 2, 2026-11-25)

The plan was approved with eight additional binding requirements. They are folded into the sections
named below and each one has a task-level acceptance criterion; §21 is the coverage table that ties
every requirement to the code and the test that proves it.

| # | Requirement | Folded into |
| --- | --- | --- |
| 1 | TikTok account statistics map to the granted `user.info.stats` scope; `video.list` stays separate | §5.6, §6, P8.8 |
| 2 | Graph version stays pinned and configurable; version monitoring + explicit compatibility tests; never a silent upgrade | §4.4, §4.5, P8.4, P8.24 |
| 3 | Retries are idempotent across one logical collection run; snapshots stay append-only **between** genuinely separate runs | §7.2, §7.4, §8.3, P8.9 |
| 4 | Original value type preserved (numeric, structured breakdown, time-series); never coerced to a numeric-only field | §3.6, §7.2, P8.4, P8.6–P8.9 |
| 5 | Cumulative vs period vs point-in-time metrics are distinct; no change is computed from incomparable observations | §3.5, §7.1, §7.6, P8.12 |
| 6 | No invented metrics and no misleading cross-platform comparison | §3.4, §12, §13.1, P8.12, P8.19 |
| 7 | Every AI recommendation carries source measurements, periods, sample sizes, confidence and limitations | §13.3, P8.14, P8.15 |
| 8 | Recommendation acceptance and publishing approval are two independent human decisions | §13.4, P8.14, P8.23 |

---

## 1. Repository inspection findings

Inspected: all 114 backend Python modules, 4 model modules, 8 Alembic revisions, 56 backend test
modules, 44 desktop TS/TSX modules, 64 documents in `docs/` (plus 7 phase evidence files),
ADR-01…ADR-28, and the 7 evidence reports.

### 1.1 Database — the real 42 tables

`<DATA_ROOT>\data\database\creatoros.db`, `alembic_version = a7b8c9d0e1f2`, `integrity_check ok`,
`foreign_key_check []`. 41 domain tables + `alembic_version` = **42**.

| Module | Tables |
| --- | --- |
| `db/models/content.py` (8) | `app_setting`, `brand_profile`, `campaign`, `media_asset`, `media_derivative`, `transcript`, `content_project`, `platform_variant` |
| `db/models/ops.py` (20) | `agent_department`, `agent_definition`, `agent_task`, `task_edge`, `agent_run`, `agent_memory`, `agent_objective`, `approval_request`, `prompt_template`, `tool_registry`, `queue_job`, `audit_event`, `notification`, `analytics_snapshot`, `automation_rule`, `automation_run`, `model_profile`, `model_benchmark`, `ai_execution`, `diagnostics_run` |
| `db/models/platform.py` (7) | `secret_ref`, `provider_app_config`, `social_account`, `capability_snapshot`, `account_capability`, `oauth_session`, `credential_rotation` |
| `db/models/publishing.py` (6) | `publish_job`, `publish_attempt`, `publish_delivery`, `publish_reconciliation`, `approval_record`, `webhook_event` |

Live row counts (read-only query): `agent_definition` 111 · `media_asset` 2 · `queue_job` 14 ·
`agent_task` 62 · every publishing, social and analytics table **empty**.

### 1.2 Analytics-relevant infrastructure that exists

| Thing | Reality | Consequence |
| --- | --- | --- |
| `analytics_snapshot` | Exists since Phase 2. Columns: `platform`, `account_id` (no FK), `post_id`, `metric`, `value` (float **NOT NULL**), `unit`, `captured_at`, `hour_bucket`, `source`, `completeness`, `sample_size`. Unique on `(platform, post_id, metric, hour_bucket)`. **Zero readers, zero writers** in the whole codebase — only the model and the `MetricCompleteness` enum. | Reuse the name, rebuild the shape (§7). It cannot express "missing ≠ 0" (`value` NOT NULL), cannot hold both `lifetime` and `day` for one metric, and its unique key lets unlimited duplicate account-level rows because SQLite treats `NULL` `post_id`s as distinct. |
| `MetricCompleteness` | `complete`, `partial`, `small_sample`, `unavailable` | Keep for completeness, but it is *not* an availability vocabulary — Phase 8 needs 10 states (§3.4). |
| Remote publication identity | `publish_job.remote_post_id`, `provider_media_id`, `provider_container_id`, `provider_permalink`, `provider_published_at`, `account_id`, `platform`, `asset_id`, `derivative_id`, `title`, `caption`, `hashtags_json`, `metadata_json`; `intent_key`, `payload_hash`, `job_state` (18-state enum) | **This is the join key for all content analytics.** No new publication-identity table is needed. |
| Attribution chain | `campaign` → `content_project` → `platform_variant` → `publish_job`; plus `media_asset`, `brand_profile`, `transcript` | Campaign/project/asset attribution is a join, not a new column set — except where retention requires an immutable copy (§7.3). |
| Capability engine | `account_capability` + `capability_snapshot` + `integrations/social/config.py::CAPABILITY_CATALOGUE` already declares `facebook.page.read_insights` and `instagram.read_insights` as `headline=False`, with the note that the permission is **not requested by the connection flow** | Analytics availability must be derived from this table, not assumed. Those two capabilities will read `needs_user_consent` on every account until the scope is added (P8.20). |
| OAuth / tokens / vault | `services/social/tokens.py::TokenService.access_token(account)` is the single accessor; DPAPI vault behind the Electron broker; `credential_rotation` journal; `secret_ref` metadata | Analytics acquires tokens **only** through `TokenService`; no new credential path, no token in an analytics row. |
| Queue | `queue_job` with named lanes `default`, `media`, `publish` (3 lanes, 1 + 1 + N threads, `BackgroundWorker`), one sweeper thread calling `PublishScheduler.tick()` every `CREATOROS_SCHEDULER_SECONDS` (15 s) | Add a **fourth lane `analytics`** on the same table/machinery; add the analytics dispatcher to the **existing** sweeper thread (no fifth thread). |
| Agent workforce | 111 agents seeded, `analytics_growth` department exists with a leader `analytics_director` and 10 specialists, including `performance_analysis_agent`, `views_analysis_agent`, `retention_analysis_agent`, `engagement_analysis_agent`, `campaign_analysis_agent`, `hashtag_performance_agent`, `audience_growth_agent`, `content_comparison_agent`, `ab_testing_agent`, `growth_reporting_agent`; `strategy_director` (strategic_intelligence leader), `trend_research_agent`, `research_synthesis_agent` (research_intelligence) | The 13 agents named in the brief already exist. Phase 8 adds **tools**, not agents. |
| Tool surface | 21 declared tools in `services/toolbox.py::TOOL_SPECS`, all with real handlers; `report.generate` already writes a Markdown report under the data root and returns path + hash. **Census mismatch found:** `agents/catalog.py::TOOL_IDS` still lists 18 and omits `publishing.queue.read`, `publishing.destination.read` and `publishing.draft.create` — harmless today (grants are read from `tool_registry`), but it must be fixed in P8.15 so a new analytics tool cannot be announced without a catalogue entry. | Analytics needs 4 read tools + 1 write (recommendation proposal). No publish tool. |
| Approvals | `ApprovalService` (hash-bound, single-use) + `approval_record` ledger | Recommendation "apply" must go through these, not a new approval store. |
| API conventions | `/version` capability map, `{code,message,details?,correlation_id}` error envelope, `Page[T]`, `Pagination`, per-launch bearer token, `require_ipc_signature` for path-bearing calls, routers registered in `main.py` | New `/analytics` router follows exactly this. |
| GUI | 36 page components across 5 nav sections (37 nav entries incl. detail pages), `components/ui.tsx` (Card/Row/Badge/Button/Field/Progress/Stat/EmptyState). **No chart library** (`package.json` deps: react, react-dom only) | Charts must be hand-built deterministic SVG components — not a new dependency, and not decorative data. |
| Diagnostics | `/diagnostics` (disk, ffmpeg, Ollama, memory), `/version` (capability map), `docs/evidence/P*.md`, `scripts/ui_drive.py`, `diagnose.ps1` | Phase 8 adds analytics-specific capability values and a diagnostics section, same pattern. |
| Tests | 1114 passed / 9 skipped; provider HTTP always `httpx.MockTransport` with recorded shapes | Analytics fixtures extend the same pattern; no live calls by default. |

### 1.3 What does **not** exist (so nothing is duplicated)

No analytics router, service, adapter, collector, aggregation, report or recommendation code. No
scheduler other than the publish sweeper. No chart primitive. No `analytics_*` usage of any kind.
No provider permission for insights is requested in any `RouteSpec`.

---

## 2. Current architecture map

```
                     desktop renderer (React, no secrets, no direct HTTP)
                     ┌──────────────────────────────────────────────────┐
                     │ Studio · Library · Workforce · Social · Publishing│
                     └───────────────┬──────────────────────────────────┘
                                     │ REST + bearer token (loopback)
┌────────────────────────────────────▼─────────────────────────────────────┐
│ FastAPI backend  (api/routes/*, one router per domain)                   │
│   services/            publishing/, media/, social/, orchestrator, queue │
│   integrations/social/ adapters (facebook · instagram · tiktok)          │
│   integrations/social/publishing/ publishers + media_delivery + metadata │
└───────┬───────────────────┬──────────────────────┬───────────────────────┘
        │                   │                      │
   queue_job           SQLite (WAL)          Ollama gateway + governor
   lanes: default     41 domain tables       qwen3 0.6b / 1.7b / 4b
          media        (see §1.1)            1 generation at a time
          publish
        [analytics]  ← Phase 8 adds this lane, same table, same worker class

External: graph.facebook.com · graph.instagram.com · open.tiktokapis.com
          S3-compatible bucket (Instagram media delivery only)
```

**Phase 8 sits entirely on top of this.** The analytics path is:

```
publish_job (real remote ids)  ──┐
social_account + token service ──┤
account_capability            ──┼──► analytics collection (queue lane "analytics")
metric registry (data)        ──┘         │  provider adapters, one per platform
                                          ▼
                              analytics_snapshot (append-only observations)
                                          │  deterministic SQL/Python
                                          ▼
                              analytics_aggregation (windows, comparisons)
                                          │
                    ┌─────────────────────┴──────────────────────┐
                    ▼                                            ▼
        analytics_recommendation                      analytics_report
                    │                                            │
                    └──────────► agent workflows (existing 111) ◄┘
                                          │
                                          ▼
                              /analytics/* API  →  ANALYTICS CENTER (GUI)
```

No second queue, no second account store, no second approval store, no second report service.

---

## 3. Existing analytics infrastructure — what is reused, what is rebuilt

### 3.1 Reused unchanged
`publish_job` (+`publish_attempt`) as the publication ledger · `social_account` as the account
identity · `account_capability` as the availability oracle · `TokenService` and the DPAPI vault as
the only credential path · `queue_job` and `QueueWorker` as the execution substrate ·
`agent_definition`/`agent_task`/`agent_run`/`agent_memory` for the workforce ·
`ApprovalService`/`approval_request`/`approval_record` for any action that would change publishing ·
`audit_event` · `app_setting` (analytics settings live here, keyed `analytics.*`) · `report.generate`
tool for file output · `/diagnostics` and `/version`.

### 3.2 Rebuilt in place
`analytics_snapshot` — empty, unused, and structurally unable to meet the brief (value NOT NULL, no
provider object identity, no API version, no observation period, no availability state, NULL-unique
defect for account rows). It is **rebuilt by table-rebuild** (the Phase 7 pattern), keeps its name,
and gains a proper key. Rebuilding an unused empty table is cheaper and more honest than shipping a
second observation table next to a broken one.

### 3.3 New, and why each one is unavoidable

| Proposed | Needed because | Cannot be derived from |
| --- | --- | --- |
| `analytics_metric_definition` | The brief forbids erasing provider metric identity and requires the definition, unit, API source and availability rules to be stored per metric. Provider names change per API version. | Code constants alone: the stored observation must be interpretable years later, after the code changed. |
| `analytics_snapshot` (rebuilt) | The measurement ledger: one row per provider observation, availability-aware, append-only. | Nothing else stores per-object, per-period, per-version provider answers. |
| `analytics_collection_run` | Duplicate-collection prevention, retry/error classification, rate-limit evidence, "why is this metric stale" answers. | `queue_job.last_error` is one string and is deleted by retention; attempts are per job type, not per provider call batch. |
| `analytics_sync_state` | Initial sync, discovery cursor, incremental watermark, next-due calculation, backfill progress — per account and object type. | `analytics_snapshot.max(captured_at)` cannot say "discovery not finished" or "backfill stopped at cursor X". |
| `analytics_aggregation` | Performance windows (1 h/24 h/48 h/7 d/28 d), campaign/platform/account rollups, comparisons — with the method, window tolerance and input snapshot ids preserved. | Recomputing in the API on every request would make confidence/sample-size logic unauditable and slow. |
| `analytics_recommendation` | The brief demands observation, evidence, sample size, period, confidence, limitations, proposed experiment, estimate flag, and a human approval trail. | Agent task output JSON is free text with no lifecycle. |
| `analytics_report` | Immutable generated reports (period, filters, sections, file sha256, generator, task id) that survive a model change. | Files on disk have no queryable catalogue. |

**Rejected names from the brief:** `analytics_data_source` (a metric's source is
`analytics_metric_definition.source_endpoint` + `source_doc_url`; the *state* of a source for an
account is `analytics_sync_state` + `account_capability`) and `analytics_metrics` (that would be a
second value store; the observation table is the value store).

### 3.4 Vocabulary additions (enums, `db/enums.py`)

```python
class AnalyticsObjectType(StrEnum):     # what the measurement is about
    ACCOUNT = "account"; POST = "post"; VIDEO = "video"; REEL = "reel"; STORY = "story"

class MetricPeriod(StrEnum):            # the provider's aggregation window
    DAY = "day"; WEEK = "week"; DAYS_28 = "days_28"; MONTH = "month"
    LIFETIME = "lifetime"; TOTAL_OVER_RANGE = "total_over_range"; INSTANT = "instant"

class MetricAvailability(StrEnum):
    OBSERVED = "observed"                      # provider returned a value; 0 is a real 0
    MISSING = "missing"                        # documented as available, provider returned nothing
    BELOW_THRESHOLD = "below_threshold"        # provider withholds it for this account/object
    NOT_SUPPORTED = "not_supported"            # the provider does not expose it for this object type
    NOT_AUTHORIZED = "not_authorized"          # scope/permission missing on this account
    NOT_COLLECTED = "not_collected"            # not yet collected (queued, or window not reached)
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"  # 429/5xx/timeout after retries
    EXPIRED = "expired"                        # past the provider's retention window
    REDACTED_BY_POLICY = "redacted_by_policy"  # deliberately not collected (privacy minimisation)
    UNKNOWN = "unknown"

class AnalyticsRunOutcome(StrEnum):
    SUCCEEDED = "succeeded"; PARTIAL = "partial"; RATE_LIMITED = "rate_limited"
    NOT_AUTHORIZED = "not_authorized"; FAILED = "failed"; SKIPPED = "skipped"

class AnalyticsErrorClass(StrEnum):        # reuses the Phase 7 classification idea
    PERMANENT = "permanent"; TRANSIENT = "transient"; AUTHORIZATION = "authorization"
    PERMISSION = "permission"; RATE_LIMIT = "rate_limit"; THRESHOLD = "threshold"
    VALIDATION = "validation"; PROVIDER_PROCESSING = "provider_processing"
```

Rule enforced by a test and by the DB: **an unavailable metric never becomes `0`.** `value_num` is
`NULL` for every state except `OBSERVED`.

### 3.5 Metric taxonomy: cumulative, period, point-in-time (approval requirement 5)

Comparing two measurements is only legal when they are the same *kind*. The registry therefore
carries a mandatory `metric_class`, and **every comparison path reads it**:

| `metric_class` | Meaning | Examples | Legal arithmetic |
| --- | --- | --- | --- |
| `cumulative` | A counter that only grows for the life of the object; each observation is a running total | `tt.video.views`, `fb.video.views`, `ig.media.likes` (lifetime), `total_video_view_total_time` | **differences between two observations of the same object** (`v2 − v1`), never sums across objects, never a "change" between two different objects |
| `period` | The provider's own aggregation over a fixed window (`day`, `week`, `days_28`, `month`) | `fb.page.media_views` (day), `ig.account.reach` (day), `post_video_retention_graph` | sums/means **within the same `period_kind` and equal window length**, and only when windows do not overlap |
| `point_in_time` | A value describing the object at the moment of collection, not an interval | `fb.page.followers_total`, `ig.account.followers`, `tt.account.followers` | differences between observations of the same object, labelled `derived_from_snapshots`; never a "growth rate" without stating the observation gap |
| `ratio` | A provider-computed ratio or percentage | `reels_skip_rate`, retention-curve points | compare within the same provider definition only; never re-based or re-scaled |
| `series` | A structured vector or curve | `post_video_retention_graph` (40 buckets), `follows_and_unfollows` (breakdown) | compare only when both series have the same cardinality and dimension keys |

Enforced by: `metric_class` NOT NULL on `analytics_metric_definition`; a CHECK that `period` and
`point_in_time` metrics cannot be stored with a `lifetime` period unless the registry says so;
`analytics_aggregation.window_kind` must be compatible with the class; and
`comparison.refuse_incomparable()` returning a typed refusal (`INCOMPARABLE_CLASS`,
`INCOMPARABLE_PERIOD`, `OVERLAPPING_WINDOWS`, `INCOMPARABLE_SERIES`) that the GUI renders as text,
not as a number.

Concretely: "views grew 12 %" is only computed between two observations of the **same** publication
or the **same** account with the gap stated; a 28-day `period` metric is never compared with a
`lifetime` counter; and TikTok's cumulative `view_count` is never compared with Instagram's daily
`views`.

### 3.6 Value types are preserved, never coerced (approval requirement 4)

`analytics_metric_definition.value_kind` decides the storage column, and the writer refuses a
mismatch instead of casting:

| `value_kind` | Stored as | Never |
| --- | --- | --- |
| `scalar` | `value_num` (float) + `unit` | — |
| `breakdown` | `value_json` = `{"dims": {...}, "items": [{...}]}` (IG `total_value.breakdowns`, FB `*_by_type`, TikTok none) | folded into a single number, or dropped because it is not scalar |
| `series` | `value_json` = `{"points": [...], "cardinality": n, "dimension": "..."}` (retention curves, `follows_and_unfollows` time series) | truncated, resampled, or averaged into a scalar |
| `object` | `value_json` = the provider's own object, keys preserved | summarised into a count |

A `CHECK` pairs the columns: `value_kind='scalar'` ⇒ `value_json IS NULL`; every other kind ⇒
`value_num IS NULL`. The API returns the original structure (`value` **or** `value_json`), and the
GUI's chart primitives accept a vector — a curve is drawn as a curve, never as its average.

---

## 4. Official API research (read 2026-11-23)

### 4.1 Facebook (Meta Graph API)

Sources: `developers.facebook.com/docs/graph-api/reference/insights/`,
`/docs/graph-api/reference/video/video_insights/`, `/docs/video-api/guides/insights/`,
`/blog/post/2026/02/18/introducing-graph-api-v25-and-marketing-api-v25/`,
`/docs/instagram-platform/insights/`, `/docs/instagram-platform/reference/instagram-media/insights`,
`/docs/instagram-platform/api-reference/instagram-user/insights`,
`/docs/instagram-platform/changelog`, `/docs/graph-api/overview/rate-limiting/`.

| Fact | What the documentation says |
| --- | --- |
| Endpoint | `GET /{object-id}/insights?metric=…&period=…&since=…&until=…` for Pages and Page posts; `GET /{video-id}/video_insights?metric=…` for videos **and Reels**. |
| Permissions | Page insights: `read_insights` **and** `pages_read_engagement`, plus a Page token from someone with the `ANALYZE` Page task. Video insights page states `pages_manage_engagement` + `read_insights` + `ANALYZE`. |
| Account threshold | Page Insights **only for Pages with ≥ 100 likes**. |
| Freshness | Most metrics update **once every 24 hours**. |
| Retention | Only the **last two years** are available, for Page insights and for video insights. |
| Range | `since`/`until` windows limited to **90 days per request**; `period` values `day`, `week`, `days_28`, `month`, `lifetime`, `total_over_range`; `date_preset` list published. |
| Breakdowns | e.g. `page_media_view` supports `is_from_ads`, `is_from_followers`; radix/`*` metrics refresh several times a day; `_unique` values are **estimates**. |
| Reels caveat | "Interactions on Reels are not included" in Page insights; Reels metrics come from `video_insights` (`blue_reels_play_count`, `fb_reels_total_plays`, `fb_reels_replay_count`, `post_video_avg_time_watched`, `post_video_retention_graph`, `post_video_view_time`, `post_video_followers`, `post_video_likes_by_reaction_type`, `post_video_social_actions`, `post_impressions_unique`). |
| Reshare caveat | Several video metrics **return 0 for reshared videos** (documented per metric); `post_video_avg_time_watched` only for videos created after 2019-09-01. |
| Deprecation, June 2026 (all versions) | Page/post **reach + impression** family deprecated: `page_impressions_unique|paid_unique|viral_unique|nonviral_unique`, `page_posts_impressions*`, `post_impressions_unique|paid_unique|fan_unique|organic_unique|viral_unique|nonviral_unique`, video `total_video_impressions*`, story impressions. Replacement: **`page_media_view` / `page_total_media_view_unique` / `post_media_view` / `post_total_media_view_unique`**. |
| Deprecation, 3-second viewers (June 2026) | `page_video_views_unique`, `post_video_views_unique|organic_unique|paid_unique`, `total_video_views_*_unique` → replacement `*_total_media_view_unique` + `*_media_view`. |
| Deprecation, earlier | `page_video_views_10s*` above v18; `post_impressions` and the impression family above v25; `page_impressions_unique` above v25. |
| Version lifecycle | v25 released 2026-02-18 · v26 2026-07-29 · v27 2026-11-04 (documented as latest). v19 **retired 2026-05-21**, v20 **retired 2026-09-24**. Repo pins v23.0 by default. |
| Rate limits | Platform limits per app+user, and **BUC** caps for Page-token calls, surfaced by the `x-business-use-case-usage` header; error `80001` = "too many calls to this Page account"; the insights reference advises **reducing the number of metrics per request** when a call times out. |
| Unavailable on Facebook | No per-post "saves"; no public replication of the Creator Studio UI numbers; Page insights cannot be read for personal profiles (repo already models this as `personal_unsupported`). |

### 4.2 Instagram (Instagram Platform)

| Fact | What the documentation says |
| --- | --- |
| Two login paths, both supported by analytics | Instagram API with **Instagram Login** (`graph.instagram.com`, `instagram_business_basic` + `instagram_business_manage_insights`) and with **Facebook Login** (`graph.facebook.com`, `instagram_basic` + `instagram_manage_insights` + `pages_read_engagement`). Unified Login additionally uses `instagram_basic` + `instagram_manage_insights`. |
| Access level | Advanced Access (App Review + Business Verification) **only** if the app serves professional accounts it does not own/manage; Standard Access for own accounts added to the app. |
| Endpoints | `GET /{ig-user-id}/insights` (account) and `GET /{ig-media-id}/insights` (media). `metric_type=total_value` for totals, `time_series` where documented; `period` `day|week|days_28|month|lifetime|total_over_range`; `since`/`until`; `timeframe` for demographics; `breakdown` `media_product_type`, `follow_type`, `contact_button_type`, `age|city|country|gender`. |
| Freshness | Data used to calculate metrics **may be delayed up to 48 hours**. |
| Retention | Media metric data **stored up to 2 years**; **user metric data stored up to 90 days**; `online_followers` only last 30 days; story insights **24 hours** (webhook `story_insights` only on Facebook Login). |
| Thresholds | Account metrics `follower_count`, `online_followers`, `follows_and_unfollows`, `follower_demographics` **not available under 100 followers**; story metrics with fewer than 5 viewers return error `10` "Not enough viewers"; demographics return top 45 only. |
| Missing ≠ zero | "If insights data you are requesting does not exist or is currently unavailable the API will return an **empty data set instead of 0**". This is the single most important sentence for §3.4. |
| Album/story caveat | No insights data for media inside an album; story metrics only for 24 h. |
| Ads crossover | `total_likes`, `total_comments`, `total_views` include promoted/boosted and (optionally) Facebook crossposted counts and exist **only for Facebook Login**; `likes`/`comments`/`views` are organic-only. `facebook_views`, `crossposted_views`, `reels_skip_rate`, `reposts` are newer additions. |
| Deprecations | `impressions` deprecated for v22.0+ and all versions 2025-04-21 (media created on/after 2024-07-02 returns an error); `plays`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count` deprecated; `video_views` removed. Replacement: **`views`**. |
| Reels-only metrics | `ig_reels_avg_watch_time`, `ig_reels_video_view_total_time` (in development), `reels_skip_rate` (estimated), `crossposted_views`, `facebook_views`. |
| Unavailable on Instagram | No watch-time for Feed video (Reels only); no reach/impressions for carousels per slide; no saves for stories; no per-story retention curve; no follower demographics under 100 followers; no historical series beyond the retention windows. |

### 4.3 TikTok (Display API v2)

Sources: `developers.tiktok.com/doc/display-api-overview`, `/doc/tiktok-api-v2-video-object`,
`/doc/tiktok-api-v2-video-list`, `/doc/tiktok-api-v2-get-user-info`, `/doc/display-api-get-started`,
`/doc/tiktok-api-v2-rate-limit`, `/doc/getting-started-create-an-app`.

| Fact | What the documentation says |
| --- | --- |
| Product | **Display API**: `/v2/user/info/`, `/v2/video/list/`, `/v2/video/query/`. |
| Scopes | `user.info.basic`, `video.list` (plus optional `user.info.profile`, `user.info.stats`). The repo already requests `user.info.basic`, `video.list`, `video.upload`. |
| Eligibility | Requires a TikTok developer account, **approval for both Login Kit and the TikTok API product**, the granted scopes, and app review (production app is `Draft → In review → Live / Not approved`); demo video + URL ownership verification required. |
| Video object fields | `id`, `create_time` (epoch s), `cover_image_url` (TTL 6 h), `share_url`, `video_description` (≤150), `duration` (s), `height`, `width`, `title`, `embed_html`, `embed_link`, **`like_count`, `comment_count`, `share_count`, `view_count`**, `is_aigc`. |
| User info fields | `open_id`, `union_id`, `avatar_url`, `display_name`, `username`, `profile_deep_link`, `is_verified`, **`follower_count`, `following_count`, `likes_count`, `video_count`**, `bio_description`. |
| List semantics | `POST /v2/video/list/` returns **public** videos of the token's user, newest first, paginated (`cursor` = UTC ms timestamp, `max_count` ≤ 20); the cursor can be set to a timestamp to page backwards. |
| Rate limits | 600 requests/minute per endpoint, sliding window; exceeding → HTTP 429 `rate_limit_exceeded`. |
| Retention | No retention window is documented for these counters; there is **no time series** — each call is a current snapshot. |
| Unavailable on TikTok | No watch time, no average watch time, no retention curve, no completion rate, no replays, no impressions, no reach, no unique viewers, no saves, no follower growth series, no story metrics, no per-video traffic sources. |
| Research API | **Not a substitute.** It is a separate product with its own eligibility and intended research use; the brief forbids treating it as creator analytics and this plan does not use it. |

### 4.4 Version and deprecation policy for Phase 8

1. **The API version is an observation attribute, not a constant.** Every
   `analytics_snapshot` row stores the version used to obtain it.
2. Default stays **v23.0** until P8.4's live probe is run. The registry marks
   `page_impressions_unique`, `post_impressions`, `post_impressions_unique`,
   `page_video_views_unique`, `post_video_views_unique` as **`NOT_SUPPORTED` from v25** and
   `page_media_view`/`post_media_view`/`*_total_media_view_unique` as the **preferred** reach metrics.
3. Because Meta retires versions on a schedule (v19 gone, v20 gone), a **version watch** test
   (P8.24) asserts the pinned version is still in the published deprecation calendar and fails loudly
   (test name `test_graph_version_still_supported`) with a comment pointing at the changelog URL.
4. No metric is ever silently renamed. A provider rename is a **new** `analytics_metric_definition`
   row with `supersedes_key`, so old observations keep their meaning.

### 4.5 Version pinning, monitoring and compatibility (approval requirement 2)

The version is pinned in three places, and none of them is a code constant that someone can bump by
accident:

1. **Per app record:** `provider_app_config.api_version` (already exists). `integrations/social/config.py`
   keeps `DEFAULT_GRAPH_VERSION = "v23.0"` only as the first-boot default; a resolved value always
   comes from the app record. No `v25`/`v27` string may appear in an adapter.
2. **Per observation:** `analytics_snapshot.api_version` records what actually answered. A mixed
   dataset is visible, not averaged away.
3. **Per registry row:** `api_version_min` / `api_version_max` encode the documented availability
   window of the metric, so a version change is a *data* change with a migration note, not a silent
   behaviour change.

Monitoring (`services/analytics/version_monitor.py`, surfaced by `GET /analytics/versions` and
`/diagnostics`):

* reports configured version, latest documented version, versions still inside the published
  deprecation calendar, and any metric whose `api_version_max` is below the configured version;
* fails closed with `GRAPH_VERSION_UNSUPPORTED` rather than sending a request when the configured
  version is past its retirement date (the request is never attempted);
* never upgrades anything. There is **no** code path that writes a new `api_version`; it is a human
  edit in App settings, followed by a compatibility run.

Explicit compatibility tests (`test_analytics_version_compat.py`):

| Test | Proves |
| --- | --- |
| `test_no_adapter_hardcodes_a_graph_version` | every adapter asks the credentials for the version |
| `test_pinned_version_is_the_app_record_version` | a request built under `v23.0` says `v23.0` |
| `test_metric_above_api_version_max_is_not_requested` | a deprecated metric is never requested, and reads `not_supported` |
| `test_version_change_requires_explicit_setting` | changing the app record is the only way the version changes; the analytics service refuses an unknown version string |
| `test_retired_version_is_refused_before_http` | no HTTP call is made for a retired version |
| `test_recorded_api_version_is_returned_with_every_metric` | the API never hides which version produced a value |

---

## 5. Provider-specific metric catalogue

Columns: **key** (our stable id) · **provider name** (exact string sent to the API) · **period** ·
**unit** · **source** · **notes/availability**. `D` = deprecated (still collected only if the pinned
version still serves it, otherwise `NOT_SUPPORTED`) · `E` = estimate per provider docs.

### 5.1 Facebook — Page (account level)

Source: `GET /{page-id}/insights` unless stated. Permissions `read_insights` +
`pages_read_engagement`, `ANALYZE`, ≥ 100 Page likes, 2-year retention.

| key | provider name | period | unit | notes |
| --- | --- | --- | --- | --- |
| `fb.page.followers_total` | `followers_count` (field on `GET /{page-id}`) | instant | count | current total; distinct from likes |
| `fb.page.likes_total` | `fan_count` (field) | instant | count | Page likes ≠ followers; keep both |
| `fb.page.media_views` | `page_media_view` | day/week/days_28 | count | **preferred reach/display metric**; breakdowns `is_from_ads`, `is_from_followers` |
| `fb.page.media_viewers_unique` | `page_total_media_view_unique` | day/week/days_28 | count | E (unique) |
| `fb.page.impressions` | `page_impressions` | day/week/days_28 | count | D (June 2026) — collected only while available, never used in recommendations |
| `fb.page.impressions_unique` | `page_impressions_unique` | day/week/days_28 | count | D from v25 |
| `fb.page.engagements` | `page_post_engagements` | day/week/days_28 | count | reactions+comments+shares and more |
| `fb.page.reactions_like` | `page_actions_post_reactions_like_total` | day/week/days_28 | count | also `_love`, `_wow`, `_haha`, `_sorry`, `_anger` |
| `fb.page.reactions_total` | `page_actions_post_reactions_total` | day | count | by type |
| `fb.page.follows` | `page_follows` | day | count | total followers |
| `fb.page.follows_new` | `page_daily_follows_unique` | day/week/days_28 | count | E |
| `fb.page.unfollows` | `page_daily_unfollows_unique` | day/week/days_28 | count | E |
| `fb.page.likes_new` | `page_fan_adds_unique` | day/week/days_28 | count | E |
| `fb.page.likes_removed` | `page_fan_removes_unique` | day/week/days_28 | count | E |
| `fb.page.video_views` | `page_video_views` | day/week/days_28 | count | ≥3 s view |
| `fb.page.video_views_organic` / `_paid` | `page_video_views_organic` / `_paid` | day/week/days_28 | count | |
| `fb.page.video_view_time` | `page_video_view_time` | day | **ms** | aggregate watch time |
| `fb.page.video_complete_30s` | `page_video_complete_views_30s` | day/week/days_28 | count | |
| `fb.page.video_repeat_views` | `page_video_repeat_views` | day/week/days_28 | count | replays |
| `fb.page.video_views_unique` | `page_video_views_unique` | day/week/days_28 | count | D from v25, June 2026 |
| `fb.page.profile_views` | `page_views_total` | day/week/days_28 | count | |

### 5.2 Facebook — Page post (content level)

Sources: `GET /{post-id}/insights` (period `lifetime` unless stated) and the Post node itself for
engagement counters (`GET /{post-id}?fields=shares,reactions.summary(true).limit(0),comments.summary(true).limit(0)`).

| key | provider name | source | unit | notes |
| --- | --- | --- | --- | --- |
| `fb.post.media_views` | `post_media_view` | insights | count | **preferred** reach/display |
| `fb.post.media_viewers_unique` | `post_total_media_view_unique` | insights | count | E |
| `fb.post.impressions` | `post_impressions` | insights | count | D from v25 |
| `fb.post.impressions_unique` | `post_impressions_unique` | insights | count | D from v25 |
| `fb.post.reactions_like` | `post_reactions_like_total` | insights | count | plus `_love/_wow/_haha/_sorry/_anger`, `post_reactions_by_type_total` |
| `fb.post.clicks` | `post_clicks` | insights | count | |
| `fb.post.clicks_by_type` | `post_clicks_by_type` | insights | json | breakdown object |
| `fb.post.activity_by_action_type` | `post_activity_by_action_type` | insights | json | story types |
| `fb.post.shares` | `shares.count` | Post node | count | official Post field |
| `fb.post.reactions_total` | `reactions.summary(true).limit(0).total_count` | Post node | count | |
| `fb.post.comments` | `comments.summary(true).limit(0).total_count` | Post node | count | |
| `fb.post.video_views` | `post_video_views` | insights | count | |
| `fb.post.video_views_organic` / `_paid` | same | insights | count | |
| `fb.post.video_views_15s` / `_60s` | `post_video_views_15s`, `post_video_views_60s_excludes_shorter` | insights | count | |
| `fb.post.video_view_time` | `post_video_view_time` | insights | ms | total watch time |
| `fb.post.video_avg_watch_time` | `post_video_avg_time_watched` | insights | ms | **the only average-watch-time source on FB**; 0 for reshared videos |
| `fb.post.video_length` | `post_video_length` | insights | ms | duration as the provider sees it |
| `fb.post.video_complete_views` | `post_video_complete_views_organic/_paid` | insights | count | 97 % completion |
| `fb.post.video_retention_curve` | `post_video_retention_graph` | insights | json[40] | percentages per 1/40 segment |
| `fb.post.video_social_actions` | `post_video_social_actions_count_unique` | insights | count | E |
| `fb.post.video_followers` | `post_video_followers` | insights | count | follows attributed |

### 5.3 Facebook — Video / Reels object (`video_insights`)

| key | provider name | unit | notes |
| --- | --- | --- | --- |
| `fb.video.views` | `total_video_views` | count | ≥3 s, replays excluded |
| `fb.video.views_unique` | `total_video_views_unique` | count | D from v25 / June 2026 |
| `fb.video.views_autoplayed` / `_clicked_to_play` | `total_video_views_autoplayed`, `total_video_views_clicked_to_play` | count | |
| `fb.video.views_sound_on` | `total_video_views_sound_on` | count | |
| `fb.video.complete_views` | `total_video_complete_views` | count | 97 % |
| `fb.video.views_10s` / `_15s` / `_60s` | `total_video_10s_views`, `total_video_15s_views`, `total_video_60s_excludes_shorter_views` | count | |
| `fb.video.view_total_time` | `total_video_view_total_time` | ms | **total watch time** |
| `fb.video.avg_time_watched` | `total_video_avg_time_watched` | ms | **average watch time** |
| `fb.video.retention_curve` | `total_video_retention_graph` (+`_autoplayed`, `_clicked_to_play`) | json[40] | |
| `fb.video.reactions_by_type` | `total_video_reactions_by_type_total` | json | |
| `fb.reel.plays` | `blue_reels_play_count` | count | Reels surface |
| `fb.reel.total_plays` | `fb_reels_total_plays` | count | includes replays |
| `fb.reel.replays` | `fb_reels_replay_count` | count | |
| `fb.reel.avg_watch_time` | `post_video_avg_time_watched` | ms | Reels-form of the same provider name — stored as a **separate metric key** because the calculation differs (replays included) |
| `fb.reel.view_time` | `post_video_view_time` | ms | |
| `fb.reel.retention_curve` | `post_video_retention_graph` | json[40] | |
| `fb.reel.followers` | `post_video_followers` | count | |
| `fb.reel.likes` | `post_video_likes_by_reaction_type` | json | |
| `fb.reel.social_actions` | `post_video_social_actions` | count | comments + shares |
| `fb.reel.viewers_unique` | `post_impressions_unique` | count | E, D (June 2026) |

Ad-break/earnings metrics (`total_video_ad_break_*`, `creator_monetization_qualified_views`,
`content_monetization_earnings`) are documented but **out of scope**: the brief asks for content
performance, and monetization data implies a payment context the app does not model. Registry rows
exist with `availability_default = not_collected` so the omission is visible, not implicit.

### 5.4 Instagram — professional account

| key | provider name | period | unit | notes |
| --- | --- | --- | --- | --- |
| `ig.account.followers` | `followers_count` (IG User field) | instant | count | both paths; `follows_count`, `media_count` likewise |
| `ig.account.views` | `views` | day (total_value) | count | **replacement for impressions**; breakdowns `media_product_type`, `follower_type` |
| `ig.account.reach` | `reach` | day | count | E; breakdowns as above |
| `ig.account.accounts_engaged` | `accounts_engaged` | day | count | E |
| `ig.account.total_interactions` | `total_interactions` | day | count | includes boosted |
| `ig.account.likes` | `likes` | day | count | organic |
| `ig.account.comments` | `comments` | day | count | organic |
| `ig.account.saves` | `saves` | day | count | |
| `ig.account.shares` | `shares` | day | count | |
| `ig.account.replies` | `replies` | day | count | story replies |
| `ig.account.reposts` | `reposts` | day | count | |
| `ig.account.follows_unfollows` | `follows_and_unfollows` | day | json | `follow_type` breakdown; **≥100 followers** |
| `ig.account.profile_link_taps` | `profile_links_taps` | day | json | `contact_button_type` |
| `ig.account.impressions` | `impressions` | day | count | D — registry keeps it, collection disabled |
| `ig.account.follower_demographics` | `follower_demographics` | lifetime | json | available, **policy: redacted_by_policy** (privacy minimisation, §15) |
| `ig.account.engaged_demographics` | `engaged_audience_demographics` | lifetime | json | same policy |

### 5.5 Instagram — media

| key | provider name | product types | unit | notes |
| --- | --- | --- | --- | --- |
| `ig.media.views` | `views` | FEED, REELS, STORY | count | |
| `ig.media.reach` | `reach` | FEED, REELS, STORY | count | E |
| `ig.media.likes` | `likes` | FEED, REELS | count | |
| `ig.media.comments` | `comments` | FEED, REELS | count | |
| `ig.media.saved` | `saved` | FEED, REELS | count | |
| `ig.media.shares` | `shares` | FEED, REELS, STORY | count | |
| `ig.media.reposts` | `reposts` | FEED, REELS, STORY | count | |
| `ig.media.total_interactions` | `total_interactions` | FEED, REELS, STORY | count | |
| `ig.media.avg_watch_time` | `ig_reels_avg_watch_time` | **REELS only** | ms | |
| `ig.media.view_total_time` | `ig_reels_video_view_total_time` | **REELS only** | ms | in development |
| `ig.media.skip_rate` | `reels_skip_rate` | **REELS only** | ratio | E |
| `ig.media.reels_replays` | *(none)* | — | — | **not exposed** → `not_supported` |
| `ig.media.profile_visits` | `profile_visits` | FEED, STORY | count | |
| `ig.media.follows` | `follows` | FEED, STORY | count | |
| `ig.media.facebook_views` | `facebook_views` | FEED, REELS, STORY | count | throws if not crossposted |
| `ig.media.crossposted_views` | `crossposted_views` | REELS | count | |
| `ig.media.total_likes` / `total_comments` / `total_views` | same | FEED, REELS | count | **Facebook Login only** |
| `ig.media.impressions` | `impressions` | FEED, STORY | count | D (media created on/after 2024-07-02 errors) |
| `ig.media.link_clicks` / `navigation` | same | STORY | count/json | stories only, 24 h |

### 5.6 TikTok (Display API v2)

**Scope split (approval requirement 1).** Account-level statistics are only readable with the
dedicated **`user.info.stats`** scope (`follower_count`, `following_count`, `likes_count`,
`video_count`). `user.info.basic` covers identity (`open_id`, `union_id`, `avatar_url`,
`display_name`). `video.list` covers the per-video counters and nothing else. The three are kept
separate in the catalogue (`required_permissions_json` per row), in the collector's plan, and in
the capability verdicts: an account can have `video.list` and *not* `user.info.stats`, and the GUI
must say so rather than showing an empty followers tile. `user.info.stats` is currently an
`extra_scope` in `config.py` and is therefore **not** granted by the existing connect flow — every
account reads `needs_user_consent` for those four metrics until it re-consents (P8.8 + P8.20).

The repo's TikTok adapter currently requests `follower_count` inside `USER_FIELDS` under whatever
scopes the token has; that is a latent lie when `user.info.stats` was not granted, and P8.8 splits
the field lists per scope.

| key | provider name | scope | source | unit | notes |
| --- | --- | --- | --- | --- | --- |
| `tt.account.followers` | `follower_count` | `user.info.stats` | `/v2/user/info/` | count | current only |
| `tt.account.following` | `following_count` | `user.info.stats` | `/v2/user/info/` | count | |
| `tt.account.likes_total` | `likes_count` | `user.info.stats` | `/v2/user/info/` | count | |
| `tt.account.video_count` | `video_count` | `user.info.stats` | `/v2/user/info/` | count | |
| `tt.account.display_name` | `display_name` | `user.info.basic` | `/v2/user/info/` | text | identity, not performance |
| `tt.video.views` | `view_count` | `video.list` | `/v2/video/list/`, `/v2/video/query/` | count | cumulative |
| `tt.video.likes` | `like_count` | `video.list` | same | count | cumulative |
| `tt.video.comments` | `comment_count` | `video.list` | same | count | cumulative |
| `tt.video.shares` | `share_count` | `video.list` | same | count | cumulative |
| `tt.video.duration` | `duration` | `video.list` | same | s | object metadata, not performance |
| `tt.video.created_at` | `create_time` | `video.list` | same | epoch s | attribution |

Everything else the brief names for video performance — watch time, average watch time, retention,
completion rate, replays, audience-retention curve, impressions, reach, unique viewers, saves,
follower growth series — is **`not_supported`** on the TikTok Display API and will be rendered as
"TikTok does not expose this" in the GUI, never as `0` and never estimated from views.

### 5.7 Derived (never stored as provider metrics)

Computed in `analytics_aggregation` with `method` + `input_metric_keys` recorded:

`engagement_rate = (likes + comments + shares + saves) / views` (only when both operands are
`OBSERVED` and `views > 0`) · `completion_rate = complete_views / views` · `follows_per_1k_views` ·
`net_follow_change = follows_new − unfollows` (Instagram `follows_and_unfollows` is provider data;
the FB/TikTok equivalent is **our own delta between snapshots**, labelled `derived_from_snapshots`,
never presented as a provider metric) · `performance_at_24h / 48h / 7d / 28d` (from collection
windows) · `median/mean per group` with sample size and interval.

Rule: a derived value inherits the **weakest** availability of its inputs. If `views` is
`NOT_SUPPORTED`, engagement rate is `NOT_SUPPORTED`, not "unavailable-ish".

---

## 6. Permissions and approval matrix

| Capability | Platform | Scopes / permission | Extra requirement | Repo state today | Phase 8 state |
| --- | --- | --- | --- | --- | --- |
| Page insights | Facebook | `read_insights` + `pages_read_engagement`; Page token with `ANALYZE` | App Review for Advanced Access; Business Verification; Page ≥ 100 likes | not requested by the connection flow; capability `facebook.page.read_insights` = `needs_user_consent` | `needs_user_consent` until P8.20 adds the scope to the Facebook route + a re-consent prompt |
| Video/Reels insights | Facebook | `read_insights` + `pages_manage_engagement` | same | as above | same |
| Page follower/like totals | Facebook | `pages_read_engagement` | none beyond Standard Access for own Pages | **already available** (repo reads `followers_count`, `fan_count`) | works on day one |
| IG account insights (Instagram Login) | Instagram | `instagram_business_basic` + `instagram_business_manage_insights` | Standard Access for own accounts; Advanced Access otherwise | `instagram_business_manage_insights` is in `extra_scopes` but **not requested in the connect flow** → `needs_user_consent` | `needs_user_consent` until re-consent |
| IG media insights (Instagram Login) | Instagram | same two scopes | same | same | same |
| IG insights (Facebook Login) | Instagram | `instagram_basic` + `instagram_manage_insights` + `pages_read_engagement` | as above | `instagram_manage_insights` not requested | `needs_user_consent` |
| IG total_* metrics | Instagram | as FB-Login path | Facebook Login only | — | optional, capability-gated |
| TikTok profile + video counters | TikTok | `user.info.basic`, `video.list` | TikTok app review; **Login Kit + TikTok API product approved**; demo video; URL ownership | scopes are already requested and granted on a normal Login Kit connection | `available` once an approved app exists; otherwise `requires_developer_approval` |
| TikTok analytics beyond counters | TikTok | — | — | — | **`not_supported`** (no such API) |
| TikTok Research API | TikTok | separate product, separate eligibility | — | — | **explicitly excluded** |

Matrix used by the UI (`GET /analytics/capabilities`), computed from
`account_capability` + `analytics_metric_definition` + the granted scopes stored on
`social_account.scopes_json` — never from a hardcoded "should work" assumption.

---

## 7. Proposed database changes

One migration: **`20261125_0900_b8c9d0e1f2a3_analytics_engine.py`**, after `a7b8c9d0e1f2`.
Net effect: **42 → 48 tables** (6 new, 1 rebuilt, 0 dropped).

### 7.1 `analytics_metric_definition` (new)

| Column | Type | Why |
| --- | --- | --- |
| `metric_key` | str(80) **PK** | our stable identity, e.g. `fb.reel.avg_watch_time` |
| `platform` | enum `Platform` | facebook · instagram · tiktok |
| `object_type` | enum `AnalyticsObjectType` | account · post · video · reel · story |
| `provider_metric_name` | str(120) | **exact** provider string (`ig_reels_avg_watch_time`) |
| `provider_object` | str(40) | `page`, `post`, `video`, `ig_user`, `ig_media`, `tiktok_video` |
| `source_endpoint` | str(300) | `/v23.0/{ig-media-id}/insights` shape |
| `source_doc_url` | str(500) | the page the row was read from |
| `unit` | str(24) | `count`, `ms`, `ratio`, `json`, `seconds` |
| `value_kind` | str(16) | `scalar` · `series` · `breakdown` · `object` (approval requirement 4) |
| `metric_class` | str(16) | `cumulative` · `period` · `point_in_time` · `ratio` · `series` (approval requirement 5) |
| `period_kinds_json` | JSON | allowed `MetricPeriod` values |
| `definition_text` | Text | the provider's documented meaning, condensed for display; `source_doc_url` is the authority |
| `is_estimate` | bool | provider marks it estimated |
| `is_cumulative` | bool | kept for legacy reads; `metric_class` is authoritative |
| `is_deprecated` | bool + `deprecated_note` | deprecation with date/API version |
| `supersedes_key` / `superseded_by_key` | str(80) FK self | rename/version trail |
| `api_version_min` / `api_version_max` | str(16) | e.g. `post_impressions` → max `v21`, `page_impressions_unique` → max `v24` |
| `required_permissions_json` | JSON | scopes needed: a flat list means **all of them**; `{"any_of": [[...],[...]]}` means **one login route** (Instagram Login vs Facebook Login) |
| `account_threshold_json` | JSON | e.g. `{"min_page_likes": 100}`, `{"min_followers": 100}` |
| `availability_default` | enum `MetricAvailability` | e.g. `not_supported`, `redacted_by_policy` |
| `preferred_for` | str(40) nullable | `reach`, `views`, `watch_time`, … so the UI picks the modern metric |
| `catalog_version` | str(32) | seed version, like `agent_definition.catalog_version` |
| `docs_checked_at` | UtcDateTime | when the row was last verified |

Seeded from `analytics/metric_catalog.py` (a pure data module, mirroring
`integrations/social/config.py`), idempotent like `ensure_tools_seeded`. Index:
`(platform, object_type, availability_default)`, `(preferred_for)`.

### 7.2 `analytics_snapshot` (rebuilt)

| Column | Type | Why |
| --- | --- | --- |
| `id` | str(26) ULID PK | |
| `account_id` | str(26) FK `social_account.id` **ON DELETE CASCADE**, indexed | who the measurement belongs to; revoking an account removes its rows with it (§15) |
| `platform` | enum `Platform` | redundant but indexed for provider-scoped queries |
| `object_type` | enum `AnalyticsObjectType` | |
| `provider_object_id` | str(190) | Page id, post id, IG media id, TikTok item id |
| `publication_id` | str(26) FK `publish_job.id` **ON DELETE SET NULL**, indexed | the publication this observation describes, when it is ours |
| `content_project_id` / `campaign_id` / `asset_id` | str(26) nullable, indexed | **attribution captured at collection**, deliberately not FKs (§7.3) |
| `metric_key` | str(80) FK `analytics_metric_definition.metric_key` | |
| `value_kind` | str(16) | **copied** from the definition at write time: the CHECK below cannot read another table, and a metric whose kind changed must not silently reinterpret old rows |
| `period_kind` | enum `MetricPeriod` | `lifetime`, `day`, … |
| `period_start` / `period_end` | UtcDateTime nullable | the provider's own observation window |
| `value_num` | Float **nullable** | NULL unless `availability = observed` |
| `value_json` | JSON nullable | curves, breakdowns, multi-value answers |
| `unit` | str(24) | copied from the definition at write time |
| `availability` | enum `MetricAvailability` | the whole point (§3.4) |
| `availability_reason` | str(300) | provider wording, redacted |
| `breakdown_key` | str(120) default `""` | e.g. `media_product_type=REELS` |
| `api_version` | str(16) | version used |
| `api_host` | str(60) | `graph.facebook.com` / `graph.instagram.com` / `open.tiktokapis.com` |
| `source_ref` | str(300) | endpoint **path** only, never the full URL with token |
| `run_id` | str(26) FK `analytics_collection_run.id` **ON DELETE CASCADE** | |
| `collected_at` | UtcDateTime | when we asked |
| `data_as_of` | UtcDateTime nullable | provider-reported freshness when it says so |
| `completeness` | enum `MetricCompleteness` | existing enum reused |
| `sample_size` | int nullable | where the provider documents one |
| `created_at` / `updated_at` | UtcDateTime | `TimestampMixin` |

Constraints and indexes:

* `UNIQUE (run_id, provider_object_id, metric_key, period_kind, period_start, period_end, breakdown_key)`
  — one row per observation per run: retries are idempotent, history is append-only.
* `INDEX (account_id, object_type, metric_key, period_end)` — the time-series read path.
* `INDEX (publication_id, metric_key, collected_at)` — the per-publication page.
* `INDEX (provider_object_id, metric_key)` — provider identity lookups.
* `CHECK (availability = 'observed' OR value_num IS NULL)` — **the anti-zero rule, enforced by the
  database**, not only by code.
* `CHECK (availability <> 'observed' OR (value_num IS NOT NULL OR value_json IS NOT NULL))` — an
  observation must carry a value of the right shape.
* `CHECK ((value_kind = 'scalar' AND value_json IS NULL) OR (value_kind <> 'scalar' AND value_num IS
  NULL))` — **original type preserved** (approval requirement 4): a scalar cannot smuggle a curve and
  a curve cannot be flattened into a float.

"No duplicate collection" is enforced twice: the queue's `idempotency_key` per target/window, and
this unique key per run.

### 7.3 Why attribution columns are not foreign keys

`publish_job` is `ON DELETE CASCADE` from `platform_variant`, which cascades from
`content_project`. If analytics rows referenced project/campaign by FK, deleting a draft project
would delete its history of what the published video did. The brief requires that historical
measurements survive. So:

* `publication_id` **is** an FK (`SET NULL`) — integrity where it matters, graceful degradation.
* `account_id` **is** an FK (`CASCADE`) — an account whose credential was revoked and deleted must
  not leave orphan analytics (§15).
* `content_project_id`, `campaign_id`, `asset_id` are **plain strings captured at collection**, with
  a comment in the model and a note in `ANALYTICS_ARCHITECTURE.md` explaining that they are an
  immutable attribution snapshot. `PRAGMA foreign_key_check` stays clean because these are not
  declared relationships.

### 7.4 `analytics_collection_run` (new)

`id`, `account_id` (FK CASCADE), `platform`, `target_kind` (`account_sync` · `discovery` ·
`metrics` · `backfill` · `manual`), `target_object_id` nullable, `job_id` (queue job id, nullable),
`started_at`, `finished_at`, `outcome` (`AnalyticsRunOutcome`), `error_class`
(`AnalyticsErrorClass`), `error_code`, `error_detail_redacted`, `http_calls`, `metrics_attempted`,
`metrics_observed`, `metrics_unavailable`, `metrics_written`, `rate_limit_remaining`,
`rate_limit_reset_at`, `api_version`, `dedupe_key` (**UNIQUE**), `request_fingerprint`,
`created_at`, `updated_at`.

Indexes: `(account_id, started_at)`, `(outcome, started_at)`, `(dedupe_key)`.

`dedupe_key = sha256(account|target_kind|target_object|window|utc_bucket)` — a second identical run
inside the same bucket is a no-op even if the queue is replaying.

### 7.5 `analytics_sync_state` (new)

`id`, `account_id` FK CASCADE, `platform`, `object_type`, `state`
(`not_started` · `discovering` · `backfilling` · `current` · `stalled` · `blocked`),
`discovery_cursor` str(190), `discovery_complete` bool, `backfill_until` UtcDateTime nullable,
`last_full_sync_at`, `last_incremental_sync_at`, `next_due_at` (indexed), `poll_interval_s`,
`consecutive_failures`, `last_error_class`, `last_error_code`, `last_success_at`,
`provider_retention_days`, `notes`. **UNIQUE (account_id, object_type)**.

`next_due_at` is the scheduler input; `poll_interval_s` starts from §9's table and backs off on
failures (×2 up to the provider's documented floor), never below the provider's cadence.

### 7.6 `analytics_aggregation` (new)

`id`, `account_id` FK CASCADE, `platform`, `publication_id` nullable, `content_project_id`,
`campaign_id`, `scope` (`publication` · `account` · `campaign` · `platform` · `content_group`),
`window_kind` (`initial` · `h24` · `h48` · `d7` · `d28` · `lifetime` · `custom`),
`window_hours`, `window_tolerance_minutes`, `metric_key` (derived key or provider key),
`value_num`, `value_json`, `unit`, `sample_size`, `computed_at`,
`method` (`direct` · `sum` · `rate` · `median` · `delta` · `ratio`), `input_snapshot_ids_json`,
`input_metric_keys_json`, `availability` (weakest input), `confidence_json`
(interval + method), `aggregation_version` int, `created_at`.

**UNIQUE (account_id, scope, coalesce(publication_id,''), coalesce(campaign_id,''), metric_key,
window_kind, aggregation_version)** — recompute is an upsert, not an append.

### 7.7 `analytics_recommendation` (new)

`id`, `account_id` FK CASCADE nullable (a recommendation may span accounts), `platform` nullable,
`campaign_id` / `content_project_id` nullable strings (attribution snapshot),
`kind` (`topic` · `format` · `hook` · `caption` · `hashtag` · `timing` · `campaign` · `growth` ·
`retention` · `experiment`), `observation` Text, `evidence_json` (metric keys, values, snapshot ids,
aggregation ids), `sample_size_json`, `period_start` / `period_end`, `confidence` float,
`confidence_method` str(40), `limitations_json`, `proposed_experiment_json`, `expected_benefit_json`
(mandatory `"kind": "estimate"`), `status` (`draft` · `reviewed` · `accepted` · `rejected` ·
`superseded`), `generated_by` (`deterministic` · agent id), `agent_task_id`, `approved_by` /
`decided_at`, `approval_request_id` (FK-free string, matching `publish_job`'s pattern inline with
`ApprovalService`), `created_at`, `updated_at`. Index `(status, kind, created_at)`,
`(account_id, kind)`.

### 7.8 `analytics_report` (new)

`id`, `kind` (`overview` · `platform_comparison` · `account` · `campaign` · `content_comparison` ·
`recommendations` · `experiment`), `period_start`, `period_end`, `filters_json`, `sections_json`
(section → aggregation/recommendation ids), `summary` Text, `path` str(1024), `sha256` str(64),
`bytes`, `generated_by` (agent id or `user`), `agent_task_id`, `status`
(`draft` · `final` · `archived`), `created_at`, `updated_at`. Index `(kind, period_end)`.

### 7.9 Retention and growth

| Data | Retention | Rationale |
| --- | --- | --- |
| `analytics_snapshot` | **keep indefinitely** (≈500 B/row) | the whole point of the phase; provider retention limits what can be re-fetched, not what we may keep |
| `analytics_aggregation` | keep, recomputable (versioned) | cheap, and needed to explain a past recommendation |
| `analytics_collection_run` | **180 days** then prune (job `analytics.prune_runs`) | operational noise, not measurement |
| `analytics_report` row | keep; file under `data/exports/analytics/` | audit trail |
| `analytics_snapshot` on account delete | **deleted with the account** (FK CASCADE) | documented in §15; the user's disconnect means "stop and remove" |

Growth estimate for a realistic single-operator site (3 accounts, 12 publications/month):

| Stream | Rows/year | Bytes/year (est.) |
| --- | --- | --- |
| account metrics, 3 accounts × ~20 metrics × 365 daily | ~22 000 | ~11 MB |
| publication metrics, 144 publications × ~14 metrics × 10 collection windows | ~20 000 | ~10 MB |
| retention curves (40-point JSON) | ~3 000 | ~3 MB |
| aggregations (publication + campaign + account scopes) | ~25 000 | ~12 MB |
| collection runs (pruned at 180 d) | ~5 000 steady | ~2 MB |
| recommendations + reports | ~600 | <1 MB |
| **total** | **~76 000** | **≈ 40 MB/year** |

An aggressive 50 publications/month site is ~4× that: **≈ 160 MB/year**. Acceptable on D: with
automatic backup (`BackupService`) and WAL.

### 7.10 Migration procedure (P8.3)

1. `BackupService.create(label="manual-p8")` → `data/backups/…`.
2. Migrate a **checkpointed copy** (`-wal`, `-shm` included) under an isolated `CREATOROS_ROOT`.
3. `PRAGMA foreign_key_check` must be `[]`; `PRAGMA integrity_check` must be `ok`.
4. Table count 42 → 48; `analytics_snapshot` has the new columns and both CHECK constraints; the
   `UNIQUE` key is the new one; `SELECT count(*) FROM analytics_snapshot` still 0.
5. `downgrade` drops the 6 new tables and rebuilds `analytics_snapshot` to its Phase 2 shape, then
   `upgrade` again on the copy.
6. Verify 111 agents, 18 tools, head `b8c9d0e1f2a3`.
7. Only then apply to the real database.

---

## 8. Analytics collection architecture

```
api/routes/analytics.py ──► services/analytics/query.py      (read models for the GUI)
                        └─► services/analytics/actions.py    (manual sync, recompute, report, decide)

queue lane "analytics" (job types)                    services/analytics/
  analytics.sync_account      ──► collector.py           resolve capability → plan calls
  analytics.discover_content  ──► discovery.py           list posts/media/videos, upsert identity
  analytics.collect_metrics   ──► collector.py           one batch of metrics per object
  analytics.aggregate         ──► aggregation.py         windows + rollups (pure SQL/Python)
  analytics.report            ──► reports.py             assemble + write + hash
  analytics.prune_runs        ──► retention.py

integrations/analytics/            (HTTP only, no SQLAlchemy, no DB writes)
  base.py            AnalyticsAdapter ABC + dataclasses + HttpResponse reuse
  facebook.py        Page / post / video_insights
  instagram.py       ig_user / ig_media, both login paths
  tiktok.py          /v2/user/info/, /v2/video/list/, /v2/video/query/
  catalog.py         the metric catalogue as data (seeded into analytics_metric_definition)
  errors.py          error classification → AnalyticsErrorClass (reuses social/errors.py style)

services/analytics/
  registry.py        metric registry read/write, capability-aware plan
  snapshots.py       the only writer of analytics_snapshot (availability rules live here)
  collector.py       orchestration of one account's collection
  sync_state.py      cursors, next_due_at, backoff
  aggregation.py     performance windows + rollups
  comparison.py      group comparisons with sample gates
  statistics.py      Wilson intervals, seeded bootstrap, "inconclusive" logic
  recommendations.py deterministic candidate detection → analytics_recommendation
  reports.py         report assembly + file write + hash
  query.py           read models (never mutates)
  budget.py          per-provider local rate budget + error-429 handling
```

**Boundaries enforced by tests, not convention:**

* `integrations/analytics/*` must not import `sqlalchemy`, `db.models` or `Session` (same rule
  Phase 7 enforced for publishers).
* `services/analytics/snapshots.py` is the **only** module allowed to INSERT
  `analytics_snapshot`; `query.py` is read-only and a test asserts it contains no `session.add`.
* `recommendations.py` may not import `services/publishing/executor.py`,
  `services/publishing/composer.py` or any publisher. Recommendations cannot publish.
* No provider call outside `integrations/analytics/*` (a scan test, mirroring Phase 7's publisher
  scan).

### 8.1 One collection, step by step

1. Handler claims `analytics.collect_metrics` for `(account_id, object_type, provider_object_id,
   window)`.
2. `registry.plan(account, object_type)` reads `account_capability` + granted scopes + the metric
   catalogue and returns `available | needs_consent | requires_approval | unsupported` **per
   metric group**, with reasons. Metrics that are not allowed are **not requested**; they are
   written once as `not_authorized` / `not_supported` (so the UI can explain the gap) unless a row
   for this run already exists.
3. `budget.check(provider, account)` refuses the call if the local per-hour budget is exhausted →
   `temporarily_unavailable` for that batch + `next_due_at += backoff`.
4. Adapter performs the real call with the token from `TokenService.access_token(account)`.
5. Responses are normalised into `NormalizedMetric` dataclasses: metric_key, period, value,
   value_json, unit, availability, availability_reason, source_ref, data_as_of.
6. `snapshots.write(run, metrics)` upserts by the unique key, enforces the CHECK constraints, and
   returns counts.
7. `analytics_collection_run` is closed with outcome, counts, HTTP calls, rate-limit headers.
8. `sync_state` advances cursor/watermark and `next_due_at`.

### 8.2 Error classification (drives retry, never a silent zero)

| Provider signal | Class | Availability written | Retry |
| --- | --- | --- | --- |
| HTTP 200, metric absent from `data` | `permanent` (for this window) | `missing` | no immediate retry; next scheduled window |
| IG/ FB returns empty `data` for a metric | as above | `missing` (documented: empty ≠ 0) | no |
| Error `190` / `102` / TikTok `access_token_invalid` | `authorization` | `not_authorized` | refresh token once, then `reauthorization_required` |
| Error `200` / `10` / `(#10) Not enough viewers` | `permission` / `threshold` | `not_authorized` / `below_threshold` | no retry |
| Error `4`, `17`, `32`, `613`, `80001`, TikTok `rate_limit_exceeded` (429) | `rate_limit` | `temporarily_unavailable` | yes, backoff from `retry_after` / `x-business-use-case-usage`, cap 6 h |
| HTTP 5xx, timeout, connection reset | `transient` | `temporarily_unavailable` | yes, `backoff_delay` from `services/queue.py` |
| Deprecated metric error (`invalid metric`, removed) | `validation` | `not_supported` + registry row updated | no; surfaced in diagnostics |
| Object deleted at the provider | `permanent` | `missing` with reason `object_not_found` | no; `sync_state` notes it |

A retry never overwrites an `observed` row with `temporarily_unavailable`: the new run writes its
own row (append-only), and the read model uses the latest **observed** value with its own
`collected_at` so the GUI can say "last good value from 09:12, current collection failing since
15:00".

### 8.3 Idempotency within a run, append-only between runs (approval requirement 3)

A **logical collection run** is identified by
`dedupe_key = sha256(account | target_kind | target_object | window_kind | utc_bucket)`, where
`utc_bucket` is the scheduled slot the run belongs to, not the wall-clock second it started. Four
layers enforce one answer per logical run:

1. **Queue** `idempotency_key` per `(job_type, account, object, window)` — an open job blocks a
   second one, and a lease ensures two workers cannot hold it.
2. **`analytics_collection_run.dedupe_key` UNIQUE** — a replayed job finds the existing run and
   resumes that `run_id`; it does not open a second run. Crash-and-retry therefore never duplicates
   a measurement and never creates a "new" observation.
3. **`analytics_snapshot` UNIQUE** on `(run_id, provider_object_id, metric_key, period_kind,
   period_start, period_end, breakdown_key)` — re-delivered metrics update the existing row (same
   run) and cannot be inserted twice.
4. **`budget.py` + `sync_state`** make a duplicate provider call unlikely before it is attempted.

Between **different** logical runs, rows are append-only and never rewritten: a new scheduled slot
produces a new run id and therefore new rows, which is exactly what makes the time series and the
"observed at T+24 h" windows meaningful. No code path mutates or deletes a snapshot row of a closed
run; a test asserts that the only `UPDATE` on `analytics_snapshot` is scoped to
`run_id = :current_run`.

---

## 9. Collection schedule

Cadence is derived from what the providers actually do, not from a desire for freshness.

| Provider | Object | Documented cadence | Our interval | Notes |
| --- | --- | --- | --- | --- |
| Facebook | Page (account metrics) | insights update ≈ every 24 h | **24 h ± 37 min jitter**, staggered per account | polling hourly would return the same numbers |
| Facebook | post / video / reel | lifetime counters grow; insights daily | **+1 h, +24 h, +48 h, +7 d, +28 d**, then monthly ×6 | matches the brief's performance windows |
| Facebook | content discovery | — | **every 12 h** (`/{page}/published_posts`, `/{page}/videos`) | catches cross-posted/re-shared content too |
| Instagram | account | data delayed up to 48 h; user metrics kept 90 days | **24 h ± 37 min** | `views`/`reach` day series fetched with `since` = last 7 days to catch revisions |
| Instagram | media | 48 h delay, 2-year retention | **+1 h, +24 h, +48 h, +7 d, +28 d**, then monthly ×6 | Reels curves only for `REELS` product type |
| Instagram | discovery | — | **every 6 h** (`/{ig-user}/media` page 1) | new publication is the trigger for the windows above |
| TikTok | profile counters | no series, current value | **every 24 h** | our own deltas are labelled derived |
| TikTok | video list | current counters | **+1 h, +24 h, +7 d, +28 d**, then monthly ×3 | `max_count=20`, cursor paging ≤ 2 pages per run |
| all | manual refresh | — | **≤ 1/hour/account**, hard-gated | user button, rate-limited in `budget.py` |

Budgets (deliberately far below the provider ceilings):

| Provider | Documented limit | Our local cap |
| --- | --- | --- |
| Meta | platform + BUC caps, `80001` per Page, `x-business-use-case-usage` header | **60 calls/hour/account**, ≤ 5 metrics per insights call, sequential calls only |
| Instagram | same platform model as Meta | **60 calls/hour/account**, ≤ 5 metrics per call, sequential |
| TikTok | 600 requests/min/endpoint | **6 calls/min/account**, 1 in flight |

Backoff: `min(6 h, base 60 s × 2^failures)`, plus `retry_after` when the provider supplies it.

Backfill on first connect: discover up to **100** recent publications per account (configurable,
never unbounded), then one lifetime collection per publication, oldest first, with
`sync_state.state = backfilling`. Never attempt data older than the provider's retention
(Facebook 2 years, Instagram media 2 years / user 90 days).

Timezone: all timestamps UTC via `UtcDateTime`; the display timezone only exists for grouping in the
GUI (`publish_job.display_timezone` is reused for "which day did this publish in the user's week").

---

## 10. Queue integration

* **Fourth lane `analytics`** on the existing `queue_job` table. Same `QueueService`, same
  `QueueWorker`, same leases/heartbeats/backoff/dead-letter. No second queue.
* `CREATOROS_ANALYTICS_WORKERS` (default **1**, clamped 0–2). `0` disables the lane entirely —
  that is also the rollback switch.
* The **existing** sweeper thread (`BackgroundWorker._scheduler_loop`, 15 s tick) calls
  `AnalyticsScheduler.tick()` right after `PublishScheduler.tick()`. One thread, two services; the
  analytics tick only enqueues jobs whose `analytics_sync_state.next_due_at <= now`, so its cost is
  one indexed query per tick.
* Job types and idempotency keys:

| job_type | idempotency key | lane | typical duration |
| --- | --- | --- | --- |
| `analytics.sync_account` | `analytics.sync:{account_id}:{bucket}` | analytics | < 1 s (planning) |
| `analytics.discover_content` | `analytics.discover:{account_id}:{object_type}:{bucket}` | analytics | 1–5 s |
| `analytics.collect_metrics` | `analytics.collect:{account_id}:{provider_object_id}:{window}` | analytics | 0.3–3 s |
| `analytics.aggregate` | `analytics.aggregate:{account_id}:{window_kind}:{bucket}` | analytics | 1–10 s (SQL) |
| `analytics.report` | `analytics.report:{report_type}:{period}:{bucket}` | analytics | 5–60 s (includes one or more model calls) |
| `analytics.prune_runs` | `analytics.prune:{date}` | analytics | < 1 s |

* **Isolation guarantees:** the analytics lane never writes `publish_job`, never touches
  `media_derivative`, never calls a publisher or FFmpeg; publishing/media workers never consume
  analytics jobs. Tests assert both directions (a job enqueued on `analytics` is not claimed by the
  `publish` worker and vice versa).
* **Priority:** collection `40`, aggregation `60`, report `70` — the queue claims `priority ASC`, so
  collection precedes aggregation precedes reporting. A queued publish (lane `publish`, default
  priority 50) is never delayed by an analytics job because each lane has its own thread.
* **Resource interaction:** the analytics lane is capped at one worker and one in-flight HTTP call,
  and report generation goes through the governor (one generation at a time). A long analytics
  report can therefore delay chat generation by its model call, which is the same trade-off Phase 3/4
  already accept for objectives.

---

## 11. Agent workflows

Agents already exist. Phase 8 adds **data access**, **evidence contracts**, and **three workflows**.

### 11.1 New tools (all in `services/toolbox.py`, all in `tool_registry`)

| Tool | Risk | What it returns | Who may call it |
| --- | --- | --- | --- |
| `analytics.metrics.read` | read | observed metrics for a publication/account/campaign in a window, **with availability per metric** and the aggregation ids | analytics_growth, strategic_intelligence, quality_governance (read-only verdicts) |
| `analytics.publications.read` | read | publications with remote ids, publish time, campaign/project/asset, format/caption/hashtags, and whether metrics exist | analytics_growth, platform_adaptation, publishing_operations, strategic_intelligence |
| `analytics.comparison.read` | compute | deterministic comparison result: groups, medians, intervals, sample sizes, and an explicit `inconclusive` flag | analytics_growth, strategic_intelligence |
| `analytics.recommendations.read` | read | stored recommendations with evidence and status | analytics_growth, strategic_intelligence, research_intelligence |
| `analytics.recommendation.propose` | write | writes a **draft** `analytics_recommendation`; cannot approve, schedule or publish | analytics_growth (specialists via leader) |

Grants: `analytics_growth` gets all five; `strategic_intelligence` gets the four reads;
`research_intelligence` gets `analytics.recommendations.read` + existing `report.generate`;
`publishing_operations` gets `analytics.publications.read` only. **No analytics role gets a
publishing tool** (asserted test).

### 11.2 Evidence contract (how "no invented statistics" is enforced)

* Every analytics tool result carries `evidence`: `{metric_key, value, unit, period, availability,
  snapshot_ids[], aggregation_ids[], collected_at}`.
* `AgentTask.evidence_refs_json` is filled by the orchestrator from the tool output (the column and
  the plumbing already exist).
* `services/structured.py` validation is extended with `assert_numbers_traceable(artifact, evidence)`:
  every numeric literal in `summary`/`findings`/`sections` must appear in the evidence payload (after
  rounding to the reported precision) or be a date/count of items. A mismatch downgrades the task to
  `partial` with `error_code="UNTRACEABLE_NUMBER"` and the artifact keeps a warning — the number is
  never silently kept.
* If a metric is absent, the tool returns it with `availability`, and the prompt (a new
  `analytics` section in `build_system_prompt`) says: *"A metric whose availability is not
  `observed` must be described as unavailable and never as zero."*
* Correlation language rule in the prompt and in a post-check: a sentence containing a causal verb
  (caused/led to/resulted in/thanks to) about a metric must be flagged; `analytics_growth`
  specialists get `"correlation_is_not_causation": true` in their stored prompt for this phase.

### 11.3 Workflows

**W1 — Weekly growth review (scheduled or user-triggered)**
```
chief_orchestrator (objective: "Weekly performance review, 2026-11-16..2026-11-22")
   └─ analytics_director (plan)
        ├─ performance_analysis_agent      (aggregations: totals, medians, best/worst)
        ├─ views_analysis_agent            (reach/views by platform, availability-aware)
        ├─ engagement_analysis_agent       (rates normalised by views, sample gates)
        ├─ retention_analysis_agent        (curves where available; says where not)
        ├─ audience_growth_agent           (follows/unfollows or derived deltas, labelled)
        ├─ hashtag_performance_agent       (groups with ≥ 5 publications only)
        ├─ campaign_analysis_agent         (campaign vs stated goal)
        └─ content_comparison_agent        (two clearly-defined groups, controlled dimensions)
   └─ growth_reporting_agent (report contract; measured vs interpreted separated)
   └─ strategy_director (direction + trade-offs, no publishing)
   └─ chief_orchestrator (compiled answer + analytics_report row)
```

**W2 — Single-publication diagnosis**
`performance_analysis_agent` → `retention_analysis_agent` (if a curve exists) →
`engagement_analysis_agent` → `content_comparison_agent` (vs the 5 most similar publications) →
`analytics_director` verdict.

**W3 — Experiment design**
`ab_testing_agent` (one variable, pre-declared success metric, sample requirement, stop rule) →
`analytics_director` review → `strategy_director` fit check. Output is a **draft recommendation**
with `kind="experiment"`; adopting it only writes a draft `content_project` through the existing
composer path when a human explicitly approves — never a schedule, never a publish.

`trend_research_agent` / `research_synthesis_agent` are used **only** when a recommendation needs
external context (seasonality, platform policy change); their output must carry sources or an
explicit `unsourced` marker, and it may never be merged into a measured claim.

### 11.4 Cost and model choice per step

| Step | Profile | Why |
| --- | --- | --- |
| topic/format/hashtag categorisation of publications | `router_light` (qwen3:0.6b) | deterministic-ish classification, many items, cheap |
| per-publication analysis, engagement/retention wording | `specialist` (qwen3:1.7b) | the default worker at 45 tok/s measured |
| campaign synthesis, weekly report, experiment design | `planner` (qwen3:4b) | only where reasoning quality matters, subject to the existing fallback chain |
| all numeric computation | **none** (Python/SQL) | numbers must not come from a model |

---

## 12. Desktop GUI plan — ANALYTICS CENTER

A sixth nav section, `Analytics`, 12 pages, same visual language as Publishing (`Card`, `Row`,
`Badge`, `Stat`, `EmptyState`).

| # | Page | Content |
| --- | --- | --- |
| 1 | Global overview | KPIs straight from `analytics_aggregation` (observed only), freshness per account, "what is missing and why" panel, top/bottom publications, 28-day trend |
| 2 | Platform comparison | same metric across platforms **only where the provider defines it comparably**; explicit "not comparable" chips (e.g. TikTok has no watch time) |
| 3 | Account performance | per account: followers, follows/unfollows, views/reach, engagement rate, collection health, provider thresholds |
| 4 | Individual post analytics | one publication: identity (remote id, permalink), every metric with availability, retention curve or an explicit "not exposed by this platform", collection history |
| 5 | Video performance | watch time, average watch time, completion, replays, retention curves; per platform availability matrix at the top |
| 6 | Audience growth | follows/unfollows (provider) vs derived deltas (ours, labelled), net growth, per-account comparison |
| 7 | Campaign analytics | campaign → publications → metrics, goal vs observed, sample size, confidence, per-platform split |
| 8 | Content comparison | pick 2–N publications or 2 groups, choose dimensions (format, length, hook, hashtag set, publish window), see medians + intervals + "inconclusive" |
| 9 | Hashtag analysis | hashtag groups with n ≥ 5, engagement rate intervals, caveats, suggested next test |
| 10 | AI recommendations | stored recommendations with observation/evidence/sample/period/confidence/limitations/experiment/estimate; Accept (writes approval request) or Reject |
| 11 | Historical reports | `analytics_report` catalogue, open/download, hash shown |
| 12 | Analytics settings | cadence per provider (bounded by §9), backfill depth, retention, demo-metric policy, consent state per account, "collect now" (rate-limited), export defaults |

GUI requirements implemented literally:

* **Real charts only.** `components/charts.tsx` (new): `LineChart`, `BarChart`, `RetentionCurve`,
  `Sparkline`, `Heatmap`, `ComparisonDot`. Hand-rolled SVG, no dependency, deterministic. Every chart
  takes `points: {x, y, availability}[]` and **renders gaps where availability ≠ observed** — a
  missing metric is a gap with a label, never a zero on the axis.
* Date-range filters (presets + custom, UTC + display timezone), account filter, platform filter,
  campaign filter, metric selector, comparison mode (publication↔publication, period↔period,
  group↔group).
* **Data freshness indicators**: per metric group `collected_at`, provider lag, `stale` after 1.5×
  the scheduled interval, and the reason.
* **Missing-metric explanations**: driven by `analytics_metric_definition.availability_default` +
  the most recent `availability`/`availability_reason`; the copy is generated from data, e.g.
  "TikTok does not expose watch time" / "Needs `read_insights` — grant it on the Permissions page".
* **Export**: CSV (metric long-format) and Markdown/PDF-less report download; export routes are
  deterministic and audited.
* Disabled-with-reason everywhere: no chart with zero points, no "0" for an unavailable metric, no
  placeholder statistics.

---

## 13. Recommendation engine

Two-stage by design: **deterministic detection, model narration.** A model never decides that a
pattern exists; it explains a pattern the SQL found.

### 13.1 Deterministic candidates (`services/analytics/recommendations.py`)

| Candidate | Rule (all sample-gated) | Emits |
| --- | --- | --- |
| Strong topic | topic group with ≥ 8 publications and ≥ 1 000 combined views, median engagement rate interval strictly above the account median interval | `kind=topic` |
| Weak format | format with ≥ 8 publications, interval strictly below account median | `kind=format` |
| Retention cliff | ≥ 3 publications with curves whose largest drop segment is the same bucket | `kind=retention` |
| Hashtag group | group used ≥ 5 times with a non-overlapping interval vs unused publications | `kind=hashtag` |
| Timing window | ≥ 10 publications per bucket, interval non-overlapping | `kind=timing` |
| Declining account | 28-day views/follows trend slope negative with ≥ 4 observed points | `kind=growth` |
| Campaign attention | campaign with < 3 publications or > 50 % unavailable metrics | `kind=campaign` |

Statistics (pure functions, tested): Wilson 95 % interval for proportions, seeded bootstrap
(1 000 resamples) for medians, overlap test returning `conclusive | inconclusive`, and a
`min_sample` guard that refuses to emit rather than emitting weak claims. No p-values are reported;
intervals and "inconclusive" are.

### 13.2 Narration

`analytics_director` (planner) receives **only** the candidate + evidence JSON and must return the
seven required fields. `assert_numbers_traceable` applies. If it fails twice, the recommendation is
stored with `generated_by="deterministic"` and a template narrative — the deterministic text is
still published, with `confidence_method="interval_overlap"`, because a missing explanation must not
hide a real finding.

### 13.3 Shape of a stored recommendation (all fields mandatory)

Required by approval requirement 7, and refused by the writer when any is missing:

* `observation` — one or two sentences, no causal claim.
* `evidence_json` — the **source measurements**: `[{metric_key, provider_metric_name, value or
  value_json, unit, period_kind, availability, snapshot_ids[], aggregation_ids[], collected_at}]`.
  A recommendation with an empty evidence list cannot be stored.
* `sample_size_json` — n per compared group, plus total observations.
* `period_start` / `period_end` — the observation periods actually covered, per group.
* `confidence` + `confidence_method` — interval-based, with the method that produced it
  (`wilson_95`, `bootstrap_median_95`, `interval_overlap`); never a bare percentage.
* `limitations_json` — at least one entry (sample size, provider threshold, unavailable metric,
  confounded variable, collection gap, or observation window), taken from the analysis.
* `proposed_experiment_json` — one variable changed, success metric chosen **before** the test, stop
  rule, required sample.
* `expected_benefit_json` — `{"kind": "estimate", "range": [low, high], "basis": "..."}`, never a
  promise and never a single fabricated number.

`test_analytics_recommendations.py::test_every_recommendation_carries_its_evidence` walks every
stored recommendation and resolves each `snapshot_id` / `aggregation_id`; a dangling reference fails
the test.

### 13.4 Approval boundary — two independent human decisions (approval requirement 8)

* A recommendation **never** changes publishing behaviour, scheduling, cadence, captions or targets.
* **Decision 1 — accept the recommendation.** Writes an `ApprovalService` request bound to the
  recommendation payload hash (`payload_hash = sha256(canonical(recommendation))`); the human
  decision is recorded in `approval_request`/`approval_record` with `consent_text_version`.
  Accepting authorises *work*, not publication: it may create a **draft** intent through the
  existing composer.
* **Decision 2 — publish.** Any resulting destination still needs its own Phase 7 approval, bound to
  the outbound payload hash, before a publisher is reached. Accepting a recommendation never
  satisfies that approval, and the publishing approval never references a recommendation.
* The API keeps them on separate routes (`/analytics/recommendations/{id}/accept` vs.
  `/publishing/destinations/{id}/approval`), and a test asserts that accepting a recommendation
  creates **no** `publish_job` in a publishable state and **no** `approval_record` for a publish
  action.
* Rejections are stored with a reason so the engine can see which suggestions the operator refuses
  (stored, not auto-applied, and not re-proposed automatically in the same period).

---

## 14. Resource requirements (this machine)

Machine: i5-8300H · 24 GB RAM · GTX 1050 Ti Max-Q 4 GB (Pascal, partial offload) · Ollama 0.40.17 ·
9.3 GB of models on D:.

| Work | Runtime | Cost |
| --- | --- | --- |
| Collection (HTTP only) | analytics lane, 1 worker, 1 in-flight request | negligible CPU; no GPU |
| Aggregation | SQL + Python in-process | 1–10 s per account/window on ~76 k rows; ~40 MB DB |
| Comparison/statistics | Python, 1 000 bootstrap resamples | < 1 s per comparison, capped at 10 comparisons per report |
| Categorisation (topic/format/hashtag) | `router_light` qwen3:0.6b, batched 10 items | the measured cheapest path; governor slot 1 |
| Per-publication narration | `specialist` qwen3:1.7b | ~45 tok/s measured |
| Weekly report / experiment design | `planner` qwen3:4b (ctx capped at 2 048 in the registry) | ~15.5 tok/s at 69 % GPU; only 1–3 calls per report |

Rules:

* Aggregation is **deterministic Python/SQL, always**. No model ever computes a number.
* No cloud AI anywhere; `grep` test asserts no provider host in `services/analytics/` (mirroring the
  existing router test).
* The governor stays at one generation at a time; analytics report generation requests a slot and
  honours `ResourcePressure` (falls back to the deterministic template rather than blocking).
* `CREATOROS_ANALYTICS_WORKERS=0` and `analytics.*>enabled=false` settings stop all collection
  without stopping publishing.

---

## 15. Security design

| Requirement | Implementation |
| --- | --- |
| No token exposure | tokens come from `TokenService.access_token(account)` at call time; `analytics_*` rows store only `source_ref` (path), `api_host`, `api_version`. A test greps every analytics response model and every snapshot row for token-shaped strings and full URLs with query strings. |
| Minimum permissions | only read scopes are added by P8.20 (`read_insights`, `instagram_manage_insights` / `instagram_business_manage_insights`); no messaging, no ads, no demographic scopes. |
| Approved metrics only | `analytics_metric_definition` is the allowlist; the collector refuses an unknown `metric_key`; request payloads are built from the registry, never from user input. |
| No private messages / unrelated data | no `/conversations`, no `/messages`, no comment author identities, no demographic breakdowns (policy `redacted_by_policy`). Comment **counts** only. |
| Redaction | `analytics_collection_run.error_detail_redacted` and `availability_reason` pass through the existing redaction patterns; signed URLs and `access_token=` never reach a row or a log. |
| Disconnection / revocation | `TokenService` failure or `connection_status != connected` → `sync_state.state=blocked`, collection stops immediately, `analytics_sync_state.next_due_at=NULL`; the documented retention policy then applies: rows are kept read-only until the account is deleted, and **deleting** the account cascades (`analytics_snapshot.account_id ON DELETE CASCADE`) — the GUI states this before the user disconnects. |
| Provider terms | only documented endpoints; `research API` excluded; no scraping, no browser automation, no private endpoints; retention windows respected (no request older than 2 years FB / 2 years IG media / 90 days IG user / 24 h story). |
| Local-only | same loopback + per-launch token as every other route; exports written under the data root with a recorded sha256; no analytics data leaves the machine. |
| Agent safety | analytics tools are read/compute/propose only; no publishing tool in any analytics grant; `recommendation.propose` writes a draft and cannot approve. |

---

## 16. Testing strategy

| Layer | Test file(s) | What must be proven |
| --- | --- | --- |
| Vocabulary + anti-zero rule | `test_analytics_availability.py` | 10 availability states; the CHECK constraint rejects `value_num` on a non-observed row and rejects NULL on `observed`; a property test asserts no code path converts unavailable → 0 |
| Metric registry | `test_analytics_catalog.py` | every catalogue row has a doc URL, a unit, an object type and a period; exact provider names match a golden snapshot; deprecated rows carry `api_version_max`; seed is idempotent |
| Migration | `test_analytics_migration.py` | copy of the real DB, 42 → 48 tables, CHECK constraints present, downgrade/upgrade round trip, `foreign_key_check []`, `integrity_check ok` |
| Facebook adapter | `test_analytics_facebook.py` | Page/post/video/reel fixtures (`httpx.MockTransport`), the two June-2026 deprecation cases, `0 for reshared` handling, empty-`data` → `missing`, threshold response → `below_threshold` |
| Instagram adapter | `test_analytics_instagram.py` | both login paths and both hosts, `total_value` vs `time_series` shapes, `REELS`-only metrics rejected for FEED, story 24 h/error `10`, ≥100-follower threshold, empty data set ≠ 0, `total_*` metrics only on the Facebook Login path |
| TikTok adapter | `test_analytics_tiktok.py` | user info + video list paging by cursor, `error.code == "ok"`, 429 `rate_limit_exceeded`, absent watch-time metrics reported as `not_supported` |
| Ingestion | `test_analytics_ingestion.py` | run dedupe, snapshot upsert idempotency, run counts, availability reasons recorded, retry does not overwrite an observed value |
| Sync/discovery | `test_analytics_sync.py` | cursor advance, backfill bounds, 2-year/90-day retention stop, `next_due_at` backoff, blocked account stops collection |
| Aggregation | `test_analytics_aggregation.py` | window selection tolerance (+1 h/24 h/48 h/7 d/28 d), weakest-input availability propagation, derived-rate refusal when `views` is missing/0, versioned recompute |
| Statistics | `test_analytics_statistics.py` | Wilson interval against known values, seeded bootstrap determinism, `inconclusive` when intervals overlap, sample-gate refusal |
| Recommendations | `test_analytics_recommendations.py` | every field present, limitations never empty, estimate marker, no publishing module import, accept-path creates an approval request, reject stores a reason |
| Queue/integration | `test_analytics_queue.py` | lane isolation both ways, `CREATOROS_ANALYTICS_WORKERS=0` disables, sweeper enqueues only due accounts, publishing is never delayed by a lane-`analytics` job |
| API | `test_analytics_api.py` | ~55 routes, `Page[T]` paging, error envelope, no token/URL leakage, filters behave, export content matches the query |
| Agent tools | `test_agent_analytics_tools.py` | grants per department, no publish tool reachable, `assert_numbers_traceable` rejects an invented number, evidence refs populated |
| Security/leaks | `test_analytics_security.py` | redaction patterns, no full URLs, disconnect cascade, no demographics collected, policy rows visible |
| GUI | `tsc --noEmit`, `vite build`, `scripts/ui_drive.py` + screenshots | charts render real points, gaps for unavailable, disabled-with-reason, freshness text, export works |

Plus: full suite must stay green (1 114 existing tests), `ruff check src tests scripts` clean,
migration applied to the live DB with a backup, and `docs/evidence/P8.md` with categories
A (implemented and tested), B (**empty on this machine** — no provider account), C (blocked by
developer approval), D (blocked by permissions/consent).

---

## 17. API endpoint inventory (new `/analytics` router, token-authenticated)

| Group | Routes |
| --- | --- |
| Overview | `GET /analytics/overview` · `GET /analytics/freshness` · `GET /analytics/capabilities` |
| Registry | `GET /analytics/metrics` · `GET /analytics/metrics/{metric_key}` · `GET /analytics/availability` |
| Accounts | `GET /analytics/accounts` · `GET /analytics/accounts/{account_id}` · `GET /analytics/accounts/{account_id}/growth` |
| Publications | `GET /analytics/publications` · `GET /analytics/publications/{publication_id}` · `GET /analytics/publications/{publication_id}/metrics` · `GET /analytics/publications/{publication_id}/retention` · `GET /analytics/publications/{publication_id}/timeline` |
| Series / charts | `GET /analytics/series` (metric, scope, range, bucket) · `GET /analytics/platforms` |
| Campaigns | `GET /analytics/campaigns` · `GET /analytics/campaigns/{campaign_id}` |
| Comparison | `POST /analytics/compare` · `GET /analytics/hashtags` · `GET /analytics/timing` |
| Recommendations | `GET /analytics/recommendations` · `GET /analytics/recommendations/{id}` · `POST /analytics/recommendations/{id}/accept` · `POST /analytics/recommendations/{id}/reject` · `POST /analytics/recommendations/refresh` |
| Reports | `GET /analytics/reports` · `POST /analytics/reports` · `GET /analytics/reports/{id}` · `GET /analytics/reports/{id}/file` · `DELETE /analytics/reports/{id}` |
| Exports | `GET /analytics/export/metrics.csv` · `GET /analytics/export/publications.csv` |
| Collection control | `POST /analytics/sync/{account_id}` · `POST /analytics/backfill/{account_id}` · `POST /analytics/prune` · `GET /analytics/runs` · `GET /analytics/runs/{run_id}` |
| Settings | `GET/PUT /analytics/settings` |
| Agent views (reads only) | reuses `/agents/objectives*` and the approvals API — no parallel approval endpoints |

Conventions: `Page[T]`, the existing error envelope, UTC timestamps, `availability` on every metric
payload, and **never** a token, full provider URL, or raw provider error text.

---

## 18. Implementation sequence

Classification legend:
**A** implementable now with no external account · **B** fixture-testable against recorded
provider payloads · **C** requires a connected Facebook Page · **D** requires a connected Instagram
professional account · **E** requires TikTok developer/app approval · **F** requires additional
permissions/app review (Advanced Access).

Each task lists expected files, database work, the test file, acceptance, dependencies and rollback.

### P8.1 — This plan
* **Files:** `docs/PHASE8_PLAN.md` (only).
* **DB:** none. **Test:** none (review). 
* **Acceptance:** committed alone, before any code; `git status` clean.
* **Depends:** —. **Rollback:** `git revert`.
* **Class:** A.

### P8.2 — Vocabulary and guardrails
* **Files:** `db/enums.py` (+5 enums), `db/models/__init__.py`, `docs/ANALYTICS_ARCHITECTURE.md` (new).
* **DB:** none. **Test:** `test_analytics_availability.py`.
* **Acceptance:** enums exported; `normalize_availability` helper with a total mapping; no model imports.
* **Depends:** P8.1. **Rollback:** revert (no schema).
* **Class:** A.

### P8.3 — Migration `b8c9d0e1f2a3`
* **Files:** `db/models/analytics.py` (new), `migrations/versions/20261125_0900_b8c9d0e1f2a3_analytics_engine.py`, `db/models/ops.py` (remove `AnalyticsSnapshot` here).
* **DB:** rebuild `analytics_snapshot`; create `analytics_metric_definition`, `analytics_collection_run`, `analytics_sync_state`, `analytics_aggregation`, `analytics_recommendation`, `analytics_report`; both CHECK constraints; 8 indexes; 4 FKs.
* **Test:** `test_analytics_migration.py` (real DB copy, round trip).
* **Acceptance:** 42 → 48 tables, `foreign_key_check []`, `integrity_check ok`, downgrade restores the old shape, live DB untouched until the test passes.
* **Depends:** P8.2. **Rollback:** `alembic downgrade a7b8c9d0e1f2` + restore `data/backups/manual-p8-*.db`.
* **Class:** A.

### P8.4 — Metric catalogue + registry seed + version monitor
* **Files:** `integrations/analytics/catalog.py`, `services/analytics/registry.py`, `services/analytics/version_monitor.py`.
* **DB:** rows in `analytics_metric_definition` (seed, idempotent).
* **Test:** `test_analytics_catalog.py` (golden snapshot of names/units/periods/doc URLs/**classes/kind/scope**) + `test_analytics_version_compat.py` (six version tests, §4.5).
* **Acceptance:** every metric in §5 present exactly once; **`metric_class` and `value_kind` mandatory on every row** (requirements 4 and 5); deprecated rows carry `api_version_max` and `is_deprecated`; `preferred_for` set for reach/views/watch-time; per-row `required_permissions_json` (TikTok: `user.info.stats` vs `video.list` kept apart, requirement 1); unknown metric rejected by the collector; **no adapter or registry hardcodes a Graph version** (requirement 2).
* **Depends:** P8.3. **Rollback:** delete seed rows (`catalog_version`), no schema change.
* **Class:** A.

### P8.5 — Adapter base + Facebook account-level
* **Files:** `integrations/analytics/base.py`, `facebook.py`, `errors.py`.
* **DB:** none (HTTP only).
* **Test:** `test_analytics_facebook.py` (Page insights fixtures, 90-day range splitting, `since`/`until`, deprecation cases).
* **Acceptance:** no SQLAlchemy import; a failing metric in a multi-metric request is isolated, not fatal; retry-after honoured.
* **Depends:** P8.4. **Rollback:** delete modules; nothing calls them yet.
* **Class:** A + B (live shape: C + F).

### P8.6 — Facebook post + video + Reels
* **Files:** `integrations/analytics/facebook.py` (post/video paths), `integrations/analytics/normalize.py`.
* **DB:** none. **Test:** same file, post/video/reel fixtures incl. 40-point curve and "returns 0 for reshared".
* **Acceptance:** Post-node engagement (`shares`, `reactions.summary`, `comments.summary`) and insights combined correctly; Reels metrics routed to `reel` object type; curve stored as `value_json` with 40 buckets validated.
* **Depends:** P8.5. **Rollback:** revert.
* **Class:** A + B.

### P8.7 — Instagram (both login paths)
* **Files:** `integrations/analytics/instagram.py`.
* **DB:** none. **Test:** `test_analytics_instagram.py`.
* **Acceptance:** host selection by route (`graph.instagram.com` vs `graph.facebook.com`); `metric_type=total_value` vs `time_series` handled; product-type gating (`REELS`-only metrics rejected for FEED); `total_*` only on the FB-Login path; empty `data` → `missing`.
* **Depends:** P8.5. **Rollback:** revert.
* **Class:** A + B (live: D + F).

### P8.8 — TikTok Display API (scope-separated)
* **Files:** `integrations/analytics/tiktok.py`, `integrations/social/tiktok.py` (split `USER_FIELDS` per scope), `integrations/social/config.py` (`user.info.stats` capability note).
* **DB:** none. **Test:** `test_analytics_tiktok.py`.
* **Acceptance:** `user/info` + `video/list` with cursor paging (`max_count` 20), `error.code=="ok"`, 429 handling, request count ≤ budget; **account statistics are requested only when `user.info.stats` is granted, and read `not_authorized` (with the scope named) when it is not** (requirement 1); `video.list` metrics are planned independently of the account scope; watch time / retention / replays reported `not_supported`, never missing-zero.
* **Depends:** P8.5. **Rollback:** revert.
* **Class:** A + B (live: E).

### P8.9 — Snapshot writer + ingestion (run-scoped idempotency)
* **Files:** `services/analytics/snapshots.py`, `collector.py`, `budget.py`, `runs.py`.
* **DB:** writes `analytics_snapshot`, `analytics_collection_run`.
* **Test:** `test_analytics_ingestion.py`.
* **Acceptance:** single writer; **a replayed job resumes the same `run_id` and writes no duplicate row** (requirement 3); a *new* logical run appends and never rewrites a closed run's rows; CHECK constraints respected (no value on a non-observed row; `value_kind` matches the column); structured/series values stored whole in `value_json` (requirement 4); retry never overwrites an observed value; rate-limit budget enforced.
* **Depends:** P8.3–P8.8. **Rollback:** delete rows for the affected accounts; code revert.
* **Class:** A + B.

### P8.10 — Discovery, sync state, backfill
* **Files:** `services/analytics/discovery.py`, `sync_state.py`.
* **DB:** `analytics_sync_state` writes; `publish_job` read to link remote ids.
* **Test:** `test_analytics_sync.py`.
* **Acceptance:** publication discovery links `provider_object_id` ↔ `publish_job.remote_post_id` when we published it, and creates an unlinked observation when we did not; cursor/backfill bounds; retention stop; backoff; blocked account stops.
* **Depends:** P8.9. **Rollback:** set `sync_state.state='not_started'`.
* **Class:** A + B (live linking: C/D/E).

### P8.11 — Analytics queue lane + scheduler
* **Files:** `services/analytics/jobs.py`, `scheduler.py`, `workers/runner.py`, `core/settings.py` (`CREATOROS_ANALYTICS_WORKERS`).
* **DB:** `queue_job` rows on lane `analytics`.
* **Test:** `test_analytics_queue.py`.
* **Acceptance:** lane isolation both ways; sweeper enqueues only due work; `0` workers disables; publish lane unaffected; no new thread.
* **Depends:** P8.9, P8.10. **Rollback:** `CREATOROS_ANALYTICS_WORKERS=0` (instant), then revert.
* **Class:** A + B.

### P8.12 — Metric classes, performance windows and aggregation
* **Files:** `services/analytics/aggregation.py`, `windows.py`, `comparability.py`.
* **DB:** `analytics_aggregation` upserts.
* **Test:** `test_analytics_aggregation.py`, `test_analytics_comparability.py`.
* **Acceptance:** +1 h / 24 h / 48 h / 7 d / 28 d windows chosen with documented tolerance; **cumulative vs period vs point-in-time enforced** (requirement 5): a delta is only computed for the same object and the gap is recorded, a `period` metric never mixes window lengths, overlapping windows are refused; derived rates refused when inputs missing/zero; weakest-availability propagation; **incomparable pairings returned as typed refusals, never rendered as a number** (requirement 6); `aggregation_version` bump recomputes without losing history.
* **Depends:** P8.11. **Rollback:** delete aggregations for a version, recompute.
* **Class:** A + B.

### P8.13 — Statistics + comparison
* **Files:** `services/analytics/statistics.py`, `comparison.py`.
* **DB:** none beyond aggregation. **Test:** `test_analytics_statistics.py`.
* **Acceptance:** Wilson/bootstrap correctness, determinism, sample gates, `inconclusive` output, comparison caps.
* **Depends:** P8.12. **Rollback:** revert (pure functions).
* **Class:** A.

### P8.14 — Recommendation engine
* **Files:** `services/analytics/recommendations.py`, `services/analytics/evidence.py`.
* **DB:** `analytics_recommendation`.
* **Test:** `test_analytics_recommendations.py`.
* **Acceptance:** §13.1 rules with gates; **all eight mandated fields stored, with resolvable source measurements, periods, sample sizes, confidence method and at least one limitation** (requirement 7); an empty evidence list is refused at write time; estimate flag; **accepting creates no publishable job and no publish approval** (requirement 8); no publishing imports; deterministic narrative fallback.
* **Depends:** P8.13. **Rollback:** delete draft recommendations; revert.
* **Class:** A + B.

### P8.15 — Agent tools + evidence contract
* **Files:** `services/toolbox.py`, `schemas/agents.py`, `agents/catalog.py` (grants + **fix `TOOL_IDS`**, which currently omits the three Phase 7 publishing tools; **no new agents**), `services/structured.py`, `services/analytics/agent_grants.py`.
* **DB:** `tool_registry` rows; `agent_definition.allowed_tools_json` updates.
* **Test:** `test_agent_analytics_tools.py`.
* **Acceptance:** 5 tools; grant matrix; untraceable-number rejection; evidence refs; no publish reach.
* **Depends:** P8.12–P8.14. **Rollback:** disable tools in `tool_registry`, revert grants.
* **Class:** A.

### P8.16 — Agent workflows (W1–W3)
* **Files:** `services/analytics/workflows.py`, `prompt_template` seeds (`analytics.*`), `orchestrator` glue only if needed.
* **DB:** `prompt_template`, `agent_task.evidence_refs_json` already exists.
* **Test:** `test_agent_analytics_workflows.py` (faked model gateway; asserts task graph shape and contracts).
* **Acceptance:** W1 produces the documented task chain; W2/W3 shapes; failures become `partial` with reasons; no workflow publishes.
* **Depends:** P8.15. **Rollback:** stop enqueuing objectives; revert prompts.
* **Class:** A (model calls use the local gateway; no cloud).

### P8.17 — API router
* **Files:** `api/routes/analytics.py`, `schemas/analytics.py`, `main.py`.
* **DB:** none.
* **Test:** `test_analytics_api.py`.
* **Acceptance:** ~55 routes, paging, envelope, availability on every metric, no leakage, exports match queries.
* **Depends:** P8.12–P8.14. **Rollback:** unregister router.
* **Class:** A + B.

### P8.18 — Chart primitives
* **Files:** `app/desktop/src/components/charts.tsx`, `styles.css` (tokens only).
* **DB:** none. **Test:** typecheck + build + a render smoke test in `scripts/ui_drive.py`.
* **Acceptance:** gaps for unavailable points; no zero-substitution; deterministic axis; labelled "no data" state.
* **Depends:** P8.17 (types only). **Rollback:** revert page imports.
* **Class:** A.
* **Delivered:** `src/components/chartModel.ts` (pure logic: availability, gap segmentation, deterministic
  axis, formatting — unit-tested without a DOM) and `src/components/charts.tsx` (`LineChart`, `BarChart`,
  `MetricWidget`, `ChartLegend`, `ChartEmptyState`), plus `--chart-*` tokens in `styles.css`. 20
  `node --test` cases; `tsc --noEmit` and `vite build` clean and the application bundle is unchanged
  (479.61 kB / 124.85 kB gzip). A dev-only harness (`charts-preview.html`, `vite.charts.config.ts`,
  `scripts/charts-screenshot.cjs`) renders the real components with labelled synthetic fixtures and
  captures five PNGs into `docs/evidence/` (`p8-chart-*.png`). An unavailable observation is a gap, an
  all-unavailable metric renders an explicit "No data" panel, and a real observed zero keeps a readable
  flat axis — never a zero line masquerading as missing.

### P8.19 — GUI batch 1: Overview, Platform comparison, Account performance
* **Files:** `App.tsx` (nav section), `apiAnalytics.ts`, `pages/analytics/{Overview,PlatformComparison,AccountPerformance}.tsx`.
* **DB:** none. **Test:** `tsc`, build, screenshots.
* **Acceptance:** real data, freshness badges, missing-metric panel, disabled-with-reason, no invented chart.
* **Depends:** P8.18. **Rollback:** hide nav section.
* **Class:** A + B.
* **Delivered:** a `Analytics` navigation section with Overview, Platform comparison and Account
  performance; `src/services/apiAnalytics.ts` (typed, cancellable client over 17 `/analytics`
  endpoints with a stubbed-fetch test suite); `src/pages/analytics/{shared,Overview,PlatformComparison,AccountPerformance}.tsx`.
  KPI cards, a real trend with an account/metric selector, per-platform count widgets, recent
  publications with their observed metrics, freshness badges, account capability/availability, growth,
  stored metrics and collection runs. The comparison page compares a **concept** (`preferred_for`,
  e.g. `views`) resolved to each platform's own metric, with an explicit comparable/not-comparable
  banner and one axis per platform. 33 frontend tests; `tsc` and `vite build` clean (525.61 kB / 135.70
  kB gzip). Seven screenshots under `docs/evidence/p8-analytics-*.png`.

### P8.20 — Consent path for insights scopes
* **Files:** `integrations/social/config.py` (scope + capability notes), `services/social/oauth_engine.py` (re-consent), `api/routes/analytics.py` (`POST /analytics/consent/{account_id}`), `services/social/capabilities.py`.
* **DB:** `account_capability`, `capability_snapshot`, `social_account.scopes_json` updates via existing services.
* **Test:** `test_analytics_consent.py` + capability matrix test.
* **Acceptance:** requesting `read_insights` / `instagram_*_manage_insights` is opt-in, documented in the UI, and produces `available` only when the provider actually grants it; declining leaves analytics `not_authorized` with a reason.
* **Depends:** P8.7. **Rollback:** revert scope request (capabilities fall back to `needs_user_consent`).
* **Class:** A (code) + F (live availability).
* **Delivered:** `services/social/consent.py` (`ConsentService.plan/start`) computes the *precise* scope
  difference per account from the capability catalogue and the login path — `read_insights` +
  `pages_read_engagement` (Facebook), `instagram_business_manage_insights` (Instagram Login),
  `instagram_basic`/`pages_show_list`/`pages_read_engagement` + `instagram_manage_insights` (Instagram
  via Facebook Login), `user.info.basic` + `user.info.stats` (TikTok) — and never trusts a
  client-supplied scope list. `OAuthEngine.start` is now account-bound (`account_id`, `purpose`,
  `context`) and `OAuthEngine._complete_upgrade` adopts a re-consent credential for the *same* provider
  identity only, verifying the identity first and revoking the old credential only after the new one is
  stored (so a denial, an identity mismatch or a vault failure leaves the account working). The
  `ConsentStatus` vocabulary distinguishes user consent, developer review, a missing Page `ANALYZE`
  task, an unsupported account and the authorization outcome. New routes `GET /analytics/consent`,
  `GET /analytics/consent/{account_id}`, `POST /analytics/consent/{account_id}` (authenticated; the POST
  accepts only an intent note). `tiktok.read_insights` joins the catalogue; `instagram.read_insights`
  now resolves per login path. UI: a consent card on Account performance and a "Missing analytics
  permissions" card on Social accounts → Permissions, both with a "Grant analytics access" button that
  opens the provider window and reports the real remaining blocker. 30 backend tests
  (`test_analytics_consent.py`), 3 new frontend client tests; `ruff`, `tsc` and `vite build`
  (532.28 kB / 137.19 kB gzip) clean.

### P8.21 - GUI batch 2: Publications, content and campaigns
* **Files:** `pages/analytics/{PublicationDetail,ContentComparison,CampaignAnalytics}.tsx`, `components/analyticsModel.ts`, `services/apiAnalytics.ts`, `App.tsx`.
* **Test:** `tests/analyticsModel.test.mjs`, `tests/apiAnalytics.test.mjs`; typecheck/build/screenshots.
* **Acceptance:** retention curve or explicit "not exposed"; derived deltas labelled; provider vs derived clearly separated; no cross-platform metric total; no performance implied by the existence of publish jobs alone; every comparison discloses its metric definition, sample size and limitations.
* **Depends:** P8.19, P8.20. **Rollback:** hide the three nav entries.
* **Class:** A + B.
* **Delivered:** the mission's "publication-level performance, content comparisons and campaign analytics"
  scope: **Publication detail** (identity with an explicit no-thumbnail placeholder — the analytics API
  exposes no media asset; observed metrics by window; the retention curve only when the provider
  returned one; a per-metric performance history; the provider-exposure panel; collection runs) and the
  plan's `ContentComparison` + `CampaignAnalytics` pages pulled forward. **Content comparison** ranks
  publications by an observed concept within one account/platform (never cross-platform), uses the
  deterministic `POST /analytics/compare` service for group medians with sample sizes and limitations,
  and charts the trend. **Campaign analytics** renders the stored attribution chain
  (campaign → project → variant → publish job → observation), platform/account destination counts,
  destination success/failure, freshness, a two-campaign count comparison, and **separate per-platform
  metric tables** — never a summed total — with an explicit empty state when publishing jobs exist but
  no observation does. Page logic lives in a pure, `node --test`-covered `components/analyticsModel.ts`
  (missing ≠ zero, retention never invented). 16 `analyticsModel` + 6 new client tests (58 frontend
  total); `tsc` and `vite build` (572.07 kB / 144.78 kB gzip) clean; 12 new screenshots under
  `docs/evidence/`. `VideoPerformance`, `AudienceGrowth` (this batch's remaining plan pages) and
  `HashtagAnalysis` stay for the next batch.

### P8.22 - GUI batch 3: Video performance, Audience growth, Hashtag analysis — **delivered**
* **Files:** `pages/analytics/{VideoPerformance,AudienceGrowth,HashtagAnalysis}.tsx`, plus
  `components/analyticsModel.ts` (concept resolution, ranking, growth bucketing, hashtag catalogue,
  sample-gated group selection) and `services/apiAnalytics.ts` (`hashtags()`).
* **Test:** `tests/analyticsPagesP822.test.mjs` (28), `tests/pagesP822.source.test.mjs` (11),
  `tests/apiAnalytics.test.mjs` (+2), typecheck/build/screenshots.
* **Acceptance:** comparison dimensions explicit; intervals + `inconclusive` shown; sample sizes visible; no causal wording.
* **Depends:** P8.19, P8.13. **Rollback:** revert pages.
* **Class:** A + B.
* **Note:** `CampaignAnalytics` and `ContentComparison` were delivered in P8.21; this batch carries the
  remaining `VideoPerformance`/`AudienceGrowth` pages from the old P8.21 plus `HashtagAnalysis`.
* **Delivered as:** the leaderboard ranks a **concept** (views, reach, likes…) resolved against each
  row's own platform metric key, so a TikTok row is never compared with, or shown a missing value for,
  a Facebook metric; comparison blocks are per metric key at one window with `n=` per block. Retention
  renders only a stored provider curve (TikTok: none) and says so. Audience growth keeps one card per
  account, labels a net change **derived**, and explains an absent follower metric from the availability
  plan (permission / provider / threshold / not collected) rather than blaming the registry. Hashtag
  analysis carries a standing correlation banner, discloses every group it declines to rank, and calls
  `GET /analytics/hashtags` (no new backend endpoint, no backend change).
* **Two gaps reported, not papered over:** the analytics API exposes **no media asset**, so there is no
  thumbnail column (the page states this in-line); and the aggregation page caps at 200 rows, so each
  page discloses when it is showing a subset.
* **Side fix:** the shared `MetricWidget` unit label overlapped the topmost y tick; it now sits at the
  plot's top-right. Re-captured the P8.19–P8.21 evidence in the same run.

### P8.23 - GUI batch 4: Recommendations, Reports, Settings — **delivered**
* **Files:** `pages/analytics/{Recommendations,Reports,AnalyticsSettings}.tsx`, plus
  `components/analyticsDecisions.ts` (decision gating, mandated-field check, evidence resolution,
  report/settings view models) and `components/reportIntegrity.ts` (WebCrypto SHA-256 verification),
  and `services/apiAnalytics.ts` (12 methods: recommendations, decision, refresh, reports, download,
  delete, settings read/write).
* **Test:** `tests/analyticsPagesP823.test.mjs` (42), `tests/pagesP823.source.test.mjs` (18),
  `tests/apiAnalytics.test.mjs` (+13), typecheck/build/screenshots.
* **Acceptance:** every recommendation field visible (observation, evidence measurements, period, sample size, confidence + method, limitations, experiment, estimate marker); **accept/reject is presented as a separate decision from publishing, with two distinct confirmations and no publish button on the recommendation page** (requirement 8); report download hash matches; settings bounded by §9 and persisted in `app_setting`.
* **Depends:** P8.14, P8.17. **Rollback:** revert pages; settings keys ignored.
* **Class:** A.
* **Delivered as:**
  * **Recommendations** renders *every* field of `RecommendationOut` plus the evidence from
    `GET /analytics/recommendations/{id}`, checked against the eight mandated fields: a record whose
    `limitations` list is empty, or whose `proposed_experiment` is `{}`, is flagged as a defect rather
    than displayed as compliant. Evidence is **re-resolved against the stored ledger** — the cited
    `snapshot_ids` are looked up in the account's snapshot page — and classified resolved / missing /
    not-observed / stale (>30 days) / unreferenced, with an unavailable metric labelled *not a zero*.
  * **Two decisions, two confirmations.** `Review acceptance…` opens an acceptance panel; `Review
    rejection…` opens a rejection panel with a mandatory stored reason and a disabled confirm button
    until one is typed. Each names the recommendation, its category, its sample size and — for
    acceptance — its confidence, and each carries its own consequence text. Acceptance says an
    *approval request for the experiment* is created; rejection says the reason is stored. Neither
    panel publishes, schedules or approves publishing, and the page has no publish control and no
    publishing/queue import (asserted by `pagesP823.source.test.mjs`).
  * **No optimistic state.** Nothing is marked decided locally: the row is replaced by the server's
    response, a 409 is explained as an already-decided conflict with a re-read, and a 404 says the
    record is gone. `POST /analytics/recommendations/refresh` generates drafts for one account and says
    so — it decides nothing.
  * **Reports** verifies a download before offering it: metadata digest from `GET
    /analytics/reports/{id}` → authenticated byte fetch → WebCrypto SHA-256 over exactly those bytes →
    compare. An empty body is refused even with a digest, a record without a digest is refused (an
    unverifiable file is not a deliverable), and a mismatch is shown as an integrity error with both
    digests and the reason — never renamed, regenerated or retried silently. Only a verified payload
    gets a *Save verified file* button. The library shows id, title (from `filters.title`), kind,
    created, range, scope, status, format (`json` — the only implemented format), size and digest, and
    the detail card shows configuration, source, sections, summary, file information and why the
    schema has no warnings array. Delete needs the report id typed in a separate confirmation.
  * **Settings** edit exactly the five keys `PUT /analytics/settings` stores, using the `bounds` the
    API returns: a value outside them is an error, never a clamp, and the single `PUT` carries only
    changed keys. Current value, allowed range, unit and *when it takes effect* are shown per row, with
    an unsaved-changes indicator, Save / Cancel, and a re-seed from the response after saving. The
    backend note is displayed verbatim — the sweeper reads these on its next tick, and the Resource
    Governor, queue limits, provider rate limits, retention and security are untouched. Reporting
    defaults, per-run budgets and a reset-to-defaults are **declared as gaps** rather than rendered as
    controls that save nowhere: the endpoint returns stored values and bounds but not its defaults.
  * **No backend change.** Every call is an existing route from P8.14/P8.17.
  * **Three contract gaps reported in place:** `RecommendationOut` has no `title`, `priority` or
    expiry field (`kind` is the category, `observation` is the narrative); `ReportOut` has no
    warnings array (limits live inside the section bodies) and no `path`; the settings endpoint
    publishes no defaults.

### P8.24 - Report formats, retention execution, version watch, diagnostics — **delivered**

* **Files:** `services/analytics/reports.py` (rewritten), `pdf.py` (new), `retention.py` (new),
  `version_watch.py` (new), `api/routes/analytics.py`, `schemas/analytics.py`, `db/enums.py`,
  `services/analytics/{jobs,scheduler}.py`, `main.py`, `tests/test_analytics_retention_reports.py`,
  `docs/ANALYTICS_{REPORTS,RETENTION}.md`, `docs/ADR/ADR-29..33`, `docs/evidence/P8.md`, and the
  desktop `Reports` format selector + `AnalyticsSettings` retention panel.
* **DB:** no migration. `analytics_report` rows gain the format inside `filters_json`; retention
  deletes existing rows under a new policy. Head stays `b8c9d0e1f2a3`, 48 tables.
* **Test:** `tests/test_analytics_retention_reports.py` (40), `tests/test_analytics_queue.py` (25,
  maintenance filtered), `app/desktop/tests/analyticsPagesP824.test.mjs` (17). Full suite **1529
  passed, 9 skipped**; frontend **188 passed**; `ruff`, `tsc`, `vite build` clean.

**Delivered**

  * **Three formats from one content model.** `build_sections()` is the only place that reads the
    database; `render_json`, `render_csv` and `render_pdf` consume it. `sha256`/`bytes` come from the
    bytes actually written (after rendering, before `os.replace()`), so the digest the desktop verifies
    is the digest of the file it received. Unknown formats are refused with 422
    `REPORT_FORMAT_UNSUPPORTED` rather than downgraded to JSON. PDF uses a dependency-free PDF 1.4
    writer (`pdf.py`) — no new runtime dependency for an unsigned installer. Caps
    (`MAX_AGGREGATION_ROWS = 500`, `MAX_RECOMMENDATIONS = 100`) are stated inside the report.
  * **Retention executes, under a per-category policy.** `retention.py` shares `plan()` between
    preview and execution, deletes in foreign-key order, asserts `PRAGMA foreign_key_check` is empty,
    batches at 2 000 rows per category and re-queues while `work_remaining`, and audits both the
    preview (`analytics.retention.preview`) and the execution (`analytics.retention.execute`).
    Business history — publications, campaigns, accounts, media, metric definitions and *decided*
    recommendations — is structurally out of scope and reported as `protected`.
    `POST /analytics/prune` stays a preview and now returns the full plan; deletion lives at
    `POST /analytics/retention/execute` with `dry_run` defaulting to `true`.
  * **Version watch observes; it never upgrades.** `version_watch.py` records the pinned version, the
    latest documented version and the observation date, marks an observation older than 90 days
    `stale`, and contains no code that writes `provider_app_config.api_version`. A live fetch is gated
    behind `CREATOROS_ANALYTICS_VERSION_FETCH=1`. `GET /analytics/versions` reports the age and the
    sunset calendar; `POST /analytics/versions/check` records an observation; a scheduled job does it
    weekly on the existing lane.
  * **Diagnostics report facts.** `GET /analytics/diagnostics` gained `retention`, `reports`,
    `collection` and `version_watch`, each with a timestamp or an explicit "never run". `/version`
    gained `analytics_reports`, `analytics_reports_formats`, `analytics_retention`,
    `analytics_version_watch` and `analytics_version_watch_fetch`, and `api` moved to `phase-8.24`
    (two older tests asserted the literal `phase-8` and were updated).
  * **Two maintenance job types on the existing lane.** `JOB_RETENTION` (70) and `JOB_VERSION_CHECK`
    (75) are queued by the existing sweeper after collection dispatch, idempotently, daily and weekly.
    No new lane and no new thread (ADR-33).
  * **Desktop: formats and retention, no new page.** The Reports form selects JSON/CSV/PDF from the
    client's `REPORT_FORMATS` list, the library shows each record's format, and a verified download
    keeps the extension of the format generated. The Settings page gained a Retention card: preview
    table with a per-category reason, protected-count line, both cutoffs, an execute button armed only
    by typing `DELETE`, and the execution result with `bytes_freed`, `foreign_key_violations`,
    `integrity_ok` and the audit id, followed by a re-read preview.

**Deviations from the brief above**

  * `scripts/live_analytics.py` was not written. A harness that cannot reach a provider (no Meta app,
    no TikTok app, no connected account on this machine) would be untested code that *looks* like a
    verification path. `docs/evidence/P8.md` states the blocker instead, per the brief's own rule
    about separating fixture-tested features from real-account verification.
  * `scripts/diagnose.ps1` was not given new checks; `GET /analytics/diagnostics` and `GET /version`
    are the diagnostics surface for analytics, and `diagnose.ps1` keeps its existing backend/DB role.
  * `docs/ANALYTICS_*.md` grew by two files (`ANALYTICS_REPORTS.md`, `ANALYTICS_RETENTION.md`) rather
    than four; the API and architecture docs were extended in place instead of split.
  * Report **scheduling** (a recurring report) is not implemented and is not offered in the UI.

* **Depends:** all above. **Rollback:** revert; `analytics_report` rows and retention deletions are not
  reversible — retention is the one Phase 8 feature whose effect is permanent, which is why it is
  preview-first, typed-confirmation and audited.
* **Class:** A + B (+ C/D/E/F documented as blocked).

**Sequence rule:** P8.1–P8.5 can start immediately. P8.6–P8.17 are code-only and fixture-testable. Nothing
in Phase 8 requires a live provider to be *implemented*; only category B evidence requires one.

---

## 19. Acceptance criteria

Phase 8 is done when **all** of the following are true:

1. `docs/PHASE8_PLAN.md` is committed before any implementation commit.
2. Migration `b8c9d0e1f2a3` is applied to the real database from a verified backup; 48 tables;
   `foreign_key_check []`; `integrity_check ok`; downgrade/upgrade round trip proven on a copy.
3. `analytics_metric_definition` contains every metric in §5 with exact provider names, units,
   periods, doc URLs and deprecation bounds; a golden test guards the catalogue.
4. `analytics_snapshot` is append-only, availability-aware, and **cannot** store a value for a
   non-observed metric (DB CHECK, not only code).
5. Collection runs on the `analytics` lane, is idempotent at three layers, respects local budgets
   below every provider limit, and never blocks publishing or media work (lane-isolation test).
6. All six performance windows (initial, 24 h, 48 h, 7 d, 28 d, lifetime) are computed with the
   method, tolerance and input snapshot ids recorded.
7. Missing ≠ zero: a test enumerates all ten availability states through the API and the GUI
   contract, and no path produces `0` for an unavailable metric.
8. Unavailable metrics are explained in the GUI from stored data (registry + latest availability +
   account capability), including "TikTok does not expose this".
9. The 12 ANALYTICS CENTER pages exist, render real data, and show gaps instead of invented points.
10. Recommendations carry all nine mandated fields, an explicit estimate marker, at least one
    limitation, and cannot change publishing behaviour; "accept" produces a hash-bound approval
    request.
11. Analytics agents operate only on stored analytics, cite snapshot/aggregation ids, and cannot
    reach a publisher (import-scan test).
12. TikTok watch time / retention / replays are reported `not_supported` everywhere — never
    estimated from views.
13. TikTok Research API appears nowhere in code, prompts or docs except as an explicit exclusion.
14. Security: no token, no full provider URL, no signed URL, no demographic/private data in any
    analytics row, log, response or export; disconnecting an account stops collection immediately.
15. `/version` and `/diagnostics` report analytics capability per platform from facts.
16. Full test suite green (existing 1 114 + new), `ruff` clean, `tsc` clean, `vite build` clean,
    `docs/evidence/P8.md` present with A/B/C/D split (B empty on this machine, stated in the first
    paragraph).

---

## 20. Known blockers and architecture concerns

### 20.1 External blockers (cannot be solved by code on this machine)

1. **No Meta app, no TikTok app, no connected account.** Every Facebook and Instagram collector is
   implementable and fixture-testable, but no live response can be verified here. Category B
   (verified against a real account) will be empty, exactly as in Phase 7 — stated up front.
2. **Facebook insights needs App Review (Advanced Access) + Business Verification** and a Page with
   ≥ 100 likes; the connection flow currently does not request `read_insights`, so the capability is
   `needs_user_consent` until the operator re-consents after P8.20.
3. **Instagram insights needs `instagram_manage_insights` / `instagram_business_manage_insights`**,
   which the current connect flow does not request; Standard Access only covers accounts the app
   owns/manages. Under 100 followers, follower metrics are simply not returned.
4. **TikTok needs app review for Login Kit + the TikTok API product.** The scopes we need are
   already requested, so a normal approved app unlocks profile + video counters. Everything else
   (watch time, retention, saves, reach) does not exist in any approved TikTok API.
5. **Meta's June 2026 metric retirement** means the reach/impression family is on a countdown for all
   versions. Starting Phase 8 on the modern `*_media_view` metrics avoids building on metrics already
   scheduled to disappear.
6. **Graph version drift.** v19 and v20 are already retired; v23 is pinned in this repository. The
   plan records the version per observation and adds a version-watch test, but the actual bump
   decision (v23 → v25+) is an operational one that changes which metrics exist.

### 20.2 Architecture concerns (decided here, worth re-checking at review)

1. **`analytics_snapshot` is rebuilt, not extended.** Adding columns could not fix `value NOT NULL`
   (missing ≠ 0) or the `(platform, post_id, metric, hour_bucket)` unique key (no room for period,
   provider object, or an account-level row that cannot duplicate). Rebuilding an empty, unused table
   is the lower-risk option; the alternative (a second observation table) would be the parallel
   architecture the brief forbids.
2. **Attribution is captured, not only joined.** `content_project_id`/`campaign_id`/`asset_id` are
   stored as immutable strings so deleting a draft project cannot erase what a published video did.
   This is a deliberate, documented departure from pure normalisation (§7.3).
3. **Two "totals" per account are not the same number.** Facebook followers ≠ Page likes; Instagram
   followers from the User field ≠ the `follows_and_unfollows` insight; TikTok follower count is a
   snapshot with no growth series. The schema and the GUI keep them separate; a single "followers"
   tile that silently picks one would be the first lie in the product.
4. **Engagement rate is dangerous to compare across platforms.** TikTok gives no reach, Instagram
   `reach` is an estimate, Facebook reach metrics are being replaced. Comparisons are therefore
   *within-provider* by default, and a cross-platform comparison shows the availability matrix
   before any number.
5. **Collection cadence vs provider cadence.** Polling an endpoint that updates once per 24 h is pure
   waste and a fast route to BUC throttling. §9 encodes the provider cadence as the floor, not as a
   target.
6. **Model narration must never own a number.** The `assert_numbers_traceable` post-check is the
   enforcement; if it proves too blunt (dates, ordinals, counts), it must be refined — **not**
   relaxed to a warning that everyone ignores.
7. **Recommendation → experiment → publishing is a two-approval path.** Recommendation acceptance and
   publishing approval are separate, deliberately. Merging them would let one click publish content
   that only had a second click's worth of review.
8. **Growth of the `analytics_snapshot` table is the one thing that compounds.** ~40 MB/year at a
   realistic cadence, ~160 MB/year at 50 publications/month. The retention job prunes only
   operational rows; if this ever becomes a problem the answer is downsampling *old* observations
   into `analytics_aggregation`, not deleting history.

---

## 21. Approval requirements — coverage matrix

Each of the eight requirements approved on 2026-11-25, where it is implemented, and the test that
fails if it is dropped. Nothing here is satisfied by documentation alone.

| # | Requirement | Code | Test | Milestone |
| --- | --- | --- | --- | --- |
| 1 | TikTok account stats map to granted `user.info.stats`; `video.list` kept separate | `integrations/analytics/catalog.py` (`required_permissions_json`), `integrations/analytics/tiktok.py` (per-scope field lists), `integrations/social/tiktok.py`, `services/analytics/registry.py` | `test_analytics_tiktok.py::test_stats_metrics_require_user_info_stats`, `::test_video_list_metrics_plan_without_stats_scope` | P8.8 |
| 2 | Graph version pinned + configurable, monitored, compatibility-tested, never silently upgraded | `integrations/social/config.py` (default only), `provider_app_config.api_version` (authority), `services/analytics/version_monitor.py`, `analytics_snapshot.api_version` | `test_analytics_version_compat.py` (6 tests), `test_analytics_api.py::test_versions_endpoint` | P8.4, P8.24 |
| 3 | Retries idempotent per logical run; snapshots append-only between runs | `services/analytics/runs.py`, `snapshots.py`; `dedupe_key` UNIQUE; snapshot UNIQUE per run; writer scoped to `run_id` | `test_analytics_ingestion.py::test_retry_resumes_same_run`, `::test_new_run_appends_and_never_rewrites`, `::test_closed_run_rows_are_immutable` | P8.9 |
| 4 | Original value type preserved (numeric / breakdown / series) | `value_kind` on the definition and on the row, paired CHECK constraints, `NormalizedMetric` dataclasses, API returns `value` **or** `value_json` | `test_analytics_ingestion.py::test_series_and_breakdown_are_not_flattened`, `test_analytics_migration.py::test_value_kind_checks` | P8.3, P8.9 |
| 5 | Cumulative vs period vs point-in-time never mixed; no change from incomparable observations | `metric_class` on the definition, `services/analytics/comparability.py`, `windows.py`, `analytics_aggregation.window_kind` | `test_analytics_comparability.py::test_delta_requires_same_object`, `::test_period_windows_must_match`, `::test_overlapping_windows_refused`, `::test_point_in_time_gap_recorded` | P8.12 |
| 6 | No invented metrics, no misleading cross-platform comparison | availability vocabulary + DB CHECK, `comparability.refuse_incomparable`, GUI availability matrix, recommendation sample gates | `test_analytics_availability.py::test_no_path_coerces_unavailable_to_zero`, `test_analytics_comparability.py::test_cross_platform_refusal`, `test_analytics_api.py::test_every_metric_carries_availability` | P8.2, P8.12, P8.19 |
| 7 | Recommendations carry source measurements, periods, sample sizes, confidence, limitations | `services/analytics/recommendations.py`, `evidence.py`, `analytics_recommendation` columns | `test_analytics_recommendations.py::test_every_recommendation_carries_its_evidence`, `::test_empty_evidence_refused`, `::test_limitations_never_empty` | P8.14 |
| 8 | Recommendation acceptance and publishing approval are independent | `api/routes/analytics.py` (`accept` → `ApprovalService`), untouched `services/publishing/approvals.py`, GUI two-step | `test_analytics_recommendations.py::test_accept_creates_no_publishable_job`, `test_analytics_api.py::test_accept_is_not_publish`, `test_agent_analytics_tools.py::test_no_analytics_role_can_publish` | P8.14, P8.23 |

Two invariants worth repeating because they are easy to break by accident:

* Adding a metric to the catalogue without `metric_class`, `value_kind` and
  `required_permissions_json` must fail at import/seed time, not at collection time
  (`ensure_catalog_is_consistent()`, same technique as `agents/catalog.py`).
* A new comparison path must call `comparability.assert_comparable(...)` **before** it computes
  anything. A test scans `services/analytics/` for arithmetic over two metric rows that skipped it.


## Appendix A — Proposed ADRs for this phase

| ADR | Decision |
| --- | --- |
| ADR-29 | Provider metric identity is preserved in a versioned registry; normalized metrics never overwrite provider names (`analytics_metric_definition`). |
| ADR-30 | One observation ledger, append-only, availability-aware, with a DB CHECK that a non-observed metric has no value (`analytics_snapshot` rebuild). |
| ADR-31 | Analytics runs on a fourth queue lane (`analytics`) inside the existing queue, dispatched by the existing sweeper thread; no second queue, no fifth thread. |
| ADR-32 | Deterministic detection, model narration: statistics and sampling gates are Python/SQL; a model may explain, never compute or invent. |
| ADR-33 | Attribution is captured at collection time and survives project deletion; only `account_id` and `publication_id` are foreign keys. |

## Appendix B — Classification of what can be proven on this machine

| Category | Meaning | Phase 8 items |
| --- | --- | --- |
| **A** | Implementable now, no external account | schema, registry, adapters, ingestion, aggregation, statistics, recommendations, API, GUI, tools, docs |
| **B** | Fixture-testable against recorded payloads | every adapter, availability matrix, deprecation cases, error classification, rate-limit handling |
| **C** | Requires a connected Facebook Page | Page/post/video/reel live collection, `read_insights` grant, ≥100-like behaviour |
| **D** | Requires a connected Instagram professional account | account/media insights on both login paths, Reels watch time, follower thresholds |
| **E** | Requires TikTok developer/app approval | Display API profile + video counters; everything else stays `not_supported` |
| **F** | Requires additional permissions / App Review / Business Verification | Advanced Access for Meta insights, `instagram_*_manage_insights` consent, `pages_manage_engagement` for video insights |

Nothing in categories C–F can be verified on this machine. The plan does not pretend otherwise, and
`docs/evidence/P8.md` will say so in its first paragraph.
