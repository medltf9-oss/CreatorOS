# Phase 9 — production packaging: plan and record

**Status: delivered** (`0.8.0`, installer built and verified). Written before implementation, updated
as each milestone landed, so the deviations at the end are visible rather than folded in.

Baseline: Phase 8 complete at `a9bf055` (`0.7.0` code, `phase-8.24` API).

---

## 1. What "production ready" has to mean here

The brief asks for an installed application that runs with no IDE, no terminal, no pnpm, no Python on
PATH, and that preserves every byte of existing data. That decomposes into eight independent claims,
each of which has to be provable on this machine or explicitly blocked:

| # | Claim | How it is proven |
| --- | --- | --- |
| 1 | A branded application icon exists and is used by the exe's shortcuts, the window and the installer | icon files at nine sizes, `.lnk` icon paths, screenshots |
| 2 | The shell finds its backend when packaged | `electron/paths.cjs` + 11 unit tests, including the "backend inside app.asar" case |
| 3 | The backend runs with no Python installed | bundled interpreter, `python.exe -m creatoros serve` from `%LOCALAPPDATA%` |
| 4 | A fresh data root works, and an existing one is upgraded in place | `migrate` on an empty root, then on `<PROJECT_ROOT>`; revision equals head |
| 5 | A Windows installer exists, per user, with Desktop and Start Menu shortcuts | `CreatorOS-Setup-0.8.0.exe`, `verify-install.ps1` |
| 6 | The installed app launches and works | CDP-driven screenshots of the installed app |
| 7 | Uninstalling removes the app and preserves the data | `verify-install.ps1 -Uninstall` |
| 8 | A single command rebuilds the release reproducibly | `scripts/build-release.ps1` + `SHA256SUMS.txt` |

Anything that cannot be proven is stated as a blocker with the exact remedy, not hidden behind a mock.

---

## 2. Milestones

### P9.1 — Brand mark and icon pipeline
* `assets/brand/creatoros-icon.svg` (master, 1024 px) and `creatoros-icon-small.svg` (16–32 px).
* `app/desktop/scripts/make-icons.cjs` renders each size *at that size* in Chromium and writes the ICO
  container itself; `scripts/electron-run.cjs` runs it with a clean environment.
* Outputs: nine PNGs, `build/icon.ico` (7 sizes), `build/installerIcon.ico`, `build/icon.png`.
* Docs: `docs/CREATOROS_BRAND.md`.

### P9.2 — Packaged-shell correctness
* `electron/paths.cjs`: backend directory from `CREATOROS_BACKEND_DIR` → `resources/backend` → repo;
  backend command as a bundled interpreter or the venv; explicit argument vectors; renderer as URL or
  file; `userData` **pinned** to `creatoros-desktop` so the vault's DPAPI key survives a product rename.
* `main.cjs`: migrate before serve, refuse to start the API if migration fails, single-instance lock,
  honest failure dialog with the log path, window icon, title `CreatorOS`.
* `creatoros/__main__.py`: `serve | migrate | version`, alembic root discovered from checkout, bundle
  or `CREATOROS_BACKEND_ROOT`.
* Tests: `app/desktop/tests/electronPaths.test.cjs` (11).

### P9.3 — Backend bundle
* First attempt: PyInstaller onedir, 39 MB. It ran twice, then Windows Smart App Control blocked every
  subsequent build (`CodeIntegrity 3077`), including from other directories and after a self-signed
  signature. Recorded, then abandoned.
* Final: a **signed interpreter bundle** — PSF-signed `python.exe`/DLLs copied from the venv's own base
  installation plus a pure-Python payload (79 MB). `scripts/build-backend.ps1` asserts the signature,
  prunes the stdlib, then verifies by running `version`, `migrate` on an empty root and a live
  `/health/live` check.

### P9.4 — Installer
* `app/desktop/electron-builder.yml`: per-user NSIS, Desktop shortcut always, Start Menu entry,
  `resources/icon.ico`, backend as `extraResources`, `signAndEditExecutable: false` +
  `disableAsarIntegrity: true` so the app exe stays byte-identical to the vendor binary.
* `app/desktop/build/installer.nsh`: recreates both shortcuts with the brand icon.
* `scripts/capture-installed.cjs`: drives the installed app over CDP (global `WebSocket`, no
  dependency) — this is what produced the installed-app screenshots.
* Defect found by looking at the installed app: a 90-character interpreter path printed over the
  neighbouring card. Fixed in `App.tsx`.

### P9.5 — Release build, install/uninstall verification, documentation
* `scripts/build-release.ps1`: icons → backend → typecheck → tests → `vite build` → electron-builder →
  `SHA256SUMS.txt`, with a gate after each stage and a version-consistency check across
  `package.json`, `pyproject.toml` and `settings.py`.
* `scripts/verify-install.ps1`: install, shortcuts and their icons, window, backend provenance,
  migration-before-serve, database at head; `-Uninstall` adds removal and data-preservation checks.
  Detects the Smart App Control block and prints the two remedies instead of a stack trace.
* `scripts/install-unpacked.ps1` / `uninstall-unpacked.ps1`: the documented fallback when the unsigned
  installer is blocked — same layout, same shortcuts, same "Installed apps" entry, no NSIS wrapper.
* Docs: `INSTALLATION.md`, `UPGRADING.md`, `UNINSTALLATION.md`, `RELEASE_NOTES_0.8.0.md`,
  `CREATOROS_BRAND.md`, `ADR-34`, `docs/evidence/P9.md`.

### P9.6 — Final acceptance
* Full backend suite, frontend suite, `ruff`, `tsc`, `vite build`, `alembic current`.
* Installed app exercised on the live data root; evidence captured; data integrity re-checked.
* Every claim in `docs/evidence/P9.md` classified by what was actually observed.

---

## 3. Honest machine limits

| Limit | Consequence | Remedy |
| --- | --- | --- |
| **Smart App Control is ON** on this machine | a newly built **unsigned installer** may be refused; the app and backend are fine because they are copies of signed binaries | sign the build (`CSC_LINK`), or install with `scripts\install-unpacked.ps1` |
| No code-signing certificate | the installer is unsigned; SmartScreen shows "unknown publisher" on machines without SAC | buy a certificate; the build path is ready for it |
| No Meta / TikTok app, no connected accounts | no live provider metric has ever been collected | documented as category C–F blockers in `docs/evidence/P8.md` |
| No Ollama models bundled | AI pages report the gateway as unavailable; everything else works | install Ollama + pull models (unchanged from Phase 3) |
| No NVENC | video transcodes use libx264 | unchanged from Phase 5 |

None of these is worked around by pretending: each is visible in the UI, in `/version`, or in the
evidence files.
