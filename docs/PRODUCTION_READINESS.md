# Production readiness

What was verified on this machine, before the product is offered as a production release, and what
remains unverified because it needs an account, a certificate or a second machine.

This page is the consolidation of four sources: `scripts/production-health.ps1` (the installed app, end
to end), `scripts/verify-install.ps1` (the installer), `scripts/activation_status.py` (what exists for
providers) and `scripts/live_verify.py` (what a real provider says).

---

## 1. Status at a glance

| Area | Status | Evidence |
| --- | --- | --- |
| Installed application | **verified** | 35/35 production-health checks, 45/45 including upgrade + uninstall (2026-11-25) |
| Every module opens | **verified** | 49/49 pages, no error card, no console error |
| Desktop + Start Menu shortcuts | **verified** | both point at the install, brand icon on both |
| Backend starts by itself | **verified** | bundled interpreter, not a Python on PATH |
| Database | **verified** | at head `b8c9d0e1f2a3`, `integrity_check ok`, 0 FK violations |
| Credential vault | **verified** | seal → adopt → read → unlink → destroy round trip |
| Data survives upgrade | **verified** | installer over the existing install, counts unchanged |
| Data survives uninstall | **verified** | data root untouched, app and shortcuts removed, reinstall works |
| Clean shutdown | **verified** | no orphan `CreatorOS.exe`, no orphan backend process |
| Ollama | **reachable, no models** | v0.40.17; 0 GGUF models installed, so AI pages report it |
| FFmpeg | **verified, data-root tool** | 9.1-essentials build at `<DATA_ROOT>\tools\ffmpeg\bin`; the installer does not bundle it, so a fresh root without the tools reports it unavailable (production health then fails that one check) |
| Installer signature | **not signed** | no certificate; `scripts/sign-release.ps1 -Check` |
| Provider connections | **none** | 0 provider apps, 0 accounts (see §4) |
| Live publication | **not attempted** | requires an account and explicit approval |

---

## 2. The production health gate

```powershell
powershell -ExecutionPolicy Bypass -File scripts\production-health.ps1
powershell -ExecutionPolicy Bypass -File scripts\production-health.ps1 -IncludeUpgrade -IncludeUninstall
```

It launches the app **from the Desktop shortcut** (not from the exe path: the shortcut is what a user
has), waits for the renderer bridge, reads the per-launch session token from the app's own preload
bridge - the only way to reach the backend, because that token is deliberately never written to disk -
and then checks, in order:

| # | Check | What it proves |
| --- | --- | --- |
| 1 | no CreatorOS already running | the measurement starts from a clean state |
| 2 | installed exe, bundled interpreter, both shortcuts, database present | the install is complete |
| 3 | `integrity_check ok`, 0 FK violations, revision recorded | the data is sound before anything runs |
| 4 | app process tree starts | the shortcut works |
| 5 | renderer bridge answers with `apiBase`, `root`, `isDev=false`, version | the window loaded and is not a dev build |
| 6 | `backend ready on 127.0.0.1:PORT` in the log | the app started its own backend |
| 7 | `/diagnostics` reports the bundled interpreter and the install directory | no system Python is being used |
| 8 | `/health/ready` = `ready` | database + disk |
| 9 | `/version` = 0.8.0 | the running build is the installed one |
| 10 | vault seal/adopt/read/unlink/destroy round trip | the DPAPI vault works from the renderer |
| 11 | FFmpeg version, Ollama discovery, disk space, data root | the local environment is described honestly |
| 12 | 49 pages open with no error card and no console error | every module works |
| 13 | one app process tree, no stray interpreter | nothing unexpected is running |
| 14 | window closes, all processes exit within 20 s | clean shutdown |
| 15 | no orphan backend afterwards | nothing is left holding the database |
| 16 | row counts, revision and integrity after the cycle | a launch/close cycle changes nothing it should not |
| 17 | (`-IncludeUpgrade`) installer over the existing install | the upgrade path preserves data |
| 18 | (`-IncludeUninstall`) uninstall, then reinstall | uninstall preserves the data root |

Sample of the last full run:

```
  [PASS] data root reported by the app - <PROJECT_ROOT>
  [PASS] backend ready line in the log - 2026-11-24T09:59:02.538Z backend ready on 127.0.0.1:59327
  [PASS] bundled interpreter is the one running - ...\Programs\CreatorOS\resources\backend\python.exe
  [PASS] vault seals a probe secret - steps: reserve -> seal -> adopt -> verify -> unlink -> destroy
  [PASS] FFmpeg present - ffmpeg version 9.1-essentials_build-www.gyan.dev ...
  [PASS] Ollama reachable - v0.40.17, 0 model(s)
        Ollama is running but has no models: AI pages will report that, correctly
  [PASS] all sidebar pages render - 49/49 pages, screenshots in temp\p10-health
  [PASS] no orphan backend - 0 backend process(es) still running
  [PASS] only append-only tables grew - audit_event/queue_job may grow, plus one revoked
         secret_ref for the probe; nothing else changed
PRODUCTION HEALTH: 35/35 checks passed
```

### The page sweep can fail, and that is tested

A sweep that prints 49/49 is worthless if the probe cannot see a failure. `node
app/desktop/scripts/ui_health.cjs --port 9444 --self-check` injects the exact DOM the app renders for a
failed call (the `ErrorNote` card), asserts the probe reports it, removes it and asserts the probe is
clean again:

```
self-check: injected error detected=true, cleared after removal=true
```

The probe's definition of "an error is on screen" is the app's single error surface
(`border-rose-500/30` + `bg-rose-500/10`). A page that legitimately shows an empty state - every page on
this machine, which has no connected accounts - passes; a page whose API call failed does not.

---

## 3. Installation and removal

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-install.ps1              # 12 checks
powershell -ExecutionPolicy Bypass -File scripts\verify-install.ps1 -Uninstall   # + 5 checks
```

Verified in this phase: the installer from `releases/0.8.0` installs without elevation, creates both
shortcuts with the brand icon, the app launches with no development tooling present, the bundled backend
serves, the database is at head, an *upgrade* install leaves every row in place, and an *uninstall*
removes the app and both shortcuts while leaving the data root untouched. Re-measured on 2026-11-25
against the final build `AD8B7772…`: `scripts/verify-install.ps1 -Uninstall` reported **17/17** on a
fresh acceptance root, after Smart App Control had been turned off by the machine owner (see §F of
`docs/evidence/P9.md`).

The installer is **unsigned** (no certificate exists). With Smart App Control at its default
(`VerifiedAndReputablePolicyState = 1`) the freshly built stub was refused at process creation
(CodeIntegrity 3118) and packaging itself stopped at the uninstaller step; the owner of this machine
then turned Smart App Control off for the 2026-11-25 acceptance run, after which the same build
installed, launched and uninstalled (17/17). See `docs/CODE_SIGNING.md` for why that still means the
build cannot be distributed as-is, and `docs/INSTALLATION.md` for the unpacked fallback. No test has
been run on a clean Windows machine with Smart App Control left on, so no claim is made about it.

---

## 4. What is not production-ready, stated plainly

These are facts about this installation, re-checkable with `scripts/activation_status.py`:

| Missing | Consequence | Next action |
| --- | --- | --- |
| Meta developer app | Facebook Pages and both Instagram login paths cannot connect | `docs/PHASE10_ACTIVATION.md` §3.2 |
| App Review + Business Verification | even with an app, Pages publishing and insights stay `needs_app_review` | Meta console, external |
| TikTok developer app + audit | no TikTok connection; Direct Post needs `video.publish` approval | §3.3 |
| S3-compatible bucket | Instagram Reels cannot publish (it fetches from a URL) | §3.4 |
| Code-signing certificate | installer unsigned; cannot be distributed | `docs/CODE_SIGNING.md` |
| Ollama models | AI pages show the gateway as unavailable, correctly | `scripts/start_ollama.ps1`, then pull a model |
| A clean Windows machine | "will a stranger be able to install this?" is unanswered | required before any release |

Analytics, publishing, delivery and the vault are all *implemented* and *fixture tested*; none of them
is *real provider tested*, because there is no account to test with. `docs/evidence/P10.md` records that
split, and `docs/LIVE_PROVIDER_VERIFICATION.md` is the procedure that closes it.

---

## 5. Upgrade and rollback

* **Upgrade**: run the new installer (or `scripts/install-unpacked.ps1`); migrations run at launch,
  before the backend serves, so an upgrade applies only the outstanding revisions. Verified: row counts
  and revision unchanged by an install-over-install.
* **Backup before upgrading**: `scripts/` has no backup command, but the data root is self-contained
  (`data/`, `config/`, `models/`, `logs/`). Copying `<PROJECT_ROOT>` while the app is closed is a complete
  backup. `docs/UPGRADING.md` documents the same thing.
* **Rollback**: install the previous version the same way. A migration that has already run is not
  reverted by an older installer - the schema stays forward. Keep the backup; that is what it is for.
* **Uninstall** keeps the data root by design (`deleteAppDataOnUninstall: false`).

---

## 6. Re-running the whole thing

```powershell
# 1. what exists for providers, and what is missing
app\backend\.venv\Scripts\python.exe scripts\activation_status.py

# 2. the installed app, end to end
powershell -ExecutionPolicy Bypass -File scripts\production-health.ps1 -IncludeUpgrade -IncludeUninstall

# 3. the installer and the shortcuts
powershell -ExecutionPolicy Bypass -File scripts\verify-install.ps1 -Uninstall

# 4. the signature situation
powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check

# 5. the automated suites
cd app\backend; .\.venv\Scripts\python.exe -m pytest -q
cd ..\desktop; .\node_modules\.bin\tsc --noEmit; node --test tests\

# 6. everything at once
powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1
```
