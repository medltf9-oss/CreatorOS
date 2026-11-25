# Installing CreatorOS

CreatorOS is a local application: everything — database, media, models, credentials, logs — lives in
one data root on your machine (default `<PROJECT_ROOT>`). Nothing is uploaded anywhere.

Requirements: Windows 11 x64 (Windows 10 1903+ works), ~450 MB of disk for the application, a data
root with room for your media (the default `<PROJECT_ROOT>` expects a `D:` drive). No Python, Node,
pnpm, IDE or terminal is needed at runtime: the backend ships as a bundled interpreter.

---

## A. Install with the installer (recommended)

```
releases\0.8.0\CreatorOS-Setup-0.8.0.exe
```

1. Double-click it. It asks nothing until the install page.
2. Choose the folder if you want; the default is `%LOCALAPPDATA%\Programs\CreatorOS`, per user, no
   administrator prompt.
3. It creates a **Desktop shortcut** and a **Start Menu entry**, both named `CreatorOS`.
4. It starts CreatorOS when finished.

The 0.8.0 artefact in this repository was measured on 2026-11-25: silent install exited 0, the
installer window offered "Juste pour moi" and marked the all-users option as requiring administrator,
and `scripts\verify-install.ps1 -Uninstall` reported **17/17** (installed exe, both shortcuts and icons,
bundled interpreter, migrate-before-serve, database at head, uninstall removing app + shortcuts while
keeping the data root). See `docs/evidence/P9.md` §F. The installer is unsigned, so a machine that still
enforces Smart App Control will refuse it — see the next section.

Verify the download first, if you like:

```powershell
Get-FileHash .\CreatorOS-Setup-0.8.0.exe -Algorithm SHA256
# compare with releases\0.8.0\SHA256SUMS.txt
```

### If Windows refuses to start the installer

The build is **unsigned** (no code-signing certificate), and Windows 11's **Smart App Control** blocks
newly built unsigned executables outright — you get "blocked by Device Guard policy", not a warning
with an "allow" button. Two options:

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
clean-machine test.

---

## B. First launch

1. The app starts its own backend on a random loopback port and opens the window. It takes ~10 s the
   first time (the database is created and migrated) and ~5 s afterwards.
2. If the backend cannot start, the window opens anyway and shows the error plus the log path. The log
   is `<data root>\logs\backend\backend.log`.
3. Left column: **Studio** (dashboard, AI infrastructure, model lab, settings), **Content library**,
   **Agent workforce**, **Social accounts**, **Publishing**, **Analytics**.

### The data root

Default `<PROJECT_ROOT>`. Override with the `CREATOROS_ROOT` environment variable before launching:

```powershell
$env:CREATOROS_ROOT = '<PROJECT_ROOT>'
& "$env:LOCALAPPDATA\Programs\CreatorOS\CreatorOS.exe"
```

The app never moves or deletes an existing data root. On first launch in an empty root it creates the
directory structure and migrates a fresh database to head (`b8c9d0e1f2a3`).

---

## C. What is where

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

## D. Verifying the installation

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

## E. Uninstalling

Installer-made installation: *Settings → Apps → Installed apps → CreatorOS → Uninstall*, or
`%LOCALAPPDATA%\Programs\CreatorOS\Uninstall CreatorOS.exe`.

Fallback installation: `powershell -File scripts\uninstall-unpacked.ps1`.

Either way your data root is left untouched; see `docs/UNINSTALLATION.md`.
