# CreatorOS — Phase 8.24 and Phase 9 release plan

> **Status: both stages delivered.** P8.24 is commit `a9bf055`; Phase 9 is recorded in
> `docs/PHASE9_RELEASE.md`, with evidence in `docs/evidence/P9.md` and release notes in
> `docs/RELEASE_NOTES_0.8.0.md`. This file is kept exactly as written before implementation, so the
> deviations listed at the end of each phase record stay visible.

**Written:** 2026-11-24 · **Baseline:** `71cef34` (P8.23)

This plan is committed before any implementation commit, as the earlier phase plans were. It records
what is actually missing at `71cef34`, what will be built, and what cannot be built on this machine.

---

## 1. What exists at the baseline (inspected, not assumed)

| Area | State at `71cef34` | Consequence for this plan |
| --- | --- | --- |
| Backend | 1 489 tests pass, 9 skipped; `ruff` clean; migration head `b8c9d0e1f2a3`, 48 tables | Nothing to repair; extend |
| Analytics API | 53 routes under `/analytics` | Reuse; add report formats, retention execution, version watch fields |
| Reports | `services/analytics/reports.py` writes **JSON only**; `sha256` + `bytes` recorded from the real bytes | Add CSV and PDF to the same service; digest stays computed from the written bytes |
| Exports | `GET /analytics/export/metrics.csv`, `/export/publications.csv` | Keep; they are live exports, not stored reports |
| Retention | `POST /analytics/prune` is a **preview** that deletes nothing, and counts only finished runs and their snapshots | Replace with a real, auditable, bounded engine; keep the preview |
| Version watch | `version_monitor.py`: pinned version per platform, documented latest, retirement dates, metrics above the configured version, `assert_version_usable()` before any HTTP call | Add a recorded *observation* of the documented versions (checked-at + staleness), an endpoint-change note, and a scheduled re-check job |
| Diagnostics | `GET /analytics/diagnostics` returns counts, blocked sync states, stale runs, version rows, warnings | Add retention state, report health, last successful collection, staleness, version-watch freshness |
| `/version` | Reports analytics capability per platform from facts | Add `analytics_reports`, `analytics_retention`, `analytics_version_watch` |
| Desktop | 12 analytics pages, 172 frontend tests, `tsc` clean, `vite build` clean (672 kB) | Add a retention panel to Analytics settings; verify the other eleven pages |
| Electron shell | `electron/main.cjs` resolves the backend from `path.join(__dirname, '..', '..', 'backend')` and spawns `.venv\Scripts\python.exe -m uvicorn` | **Broken under packaging** (path lands inside `resources/`), so production startup is real work, not configuration |
| Packaging | No `electron-builder`, no `build` block, no icon assets, no installer | Build the whole release chain |
| Brand | No `assets/` directory, no icon, no wordmark; window has no icon | Create the identity |
| Git hygiene | `core.autocrlf=false`; `App.tsx` and `PHASE8_PLAN.md` are CRLF in the index, everything else LF | Preserve per-file line endings; strip CRLF from files that were LF |

## 2. Phase 8.24 — what will be built

### 2.1 Report formats (`services/analytics/reports.py`)

* `ReportFormat = json | csv | pdf`, stored on the report row as a new `format` column? **No new
  column**: the format is part of the file name and is recorded in `filters_json.format`, which the
  API already returns. A migration for one enum column is a risk with no reader.
* One content model, three renderers. Every format carries the same facts: report id, format,
  generated-at, period, platforms, accounts, metric definitions used, observation windows,
  availability summary, warnings, source references, size, SHA-256.
* CSV: RFC 4180 with a UTF-8 BOM-less stream, `\r\n` row endings (Excel-safe), and a `section` column
  so the multi-section report stays one flat table.
* PDF: a dependency-free writer (`services/analytics/pdf.py`): PDF 1.4, Helvetica, one text line per
  row, a page per ~60 rows, correct xref byte offsets, `/Producer CreatorOS`. No images, no fonts
  embedded, so the file is small and deterministic. Verified by parsing the produced xref table in a
  test **and** by rendering the file in Electron for the evidence pack.
* Failure path: the file is written to a `*.part` name, hashed, then renamed; a failure deletes the
  partial file and marks the report `failed` (new state in the existing enum? no — the report row is
  not created at all; the API returns the error). `AnalyticsReportStatus` already has DRAFT/FINAL/
  ARCHIVED, and a DRAFT row whose file is missing is exactly "failed generation", which the retention
  engine treats as an orphan.
* No path input anywhere: the file name is derived from the id, the stamp and the format.

### 2.2 Retention (`services/analytics/retention.py`)

Policy, per category, each with a stated reason (not one duration for everything):

| Category | Eligible when | Never |
| --- | --- | --- |
| Observations (`analytics_snapshot`) | `collected_at` older than `retention_days` **and** the run is finished | — |
| Aggregations | every `input_snapshot_ids` entry is already gone, or `computed_at` older than the cutoff and all of its inputs are eligible | aggregations that still cite a live observation |
| Collection runs | `finished_at` older than the cutoff **and** no snapshot still references it | runs that are still open |
| Reports | `created_at` older than `max(retention_days, 730)` **or** status `archived` | reports newer than the floor |
| Recommendation drafts | status `superseded` and older than the cutoff | any `accepted` / `rejected` record (a decision is history), any `draft`/`reviewed` still actionable |
| Orphan files | a file under `media_exports/reports` that no report row points at and that is older than 1 day | files referenced by a row |

Execution model: `preview()` → per-category counts + a reason string + the policy that was used;
`execute(dry_run=False)` → bounded batches (`limit` per category, default 5 000), one audit event
with the counts, and the job re-enqueues itself while work remains. Nothing else in the database is
touched: `publish_job`, `media_asset`, `social_account`, `campaign`, `content_project` and the metric
registry are outside the policy, and a test asserts that a retention execution leaves every one of
those row counts unchanged.

Two invariants are enforced in code and tested:
* a publication/job/account row count is identical before and after;
* `PRAGMA foreign_key_check` is empty after a full pass (snapshot deletion happens before the run it
  belongs to, so the `ON DELETE CASCADE` never removes a live observation).

Scheduling: the **existing** sweeper dispatches `analytics.retention` on the **existing** `analytics`
lane, at most once per `retention_interval_s` (new bounded setting? No — fixed 24 h, recorded in the
audit event and documented; `retention_days` remains the only retention setting). At most one
retention job may be open at a time: the queue's `idempotency_key` guarantees it.

Recovery: a retention execution is safe to repeat; a partially deleted batch leaves no dangling
aggregation because aggregations are deleted in the same transaction as the observation they cite,
and a re-run simply finds fewer eligible rows. `docs/ANALYTICS_RETENTION.md` documents point-in-time
recovery (the existing backup service) and what is and is not recoverable.

### 2.3 Version watch (`services/analytics/version_monitor.py`, `version_watch.py`)

* A recorded observation of the official documented versions: `checked_at`, `source_url`,
  `observed_at`, `status` (`current` | `stale` | `unavailable`), stored in `app_setting`
  (`analytics.version_watch`), so "we last verified this on …" is a fact, not a constant.
* `LATEST_DOCUMENTED` gains the date it was read (2026-11-23) and the official changelog URL per
  provider. A version observation older than 90 days is reported **stale**; a failed check is
  **unavailable**. Neither is ever turned into a version bump.
* `POST /analytics/versions/check` runs the check, and the sweeper enqueues `analytics.version_check`
  every 7 days. The check has two sources: the recorded table (always available) and an optional
  official-docs fetch that is **off by default** (`CREATOROS_ANALYTICS_VERSION_FETCH=1`); with the
  fetch off or failing, the result is `stale`/`unavailable` — never an invented version.
* `GET /analytics/versions` gains `checked_at`, `observation_status`, `observation_age_days`,
  `endpoint_changes` and `sunset_dates`, and keeps `retired` + `metrics_above_version`.
* No code path writes `provider_app_config.api_version`. A test scans the module for an update
  statement against that column.

### 2.4 Diagnostics and `/version`

`DiagnosticsOut` gains `retention` (last execution, eligible counts, policy, next due),
`reports` (count, last generated, failed/orphan files), `collection` (last successful run per
account, oldest observation age, stale accounts), `version_watch` (status, age, warnings) and keeps
`warnings` as the summary a person reads. `/version` gains `analytics_reports`, `analytics_retention`,
`analytics_version_watch` with honest states, and the API string moves to `phase-8.24`.

`scripts/diagnose.ps1` gains an analytics probe (`scripts/analytics_readiness.py`) that prints the
same facts from the database, so the release check and the API cannot disagree.

### 2.5 Desktop

`AnalyticsSettings.tsx` gains a **Retention** card: policy table, preview counts (`dry_run`), and an
execution that requires the literal word `DELETE` typed in its own confirmation, shows the audit
reference afterwards, and re-reads the preview. The other eleven analytics pages are verified, not
rewritten.

### 2.6 Phase 8 acceptance

`docs/P8_FINAL_ACCEPTANCE.md` walks criteria 1–16 of §19 of `docs/PHASE8_PLAN.md` with the command or
test that proves each one, and states the empty category B honestly. Acceptance runs the full suite,
`ruff`, `tsc`, `vite build`, `alembic current`, `foreign_key_check`, `integrity_check`, a migration
round trip on a **copy** of the real database, and `diagnose.ps1`.

Phase 8 is then closed with ADR-29…ADR-34 (retention, report formats, version watch, export licensing,
packaging/runtime, single-source brand).

---

## 3. Phase 9 — production desktop application

### 3.1 Brand (`assets/brand/`, `docs/CREATOROS_BRAND.md`)

A single master, `creatoros-icon.svg`: midnight-navy rounded square (`#111827`), a continuous open
loop reading as a "C" with an orbital terminal, stroke `#38BDF8`, one mint (`#34D399`) node at the
upper-right terminal. Rendered to 1024/512/256/128/64/48/32/16 PNG and to a multi-resolution
`creatoros.ico` (16,24,32,48,64,128,256). Wordmark `creatoros-wordmark.svg` is independent of the
mark. **One source of truth**: `assets/brand/brand.json` lists the files, and both the Electron build
config and the runtime read from it, so the window, taskbar, shortcut and installer cannot drift.

Rendering tools: no new graphics dependency. The SVGs are written by hand; PNG/ICO generation uses a
small pinned Node script (`scripts/brand/build-brand.cjs`) driven by the Electron renderer already
installed (Chromium rasterises the SVG at each size), and ICO is assembled from those PNGs with a
hand-written container. Nothing is downloaded.

### 3.2 Packaging (`electron-builder`, per-user NSIS)

* `electron-builder` pinned, installed with the existing pnpm/npm workspace and the D:-based cache
  (`ELECTRON_BUILDER_CACHE`, `ELECTRON_CACHE`), added as a dev dependency only.
* Target: `nsis`, x64, per-user (`perMachine: false`), `allowToChangeInstallationDirectory: true`,
  default directory `<DATA_ROOT>\Programs\CreatorOS`, `createDesktopShortcut: always`,
  `createStartMenuShortcut: true`, `shortcutName: CreatorOS`, `artifactName:
  CreatorOS-Setup-x64.exe`, `appId: io.creatoros.studio`, `productName: CreatorOS`,
  `executableName: CreatorOS`, icon from the brand manifest, `deleteAppDataOnUninstall: false`.
* The installer never includes models, the database, media or the Python venv by copy: the app
  **references** `<PROJECT_ROOT>` (env `CREATOROS_ROOT`), and the shipped tree is the Electron app
  plus the compiled frontend.
* Output goes to `<PROJECT_ROOT>\releases\<version>\` (git-ignored), never into the repository.

### 3.3 Production startup (the part that actually matters)

`electron/main.cjs` becomes environment-aware without changing its security model:

1. `ROOT` = `CREATOROS_ROOT` or `<PROJECT_ROOT>`; if it is missing, show a dialog that explains how to
   restore it and refuse to create a second database on C:.
2. `BACKEND_DIR` = `CREATOROS_BACKEND_DIR` or `<ROOT>\app\backend` — **not** `__dirname` — with the
   packaged path used only as a diagnostic hint. A missing backend directory or venv produces a
   readable error window, not a blank one.
3. `VENV_PYTHON` = `<BACKEND_DIR>\.venv\Scripts\python.exe`; the fallback to a bare `python` stays
   but is logged loudly and surfaced in the diagnostics payload.
4. Electron userData/logs/cache/sessionData are pinned to `<DATA_ROOT>\data\app\…` **before** `app.whenReady()`,
   so the packaged app does not create a large C: profile.
5. Everything else (token, free port, health check, vault broker, graceful kill of the process tree)
   is preserved exactly.

The released app therefore launches from the shortcut, spawns the real Python backend from the D:
venv, shows the window, and never needs pnpm, Vite, a terminal or an IDE.

### 3.4 Installer and shortcut verification (real, not assumed)

Installer built → installed to a **test directory** (`<DATA_ROOT>\Programs\CreatorOSTest`) → desktop
and Start-menu shortcuts inspected through the Windows shell (`WScript.Shell` `CreateShortcut`)
for target path, arguments, working directory and icon → the app launched from the **shortcut
itself** → window captured → backend health, DB, FFmpeg, Ollama and analytics diagnostics captured →
app closed → child processes checked → uninstalled → data verified intact → silent install of the same
build over itself to exercise the upgrade path.

Evidence: screenshots of the desktop with the shortcut, the Start-menu entry, the installer wizard,
the installed app running, and the diagnostics window; plus the shortcut target report.

### 3.5 Release pipeline (`scripts/build-release.ps1`)

deps check → `git status` clean → backend suite → `ruff` → `tsc` → `vite build` → `electron-builder`
→ installer SHA-256 → `release-manifest.json` + `SHA256SUMS.txt` + `RELEASE_NOTES.md` into
`<PROJECT_ROOT>\releases\<version>\`.

### 3.6 Documents

`docs/PHASE9_RELEASE.md`, `docs/CREATOROS_BRAND.md`, `docs/INSTALLATION.md`, `docs/UPGRADING.md`,
`docs/UNINSTALLATION.md`, `docs/RELEASE_BUILD.md`, `docs/RELEASE_SECURITY.md`,
`docs/RELEASE_TROUBLESHOOTING.md`, `docs/ANALYTICS_RETENTION.md`, `docs/P8_FINAL_ACCEPTANCE.md`;
plus `README.md`, `CHANGELOG.md`, ADR index.

---

## 4. Honest limits of this machine

* No Meta app, no TikTok app, no connected account → category B evidence stays empty; nothing here
  is verified against a live provider, and no page or document will claim otherwise.
* No trusted code-signing certificate → the installer is unsigned; SmartScreen will warn. This is
  documented, not circumvented; Smart App Control and SmartScreen are left alone.
* Browser-storage-scoped Ollama models are absent (`ollama_models=0`) → the app reports
  `ollama_unavailable` states rather than pretending inference works.
* Hardware encoding is unavailable (driver 582.78 vs NVENC 13.1 requirement) and stays reported as
  `media_hardware_encoding: unavailable`.
* Windows desktop and Start-menu shortcuts live in the Windows profile on C: by definition. That is
  the only intentional C: artifact, and it is documented in `docs/INSTALLATION.md`.

## 5. Commit sequence

1. `docs: P8.24 and Phase 9 release plan` — this file.
2. `P8.24: report formats, retention engine, version watch and analytics diagnostics`.
3. `docs: Phase 8 final acceptance` — `P8_FINAL_ACCEPTANCE.md`, ADR-29…34, evidence, README/CHANGELOG.
4. `brand: CreatorOS icon, wordmark and asset pipeline`.
5. `build: production packaging configuration (electron-builder, per-user NSIS)`.
6. `release: installer, shortcuts and verified production launch`.
7. `docs: release documentation and manifest`.

Installer binaries and `releases/` stay out of Git.
