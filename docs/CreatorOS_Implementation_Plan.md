# CreatorOS Studio — Master Implementation Plan
### Phase-by-phase, task-by-task execution plan derived from `CreatorOS_Master_Blueprint.md`

**Plan version:** 1.0 · **Derived from:** Master Blueprint (22 Nov 2026) · **Status:** ready for execution approval
**Scope:** every phase, every task, every acceptance gate, every risk, every human decision required.

---

## How to use this document

| Convention | Meaning |
|---|---|
| **P0 … P11** | The 12 build phases. Each P-number maps 1:1 to the same-numbered "Prompt" in the Blueprint (§11), so you can paste Blueprint Prompt N when this plan says "enter PN". |
| **P1.T07** | Task ID. Stable. Use it in commits (`feat(P1.T07): install ollama to D:`), in the evidence pack, and in the audit matrix. |
| **Gate** | The phase cannot be considered done until every gate line is proven with evidence. Gates are written as *observable facts*, not intentions. |
| **Evidence** | What you must be able to show. "It works" is not evidence. A command + output, a file path, a test name + result, or a screenshot is evidence. |
| **Est** | Engineering hours for one competent developer with AI assistance. Includes tests. Excludes waiting on third-party app review. |
| **🔒 INV-n** | One of the 10 non-negotiable invariants (§3). Tasks that enforce them are tagged. |
| **👤 Human** | Requires you (accounts, business verification, policy decisions, screenshots, consent text approval). Cannot be done by the coding agent. |
| **⭐ Blocking** | On the critical path; nothing downstream can start. |

**Three rules for the coding agent, to be restated in every phase kickoff:**
1. One phase at a time. No starting PN+1 while a PN gate line fails.
2. Every claim must be reproducible by a documented command.
3. If something cannot be built honestly (platform approval missing, scope not granted), it ships as a *disabled control with a precise reason* plus a documented manual path — never as a fake success.

---

## Contents

**Part I — Ground truth and specifications:** §0 machine audit · §1 architecture decisions & open questions · §2 three tracks (code / approvals / product) · §3 invariant enforcement matrix · §4 critical path and ordering · §5 repository & module blueprint · §6 data model · §7 API surface · §8 agent system spec · §9 publish state machine · §10 D-drive policy · §11 security controls · §12 testing, CI, observability · §13 estimates

**Part II — The phase plans (184 tasks with acceptance gates):** P0 constitution · P1 workspace & walking skeleton · P2 backend/DB/queue · P3 model gateway & governor · P5 media pipeline · P6 accounts & capabilities · P7 publishing & TikTok fallback · P4 the 111-agent workforce · P8 analytics/inbox/automations · P9 desktop polish · P10 security & packaging · P11 independent audit

**Part III — Execution discipline:** §14 milestones & demo scripts · §15 risk register · §16 definition of done · §17 anti-fake checklist · §18 approval track · §19 first 10 actions · §20 honest non-promises

---

## 0. Machine audit — what your PC actually has (verified, not assumed)

I inspected the real machine on the day this plan was written. Deviations from the Blueprint are called out because they change Phase 1 work.

| Item | Blueprint assumption | **Actual state** | Consequence |
|---|---|---|---|
| OS | Windows 10/11 64-bit | Windows **10.0.26300** (Windows 11 25H2-era build) | Fine. Note: `TEMP`/`TMP` global overrides are riskier on modern builds → see ADR-05. |
| CPU | i5-8300H | **i5-8300H @ 2.30 GHz** ✅ | Confirmed. 4C/8T. Governor must cap FFmpeg + inference concurrency. |
| RAM | 24 GB | **23.85 GB** ✅ | Confirmed. Usable for one 4B model at 4–8K context + backend + Electron. |
| GPU | GTX 1050 Ti Max-Q 4 GB | **GTX 1050 Ti, 4096 MiB, driver 582.78** ✅ | Pascal (compute 6.1). Modern Ollama/CUDA builds are progressively dropping Pascal-era kernels → **must verify GPU offload in P1.T07 and have a CPU-only fallback profile**. |
| Free storage | 1 TB | **D: 881.7 GB free** · **C: ~30 GB free** | D: is healthy. C: is *tight* — the "never write to C:" rule is now a real safety requirement, not a preference. |
| Node.js | current LTS | **v26.13.1** + npm 11.20.0 | Present but unverified as LTS; Electron ships its own Node so this only affects build tooling → pin via ADR-02. |
| pnpm | required | **absent** | P1.T04. |
| Python | 3.12+ | **3.12.10** ✅ | Good; faster-whisper/ctranslate2 wheels exist for 3.12. |
| Git | required | **2.56.0.windows.2** ✅ | Good. |
| Ollama | required | **absent** | P1.T07. Installer path + `OLLAMA_MODELS` must be set before first pull. |
| FFmpeg/ffprobe | required | **absent** | P1.T03. Must be a licensed build; record source + license. |
| `<PROJECT_ROOT>` | target root | **does not exist** | P1.T02 creates it. |
| Project scratch dir | — | `D:\P` holds only the two blueprint files | Plan artifact lives at `D:\P\CreatorOS_Implementation_Plan.md`. |

**Two facts that change the risk profile:**
1. **C: has only ~30 GB free.** If Electron's userData, Chromium cache, model blobs, or a Python wheel cache land on C:, the machine degrades fast. Storage policy (§10) becomes a tested requirement, and P11.T05 runs a filesystem watcher to prove it.
2. **GTX 1050 Ti is below the practical floor for some current Ollama GPU paths.** Treat "GPU works" as an experiment with a documented result, not a foundation. The app must run acceptably CPU-only.

---

## 1. Architecture decision register (lock these before Phase 1)

Each decision must be written as `docs/ADR/ADR-0NN-*.md` in P0.T02 and marked Accepted by you in P0.

| ADR | Decision | Recommendation | Why / consequence |
|---|---|---|---|
| **ADR-01** | Data root location | `<PROJECT_ROOT>` (blueprint default), user-changeable at first launch via onboarding | Blueprint + 30 GB free on C. Path is stored in a single `app_root` setting; nothing hardcodes it. |
| **ADR-02** | Node toolchain pin | Install Node **24 LTS** side-by-side for build tooling (keep 26 available) and pin `"engines"` + `.nvmrc`; Electron bundles its own runtime | Node 26 is very new; native build deps in the toolchain are the most common "works on my machine" break. Build determinism > novelty. |
| **ADR-03** | Desktop shell | **Electron** (as blueprinted), Tauri recorded as rejected alternative | Mature packaging, tray, DPAPI via `safeStorage`, predictable Chromium rendering for a dense pro UI. Bundle size accepted. |
| **ADR-04** | Python persistence layer | SQLAlchemy 2.x (typed, sync) + **Alembic**, SQLite in WAL mode | Typed models, migration tests, no ORM lock-in surprise. Sync is fine for a single-user local service and simpler than async SQLite. |
| **ADR-05** | 🔒 `TEMP`/`TMP` handling | **Do NOT set global user `TEMP`/`TMP` to D:**. Instead: backend supervises child processes (FFmpeg, whisper, pip) with a per-process `env` pointing at `<DATA_ROOT>\temp`; set only safe caches globally (`OLLAMA_MODELS`, `PIP_CACHE_DIR`, `HF_HOME`, `npm_config_cache`, `PNPM_HOME`) | A machine-wide TEMP redirect breaks Windows installers, other apps and some drivers. Blueprint §7 lists it as a convenience; this plan supersedes it for safety. Must be recorded as an explicit deviation. |
| **ADR-06** | Local API transport & auth | FastAPI on `127.0.0.1:<ephemeral>`, random 32-byte session token minted at launch, passed to renderer through the preload bridge only, required on every route except `/health/live`; strict `Origin` allowlist | 🔒 INV-6. Ephemeral port avoids collisions; token in memory only. |
| **ADR-07** | Token encryption | Windows DPAPI (`CryptProtectData`, user scope) from the **Electron main process** via `safeStorage`; ciphertext blobs in `<DATA_ROOT>\config\secrets\`, DB stores only `SecretRef` + fingerprint | 🔒 INV-3. Keeps plaintext tokens out of SQLite, logs, crash dumps, prompts. Backend asks main process over the authenticated channel to decrypt for a single call. |
| **ADR-08** | Job queue | SQLite table with lease/heartbeat, `run_after`, exponential backoff + jitter, dead-letter, cancellation; interface `QueueBackend` so Redis can be added later | Blueprint §4. No Redis in v1. |
| **ADR-09** | Model library start set | Verify tags at pull time; start with `qwen3:1.7b` (fast) + `qwen3:4b` (reasoning, CPU-or-partial-offload); add `qwen2.5-coder:3b`, `gemma3:4b`, `nomic-embed-text` only after benchmarks | 4 GB VRAM. `qwen3:4b` at 4K ctx may be CPU-bound; measured, not assumed. |
| **ADR-10** | Facebook route | **Pages only**, Graph API, app in Development mode with your account holding a role on the app (private-use path), production/review only if you later publish the app | Meta allows app-role accounts to use unapproved permissions in dev mode; public use requires review. Must be re-verified in P6.T01. |
| **ADR-11** | Instagram route | **Instagram API with Instagram Login** first (`instagram_business_basic`, `instagram_business_content_publish`); Facebook-Login route added only if needed for a linked-Page/PPA requirement | Fewer moving parts for a single-creator tool; exact scope names re-verified in P6.T01. |
| **ADR-12** | TikTok route | **Manual export package is the primary, always-working path.** Content Posting API (Direct Post) is a gated, capability-checked adapter that is disabled unless the app's audit/intended-use status permits it | 🔒 INV-4. TikTok's guidelines exclude a private utility whose only purpose is posting to the developer/team's own accounts. Never promise approval. |
| **ADR-13** | Public endpoints needed by Meta | Meta app review/legal pages require a **public HTTPS privacy policy + data-deletion instructions/callback**. Plan: static policy page + a minimal, data-free deletion-callback endpoint hosted on a free static/edge host; document that no user data leaves the machine | 🔒 INV-2/INV-5. This is the one place the product legitimately needs a public URL. It carries zero user content. |
| **ADR-14** | Code signing | v1 ships unsigned; SmartScreen warning documented in release notes; signing deferred | Cost. Must be stated honestly in the installer UX. |

### Open decisions I need from you (block nothing before Phase 2, but needed by the stated phase)

| # | Question | Needed by | Default if you don't answer |
|---|---|---|---|
| Q1 | Confirm data root `<PROJECT_ROOT>` (not `D:\P\CreatorOS`)? | P1 | `<PROJECT_ROOT>` |
| Q2 | Keep both Node 24 LTS and 26, or uninstall 26? | P1 | Install 24 side-by-side, pin 24 |
| Q3 | Do you already have a Meta developer app / Business portfolio / Facebook Page / IG professional account? | P6 | Assume none; plan starts from app creation |
| Q4 | Do you already have a TikTok developer app with Content Posting API? | P6 | Assume none; manual path ships first |
| Q5 | Are you willing to host 2 static pages + 1 tiny callback publicly (ADR-13)? | P6 | Yes; if no, FB/IG stay in dev-mode-only forever (acceptable for private use) |
| Q6 | Is English-only UI acceptable for v1, with Arabic/French content output? | P9 | English UI + `en/ar/fr` string scaffold, RTL verified |
| Q7 | Cloud AI providers: permanently disabled, or optional behind explicit opt-in? | P3 | Optional but off by default, key stored via DPAPI |
| Q8 | Backup destination (same D: drive, or an external/second drive)? | P10 | `<DATA_ROOT>\data\backups` + exported zip you can copy off-machine |
| Q9 | Retention: how long to keep originals, working files, transcripts, logs? | P5 | Originals forever; working 14 days; logs 30 days; exports until archived |
| Q10 | Do you want Windows notifications + tray autostart, or tray only? | P8/P9 | Tray + notifications, autostart off by default |

---

## 2. Three-track plan (code, approvals, product)

The build is not one line. Three tracks run in parallel; only one of them is code.

**Track A — Engineering (this document, P0→P11).** ~700 h of build + audit work.

**Track B — Platform approvals (start day 1, calendar-bound, 👤 human).** These have external lead times of days-to-weeks and are the #1 schedule risk because they cannot be compressed by working harder.

| ID | Task | Lead time | Blocks |
|---|---|---|---|
| B01 | Create Meta developer app, add Facebook Login + Instagram products | 1 day | P6 Facebook/Instagram live verification |
| B02 | Business verification (if Meta requires it for the permission set you use) | 2–10 business days | IG/FB production permissions |
| B03 | Create Facebook Page + IG professional (Business/Creator) account and confirm you hold the Page role needed to publish | 1 day | P6/P7 |
| B04 | Publish privacy policy + data-deletion page (ADR-13) | 1–2 days | App review, and any public release |
| B05 | Record app-review screencast of the publish flow (only if going public) | 1 day after P7 | Review submission |
| B06 | Create TikTok developer app, add Content Posting API, request `video.publish` | 1–3 days to create | P7 TikTok Direct Post (gated) |
| B07 | TikTok audit/approval attempt + document the outcome honestly | 1–6 weeks | Whether Direct Post is ever enabled |
| B08 | Re-verify all official API docs and record URLs + dates + API versions | ½ day, repeated each phase | P6.T01, P7.T10 |

**Track C — Product decisions (👤 you).** Consent wording, brand voice, hashtag sets, subtitle style, automation rules, the capability matrix labels, and the go/no-go at P11.

---

## 3. Global invariants → enforcement matrix

Every invariant from Blueprint §1/§3/§10 becomes a testable control. This matrix is the anti-drift device: any task that touches a mechanism column must also touch its test.

| ID | Invariant | Enforced by | Proven by |
|---|---|---|---|
| **INV-1** | 111 agents, one model pool, concurrency 1→benchmark | `ResourceGovernor` admission control; catalogue has zero model processes | P3.T08 tests, P4.T14 tests, P11 audit |
| **INV-2** | Local-first AI; external providers off by default | `ModelRouter` has no external branch unless `providers.external.enabled=true` | P3.T09 test: no outbound call when disabled (network mock asserts zero calls) |
| **INV-3** | Official OAuth/API only; no passwords, cookies, scraping | No code path accepts a password; OAuth-only adapters; audited network allowlist | P6.T10 secret scan, P10.T04 external-call enumeration, P11.T04 |
| **INV-4** | Publishing is permission-based, per target | `PublishApprovalGate` + `ApprovalRecord` with payload hash | P7.T05/P7.T12 tests: publish tool without approval raises |
| **INV-5** | Truthful status; never "Published" before platform confirms | Publish state machine + reconciliation; per-target rollup | P7.T12/T13 tests incl. partial failure |
| **INV-6** | D-drive-first storage | `PathPolicy` single write gateway + startup preflight + watcher audit | P1.T12 tests, P11.T05 filesystem diff |
| **INV-7** | Phased, tested shipping | Phase gates + evidence pack | §16 evidence template |
| **INV-8** | Loopback-only, token-authenticated local API | Bind 127.0.0.1, token middleware, Origin allowlist | P2.T03 tests, P10.T03 |
| **INV-9** | Idempotent external actions; no blind retries | `idempotency_key` unique + reconciliation-before-retry | P7.T13 test: simulated timeout then no duplicate |
| **INV-10** | No fake data, no dead buttons, no invented API results | Anti-fake audit (P0.T07) + disabled-with-reason pattern + fixture labeling | P9 gate, P10.T01/T03, P11.T03 dead-button crawl |

---

## 4. Critical path, phase order, and why it differs from the Blueprint's numbering

The Blueprint's prompts are numbered 0–11 in *authoring* order. The build order should differ in two places, because otherwise you spend weeks building an agent engine with no real tools for it to call (a machine for inventing fake success).

```
P0 Constitution
 └─ P1 Workspace + shell + walking skeleton
     └─ P2 Backend/DB/API/queue
         └─ P3 Ollama gateway + governor + Model Lab
             ├─ P5 Media pipeline + Content Studio      ← pulled forward
             │   └─ P6 Accounts + OAuth + capabilities
             │       └─ P7 Variants + scheduler + publish + TikTok fallback
             │           └─ P4 Agent workforce (bind to real tools)   ← moved after P7
             │               └─ P8 Analytics + inbox + automations
             │                   └─ P9 GUI/product polish
             │                       └─ P10 Security + packaging + release
             │                           └─ P11 Independent audit
```

**Changes I recommend (each is a deliberate deviation):**
1. **P5 before P6/P7.** Media is the product. Publishing an unprobed, unexported file is meaningless.
2. **P4 after P7.** The orchestrator's value is calling **real** tools. Built before P6/P7, 100 specialist definitions can only *pretend*. Two P4 slices are pulled forward: the agent-definition schema + catalogue + tool registry (needed as DB seeds in P2) and the task-graph engine (needed by P5/P7 to run multi-step jobs).
3. **GUI is continuous, not a single phase.** P1 ships a real shell + Diagnostics page; every phase lands its own functional screens; P9 is the *polish, coherence, accessibility and completeness* pass over screens that already work.
4. **TikTok manual path is built in P7 alongside Direct Post**, not as an afterthought — because it is the only guaranteed-public path.
5. **P11 is a separate, adversarial pass** that must not trust P0–P10's own claims.

If you prefer to follow the Blueprint's exact prompt order (P4 agents before P5 media), say so and I will renumber the same task set — no task is lost, only the order changes.

---

## 5. Repository and module blueprint (target state)

```
<PROJECT_ROOT>\
  app\
    backend\
      pyproject.toml            # [project] deps + pytest/ruff/mypy config
      .venv\                    # Python 3.12 venv (never in git)
      src\creatoros\
        main.py                 # uvicorn entrypoint, lifecycle, preflight
        core\
          settings.py           # pydantic-settings, app_root resolution
          paths.py              # PathPolicy: the ONLY write gateway (INV-6)
          logging.py            # structlog-style JSON logs + redaction filter
          security.py           # session token, origin check, deps
          errors.py             # error envelope, stable codes
          time.py               # UTC storage / TZ helpers / DST safety
          ids.py                # ulid/uuidv7 ids, idempotency keys
        db\
          session.py            # engine, WAL pragmas, session scope
          base.py               # naming conventions, timestamps mixin
          models\               # 20+ SQLAlchemy models (§6)
          migrations\versions\  # Alembic revisions
          seed\                 # agent catalogue, platform profiles, prompts
        schemas\                # Pydantic request/response/agent IO schemas
        repositories\           # query layer, pagination, filters
        services\
          media\                # intake, probe, thumbnails, render, subtitles
          content\              # projects, variants, briefs, validation
          publishing\           # state machine, dispatch, reconcile, idempotency
          accounts\             # oauth, tokens, capability engine
          analytics\            # collectors, normalization, reports
          community\            # comments, triage, reply drafts
          automation\           # rule engine, dry-run, run history
          backup\               # VACUUM INTO, integrity check, restore
        agents\
          catalog\              # 111 YAML definitions (000–110)
          orchestrator\         # planner, task graph, executor, memory
          tools\                # tool implementations + registry
          prompts\              # versioned prompt templates
        integrations\
          ollama\  meta\  instagram\  tiktok\  ffmpeg\  whisper\
        api\
          deps.py  router.py
          routes\               # ~20 routers (§7)
        workers\                # queue runner, scheduler loop, monitors
      tests\                    # unit / integration / contract / fixtures
    desktop\
      package.json  vite.config.ts  electron-builder.yml
      electron\                 # main.ts, preload.ts, ipc handlers, secrets, sidecar
      src\                      # React app: pages, components, hooks, api client, store
      public\                   # icons, fonts
    shared\
      api-types\                # generated TS types from OpenAPI
      platform-profiles\        # facebook.json / instagram.json / tiktok.json
    installer\                  # NSIS customisation, prerequisites, icons
  data\  models\ollama\  tools\ffmpeg\bin\  cache\  logs\  temp\  config\
  docs\  (ADR, API_RESEARCH, API_SPEC, RUNBOOK, SECURITY, EVIDENCE, THIRD_PARTY)
  installer_builds\
  scripts\ (bootstrap.ps1, diagnose.ps1, preflight.ps1, set_env.ps1, ci.ps1, audit_fake.ps1)
```

**Module rules:** route → service → repository → model (no route touches a model directly); every service is unit-testable without HTTP; every integration adapter exposes the same interface shape (probe/capabilities/execute) so provider mocks are drop-in.

---

## 6. Data model (complete, with the columns that matter)

Blueprint §9 lists 11 entities. This plan implements **28 tables** — the extra ones are what make idempotency, audit and the capability matrix provable rather than aspirational.

### Core content
| Table | Key columns | Notes |
|---|---|---|
| `app_setting` | `key` PK, `value_json`, `updated_at` | Includes `app_root`, model role map, resource limits, retention. |
| `brand_profile` | `id`, `name`, `voice_json`, `banned_terms_json`, `cta_phrases_json`, `hashtag_sets_json`, `subtitle_style_json`, `is_default` | INP-4 output constraints. |
| `campaign` | `id`, `name`, `goal`, `audience_json`, `pillars_json`, `starts_on`, `ends_on` | |
| `media_asset` | `id`, `sha256` **unique**, `source_path`, `original_filename`, `size_bytes`, `duration_ms`, `width`, `height`, `fps`, `video_codec`, `audio_codec`, `rotation`, `bitrate`, `has_audio`, `loudness_json`, `thumbnail_path`, `rights_notes`, `rights_status`, `tags_json`, `archive_state`, `imported_at`, `deleted_at` | `sha256` unique = duplicate detection at intake. |
| `transcript` | `id`, `asset_id` FK, `language`, `model`, `text`, `segments_json`, `srt_path`, `vtt_path`, `edited_at` | |
| `content_project` | `id`, `title`, `brief`, `campaign_id`, `source_asset_id`, `brand_profile_id`, `status`, `transcript_id` | |
| `platform_variant` | `id`, `project_id`, `platform`, `target_account_id`, `output_path`, `aspect_ratio`, `duration_ms`, `width/height`, `caption`, `hashtags_json`, `cta`, `cover_path`, `privacy_setting`, `validation_report_json`, `approval_status`, `updated_at`; **unique(project_id, platform, target_account_id)** | One row per destination. Never a shared caption field. |

### Accounts, secrets, capability
| Table | Key columns | Notes |
|---|---|---|
| `social_account` | `id`, `platform`, `provider_account_id`, `display_name`, `username`, `avatar_url`, `account_type` (page / ig_business / ig_creator / tiktok_creator / personal_unsupported), `parent_page_id`, `scopes_json`, `capabilities_json`, `token_ref_id`, `token_expires_at`, `refresh_expires_at`, `connection_status`, `last_verified_at`, `health_detail`; **unique(platform, provider_account_id)** | |
| `secret_ref` | `id`, `provider`, `key_alias`, `fingerprint`, `cipher`, `dpapi_scope`, `created_at`, `rotated_at`, `revoked_at` | **No ciphertext in SQLite** (INV-3/ADR-07). |
| `capability_snapshot` | `id`, `account_id`, `checked_at`, `docs_version`, `checks_json`, `evidence_ref` | The stored proof behind every capability badge. |
| `oauth_session` | `id`, `platform`, `state`, `code_verifier`, `redirect_uri`, `created_at`, `expires_at`, `consumed_at` | Single-use; PKCE + state validation. |

### Publishing
| Table | Key columns | Notes |
|---|---|---|
| `publish_job` | `id`, `variant_id`, `platform`, `account_id`, `scheduled_at_utc`, `display_timezone`, `job_state`, `attempts`, `idempotency_key` **unique**, `remote_container_id`, `remote_post_id`, `remote_status_json`, `last_error_code`, `last_error_detail`, `next_attempt_at`, `lease_owner`, `lease_expires_at` | INV-9. |
| `publish_attempt` | `id`, `job_id`, `attempt_no`, `started_at`, `finished_at`, `http_status`, `request_id`, `response_code`, `response_excerpt_redacted`, `outcome` | Full forensic trail per try. |
| `approval_record` | `id`, `actor`, `action`, `target_type`, `target_id`, `payload_hash`, `consent_text_version`, `approved_at`, `expires_at`, `result` | Immutable. Hash binds approval to exact content. |
| `webhook_event` | `id`, `platform`, `received_at`, `signature_valid`, `payload_ref`, `processed_at` | Replay-protected. |

### Agents
| Table | Key columns | Notes |
|---|---|---|
| `agent_definition` | `agent_id` PK (`000`–`110`), `name`, `team_id`, `mission`, `prompt_key`, `input_schema_json`, `output_schema_json`, `allowed_tools_json`, `model_profile`, `risk_level`, `approval_requirement`, `timeout_s`, `retry_policy_json`, `quality_checks_json`, `memory_scope`, `catalog_version`, `enabled` | 111 rows exactly; validated on seed. |
| `agent_task` | `id`, `agent_id`, `team_id`, `parent_task_id`, `root_task_id`, `depth`, `input_json`, `output_json`, `state`, `priority`, `retries`, `evidence_refs_json`, `error`, `deadline_at`, `cancel_requested`, `started_at`, `finished_at` | |
| `task_edge` | `from_task_id`, `to_task_id`, `edge_type` (depends/feeds) | Cycle-checked on insert. |
| `agent_run` | `id`, `task_id`, `model_tag`, `prompt_tokens`, `completion_tokens`, `duration_ms`, `tool_calls_json`, `raw_output_ref`, `finish_reason` | Cost + reproducibility. |
| `agent_memory` | `id`, `scope` (task/project/long_term), `owner_id`, `content`, `embedding_ref`, `sensitivity`, `approved_at`, `expires_at` | Curator-gated; delete controls. |
| `prompt_template` | `key`, `version`, `body`, `schema_json`, `active`, `created_at` | Versioned; never edited in place. |

### Ops
| Table | Key columns | Notes |
|---|---|---|
| `queue_job` | `id`, `queue`, `job_type`, `payload_json`, `priority`, `state`, `attempts`, `max_attempts`, `run_after`, `lease_owner`, `lease_expires_at`, `last_error` | INV-9 durability. |
| `audit_event` | `id`, `ts`, `actor`, `action`, `resource_type`, `resource_id`, `outcome`, `metadata_redacted_json`, `correlation_id` | Append-only. |
| `notification` | `id`, `severity`, `title`, `body`, `route`, `dedupe_key`, `read_at` | |
| `analytics_snapshot` | `id`, `platform`, `account_id`, `post_id`, `metric`, `value`, `unit`, `captured_at`, `source`, `completeness`, `sample_size`; **unique(platform, post_id, metric, hour_bucket)** | INV-5/§8: no invented numbers. |
| `automation_rule` / `automation_run` | rule graph JSON, `approval_required`, `enabled`, `version`; run state + result | Dry-run supported. |
| `model_profile` / `model_benchmark` | `ollama_tag`, `context_len`, `num_gpu`, `roles_json`, `verified_at`, `disk_bytes`; `tokens_per_sec`, `ttft_ms`, `vram_peak`, `ram_peak` | INV-1 evidence. |
| `diagnostics_run` | `id`, `ts`, `results_json`, `severity_summary` | Supports "copy diagnostics". |

**Cross-cutting DB rules:** every table gets `created_at`/`updated_at` (UTC, `TEXT` ISO-8601 or `INTEGER` epoch-ms — pick one, ADR-04 fixes it); foreign keys ON; every FK indexed; enums as `CHECK` constraints or lookup tables (never free strings); migrations are forward-only in CI plus tested downgrade for the last revision; `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`.

---

## 7. API surface (complete list, grouped by router)

All routes: JSON, token-authenticated, stable error envelope `{code, message, details, correlation_id}`, paginated where a collection can grow.

| Router | Routes |
|---|---|
| `system` | `GET /health/live`, `GET /health/ready`, `GET /diagnostics`, `GET /version`, `POST /diagnostics/bundle` |
| `settings` | `GET/PATCH /settings`, `GET /settings/paths`, `POST /settings/paths/validate`, `GET/PUT /settings/model-roles`, `GET/PUT /settings/resource-limits` |
| `media` | `POST /media/import` (multipart, streamed), `GET /media` (filter/sort/page), `GET /media/{id}`, `PATCH /media/{id}`, `DELETE /media/{id}`, `POST /media/{id}/probe`, `POST /media/{id}/thumbnails`, `GET /media/{id}/stream`, `GET /media/{id}/transcript`, `PUT /media/{id}/transcript`, `POST /media/{id}/transcript/run`, `GET /media/{id}/subtitles.{srt,vtt}` |
| `projects` | `CRUD /projects`, `POST /projects/{id}/brief`, `GET /projects/{id}/variants`, `POST /projects/{id}/variants`, `PATCH /variants/{id}`, `POST /variants/{id}/render`, `POST /variants/{id}/validate`, `POST /variants/{id}/caption/generate` |
| `accounts` | `GET /accounts`, `GET /accounts/{id}`, `POST /accounts/oauth/{platform}/start`, `GET /accounts/oauth/callback`, `POST /accounts/{id}/verify`, `POST /accounts/{id}/disconnect`, `DELETE /accounts/{id}`, `GET /accounts/{id}/capabilities` |
| `publish` | `POST /publish/approve`, `POST /publish/queue`, `GET /publish/jobs`, `GET /publish/jobs/{id}`, `POST /publish/jobs/{id}/cancel`, `POST /publish/jobs/{id}/retry`, `POST /publish/jobs/{id}/reconcile`, `POST /publish/jobs/{id}/manual-complete`, `GET /publish/calendar`, `PATCH /publish/jobs/{id}/schedule`, `POST /publish/manual-package/{variant_id}` |
| `agents` | `GET /agents/catalog`, `GET /agents/{agent_id}`, `POST /agents/tasks` (goal), `GET /agents/tasks`, `GET /agents/tasks/{id}` (+ graph), `POST /agents/tasks/{id}/cancel`, `GET /agents/runs/{id}`, `GET/PUT /agents/memory`, `DELETE /agents/memory/{id}` |
| `models` | `GET /models/installed`, `GET /models/library`, `POST /models/pull`, `DELETE /models/{tag}`, `POST /models/{tag}/unload`, `POST /models/{tag}/benchmark`, `GET /models/benchmarks`, `GET /models/status`, `GET /models/disk` |
| `analytics` | `GET /analytics/overview`, `GET /analytics/posts`, `GET /analytics/metrics/{metric}`, `POST /analytics/sync`, `GET /analytics/export.csv`, `GET /analytics/completeness` |
| `community` | `GET /community/items`, `GET /community/items/{id}`, `POST /community/items/{id}/classify`, `POST /community/items/{id}/reply-draft`, `POST /community/items/{id}/send-reply`, `POST /community/items/{id}/escalate` |
| `automations` | `CRUD /automations`, `POST /automations/{id}/dry-run`, `POST /automations/{id}/enable` and `/disable`, `GET /automations/{id}/runs` |
| `queue` | `GET /queue/stats`, `GET /queue/jobs`, `POST /queue/jobs/{id}/cancel`, `POST /queue/drain` |
| `audit` | `GET /audit`, `GET /audit/{id}` |
| `events` | `GET /events/stream` (SSE: job/task/model/progress) |
| `oauth-callback` | `GET /integrations/callback/{platform}` (loopback, state-checked) |

---

## 8. Agent system specification

### 8.1 Definition contract (all 111 rows identical shape)

```yaml
agent_id: "024"
name: "Caption Writer"
team_id: "002"
mission: "Draft platform-appropriate captions from an approved brief and transcript."
prompt_key: "copy.caption_writer"
input_schema:
  type: object
  required: [brief, platform, brand_voice]
  properties:
    brief: {type: string}
    platform: {enum: [facebook, instagram, tiktok]}
    brand_voice: {type: object}
    transcript_excerpt: {type: string}
    constraints: {type: object}
output_schema:
  type: object
  required: [caption, hashtags, cta, rationale]
  properties:
    caption: {type: string, maxLength: 2200}
    hashtags: {type: array, items: {type: string}, maxItems: 30}
    cta: {type: string}
    rationale: {type: string}
    used_evidence: {type: array, items: {type: string}}
    uncertainty: {type: array, items: {type: string}}
allowed_tools: ["content.read_project", "brand.read_profile", "transcript.read"]
model_profile: "fast_worker"
risk_level: "low"            # low | medium | high
approval_requirement: "none" # none | on_write | always_human
timeout_s: 90
retry_policy: {max_attempts: 2, backoff: "exponential", on: ["schema_invalid", "timeout"]}
quality_checks: ["caption_within_limit", "no_banned_terms", "hashtags_relevant", "no_fabricated_claims"]
memory_scope: "project"
```

**Validation script (P4.T02) enforces:** exactly 111 definitions; ids `000`+`001`–`010`+`011`–`110` unique and contiguous; team_id points at an existing leader; every `allowed_tools` entry exists in the Tool Registry; every schema compiles (Draft 2020-12); `timeout_s` in [15, 900]; no prompt body contains secrets patterns or non-Latin code identifiers; catalogue version recorded.

### 8.2 Tool Permission Registry (the only way an agent touches the world)

| Tool | Risk | Approval | Typical allowed roles |
|---|---|---|---|
| `media.read_asset`, `media.probe`, `transcript.read` | read | none | 031–040 |
| `media.thumbnail`, `media.render_variant` | write (local) | none (bounded to exports/) | 031–040 |
| `transcript.run_whisper` | compute | none, queue-limited | 034, 035 |
| `content.read_project`, `brand.read_profile`, `strategy.read` | read | none | most copy/strategy roles |
| `content.write_variant` | write | none (draft only) | 021–030 |
| `accounts.read_capabilities` | read | none | 04x–07x |
| `publish.*` | **external** | **always_human** | only 045/056/065, and only via the gate |
| `community.send_reply` | **external** | **always_human** | 074/078 |
| `analytics.read_remote` | external-read | none (rate-limited) | 047/048/057/058/081 |
| `queue.enqueue`, `notify.user`, `audit.write` | write | none | 101–110 |
| `secrets.read` | — | **denied to all agents** | nobody |
| `fs.read_anywhere`, `shell.exec` | — | **not implemented** | nobody |

Mechanism: tools are looked up by name, the call is checked against `agent_definition.allowed_tools`, risk class, and `approval_requirement`; high-risk calls insert an `ApprovalRecord` and pause the task in `awaiting_approval`. The Chief cannot override. **Test:** attempting `publish.instagram` as agent 021 must raise `ToolPermissionDenied`, and attempting it as agent 056 without an approval must raise `ApprovalRequired`.

### 8.3 Orchestration flow and hard limits

```
user goal → Chief plan (JSON, schema-validated)
          → validate: ≤12 subtasks, depth ≤3, no cycles, every tool permitted, budget ≤ N tokens / T minutes
          → assign to team leaders → leaders decompose → specialists
          → specialists return schema-valid output + evidence_refs
          → leader consolidates (contradictions surfaced, not hidden)
          → Chief merges into a draft for HUMAN review
          → publish only via Approval Gate
```

Limits (defaults, configurable): `max_tasks_per_request=40`, `max_delegation_depth=3`, `max_fanout_per_leader=10`, `wall_clock_budget=10 min`, `token_budget=60k`, `max_retries=2`. Exceeding a limit → task `needs_human_review`, never silent truncation.

### 8.4 Memory rules

Short-term = task input/output only. Project memory = briefs, approved captions, brand facts. Long-term = only curator-approved items, embedded with `nomic-embed-text`, filtered for PII/secrets/sensitive inferences, with per-item delete and a global retention setting. **Test:** feeding a fake token pattern and an email into the curator yields zero stored rows.

---

## 9. Publish state machine (canonical, every platform)

```
draft
  → validation            (media profile + caption + account capability checks)
  → awaiting_approval     (user consent recorded with payload_hash)
  → scheduled | queued
  → uploading             (lease held, heartbeat)
  → processing            (remote container/processing state)
  → published             (remote id confirmed)
  terminal failures: failed | needs_attention | manual_publish_required | cancelled
  uncertain: needs_reconciliation  → back to processing/published/failed after remote check
```

| Transition | Guard | On violation |
|---|---|---|
| validation→awaiting_approval | `validation_report.ok == true` | stay in draft with reasons |
| awaiting_approval→queued | valid `ApprovalRecord` whose `payload_hash` matches current variant | block + `ApprovalStale` |
| queued→uploading | capability snapshot eligible + lease acquired | `CapabilityBlocked` |
| uploading→processing | remote id received | `needs_reconciliation` on timeout |
| processing→published | remote status terminal-success | stay in processing with poll schedule |
| retry from `needs_reconciliation` | remote status ≠ published | `DuplicatePublishBlocked` |
| any→cancelled | no remote side effect, or remote cancelled | `needs_attention` |

**Master project status** = worst-of per target with explicit labels (`Partially published: 2/3`), never a global green. **Test (P7.T13):** three variants, FB succeeds, IG times out post-acceptance, TikTok blocked → after reconciliation: FB `published`, IG `published` (found remotely, no duplicate), TikTok `manual_publish_required`; master shows 2 published + 1 manual.

---

## 10. D-drive / storage policy (INV-6)

| Rule | Implementation |
|---|---|
| One root | `app_root` from `config/bootstrap.json` (created on first run), overridable by `CREATOROS_ROOT`. All paths derived from it. |
| One write gateway | `core/paths.py::resolve_under_root(kind, *parts)` — the only function that returns a writable path. It rejects `..`, absolute escapes, symlinks out of root, UNC paths. |
| Redirects | `OLLAMA_MODELS`, `PIP_CACHE_DIR`, `HF_HOME`, `npm_config_cache`, `PNPM_HOME`, `ELECTRON_USER_DATA` (`--user-data-dir=<DATA_ROOT>\cache\electron`), whisper model cache. Per-process `TEMP` for FFmpeg/whisper/pip child processes (ADR-05). |
| Free-space guard | Before import/pull/render: require `free > needed * 2` (configurable); else block with an actionable message. Warning thresholds at 20 GB / 5 GB. |
| Quota + cleanup | Working files TTL (default 14 d), orphan sweep, export archive flow, DB VACUUM after large deletes. |
| Proof | `P11.T05`: run the full journey under a filesystem watcher (`Process Monitor` or a Python watchdog) and diff writes outside `<PROJECT_ROOT>`. Expected exceptions must be listed (e.g. `%LOCALAPPDATA%\Temp` entries from Chromium startup) and either redirected or documented. |

---

## 11. Security, privacy, reliability controls

| Control | Implementation | Verification |
|---|---|---|
| Loopback bind | `uvicorn --host 127.0.0.1 --port 0`, port written to a lock file with 0600-equivalent ACL | attempt connect from LAN IP fails (P2.T03) |
| Session token | 32 random bytes per launch, memory only, in `Authorization: Bearer`; SSE via short-lived ticket | 401 matrix test |
| Origin/CORS | exact allowlist (`app://creatoros`, dev `http://localhost:5173`); no wildcard | P2.T03 |
| CSP / Electron | `contextIsolation:true`, `nodeIntegration:false`, `sandbox:true`, preload allowlist, CSP with no `unsafe-eval`, block navigation & new windows | P10.T03 |
| Token storage | DPAPI via main-process `safeStorage`, ciphertext in `config\secrets\`, DB stores ref+fingerprint | DB binary grep finds no token patterns (P6.T10) |
| Log redaction | filter on keys (`token\|secret\|authorization\|code\|cookie\|password`) + regex for `EAAG…`, `act.`, JWT; never log request bodies for token endpoints | redaction unit tests with synthetic secrets |
| Secrets scanning | `gitleaks` + custom grep in CI and pre-commit | CI job |
| Subprocess safety | FFmpeg/ffprobe via `subprocess.run([...])` lists only; no `shell=True`; semgrep rule bans it | semgrep + unit test asserting argv shape |
| Upload validation | size cap, extension allowlist, magic-byte sniff, ffprobe must parse, reject >4K/>2h/>8 GB by default | fixture matrix |
| SSRF | outbound allowlist per adapter (only known API hosts); no user-supplied URLs to backend | P10.T03 |
| Rate limits | per-account token bucket + provider `Retry-After` + jitter | P7.T07 tests |
| Idempotency | unique key + reconcile-before-retry | P7.T13 |
| Audit | every external call, approval, account change, deletion | P7.T16, P8 tests |
| Backups | `VACUUM INTO` + `PRAGMA integrity_check`, encrypted optional, restore drill | P10.T08 test |
| No telemetry | no analytics SDKs; crash data local only | dependency + network audit |

---

## 12. Testing, CI and observability strategy

**Test pyramid**
- **Unit (fast, the bulk):** schemas, validators, state machine guards, path policy, redaction, idempotency, scheduler DST math, governor admission.
- **Integration (real SQLite + real services, fake HTTP):** migrations up/down, queue durability across process restart, publish flows against recorded provider fixtures, media pipeline on 5-second generated clips.
- **Contract:** OpenAPI schema snapshot tests + generated TS client type-checks against the backend (fails the build if they drift).
- **E2E (Playwright-Electron):** launch packaged dev app, walk Dashboard→Import→Studio→Variant→Approve→Publish(mock), assert no console errors and no fake states.
- **Smoke (installed build):** clean Windows profile, launch from Start Menu, verify D-drive writes, exit, uninstall preserves data.
- **Chaos/recovery:** kill mid-upload, mid-queue, disk-full, Ollama down, DB locked, network loss.

**Coverage targets:** 80% line on `services/` and `agents/`; 100% of state-machine transitions and every capability state covered by at least one test; zero uncovered "external action" code paths.

**CI:** GitHub Actions on `windows-latest`, no paid services, no live API calls. Jobs: `lint` (ruff, mypy, eslint, tsc), `test-backend` (pytest + coverage gate), `test-desktop` (vitest), `contract` (schema drift), `build` (`electron-builder --dir`), `audit` (gitleaks, pip-audit, npm audit, semgrep). Local parity via `scripts/ci.ps1`.

**Observability:** JSON logs to `logs\backend\YYYY-MM-DD.log` (rotating 10 MB × 10), correlation ids threaded from UI action → API request → queue job → agent task → provider call; a Diagnostics page renders preflight results, queue depth, model state, disk headroom; `POST /diagnostics/bundle` zips redacted logs + versions + settings for support.

---

## 13. Estimates at a glance

These figures are **computed from the per-task estimates in §PHASE 0–11**, which are authoritative. If you change a task estimate, this table must be regenerated (the closing audit in §20 repeats the same total).

| Phase | Tasks | Est. hours | Calendar (at ~28 productive h/wk with AI assistance) |
|---|---:|---:|---|
| P0 Constitution & governance | 10 | 11.5 | 0.5 wk |
| P1 Workspace, toolchain, D-drive, walking skeleton | 16 | 46 | 1.6 wk |
| P2 Backend, DB, API, durable queue | 25 | 103 | 3.7 wk |
| P3 Ollama gateway, Model Lab, resource governor | 15 | 61 | 2.2 wk |
| P5 Media intake & Content Studio | 16 | 78 | 2.8 wk |
| P6 Accounts, OAuth, capability engine | 14 | 66 | 2.4 wk |
| P7 Variants, scheduler, publishing, TikTok fallback | 17 | 98 | 3.5 wk |
| P4 Agent workforce (111 roles, orchestration) | 15 | 86 | 3.1 wk |
| P8 Analytics, inbox, automations | 12 | 59 | 2.1 wk |
| P9 GUI/product polish & accessibility | 18 | 100 | 3.6 wk |
| P10 Security, testing, packaging, release | 17 | 80 | 2.9 wk |
| P11 Independent audit | 9 | 41 | 1.5 wk |
| **Total** | **184** | **≈830 h** | **≈30 weeks** |

Add ~25% contingency for the unknowns that always exist on a 4 GB Pascal GPU laptop and against three moving platform APIs → **plan for ~37 weeks of engineering**, with Track B approvals running in parallel from week 1. **MVP** (Milestones A–C: P0–P3, P5–P7) is ≈464 h ≈ **17 weeks**, ≈**21 weeks** with contingency. P4–P11 (the agent workforce, analytics, polish, packaging and audit) is the remaining ≈366 h.

---

## PHASE 0 — Constitution, governance and decision records
**Blueprint prompt:** 0 · **Recommended slot:** 1st · **Est:** 10 h · **Owner:** agent + 👤 you (ADR sign-off)

**Goal:** convert the blueprint's principles into enforceable repo artifacts *before* any code exists, so later phases cannot drift. No application code in this phase.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P0.T01 | Write `docs/CONSTITUTION.md` | Restate the 10 non-negotiables as R1–R10 with: statement, the file/module that enforces it, the test id that proves it. Cross-reference the invariant matrix in §3. | File + table with zero empty "enforced by" cells | — | 2 |
| P0.T02 | ADR framework | `docs/ADR/_template.md` + ADR-01…ADR-14 from §1, each with Context / Decision / Consequences / Status | 14 ADR files; 11 Accepted by default, ADR-05/10/12/13 need your sign-off | T01 | 2 |
| P0.T03 | Agent operating instructions | Root `AGENTS.md`: repo conventions, "no dead buttons", evidence requirement, anti-fake rules, commit format, phase-stop rule | File committed | — | 1 |
| P0.T04 | Repo hygiene files | `.editorconfig`, `.gitattributes`, `.gitignore` (node_modules, .venv, data/, models/, logs/, temp/, cache/, installer_builds/, *.db), `LICENSE` placeholder, `NOTICE`, `CODEOWNERS` | `git status` clean after a full build | — | 1 |
| P0.T05 | Feature status vocabulary | `docs/FEATURE_MATRIX.md` seeded with every feature row and exactly one status: `Available`, `Requires Meta approval`, `Requires TikTok approval`, `Requires eligible account`, `Manual fallback`, `Not implemented`. This file is the **only** source of UI status labels. | File + rule in AGENTS.md that UI strings import from it | — | 1 |
| P0.T06 | Evidence template | `docs/EVIDENCE_TEMPLATE.md`: (a) files changed, (b) decisions, (c) commands run, (d) test results verbatim, (e) remaining defects, (f) launch instructions, (g) functional vs not-implemented | File | — | 1 |
| P0.T07 | Anti-fake audit script skeleton | `scripts/audit_fake.ps1`: greps `TODO\|FIXME\|XXX\|HACK\|mock\|fake\|placeholder\|dummy\|lorem`, plus `except: pass`, `catch {}`, and "disabled without a reason" heuristic (`disabled` without `title=`/`reason=`). Outputs file:line + classification. | Script runs on an empty repo, exits 0 | — | 1 |
| P0.T08 | `docs/SECURITY.md` | Threat model: local attacker, malicious media file, prompt injection via transcript/captions, model output weaponised into tool calls, token theft, path escape, supply chain. One mitigation + one test per threat. | File | — | 1 |
| P0.T09 | Git bootstrap | `git init`, conventional commits, branch scheme (`phase/pN-*`), tags `v0.x.0-phaseN` | `git log --oneline`; first tag | — | 0.5 |
| P0.T10 | Standards & error envelope | `docs/CODING_STANDARDS.md`: Python (ruff/black/mypy on new modules), TS (strict, no `any` in exported types), naming, log fields, error envelope + stable code list, timezone policy (UTC in DB, TZ at edges) | File; error-code table shared by backend and desktop | — | 1 |

**Exit gate**
- ☐ Every R1–R10 names its enforcement module and its reserved test id.
- ☐ ADRs exist; ADR-05, ADR-10, ADR-12, ADR-13 marked **Accepted** by you.
- ☐ `scripts/audit_fake.ps1` executes.
- ☐ Clean initial commit; no `data/`, `models/`, `.venv`, `node_modules` tracked.
- ☐ Q1–Q10 answered or explicitly deferred.

**Phase risks:** spending a week writing documents instead of decisions (timebox P0 to one day); ADRs written as marketing prose rather than decisions with consequences.

---

## PHASE 1 — Workspace, toolchain, D-drive foundation, walking skeleton
**Blueprint prompt:** 1 · **Recommended slot:** 2nd · **Est:** 34 h

**Goal:** a reproducible Windows dev environment on D:, plus a real Electron window that starts the real backend and shows **real** diagnostics. After this phase every later phase adds muscle to a body that already stands.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P1.T01 | `scripts/preflight.ps1` | JSON + human table: OS build, arch, CPU, RAM, GPU name/VRAM/driver, per-drive free space, versions of node/npm/pnpm/python/git/ollama/ffmpeg/ffprobe, WebView2/VC++ presence, listeners on 11434/8000/5173. No secrets. Non-zero exit only on hard blockers. | Saved JSON matching §0 | P0 | 3 |
| P1.T02 | Folder tree creation | Idempotent creation of every §5 folder; write `<DATA_ROOT>\.creatoros-root` marker; verify write permission by create+delete in each root; refuse if free space < 20 GB. | Second run changes nothing; `Test-Path` matrix all true | T01 | 2 |
| P1.T03 | FFmpeg install + license record | Choose a build (gyan.dev essentials or BtbN), place in `tools\ffmpeg\bin`, verify `-version`, record source URL, version, build options and **license obligations** in `docs/THIRD_PARTY.md` (GPL build → redistribution constraints). | `ffmpeg -hide_banner -version` + THIRD_PARTY entry | T02 | 2 |
| P1.T04 | pnpm via corepack | `corepack enable`; pin `packageManager`; store-dir to `<DATA_ROOT>\cache\pnpm-store` | `pnpm store path` prints D: | T02 | 1 |
| P1.T05 | Node pin + engines | Node 24 LTS side-by-side (ADR-02); `.nvmrc` + `engines`; document switching | `pnpm install` under pinned version; lockfile committed | T04 | 2 |
| P1.T06 | Python venv + deps | venv at `app\backend\.venv` on 3.12; `pyproject.toml` base deps (fastapi, uvicorn[standard], sqlalchemy, alembic, pydantic-settings, httpx, python-multipart, psutil, jinja2, structlog) + dev deps (pytest, pytest-asyncio, pytest-cov, ruff, mypy, respx) + optional `ai` group (faster-whisper, numpy, opencv-python-headless); pip cache on D | `pip list` snapshot; venv on D | T02 | 3 |
| P1.T07 | Ollama install to D: + GPU reality check ⭐ | Install to `<DATA_ROOT>\Programs\Ollama` (`/DIR=`), set `OLLAMA_MODELS`, restart, verify blob path, then **test GPU offload empirically**: pull `qwen3:0.6b`, run verbose, record layers on GPU vs CPU on Pascal/CC 6.1 + driver 582.78 with current Ollama. Verdict: `gpu_full` / `gpu_partial` / `cpu_only` → sets default `num_gpu` in ADR-09. | `ollama list`, `ollama ps` split, blobs on D, written verdict | T02 | 4 |
| P1.T08 | Env var script with revert | `scripts/set_env.ps1 -Apply\|-Revert\|-DryRun` for SAFE vars only (`OLLAMA_MODELS`, `PIP_CACHE_DIR`, `HF_HOME`, `npm_config_cache`, `PNPM_HOME`). **Does not touch global TEMP/TMP** (ADR-05) and prints why. Backs up prior values to `config\env_backup.json`. | Dry-run diff; revert restores exact previous values | T02 | 2 |
| P1.T09 | Backend package skeleton | Full `src/creatoros` tree per §5; `main.py` serves `/health/live`; settings from `config/` + env; `python -m creatoros` entry | uvicorn starts; curl returns JSON | T06 | 3 |
| P1.T10 | Desktop skeleton | Electron + Vite + React + TS strict + Tailwind + shadcn-style primitives; `main.ts`, preload contextBridge allowlist only, renderer with CSP; `pnpm dev` opens a window | Window opens; `tsc --noEmit` clean; no Node APIs in renderer | T05 | 5 |
| P1.T11 | Walking skeleton wiring ⭐ | Main: pick free port → spawn backend with per-process env (TEMP on D) → poll `/health/ready` 30 s → show app, or a diagnostic panel with the last 50 log lines + retry. Quit kills the child tree. Backend logs to `logs\backend\`. | Cold start to live health; backend killed → honest offline state; no orphan python.exe | T09, T10 | 5 |
| P1.T12 | `PathPolicy` foundation (INV-6) | `core/paths.py::resolve_under_root(kind, *parts)` with kind enum, rejects `..`, absolute paths, symlink escapes, UNC, reserved names; unit tests incl. `\?\`, trailing dots, `CON`, 8.3 aliases | 25+ pytest cases green | T09 | 4 |
| P1.T13 | `scripts/bootstrap.ps1` | preflight → folders → deps → env(dry-run default) → backend deps → desktop deps → doctor; `-WhatIf`, `-Resume`; every step idempotent and logged | Two consecutive runs: second is a no-op | T01–T12 | 3 |
| P1.T14 | `scripts/diagnose.ps1` + Diagnostics page | Versions, D: free space, per-folder writability, Ollama health, GPU visibility, ffmpeg path+version, DB presence/integrity, secret store health, pending migrations, queue depth. Never prints secrets. Exit codes 0/1/2. | Real output + screenshot | T13 | 3 |
| P1.T15 | README + RUNBOOK | Fresh-machine setup, launch (`pnpm dev`, `pnpm start`), reset, move data root, troubleshooting table, ADR-05 TEMP note | A second person can follow it end to end | T13 | 2 |
| P1.T16 | Fresh-shell verification | New terminal (optionally reboot) → `pnpm dev` works with zero manual steps; no unexpected writes under `%USERPROFILE%` | Recorded walkthrough + localappdata diff | all | 2 |

**Exit gate**
- ☐ Every §5 folder exists on D: and is writable; second bootstrap run is a no-op.
- ☐ `nvidia-smi`, `ollama list`, `ffmpeg -version`, `python -V`, `node -v`, `pnpm -v` pass from a fresh shell.
- ☐ Walking skeleton shows **real** health and diagnostics values.
- ☐ Killing the backend produces an honest offline state — not a spinner, not fake success.
- ☐ Zero model/DB/media files on C:.
- ☐ `PathPolicy` suite green including traversal and reserved-name cases.
- ☐ ADR-05 and ADR-09 updated with measured facts.

**Phase risks:** GTX 1050 Ti may be CPU-only (T07 decides early); FFmpeg licensing (T03); Node 26 native-module breakage (T05); orphan Python processes on quit (T11 test); Max-Q thermals during long work.

---

## PHASE 2 — Backend, database, API and durable queue
**Blueprint prompt:** 2 · **Recommended slot:** 3rd · **Est:** 78 h

**Goal:** the persistent spine. Every later feature is a service on top of this. No social publishing in this phase — by design.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P2.T01 | Settings & config system | `pydantic-settings` models for app, paths, security, resources, models, providers; layering env > `config\settings.json` > defaults; validation with helpful errors; injectable (no module-level reads) | Unit tests for precedence + invalid values | P1 | 3 |
| P2.T02 | Structured logging + redaction | JSON lines, correlation id, rotating handler (10 MB × 10) on D:, redaction filter on keys and value patterns, request middleware with body-size cap and token-endpoint body suppression | Redaction tests with synthetic `EAAG…`, JWT, `act.`; log sample | T01 | 4 |
| P2.T03 | Security middleware (INV-8) | Bind 127.0.0.1 only; bearer-token dependency; Origin/Referer allowlist; exact-match CORS; body size limits; identical 404/405 for unauthenticated probes; security headers | Tests: 401 matrix, bad origin 403, LAN-IP connect refused, oversized body rejected | T01 | 5 |
| P2.T04 | Session token minting | 32-byte urandom per launch, written to `config\runtime\session.json` with restrictive ACL, deleted on shutdown; backend rejects tokens from a previous process | Stale-token test; ACL verified | T03 | 2 |
| P2.T05 | DB base + conventions | Engine with WAL / busy_timeout / foreign_keys; `Base` naming conventions; UTC timestamp mixin; uuidv7/ULID ids; enum-as-CHECK helper | `PRAGMA` output; conventions test | T01 | 3 |
| P2.T06 | Alembic wiring | `alembic.ini` + `env.py` reading runtime settings; offline/online modes; autogenerate drift guard; migrations live on D: | `alembic upgrade head` on empty DB; drift check in CI | T05 | 3 |
| P2.T07 | Migration 0001 — content core | `app_setting`, `brand_profile`, `campaign`, `media_asset`, `transcript`, `content_project`, `platform_variant` with FKs, uniques, indexes (§6) | Applies; schema dump; rollback test | T06 | 5 |
| P2.T08 | Migration 0002 — accounts/secrets | `social_account`, `secret_ref`, `capability_snapshot`, `oauth_session` | Same | T07 | 3 |
| P2.T09 | Migration 0003 — publishing | `publish_job`, `publish_attempt`, `approval_record`, `webhook_event`, unique `idempotency_key` | Same + duplicate-key test | T08 | 4 |
| P2.T10 | Migration 0004 — agents/queue/ops | `agent_definition`, `agent_task`, `task_edge`, `agent_run`, `agent_memory`, `prompt_template`, `queue_job`, `audit_event`, `notification`, `analytics_snapshot`, `automation_rule`, `automation_run`, `model_profile`, `model_benchmark`, `diagnostics_run` | Same | T09 | 6 |
| P2.T11 | Repository layer | Generic `Repository[T]` + specific repos; cursor + offset pagination; allowlisted filtering/ordering; no user-supplied SQL fragments | Repo tests with hostile inputs | T07–T10 | 5 |
| P2.T12 | Error envelope + handlers | Stable codes (`AUTH_REQUIRED`, `VALIDATION_FAILED`, `NOT_FOUND`, `CONFLICT`, `RATE_LIMITED`, `DEPENDENCY_UNAVAILABLE`, `PERMISSION_DENIED`, `APPROVAL_REQUIRED`, `NOT_IMPLEMENTED`, `INTERNAL`), Pydantic mapping, correlation echo, no stack traces to client | Contract test per code | T03 | 3 |
| P2.T13 | `/health`, `/diagnostics`, `/version` | Liveness vs readiness (DB, migrations, disk, Ollama soft); diagnostics aggregation service reused by the page | Endpoint tests + real output | T12 | 4 |
| P2.T14 | Audit service | Append-only writer (actor, resource, outcome, redacted metadata, correlation id) + decorator + filtered query API; no update/delete route | Tests on create/update/delete | T10 | 3 |
| P2.T15 | Settings/paths/media CRUD routes | Typed schemas, validation, pagination; media records only (no intake yet); paths validated via `PathPolicy` | Route tests + OpenAPI snapshot | T11 | 6 |
| P2.T16 | Projects/variants CRUD routes | Includes unique(project, platform, account) conflict handling | Same | T15 | 5 |
| P2.T17 | Agent definition/task routes | Catalogue read, task create/read/cancel; execution returns `NOT_IMPLEMENTED` until P4 and the UI must say so | Route tests; 501 semantics | T10 | 4 |
| P2.T18 | Queue routes + stats | Stats by state/queue, listing, cancel, drain (stop claiming only) | Tests | T19 | 3 |
| P2.T19 | Durable queue engine ⭐ (INV-9 durability) | Table-backed: priority + `run_after`; atomic claim (`UPDATE … WHERE state='queued' … RETURNING`); lease + heartbeat; visibility-timeout recovery; exponential backoff with jitter; max attempts → dead letter; idempotency-key dedupe; cancellation checkpoints; `QueueBackend` interface | 500-job drain; worker crash re-claims after lease; exact backoff schedule; dedupe; cancel mid-run | T10 | 9 |
| P2.T20 | Worker runner + graceful shutdown | Async loop, concurrency from governor, SIGINT/SIGTERM drain, per-job timeout, structured job logs, queue-depth metric | Shutdown mid-job returns the job to the queue | T19 | 5 |
| P2.T21 | Backup service | `VACUUM INTO` timestamped copy + `PRAGMA integrity_check` + SHA-256 + manifest; retention N; restore drill into a temp DB with row-count verification | Both tests green; backup file on D: | T10 | 4 |
| P2.T22 | Contract tests + OpenAPI export | Snapshot per route; `docs/API_SPEC.md` generated; TS type generation into `app/shared/api-types` with drift check | `pnpm gen:api && git diff --exit-code` | T15–T18 | 4 |
| P2.T23 | Migration up/down + seed harness | Empty → head → base → head; seed fixtures namespaced `test_only` | CI job green | T06–T10 | 3 |
| P2.T24 | Startup preflight self-check | Root exists+writable, migrations current, disk headroom, ffmpeg present, Ollama reachable (soft), session token fresh; writes `diagnostics_run`; readiness 503 with reasons on hard failure | Test per failure mode | T13 | 3 |
| P2.T25 | Test suite baseline | pytest config, coverage gate 80% on services/repositories, respx, factories, temp-root isolation (never touches the real data root) | `pytest -q` output + coverage report | all | 4 |

**Exit gate**
- ☐ Empty → `alembic upgrade head` → down → up, all clean in one command.
- ☐ All 28 tables exist with FKs, indexes, uniques; schema dump committed.
- ☐ Full pytest suite green; coverage ≥ 80% on `services/` and `repositories/`.
- ☐ Unauthenticated → 401; wrong Origin → 403; LAN connect → refused.
- ☐ Killing the process mid-job leaves the job re-claimable and it completes after restart.
- ☐ No synthetic secret planted by the redaction test appears in logs.
- ☐ Every write goes through `PathPolicy`; no file appears outside the temp root in tests.
- ☐ `docs/API_SPEC.md` and generated TS types are in sync.

**Phase risks:** SQLite write contention between queue and UI reads (WAL, short transactions, single writer path, `busy_timeout`); Alembic drift; repositories leaking raw enum strings; tests accidentally hitting the real data root.

---

## PHASE 3 — Ollama gateway, Model Lab and resource governor
**Blueprint prompt:** 3 · **Recommended slot:** 4th · **Est:** 46 h

**Goal:** real local inference with honest resource control. INV-1 and INV-2 live here.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P3.T01 | Ollama adapter client | httpx client for `/api/tags`, `/api/ps`, `/api/show`, `/api/pull` (stream), `/api/generate` (stream + non-stream), `/api/embeddings`, unload via `keep_alive:0`; typed models; timeouts; retry only on connection errors; error taxonomy (`offline`, `model_missing`, `oom`, `timeout`, `bad_response`) | respx tests for every endpoint incl. mid-stream abort | P2 | 6 |
| P3.T02 | Model tag verification | Before any pull, confirm the tag exists and capture size + digest; unverifiable → refuse with a reason instead of guessing | Bogus-tag test | T01 | 3 |
| P3.T03 | Model registry + profiles | Rows for `reasoning_primary`, `fast_worker`, `coding_worker`, `vision_worker`, `embedding_worker`, `transcription_worker`: tag, context, `num_gpu`, temperature, fallback chain, enabled; DB-backed and user-remappable | Seed + unknown-profile rejection test | T02 | 3 |
| P3.T04 | Download manager | One pull at a time; SSE progress (bytes, %, speed, ETA); cancel; resume-friendly; explicit confirmation with size + free-space check; refuse when `free < 2 × size`; never auto-download at startup | Cancel leaves no partial registration; disk guard blocks | T01 | 6 |
| P3.T05 | Model storage verification | Assert blobs resolve inside `OLLAMA_MODELS`; warn and name the offending path if not; per-model disk usage report | Misconfigured-env test → clear error | T04 | 2 |
| P3.T06 | Model Lab API + page | Installed (size, digest, modified), loaded state, pull/remove/unload/benchmark, role mapping editor, disk panel, Ollama-offline state with retry | Screenshot; all controls functional or disabled with a reason | T04 | 6 |
| P3.T07 | Benchmark harness ⭐ | Fixed 3-prompt suite (classify, caption, plan); measure TTFT, tokens/sec, peak RAM, peak VRAM, wall time; store `model_benchmark`; reproducible ±20% | Two recorded runs on this laptop; verdict written into ADR-09 | T04 | 5 |
| P3.T08 | Resource governor (INV-1) ⭐ | Samples RAM/VRAM/CPU/queue every 2 s; `max_concurrent_generation=1` default; admission control with priority classes (interactive > agent > background); pause background above RAM 85% / VRAM 90%; context caps; exposes state to UI | 5 queued jobs → exactly 1 generates, others `throttled`; simulated low RAM pauses background | T01 | 7 |
| P3.T09 | Model router (INV-2) | Task → profile by type, risk, latency target, availability; deterministic jobs skip the LLM; fallback primary → fast → `needs_human_review`; **no external provider branch unless explicitly enabled**; decision + reason logged per call | External disabled → network mock records zero outbound calls; fallback test | T03, T08 | 4 |
| P3.T10 | Structured output enforcement | JSON-schema validation; one repair round quoting the validation error; second failure → `needs_human_review`; raw output stored | Malformed JSON, schema violation, valid-after-repair | T09 | 4 |
| P3.T11 | Prompt template store | Immutable versions + `active` pointer, Jinja strict-undefined rendering, prompt lint (no secrets, no non-Latin identifiers in code), registry tests | Missing-variable render fails loudly | T09 | 3 |
| P3.T12 | Embedding service | `nomic-embed-text` wrapper, batching, dimension guard, storage strategy (sqlite-vec if present, else capped numpy cosine), rebuild command | Similarity sanity on 3 sentences | T11 | 3 |
| P3.T13 | Vision budget | Frame caps (default 4 frames, ≤768 px longest edge), token estimate, hard refusal above budget, CPU/partial-offload path | 40-frame request refused with reason | T09 | 2 |
| P3.T14 | Offline/failure test suite | Fake Ollama: online, offline, slow, mid-stream disconnect, OOM, model-not-found | pytest subset green | T01–T13 | 4 |
| P3.T15 | Model Lab UX states | not_installed / pulling / installed / loaded / unloading / error / ollama_offline, each with a next action; no fabricated percentages | Screenshot matrix | T06 | 3 |

**Exit gate**
- ☐ `qwen3:1.7b` installed (plus `qwen3:4b` if it runs) under `<DATA_ROOT>\models\ollama`, proven by path listing.
- ☐ Real generation works, streaming visible, unload works, `ollama ps` empties.
- ☐ Benchmark table + verdict: tokens/sec, GPU layers, RAM/VRAM peaks on this exact laptop.
- ☐ Concurrency never exceeds configuration; low-resource pause demonstrable.
- ☐ Zero outbound calls to external AI providers under defaults.
- ☐ No model download without explicit confirmation and a passing disk guard.
- ☐ Ollama offline yields an actionable state, not a dead end.

**Phase risks:** Pascal GPU support in current Ollama (T07 decides); a 4B model may exceed usable VRAM and run slower than real time (mitigate: 1.7B default for agent work); registry sizes change; Max-Q thermals during long benchmarks.

---

## PHASE 5 — Media intake, Content Studio and video pipeline
**Blueprint prompt:** 5 · **Recommended slot:** 5th (pulled ahead of P4) · **Est:** 74 h

**Goal:** the actual product. Import → probe → thumbs → transcript → variants → export, all local, all on D:, all cancellable.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P5.T01 | Media intake service | Chunked streaming write with SHA-256 computed in-flight; size caps (default 8 GB), extension allowlist, magic-byte sniff, sanitized filenames, duplicate detection by checksum with "reuse existing / keep both" choice; writes only under `media/incoming` | Tests: duplicate, oversize, wrong MIME, path-ish filename, interrupted upload cleanup | P1 | 6 |
| P5.T02 | ffprobe adapter | Duration, video/audio streams, codecs, profile, level, fps (r/fps distinction), rotation and display dimensions (swap for 90/270), bitrate, channels, sample rate, HDR/SDR flags, creation time; robust parse of odd files; actionable errors for corrupt/unsupported | Fixture matrix incl. rotated, VFR, no-audio, 4K, HEVC | T01 | 6 |
| P5.T03 | Thumbnail + storyboard generation | N cover candidates at spread timestamps, sprite sheet, black-frame and scene-cut heuristics (OpenCV capped), safe-area overlay render option, all written to `media/thumbnails` | Files + chosen-cover persistence test | T02 | 5 |
| P5.T04 | Video preview + streaming | Range-request media endpoint served through the authenticated API (no `file://` leakage, no path exposure); player UI with keyboard scrubbing; poster from thumbnail | Playwright: seek works; direct file URL impossible | T02 | 4 |
| P5.T05 | Media library UI | Grid/list, search, tag filter, sort, project links, archive, delete with typed confirmation, duplicate badge, import progress strip | Screenshots + interaction test | T04 | 6 |
| P5.T06 | Transcription service | faster-whisper lazy load (`base`/`small`), language detect (en/fr/ar), CPU thread caps, queue-limited, progress + cancel, model cache on D:, "not installed" state with install action | Real transcript of a spoken 10 s clip; cancel test | P01 | 7 |
| P5.T07 | Transcript editor + subtitle export | Segment editor (text + timing), split/merge, SRT and VTT export, optional burn-in with style preset, characters-per-second warning | Exported SRT/VTT files validated by ffprobe/parser | T06 | 6 |
| P5.T08 | Frame sampling + vision analysis | Budgeted sampling (P3.T13), per-frame findings, aggregated summary stored as project evidence; never feeds a whole video to the model | Sample output + budget refusal test | T06 | 3 |
| P5.T09 | Variant renderer ⭐ | FFmpeg argv arrays only; presets 9:16 / 1:1 / 16:9; fit vs crop vs pad-with-blur; safe-area preview before render; hardware encoder attempt with automatic CPU fallback; progress parsing from `-progress pipe:1`; cancel kills process tree cleanly; output to `media/exports` | 3 presets rendered from a real clip; cancel mid-render leaves no orphan ffmpeg.exe; source untouched | T02 | 9 |
| P5.T10 | Audio checks + optional normalization | Peak/clipping detection, silence detection, optional `loudnorm` pass with before/after numbers | Report on a clipped fixture and a clean one | T02 | 3 |
| P5.T11 | Rights metadata + warnings | Rights status field (owned / licensed / uncertain / third-party), notes, music-rights flag surfaced at validation time | Persistence + block-in-validation test | T05 | 2 |
| P5.T12 | Content brief + copy generation | Agents/LLM produce project brief, hook options, script beats, caption drafts per platform from transcript + brand profile; all land as **drafts** with rationale and uncertainty | Draft output on a real project; nothing auto-approved | T06 | 6 |
| P5.T13 | Export validator | Validates export against the platform profile JSON (container, codec, dimensions, duration, aspect, audio, bitrate, size) and writes `validation_report` | Validator passes a good export and fails a deliberately wrong one | T09 | 4 |
| P5.T14 | Versioned platform requirement profiles | `platform-profiles/{facebook,instagram,tiktok}.json` with fields + **source URL + checked date + API version**; loader validates the schema; UI shows the checked date | Files + loader tests + docs entry | T09 | 3 |
| P5.T15 | Retention, quota, cleanup | Working-file TTL (14 d default), orphan sweep, archive flow, quota guard before render, dry-run report of what would be deleted | Dry-run output; TTL job test with faked clock | T01 | 4 |
| P5.T16 | Media test suite | 5 s generated clips per case (H.264/AAC, HEVC, no audio, rotated, VFR, 4K), corrupt file, huge dimensions; ffprobe parse units; render cancel; intake idempotency | pytest subset + generated fixtures script | T01–T13 | 4 |

**Exit gate**
- ☐ Real import → probe → thumbnails → transcript → 9:16 export works end to end on a real file.
- ☐ Duplicate import detected by checksum; corrupt file rejected with a clear message.
- ☐ Every long operation is cancellable and leaves no orphan process or half-file.
- ☐ Originals are never modified; all outputs live on D:.
- ☐ Platform profiles carry real source URLs and checked dates.
- ☐ No shell string construction anywhere in media code (semgrep clean).

**Phase risks:** FFmpeg filter-graph errors on odd inputs (fixture matrix mitigates); whisper on CPU being slow (default to `base`, allow `small`); long renders blocking the laptop (queue + governor + progress + cancel); HEVC decode support on Pascal.

---

## PHASE 6 — Accounts, OAuth and capability detection
**Blueprint prompt:** 6 · **Recommended slot:** 6th · **Est:** 60 h · **Track B dependency:** B01–B04 must be started at the beginning of this phase

**Goal:** know exactly what each connected account can and cannot do, and prove it. **No publishing in this phase** — publish tools raise `NOT_ELIGIBLE` / `NOT_IMPLEMENTED`.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P6.T01 | Official API research doc ⭐ (blocking) | `docs/API_RESEARCH.md` for each platform: OAuth flow, exact scope names, account-type requirements, app-review/access-level rules, redirect URI rules, media requirements, rate limits, API version, **checked date + URL**. Also record the private-use (dev-mode) vs public-review distinction and TikTok's intended-use language verbatim. | Doc with dates; each later adapter cites a section | P2, 👤B01/B06 | 5 |
| P6.T02 | OAuth core | PKCE (S256) + state, loopback redirect listener on ephemeral port (or custom scheme if the provider forbids loopback), single-use `oauth_session` with 10 min TTL, code exchange, error mapping, timeout, no token in URL fragments/logs | Tests: state mismatch, replay, expired session, provider error, PKCE verify | T01 | 7 |
| P6.T03 | Token vault (INV-3 / ADR-07) | Encryption/decryption performed in the Electron main process via `safeStorage` (DPAPI); ciphertext in `config\secrets\{id}.bin`; DB stores `secret_ref` + fingerprint only; per-call decrypt with a narrow IPC surface; rotate and revoke | Scan of DB file and logs for token patterns finds nothing; rotation test | T02 | 6 |
| P6.T04 | Token lifecycle | Expiry tracking, proactive refresh window, refresh-on-401-once, revoke on disconnect, health state machine, re-auth prompt in UI, never log tokens or codes | Tests with fake clock; revoked-token path | T03 | 4 |
| P6.T05 | Facebook adapter | OAuth scopes as documented; list Pages via `/me/accounts`; per-Page tasks/permissions check; `debug_token` scope + expiry introspection; capability snapshot: can this Page accept a video post from this token? | Fixture-driven tests + one real account if available | T01 | 6 |
| P6.T06 | Instagram adapter | Detect provider route (Instagram Login vs Facebook Login); professional-account check; scope check; linked-Page / PPA flags; content-publishing eligibility; rate-limit metadata | Fixture tests for business, creator, personal, missing-scope | T01 | 6 |
| P6.T07 | TikTok adapter | OAuth (client key/secret in DPAPI), `creator_info` query, scope check, audit/unaudited detection, sandbox vs production, explicit "Direct Post not available" capability result | Fixture tests incl. unaudited response | T01 | 5 |
| P6.T08 | Capability engine ⭐ | Normalizes every provider result into: `connected`, `eligible`, `missing_permission`, `needs_app_review`, `unsupported_account_type`, `token_expired`, `rate_limited`, `disconnected`, `unknown` — persisted per account with the evidence snapshot and one human-readable next action per state | Table-driven test: 9 states × 3 platforms; UI strings come from FEATURE_MATRIX | T05–T07 | 6 |
| P6.T09 | Accounts UI | Connect wizard that lists exactly which permissions will be requested and why; account cards with type, scopes, capability matrix, token health, last verified; test-connection; disconnect with data-purge choice | Screenshot matrix incl. every capability state | T08 | 6 |
| P6.T10 | Secret hygiene audit | Redaction tests with synthetic credentials; grep the whole log directory; assert no token appears in any API response body or error message | Automated test + report | T03 | 3 |
| P6.T11 | Provider fixtures + mock OAuth server | Recorded/anonymised JSON for every call used; local mock OAuth server for CI; fixtures clearly labelled `test_only` and impossible to enable in production build | CI job: no network egress | T05–T07 | 4 |
| P6.T12 | Disconnect / revoke / purge | Revoke at provider where supported, delete secret, mark account disconnected, keep history for audit, optional full purge of derived data | Tests + audit events | T04 | 3 |
| P6.T13 | Rate-limit + backoff utilities | Shared token bucket per account, `Retry-After` parsing, jitter, per-platform quotas from profiles, deterministic in tests | Unit tests with fake clock | T01 | 3 |
| P6.T14 | No-publish guarantee | Publish routes/tools exist but return typed `NOT_ELIGIBLE`/`NOT_IMPLEMENTED`; tests assert no provider write endpoint is reachable in this phase | Test asserting zero write calls in fixtures | T08 | 2 |

**Exit gate**
- ☐ For each platform, the app distinguishes all nine capability states with evidence.
- ☐ OAuth works end to end against a real provider app (or the mock server if credentials are unavailable) including state/PKCE failure paths.
- ☐ Database file, logs and API responses contain no tokens (proven by scan).
- ☐ Disconnect truly revokes and purges.
- ☐ `docs/API_RESEARCH.md` exists with URLs and checked dates; adapters cite it.
- ☐ Zero publish writes performed in this phase.

**Phase risks:** platform docs drift (T01 + B08 re-verification); Meta permission naming changes; TikTok app creation may be blocked or restricted → manual path unaffected; loopback redirect restrictions on some providers; DPAPI blob portability (machine/user scoped — document that tokens do not survive a machine move).

---

## PHASE 7 — Variants, scheduler, publishing and the manual TikTok path
**Blueprint prompt:** 7 · **Recommended slot:** 7th · **Est:** 92 h

**Goal:** ship content — safely, idempotently, per target, with honest states. This is the highest-risk phase; INV-4, INV-5, INV-9 are decided here.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P7.T01 | Variant generation service | Per-platform caption, hashtags, CTA, cover frame, privacy default, aspect preset; hashtags carry an evidence/uncertainty label ("suggested", "account-derived", "unverified"); nothing published, all drafts | Generated variants for 3 platforms from one project | P5, P6 | 6 |
| P7.T02 | Platform copy rules as data | Caption length, hashtag guidance/count, mention/handle rules, link behavior, line-break handling; validation errors explain the rule and source | Rule tests + docs citation | T01 | 3 |
| P7.T03 | Validation pipeline | Media profile + caption + account capability + rights status → `validation_report` with `blocking`/`warning`/`info` items and one click-to-fix action each; queueing blocked while any `blocking` remains | Tests: 6 blocking scenarios | T02 | 5 |
| P7.T04 | Publish Center UI | Side-by-side variants, per-target account, live preview, caption/hashtag editing, cover picker, schedule picker, validation checklist, capability warnings, and a consent control that cannot be pre-checked | Screenshot with 3 targets; disabled button reasons | T03 | 7 |
| P7.T05 | Approval & consent records (INV-4) | Consent wording versioned in `docs/CONSENT.md`; `ApprovalRecord` stores payload hash of (caption+hashtags+media hash+account+privacy+schedule); approval expires; any edit invalidates it (`ApprovalStale`) | Tests: edit-after-approve blocks publish; expired approval blocks | T04 | 5 |
| P7.T06 | Scheduler | UTC storage + display TZ, DST-safe local-time selection, min lead time, per-account/per-platform slot conflicts, calendar day/week/month, drag-to-reschedule, pause/resume, duplicate prevention | DST boundary tests (spring-forward gap, fall-back duplicate hour); reschedule test | P2 | 7 |
| P7.T07 | Dispatch worker | Claims due jobs with lease, checks governor + rate limits, executes adapter, records `publish_attempt`, handles cancellation between stages, structured stage logs | Tests: throttled dispatch, lease recovery, cancel between stages | P2.T19, T05 | 6 |
| P7.T08 | Facebook publish adapter | Page video upload (resumable when documented), publish, capture video/post id, poll processing status, error taxonomy mapped to actionable reasons, duplicate guard | Mock end to end + one real Page publish if available | P6.T05 | 7 |
| P7.T09 | Instagram publish adapter | Container create → upload (documented resumable route) → poll container status → `media_publish` → capture media id; enforce the 24 h post limit; map limit/permission/processing errors | Mock end to end + one real Reel if available | P6.T06 | 8 |
| P7.T10 | TikTok Direct Post adapter (gated) | `creator_info` before settings, enforce creator duration/privacy constraints, consent gate record, chunked sequential upload with resume, status polling; **only reachable when capability snapshot says Direct Post is permitted** | Mock tests for allowed + blocked paths; blocked path shows manual package instead | P6.T07 | 8 |
| P7.T11 | Manual TikTok package (always available) | MP4 + `caption.txt` + `hashtags.txt` + cover image in a dated folder, "Open folder" and "Open TikTok" actions, "mark as posted manually" with optional URL, state `manual_publish_required` → `published(manual)` | Files on disk + state transition; works with no TikTok app at all | T04 | 4 |
| P7.T12 | Publish state machine + rollup (INV-5) | Explicit transition table with guards (§9); per-target states; master status = worst-of with counts ("Partially published 2/3"); no global green on partial failure; every illegal transition raises and is tested | Table-driven test covering all legal + illegal transitions | T07 | 7 |
| P7.T13 | Idempotency + reconciliation (INV-9) ⭐ | `idempotency_key = hash(variant, account, payload)`; timeout → `needs_reconciliation`, never blind retry; reconciliation queries remote ids/status before any retry; duplicate-publish test with an injected ambiguous failure | Scenario test: post accepted remotely, response lost, no second post created | T12 | 7 |
| P7.T14 | Status monitors / webhooks | Polling with backoff to terminal state; webhook receiver with signature verification + replay protection where the platform supports it; unsupported webhooks clearly disabled with reason | Tests: terminal detection, bad signature rejected, replay ignored | T07 | 5 |
| P7.T15 | Retry + manual resolution UI | Retry (safe only), reconcile, cancel, "mark published with URL", "mark failed with reason"; every action audited; no action that can double-post | Screenshot + tests for each action | T13 | 4 |
| P7.T16 | Publish audit trail | Every external call logged: correlation id, redacted request summary, HTTP status, provider request id, remote ids, timing | Audit rows for a full simulated publish | T09 | 3 |
| P7.T17 | Publish test matrix | Happy path ×3 platforms; rate limit; expired token; upload timeout; long processing; partial failure (FB ok / IG fail / TikTok manual); duplicate retry; cancel mid-upload | CI green with the full matrix | T08–T14 | 6 |

**Exit gate**
- ☐ Full simulated publish works for Facebook, Instagram and TikTok-manual.
- ☐ Real publish only possible for an eligible account with a fresh approval whose payload hash matches.
- ☐ Every failure maps to an actionable reason plus a next step.
- ☐ Ambiguous-timeout reconciliation proven to never duplicate.
- ☐ Master status never claims success when one target failed.
- ☐ Manual TikTok path works with zero TikTok API configuration.
- ☐ Calendar handles DST and multi-account conflicts correctly.

**Phase risks:** Meta/IG container state machines are slow and quirky (poll intervals, processing 100%); app-review requirements blocking real publishing; TikTok Direct Post approval realistically unavailable → manual path must be genuinely excellent; clock/TZ bugs; double-posting on network retries (the single most damaging bug class — T13 is the mitigation).

---

## PHASE 4 — The 111-agent workforce and orchestration engine
**Blueprint prompt:** 4 · **Recommended slot:** 8th (after P7, so agents bind to real tools) · **Est:** 76 h
**Pulled-forward slices:** the definition schema + catalogue + tool registry land in P2 seed data; the task-graph engine is available to P5/P7 for multi-step jobs.

**Goal:** 111 real, validated role definitions with a bounded, auditable orchestration engine — not 111 model processes, and not 111 animations.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P4.T01 | Canonical catalogue (111 YAML) | 1 chief (`000`), 10 leaders (`001`–`010`), 100 specialists (`011`–`110`) exactly as Blueprint §5, each with the full contract (§8.1) | 111 files; a human-readable generated table in `docs/AGENT_CATALOG.md` | P2 | 10 |
| P4.T02 | Catalogue validator | Count = 111, contiguous unique ids, team links valid, every `allowed_tools` exists in the registry, every output schema compiles (Draft 2020-12), timeouts within [15, 900], risk/approval consistency (external tool ⇒ approval) | CI job: validator fails on 5 deliberately broken fixtures | T01 | 5 |
| P4.T03 | Seed + versioning | Migration/seed loads catalogue with `catalog_version`; idempotent re-seed; changing a definition bumps version and is audited | Re-seed twice = no diff; version recorded | T02 | 3 |
| P4.T04 | Role prompt library | One versioned prompt per role family, consistent JSON-only output discipline, no non-Latin text in code, no secrets, prompt-injection resistance instructions for transcript content | Prompt lint + render tests | T01 | 6 |
| P4.T05 | Tool Permission Registry | Tool descriptors (name, JSON schema, risk: read/write/external/compute, allowed roles, approval requirement); enforcement at the call site; denial is logged and audited | Tests: 021→publish denied; 056→publish without approval raises `ApprovalRequired`; unknown tool rejected | T02 | 6 |
| P4.T06 | Task graph engine | Nodes, edges, cycle detection, depth limit, per-request caps, state machine with guard table, cancellation propagation | Tests: cycle rejected, depth>3 rejected, caps enforced, cancel cascades | T03 | 7 |
| P4.T07 | Chief planner | Goal → validated plan JSON (≤12 subtasks, explicit dependencies, assigned squads, budget); rejects over-budget plans; asks for clarification instead of inventing requirements | Tests: valid plan, oversized plan, ambiguous goal → clarification | T06 | 6 |
| P4.T08 | Leader decomposition + executor | Leaders expand subtasks; executor runs specialist tasks through the model router, validates output, retries per policy, attaches `evidence_refs`; deterministic tasks run without an LLM | Test: a subtask with no LLM requirement completes in <1 s with zero model calls | T07 | 8 |
| P4.T09 | Approval gate integration | External-risk tool calls pause the task in `awaiting_approval`, create an `ApprovalRecord` request, resume on user action, expire otherwise; Chief cannot bypass | Test: gate blocks and resumes; bypass attempt raises | T05 | 5 |
| P4.T10 | Memory system | Task / project / long-term scopes, curator approval, PII+secret filter, retention, per-item delete, embedding-based retrieval | Tests: token-like and email content never stored; delete works; retrieval returns evidence ids | P3.T12 | 6 |
| P4.T11 | Run logs + evidence store | Per-run: model tag, tokens, duration, tool calls, raw output reference, finish reason; correlation id chain from UI action | Query a run and reproduce its output | T08 | 4 |
| P4.T12 | Restart resilience | Graph persisted; on boot, `running` tasks older than the lease → `needs_human_review` with a resume/cancel choice; nothing silently lost | Kill mid-run → correct post-restart state | T06 | 4 |
| P4.T13 | Agent Command Center | Hierarchy tree (chief → 10 leaders → 100 roles), task graph view with live states via SSE, filters, evidence inspector, current model + queue + resource readouts, explicit "roles share a model pool" explanation, no fake swarm animation | Screenshots; live state changes visible; resource panel real | T11 | 8 |
| P4.T14 | Limits + cost tests | Runaway plan rejected; recursion prevented; 111 definitions load in <2 s; a 40-task run respects the governor and completes within budget or degrades honestly | CI perf test + limit tests | T06–T12 | 4 |
| P4.T15 | End-to-end orchestration demo | "Take this imported video and prepare Facebook, Instagram and TikTok variants" decomposed across ≥2 squads, using **real** media and copy tools, ending in drafts awaiting approval | Recorded run + task graph screenshot + audit log excerpt | T08, P5, P7 | 4 |

**Exit gate**
- ☐ Exactly 111 definitions load and validate; a broken fixture is rejected by CI.
- ☐ A sample goal decomposes across at least two squads with real tool calls.
- ☐ Task graph survives an app restart.
- ☐ The permission gate blocks unauthorized and unapproved external actions, provably.
- ☐ Governor limits are respected during a multi-agent run on this laptop (no OOM, no thrash).
- ☐ Every delegation and tool call appears in logs and in the UI.
- ☐ No agent can read secrets, execute shell commands, or browse arbitrary paths.

**Phase risks:** prompt-quality work is open-ended (cap each role with a schema + rubric); model JSON compliance on small models (repair round + deterministic fallbacks); token budget blowups (hard caps); the temptation to fake "100 agents thinking" (explicitly rejected by the gate); slow CPU inference making multi-agent runs feel sluggish (parallelise only deterministic tools; keep LLM calls sequential).

---

## PHASE 8 — Analytics, community inbox and automations
**Blueprint prompt:** 8 · **Recommended slot:** 9th · **Est:** 58 h

**Goal:** evidence-based insight and controlled automation — only where official access exists, with missing data shown as missing.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P8.T01 | Metric definition registry | Per platform: metric key, definition, unit, endpoint, scope required, availability, known limitations; versioned with source URL + checked date | Registry file + loader validation | P6 | 4 |
| P8.T02 | Facebook/Instagram collectors | Paginated fetch, incremental sync by `since`, dedupe on (post, metric, hour bucket), rate-limit aware, partial-result handling | Fixture tests + one real sync if eligible | T01 | 7 |
| P8.T03 | TikTok analytics (conditional) | Implement only for granted scopes; otherwise an explicit `unavailable` state with the exact missing scope named | Test: unavailable path renders correctly | T01 | 3 |
| P8.T04 | Normalization + completeness labels | Every snapshot labelled `complete`, `partial`, `unavailable`, `small_sample`; cross-platform comparisons refuse incompatible definitions with an explanation | Tests: incompatible comparison blocked | T02 | 5 |
| P8.T05 | Dashboards + export | Per-platform and cross-platform views, date ranges, post-level table, CSV export, comparability warnings, empty/insufficient-data states | Screenshots incl. "no data yet" | T04 | 7 |
| P8.T06 | Weekly report generator | Agent drafts a report (what worked, what didn't, sample-size caveats) saved as a draft artifact — never auto-sent | Generated report file + test that no external send occurs | T05 | 4 |
| P8.T07 | Comment/inbox collectors | Only documented endpoints/scopes; pagination, dedupe, author pseudonymisation, retention controls | Fixture tests; real fetch if scopes allow | P6 | 5 |
| P8.T08 | Classifier + escalation | Sentiment/topic/spam classification; hard escalation for legal, medical, financial, safety threats, minors, crisis, harassment; escalated items cannot be auto-replied | Table-driven escalation tests (12 categories) | T07 | 5 |
| P8.T09 | Reply drafts + send governance | Draft-first replies, tone check, per-item human confirmation, per-hour send caps, no bulk/DM features at all (absent by design, documented) | Tests: cap enforced, unconfirmed send impossible, bulk API absent | T08 | 5 |
| P8.T10 | Notifications | In-app center + optional Windows toast, severity, dedupe key, deep links, quiet hours, no notification for routine success | Tests + screenshot | P2 | 4 |
| P8.T11 | Automation engine | Trigger/condition/action graph schema + validator, dry-run mode showing what would happen, pause, run history, publish actions require a pre-approved rule **and** per-post consent | Tests: dry-run performs zero writes; publish automation still hits the approval gate | P2 | 6 |
| P8.T12 | Built-in automations | Import → draft variants; publish failure → notify; weekly report; token expiry warning; disk/low-space warning; each pausable and inspectable | Each rule demonstrated + run history | T11 | 4 |

**Exit gate**
- ☐ Every displayed metric carries source, timestamp and completeness.
- ☐ Missing metrics render as "not available from this account" — never as zero, estimate or guess.
- ☐ No reply can be sent without explicit per-item confirmation; no bulk messaging exists.
- ☐ High-risk topics always escalate to human review.
- ☐ Automations are pausable, dry-runnable and audited; publish automations still obey consent.
- ☐ No claim about "trending" hashtags unless backed by cited data.

**Phase risks:** insight endpoints returning sparse or delayed data (label partials honestly); metric definitions differing across platforms; automation becoming an unattended posting machine (blocked by design); scope creep into engagement automation (explicitly excluded).

---

## PHASE 9 — Desktop product polish, design system and accessibility
**Blueprint prompt:** 9 · **Recommended slot:** 10th · **Est:** 88 h

**Goal:** make 14 working pages feel like one professional studio application, with no dead controls and no fake data.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P9.T01 | Design tokens + themes | Dark-first palette (calm graphite, one restrained accent), light theme, type scale, spacing scale, elevation, radii, focus rings, motion rules with reduced-motion support; tokens as CSS variables + TS constants | Token file + theme switcher; contrast report | P5 | 5 |
| P9.T02 | Component library | Button, Input, Select, Combobox, Dialog, Toast, Badge (status-mapped), Card, virtualized Table, Tabs, Tooltip, Skeleton, EmptyState, ErrorPanel, ConfirmDialog, ProgressBar, Stepper; documented in a local `/dev/components` route | Component page + screenshots | T01 | 8 |
| P9.T03 | App shell | Collapsible sidebar (14 destinations), top bar (workspace, global search, quick import, notifications, account status), contextual right inspector, bottom status strip (backend, model, queue, resources); persisted layout | Shell screenshot at 3 window sizes | T02 | 6 |
| P9.T04 | Command palette + shortcuts | ⌘/Ctrl-K palette, import, search, save draft, navigate, open logs; cheat sheet dialog; all shortcuts discoverable and remappable-safe | Interaction test + help dialog | T03 | 4 |
| P9.T05 | All 14 pages complete | Dashboard, Content Studio, Media Library, Campaign Planner, Calendar, Publish Center, Accounts, Analytics, Inbox, Agent Command Center, Model Lab, Automations, Brand & Templates, Settings & Diagnostics — each with loading, empty, error, offline, permission-denied and success states; every control functional or disabled with a visible reason | Page-by-page screenshot matrix + control inventory | T02 | 14 |
| P9.T06 | Dashboard | Real health cards, today's schedule, queue depth, recent activity, first-run checklist, resource mini-charts | Screenshot with real data | T05 | 4 |
| P9.T07 | Content Studio composition | Dropzone, player, metadata panel, transcript panel with timing, variant tabs, safe-area preview, "generate drafts" flow | Screenshot + full-flow test | T05 | 6 |
| P9.T08 | Publish Center final-review dialog | Destination summary, media preview, caption/hashtags, privacy, schedule, capability warnings, consent checkbox (never pre-checked), disabled reasons | Screenshot + test that publish is impossible with consent unchecked | T05 | 5 |
| P9.T09 | Calendar views | Day/week/month, drag-drop, colour-blind-safe status palette, per-target badges, conflict warnings | Screenshots + drag test | T05 | 5 |
| P9.T10 | Agent Command Center visuals | Hierarchy tree, task graph canvas (live, no decorative animation), evidence inspector, run timeline, resource readouts | Screenshots + live-update test | T05 | 6 |
| P9.T11 | Model Lab visuals | Pull progress with real numbers, disk usage, benchmark charts, role mapping, offline/degraded states | Screenshots + test | T05 | 4 |
| P9.T12 | Accessibility pass | Keyboard-only navigation through every page, visible focus, ARIA labelling, dialog focus trap and restore, screen-reader labels for icon buttons, AA contrast, reduced motion honored | Automated axe pass + recorded keyboard walkthrough | T05 | 6 |
| P9.T13 | Display scaling + laptop sizes | 100/125/150% Windows scaling, 1366×768 and 1920×1080, no clipped controls or horizontal scroll on primary flows | Screenshot matrix at 6 configurations | T05 | 4 |
| P9.T14 | Live updates channel | SSE-driven progress for queue/jobs/models/agents with reconnect and backoff, no polling storms, ordering guarantees | Network trace + reconnect test | T05 | 5 |
| P9.T15 | i18n + RTL scaffold | `en` complete, `ar`/`fr` string files scaffolded, RTL layout direction support for UI chrome, content language independent of UI language, no Arabic strings inside code comments | Locale switch test + RTL screenshot | T05 | 4 |
| P9.T16 | Onboarding | First-run wizard: choose data root (validated), toolchain diagnostics, model install guidance, connect accounts (skippable), first import; every step skippable with a resume point | Recorded first-run walkthrough | T05 | 5 |
| P9.T17 | Error UX | Human-readable messages, cause, next action, "copy diagnostics", link to the matching log lines; no raw stack traces in UI | Error catalogue + screenshots | T05 | 4 |
| P9.T18 | UI test suite | Playwright-Electron: each page renders, zero console errors, no control without a handler or explicit disabled reason, visual snapshots for key screens | CI green + snapshot gallery | T05–T17 | 5 |

**Exit gate**
- ☐ Zero console errors in a full walkthrough of all 14 pages.
- ☐ Automated control inventory: every interactive element either performs a real action or is disabled with a visible reason.
- ☐ Accessibility: keyboard-complete, focus visible, axe clean on all pages.
- ☐ 100/125/150% scaling verified with screenshots.
- ☐ No fake account, post, agent or analytics data outside a clearly-labelled demo mode (which must be impossible to confuse with live data).
- ☐ Locale switch and RTL verified.

**Phase risks:** polish becoming redesign churn (freeze the design system at T02); virtualized tables + SSE causing render thrash; accessibility retrofits late (T12 scheduled, not optional); screenshots revealing layout debt hours of work each.

---

## PHASE 10 — Security, testing, packaging and release
**Blueprint prompt:** 10 · **Recommended slot:** 11th · **Est:** 74 h

**Goal:** a real Windows installer, a hardened app, and documentation that tells the truth about what works.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P10.T01 | Secret + fake-code sweep | `gitleaks` + `scripts/audit_fake.ps1` over the whole repo; fix every finding; add pre-commit + CI | Clean reports; baseline committed | P9 | 3 |
| P10.T02 | Static analysis | ruff, mypy (new modules), eslint, tsc, semgrep (shell=True, path joins, eval, unsafe deserialization), bandit; triage every hit with an explicit decision | Reports + suppression justifications | T01 | 5 |
| P10.T03 | Security test suite | Auth bypass matrix, CORS, path traversal (Windows tricks), SSRF (outbound allowlist), oversized/malformed uploads, decompression bombs, log-injection, deeply nested JSON | CI green; each test mapped to a threat in `docs/SECURITY.md` | P2–P9 | 6 |
| P10.T04 | Dependency + license audit | pip-audit, npm audit, license inventory (FFmpeg build, Electron, Python deps, fonts, icons), SBOM export | SBOM file + zero unmitigated high CVEs | T02 | 4 |
| P10.T05 | Full journey suite | Automate all 15 journeys from Blueprint Prompt 10 where possible; document the rest as a scripted manual checklist with expected results | Journey matrix PASS/FAIL | P9 | 8 |
| P10.T06 | Chaos + recovery tests | Kill during upload, during queue claim, DB locked, disk full, Ollama crash, network loss, clock change; verify states and reconciliation | Each scenario recorded with resulting state | T05 | 7 |
| P10.T07 | Performance budget | Cold start <8 s warm / <20 s first run; idle RAM; 100-job queue drain; DB p95 query <50 ms; UI list scroll at 60 fps; measured and recorded | Benchmark table on this laptop | T05 | 4 |
| P10.T08 | Backup/restore productization | UI for backup now / scheduled, restore drill with verification, retention, export-to-zip for off-machine copy, warning that DPAPI tokens do not migrate | Restore test from a real backup | P2.T21 | 4 |
| P10.T09 | Installer (NSIS via electron-builder) | App id, name, icon, version, Start Menu entry, desktop shortcut (user choice), per-user install, prerequisite detection (VC++ runtime, WebView2), data-root confirmation step, log folder, no silent admin escalation; outputs to `<PROJECT_ROOT>\installer_builds` | Built installer + install transcript | P9 | 8 |
| P10.T10 | Sidecar packaging ⭐ | Bundle the Python backend (PyInstaller or embedded runtime), ffmpeg/ffprobe binaries (license-checked), optional whisper model download flow; verify a machine **without** Python/Node installed; pin versions | Installed build runs on a clean profile with no dev tools | T09 | 8 |
| P10.T11 | Upgrade + uninstall semantics | Upgrade preserves DB/media/settings and migrates; uninstall preserves user data by default, offers explicit "also delete my data" with typed confirmation and a backup-first prompt | Upgrade test + uninstall test with before/after file inventory | T10 | 4 |
| P10.T12 | Versioning + signing decision | SemVer, changelog generation, ADR-14 outcome documented in release notes (SmartScreen warning explained with steps) | Changelog + release notes draft | T09 | 2 |
| P10.T13 | CI pipeline | windows-latest; lint, type, unit, integration, contract, build `--dir`, audit jobs; artifacts; zero paid services; local parity script `scripts/ci.ps1` | Green run + local run | T02 | 4 |
| P10.T14 | Release documentation set | `docs/RELEASE_CHECKLIST.md`, `KNOWN_LIMITATIONS.md`, `RECOVERY.md`, `BACKUP_RESTORE.md`, `API_APPROVAL_GUIDE.md`, and the finalized `FEATURE_MATRIX.md` (Available / Requires approval / Requires eligible account / Manual fallback / Not supported) | Six docs, each reviewed for truthfulness | T05 | 5 |
| P10.T15 | Clean-profile install test | New Windows user account: install, launch, complete onboarding, import a video, view diagnostics, exit, uninstall; verify D-drive writes and data preservation | Recorded walkthrough + file diff | T11 | 4 |
| P10.T16 | Local-only diagnostics bundle | `diagnostics.zip` with redacted logs, versions, sanitized settings, schema version, queue state; no media, no tokens | Bundle inspected for leaks | T13 | 2 |
| P10.T17 | Security sign-off | Findings table with severity, fix, test id; explicit residual-risk list | Signed report | T03–T16 | 2 |

**Exit gate**
- ☐ All automated suites green (backend, desktop, contract, security, UI smoke).
- ☐ Installer produces desktop + Start Menu shortcuts and works on a clean profile with no dev tools.
- ☐ App uses the configured D-drive root on the installed build; no user data on C:.
- ☐ Uninstall preserves user data by default.
- ☐ Zero open critical/high security findings.
- ☐ Release notes state real functionality and every platform restriction.
- ☐ Backup/restore proven from a real backup file.

**Phase risks:** PyInstaller + faster-whisper/ctranslate2 bundling pain (mitigate: make transcription an optional downloadable component); antivirus false positives on unsigned installers; NSIS prerequisite checks on locked-down machines; installer size/one-file extraction slowness on a 5400 rpm drive if D: is spinning.

---

## PHASE 11 — Independent senior-engineer audit
**Blueprint prompt:** 11 · **Recommended slot:** 12th · **Est:** 34 h · **Rule:** the auditor must not read P0–P10's own summaries as evidence

**Goal:** an adversarial, evidence-first verification with a truthful go/no-go.

| ID | Task | Detail / definition of done | Evidence | Dep | Est |
|---|---|---|---|---:|---:|
| P11.T01 | Reproducibility audit | Fresh clone into a clean folder, build **only** from `README.md`, run tests, package; list every undocumented step needed | Gap list (ideally empty) | P10 | 4 |
| P11.T02 | Feature-by-feature audit matrix | 24 areas from Blueprint Prompt 11 × rows: PASS / FAIL / BLOCKED BY PLATFORM APPROVAL / NOT IMPLEMENTED, each with file path + command + observed output | Matrix file | T01 | 8 |
| P11.T03 | Anti-fake crawl | Run `audit_fake.ps1`; automated dead-button crawl over all pages; inventory every `disabled` control and confirm a reason exists; search for hardcoded success paths, swallowed exceptions, dummy data serialized to the UI | Report with file:line | T01 | 4 |
| P11.T04 | External-action audit | Enumerate every network call site; verify gating, approval, audit, idempotency and rate limiting on each; confirm zero unauthenticated outbound calls | Call-site table | T01 | 5 |
| P11.T05 | D-drive write audit ⭐ | Run the full journey under a filesystem watcher; diff every write outside `<PROJECT_ROOT>`; classify each as expected (documented) or a defect | Write-audit log + exception list | T01 | 4 |
| P11.T06 | Performance + resource audit on the real laptop | Cold start, 3-agent run RAM/VRAM, tokens/sec, 20-minute render stability, thermal behaviour | Measured table | T01 | 3 |
| P11.T07 | Security re-audit | Re-run the suite plus new attempts: token in memory dump, IPC abuse from renderer, path escape via crafted filename, prompt-injection into a publish action | Findings list | T01 | 4 |
| P11.T08 | Fix critical/high + regression tests | Fix immediately, add a regression test per fix, re-run everything | Diffs + tests green | T02–T07 | 6 |
| P11.T09 | Go/no-go report | Severity-ranked issues, acceptance matrix, exact launch commands (dev + installed), honest capability summary, recommendation | Signed report | all | 3 |

**Exit gate**
- ☐ Audit matrix complete with no unexplained FAILs.
- ☐ Zero open critical/high issues; mediums listed with owners and dates.
- ☐ Every BLOCKED item names the exact missing approval.
- ☐ `FEATURE_MATRIX.md` matches reality, verified independently.
- ☐ Reproducible build from docs alone.
- ☐ Written go/no-go recommendation.

---

## 14. Milestones and demo scripts (what "done" looks like to you)

Each milestone is a **demo you personally run**, not a status report.

| Milestone | Phases | Demo script | Passes when |
|---|---|---|---|
| **A — Local foundation** | P0–P3 | Install → onboarding → Diagnostics shows real values → Model Lab pulls `qwen3:1.7b` to D: → generate a caption in a scratch panel → unload → Resource Monitor shows the queue throttling 3 jobs to 1 | No fake numbers; model file is on D:; concurrency respected |
| **B — Media production** | P5 | Drag a real phone video in → see metadata → thumbnails → transcript → edit a line → export 9:16 → open the file | Export plays correctly; original untouched; nothing on C: |
| **C — Publishing core** | P6, P7 | Connect a Page and an IG professional account → see capability states → build 3 variants → approve → schedule 2 minutes ahead → watch states move to publishing/published with remote ids | Real remote ids for eligible accounts; TikTok shows manual package; no duplicate |
| **D — TikTok compliance path** | P7 | With TikTok unconfigured: generate the manual package, copy caption, open TikTok, mark as posted manually | Full manual loop works with zero API |
| **E — Agent workforce** | P4 | Ask the Chief to prepare all 3 variants for the imported video → watch a real task graph, 2+ squads, evidence, drafts → approve one | Graph survives a restart; governor holds; no model storm |
| **F — Analytics + automation** | P8 | Sync metrics → see honest completeness labels → generate weekly report → enable "notify on publish failure" → trigger a failure → notification arrives | Missing data shown as missing; automation pausable |
| **G — Release** | P9, P10, P11 | Install from the NSIS build on a clean Windows profile → use it → uninstall → data preserved → audit report read | Installer, shortcuts, D-drive compliance, audit PASS/BLOCKED matrix |

**Demo data rule:** any seeded demo content lives behind an explicit `Demo mode` switch that is visibly labelled, is excluded from publish eligibility, and cannot be enabled in a build tagged `release` without a warning banner.

---

## 15. Risk register

| # | Risk | L | I | Mitigation | Trigger to act |
|---|---|---|---|---|---|
| R1 | GTX 1050 Ti cannot accelerate current Ollama builds (Pascal dropped) | M | M | Measure in P1.T07; ship CPU-only profile; default to 1.7B models; communicate expected speed honestly | Benchmarks <3 tok/s on the 1.7B model |
| R2 | TikTok Direct Post is never approved for a private utility | H | M | Manual package is a first-class, fully tested path (P7.T11); capability gate; no marketing claim | Audit rejection or no response in 6 weeks |
| R3 | Meta app review / business verification delays real publishing | M | H | Dev-mode private use with app-role accounts; mocks for CI; capability states surface the block | Review pending >2 weeks |
| R4 | Double-posting from ambiguous network timeouts | M | **Critical** | Idempotency key + reconcile-before-retry + attempt ledger + test (P7.T13) | Any `needs_reconciliation` row appears |
| R5 | Platform API drift breaks adapters | H | M | Versioned profiles with checked dates (P5.T14, P6.T01), fixture re-record task (B08), error taxonomy that names the drift | New error code from a provider |
| R6 | C: drive fills (only ~30 GB free) | M | H | PathPolicy + user-data-dir redirect + free-space guard + P11.T05 watcher | Any write outside root during audit |
| R7 | SQLite contention under queue + UI + agent load | M | M | WAL, short transactions, single writer path, busy_timeout, queue stats test | `database is locked` in logs |
| R8 | Agent token/cost blowups on long goals | M | M | Hard caps (tasks, depth, tokens, wall clock) + governor + `needs_human_review` | Any run hitting a cap |
| R9 | Prompt injection through transcript/caption into tool calls | M | H | Transcript text is data, never instructions; tool allowlist; approval gate for external risk (P4.T05, P10.T03) | Any tool call originating from content text |
| R10 | faster-whisper bundling/memory problems | M | M | Optional downloadable component; lazy load; CPU thread caps; graceful "not installed" state | Install size >500 MB or OOM during transcription |
| R11 | Unsigned installer SmartScreen friction | H | L | Documented workaround; consider signing later | Repeated install friction |
| R12 | Scope creep (engagement automation, scraping requests) | M | H | Explicit exclusion list in CONSTITUTION; refuse features that violate INV-3 | Any request for auto-likes/DMs/scraping |
| R13 | Solo-developer bus factor / long gap mid-phase | M | M | Phase gates + evidence packs + RUNBOOK allow resuming cold | Any phase idle >2 weeks |
| R14 | DPAPI secrets don't survive machine moves | L | M | Document it; "reconnect accounts" step after restore | Restore drill |
| R15 | Test suite becoming slow and skipped | M | M | Coverage gates in CI, <5 min unit target, fixtures cheap | Suite >10 min |

---

## 16. Definition of Done (applies to every task and phase)

A task is done only when **all** of these are true:

1. **Code** exists, is typed, linted and formatted; no `TODO` introduced without an issue id.
2. **Tests** exist at the right level and pass locally and in CI.
3. **Failure paths** are handled: timeout, cancellation, permission denied, offline, malformed input.
4. **No new write path** bypasses `PathPolicy`; no new outbound call bypasses an adapter's allowlist.
5. **Audit/logging** records the action if it affects accounts, content, jobs, files or approvals.
6. **UI surface** for the feature either works or is disabled with a visible, accurate reason.
7. **Docs** updated: feature matrix status, API spec/OpenAPI, ADR if a decision changed, RUNBOOK if an operator action changed.
8. **Evidence** captured in the phase evidence pack (`docs/evidence/PN.md`).
9. **No fake anything**: no simulated provider response, no placeholder metric, no hardcoded success, no demo row in a live view.

**Phase evidence pack format** (`docs/evidence/PN.md`): a) files changed (paths), b) decisions made and ADRs touched, c) exact commands run, d) test output verbatim (not summarised), e) remaining defects with severity, f) how to launch and exercise the phase, g) explicit functional vs not-implemented list, h) gate checklist with per-line proof.

---

## 17. Anti-fake audit checklist (run at every phase gate and in P11)

- [ ] `grep -rn "TODO\|FIXME\|XXX\|HACK" src/` → each hit has an issue id or is fixed.
- [ ] `grep -rn "mock\|fake\|dummy\|placeholder\|lorem" src/` → only test fixtures and clearly-labelled demo mode.
- [ ] Every `disabled` attribute in the UI has an adjacent human-readable reason sourced from `FEATURE_MATRIX.md`.
- [ ] No `except: pass`, no empty `catch {}`, no `return null` swallowing a failure in a service.
- [ ] No code path returns a success state without a verified provider/DB result.
- [ ] Provider adapters: every method has a fixture test **and** an explicit behaviour when the provider is unreachable.
- [ ] Analytics: every displayed number traces to a stored snapshot with a source and timestamp.
- [ ] Agents: no fabricated evidence refs; every output traces to a stored run.
- [ ] Screenshots or recordings don't show demo data unless the demo banner is visible.
- [ ] Every button in a Playwright crawl produces a state change, a navigation, a dialog, or an explicit disabled reason.
- [ ] Zero writes outside `<PROJECT_ROOT>` during a full journey (P11.T05).

---

## 18. Approval and compliance track (B-series, start now)

| ID | Task | 👤 | Notes |
|---|---|---|---|
| B01 | Create Meta developer app; add Facebook Login + Instagram products | you | Development mode is sufficient for accounts holding an app role |
| B02 | Business verification if the permission set requires it | you | Lead time 2–10 business days; start early |
| B03 | Prepare Facebook Page + Instagram professional account + confirm your Page role | you | Publishing to a Page needs the right Page task |
| B04 | Privacy policy + data-deletion page (ADR-13) | you + agent | Static page + minimal data-free callback |
| B05 | App-review screencast | you | Only if going beyond private use |
| B06 | TikTok developer app + Content Posting API product | you | Record the exact app settings and any restriction text |
| B07 | TikTok audit attempt + written outcome | you | Do not promise public posting; keep manual path primary |
| B08 | Re-verify all API docs; update `docs/API_RESEARCH.md` with URLs + checked dates | agent + you | Repeat at the start of every platform phase |

**Consent text (P7.T05) must state, in plain language:** which account, which platform, what will be posted, the visibility setting, the scheduled time, that the platform confirms publishing, and that a failed confirmation will not be re-posted automatically.

---

## 19. First 10 actions (nothing else until these are done)

1. Approve or amend ADR-01 … ADR-14 (§1), especially ADR-05 (no global TEMP redirect), ADR-10/11/12 (platform scope) and ADR-13 (public policy pages).
2. Answer Q1–Q10 or accept the defaults.
3. Start Track B: create the Meta app and the TikTok app today — these have external lead times.
4. Run P0.T01–P0.T10 (one day) and commit the constitution + ADRs.
5. Run P1.T01 (preflight) and P1.T07 (Ollama + GPU verdict) **before** writing app code — the GPU answer may change your model strategy.
6. Create `<PROJECT_ROOT>` via P1.T02 and confirm zero writes on C:.
7. Build the walking skeleton (P1.T10–P1.T11) and show the real Diagnostics page.
8. Freeze Phase 1 with an evidence pack; do not start P2 in the same breath.
9. Decide the data-root and backup answers before P2.T01 writes settings schemas.
10. Book a 30-minute demo at each milestone (A–G) — demos catch fake progress faster than code review.

---

## 20. What this plan deliberately does not promise

- That TikTok Direct Post will ever be enabled for your account. The manual path is the promise; the API is a bonus.
- That Facebook/Instagram publishing works without an eligible account type and granted permissions, or without app review if you go beyond private use.
- That a 4 GB Pascal GPU will run 4B models at conversational speed. Benchmarks decide, not optimism.
- That "100 agents" will feel like 100 parallel thinkers. It will feel like one disciplined planner with 111 well-scoped roles sharing one model — which is the point.
- That every Windows byte can be forced onto D:. The app guarantees it *itself* writes nothing outside your chosen root, and documents the OS-level exceptions it cannot control.

---

*End of plan. Total scope: 12 phases, 184 tasks, ≈830 engineering hours (≈37 weeks with contingency), 3 parallel tracks, 15 tracked risks, 10 invariants with named enforcement and test points, and one honest definition of done.*
