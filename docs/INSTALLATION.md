# Installing CreatorOS

CreatorOS is a local application: everything — database, media, models, credentials, logs — lives in
one data root on your machine. Nothing is uploaded anywhere.

**Published today: `0.8.0` (source and an unsigned Windows installer).** There is no `1.0.0` release,
tag or installer. A `1.0.0` build is only publishable after the gates in
`docs/release/V1_RELEASE_BLOCKERS.md` pass with real evidence; until then, do not describe any build
as `1.0.0`.

Requirements: Windows 11 x64 (Windows 10 1903+ works), ~450 MB of disk for the application, and a
data root with room for your media. No Python, Node, pnpm, IDE or terminal is needed at runtime: the
backend ships as a bundled interpreter.

---

## A. Install with the installer (recommended)

```
releases\0.8.0\CreatorOS-Setup-0.8.0.exe
```

1. Verify the download first, if you like:

   ```powershell
   Get-FileHash .\CreatorOS-Setup-0.8.0.exe -Algorithm SHA256
   ```

   The recorded digest is
   `b9686c6e0eeab95c9d5cff0117f469b4242000baa27c56a9837e5ff1f896c4e0`; it must match
   `releases\0.8.0\SHA256SUMS.txt`. If it differs, stop.

2. Double-click the installer. It asks nothing until the install page.
3. Choose the folder if you want; the default is `%LOCALAPPDATA%\Programs\CreatorOS`, per user, no
   administrator prompt.
4. It creates a **Desktop shortcut** and a **Start Menu entry**, both named `CreatorOS`.
5. It starts CreatorOS when finished.

The 0.8.0 artefact in this repository was measured on 2026-11-25: silent install exited 0, the
installer window offered "Juste pour moi" and marked the all-users option as requiring administrator,
and `scripts\verify-install.ps1 -Uninstall` reported **17/17** (installed exe, both shortcuts and icons,
bundled interpreter, migrate-before-serve, database at head, uninstall removing app + shortcuts while
keeping the data root). See `docs/evidence/P9.md` §F.

The installer is **unsigned**, so Windows SmartScreen warns, and a machine that still **enforces**
Smart App Control refuses it outright — "blocked by Device Guard policy", not a warning with an "allow"
button.

### If Windows refuses to start the installer

The build is **unsigned** (no code-signing certificate). Two options:

* **Install without the installer** (works today, same result minus the NSIS wrapper):

  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-unpacked.ps1 -Launch
  ```

  This copies the packaged app to `%LOCALAPPDATA%\Programs\CreatorOS`, creates both shortcuts with the
  CreatorOS icon, and registers the app under *Installed apps*. It is reversible with
  `scripts\uninstall-unpacked.ps1`.

* **Sign the build** (correct for distribution): supply a certificate and rebuild —
  `CSC_LINK=…; CSC_KEY_PASSWORD=…` then `powershell -File scripts\build-release.ps1`. Smart App Control
  accepts a signed installer.

Nothing in this repository changes a Windows security setting, and you should not have to either. The
2026-11-25 acceptance run was only possible because the owner of the test machine turned Smart App
Control off; that was an environment decision, not a supported install path, and it does not count as a
clean-machine test. (The `0.8.0` `SHA256SUMS.txt` still carries an older comment claiming the build
machine had Smart App Control on; the audited value is `0` — off.)

---

## B. First launch

1. The app starts its own backend on a random loopback port and opens the window. It takes ~10 s the
   first time (the database is created and migrated) and ~5 s afterwards.
2. If the backend cannot start, the window opens anyway and shows the error plus the log path. The log
   is `<data root>\logs\backend\backend.log`.
3. Left column: **Studio** (dashboard, first-run setup, AI infrastructure, model lab, settings),
   **Content library**, **Agent workforce**, **Social accounts**, **Publishing**, **Analytics**.

### The first-run setup page

On a machine that has never completed setup, the app opens **First-run setup** (also in the rail under
*Studio*). It reports what it observed, and it distinguishes three things that are easy to confuse:

- **present** — the backend found a file or a running service;
- **verified** — you asked it to hash a specific file against the pinned prerequisites manifest and
  every check passed (size, SHA-256, Authenticode status, publisher, timestamp where required);
- **not verified / could not check** — anything else. A signature that cannot be read is never shown
  as a pass.

The page checks the data root (exists and writable), FFmpeg and ffprobe, the optional Ollama runtime,
the integrity of the pinned manifest, and one row per prerequisite that is not approved for bundling.

Finish with **Finish setup**, or **Skip AI setup** if you do not want a local model runtime. Either
choice is recorded in the local database and the audit log. Finishing does not install, download or
enable anything, and you can reopen the page at any time to re-run the checks.

### The data root

On a fresh machine the default is `%USERPROFILE%\CreatorOS`. This development checkout points at
`<PROJECT_ROOT>` through `CREATOROS_ROOT` or `<root>\config\bootstrap.json`. Override it before
launching:

```powershell
$env:CREATOROS_ROOT = "<DATA_ROOT>"
& "$env:LOCALAPPDATA\Programs\CreatorOS\CreatorOS.exe"
```

The app never moves or deletes an existing data root. On first launch in an empty root it creates the
directory structure and migrates a fresh database to head (`b8c9d0e1f2a3`).

---

## C. Optional: local AI runtime (Ollama)

CreatorOS talks to Ollama over `http://127.0.0.1:11434`. The pinned prerequisite is:

| Field | Value |
| --- | --- |
| Version | `0.40.18` |
| Download | `https://github.com/ollama/ollama/releases/download/v0.40.18/OllamaSetup.exe` |
| Size | 1,953,587,080 bytes |
| SHA-256 | `AD2BB47AA969B21CF8DD81B2D9F9A6D5030270ED242EF55087118C37C510ACC2` |
| Publisher | `CN=Ollama Inc., O=Ollama Inc., L=Toronto, S=Ontario, C=CA` |
| Silent install | `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` |

The first-run page links to the official download. Put the file in `Downloads` (or anywhere) and use
**Verify** on the Ollama card: the backend hashes the file, compares size and digest with the pinned
manifest, and checks the Authenticode status, publisher and timestamp. It does not execute the
installer. Run the installer yourself and keep its own prompts.

`0.40.18` is **not approved for bundling** with CreatorOS. Do not redistribute the installer. Model
weights are not included; pull only models your hardware can run, from the Model Lab, which measures
memory before loading and refuses unsafe combinations.

Without Ollama the app opens normally and AI features report that the runtime is unavailable.

---

## D. Media toolchain (FFmpeg)

FFmpeg is not bundled in the `0.8.0` installer. The backend expects:

```text
<DATA_ROOT>\tools\ffmpeg\bin\ffmpeg.exe
<DATA_ROOT>\tools\ffmpeg\bin\ffprobe.exe
```

or the directory named by `CREATOROS_FFMPEG_DIR`. The pinned build is gyan.dev
`9.1-essentials_build`:

| File | Size | SHA-256 |
| --- | ---: | --- |
| `ffmpeg.exe` | 110,590,976 | `ed8f66cb877087a96f5bbb40a2944f0e764f2ad8870d5cb647360d912e62bc07` |
| `ffprobe.exe` | 110,374,912 | `df0199f606faa88e9ad192732a7c3987acfb647663534c297772e17730611a08` |

It is unsigned and GPLv3, and bundling it is blocked until the licence question is resolved (see
`docs/release/V1_RELEASE_BLOCKERS.md` §2.1). The setup page reports whether the files are present and
whether their sizes match; **Verify** performs the full hash check.

---

## E. What is where

| Path | Contents |
| --- | --- |
| `%LOCALAPPDATA%\Programs\CreatorOS` | the application (shell, renderer, bundled backend) |
| `<DATA_ROOT>\data\database\creatoros.db` | SQLite database (48 tables at 0.8.0) |
| `<DATA_ROOT>\data\media\…` | media library: incoming, originals, proxies, transcodes, thumbnails, exports |
| `<DATA_ROOT>\models` | local models (Ollama library location) |
| `<DATA_ROOT>\config\secrets` | encrypted credential blobs (DPAPI, per Windows user) |
| `<DATA_ROOT>\logs` | backend log, plus one file per run |
| `<DATA_ROOT>\data\backups` | database backups |

The application directory and the data root are independent: upgrade or reinstall the application
without touching your data; back up the data root and keep everything.

---

## F. Verifying the installation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-install.ps1
```

It installs the built release (or reports the Smart App Control block), then checks 12 things: the
installer's exit code, both shortcuts and their icons, that the window opens, that the backend started
from `resources\backend\python.exe`, that the migration ran before the server, that the database is
present and at head. `-Uninstall` adds five more and confirms the data root survives the uninstall.

For an installation that is already there, the deeper check is production health:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\production-health.ps1
```

It launches the app **from the Desktop shortcut**, confirms the bundled backend starts by itself, checks
the database and a full credential-vault round trip, reports FFmpeg/Ollama/disk, opens **all 49 pages**
and fails if any of them shows an error card, then closes the window and checks that no process is left
behind and that no data changed (35 checks). Add `-IncludeUpgrade` to prove an install-over-install keeps
every row (39), and `-IncludeUninstall` to prove an uninstall keeps the data root and that reinstalling
restores the app (40). A full run takes about four minutes.

Neither script changes a Windows security setting. See `docs/PRODUCTION_READINESS.md` for what the result
means, and `docs/CODE_SIGNING.md` for the signature situation.

---

## G. Build from source (developers)

```powershell
# Backend
cd app\backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m alembic upgrade head

# Desktop (development)
cd ..\desktop
pnpm install
pnpm dev
```

Before proposing a change, run the checks in `AGENTS.md` (`pytest`, `ruff check src tests`,
`pnpm typecheck`, `pnpm test`, `pnpm build`). `scripts\release-gate.ps1 -Stage Source` additionally
checks version consistency, documentation, the SBOM and the pinned prerequisites manifest, tracked
secrets and a clean tree. `scripts\build-release.ps1` produces a packaged installer; it still needs a
certificate to be distributable as a signed build.

---

## H. Uninstalling

Installer-made installation: *Settings → Apps → Installed apps → CreatorOS → Uninstall*, or
`%LOCALAPPDATA%\Programs\CreatorOS\Uninstall CreatorOS.exe`.

Fallback installation: `powershell -File scripts\uninstall-unpacked.ps1`.

Either way your data root is left untouched; see `docs/UNINSTALLATION.md`. Delete the data root
yourself only after exporting anything you want to keep. There is no cloud copy.

---

## I. Troubleshooting

- **Window shows "Backend unavailable".** The panel prints the captured backend log. Check
  `<DATA_ROOT>\logs\backend\backend.log` and that the data root is writable.
- **Media import fails immediately.** Confirm `ffmpeg.exe` and `ffprobe.exe` are at the path in section D
  and run `ffmpeg -version` from that directory.
- **AI features report the runtime as unavailable.** Start Ollama (`ollama serve`) or let its tray app
  run, then re-run the setup checks. The API must answer on `127.0.0.1:11434`.
- **Installer will not run.** If Smart App Control is enforced, an unsigned installer is blocked by
  design; use the unpacked install or sign the build.

---

## J. What this guide does not promise

- It does not make `1.0.0` available and does not convert `0.8.0` into it.
- It does not claim a clean-machine, Smart App Control-enforced installation; that evidence does not
  exist yet and is a release blocker.
- It does not cover model quality, provider approvals or publishing outcomes.

See `docs/release/V1_RELEASE_BLOCKERS.md` for the authoritative list of what must happen before a
truthful `1.0.0` release.
