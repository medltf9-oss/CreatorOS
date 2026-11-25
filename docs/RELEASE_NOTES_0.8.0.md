# CreatorOS 0.8.0 — release notes

**Release date:** 2026-11-24 · **Database head:** `b8c9d0e1f2a3` (48 tables, no migration in this
release) · **Installer:** `CreatorOS-Setup-0.8.0.exe` (143,055,052 bytes; 136.4 MiB; `sha256
AD8B7772B6F72AD5D1FC51C196B46308CB20ACD2001F3A0AC420875D073A7A4B`) · **Unsigned**

This release finishes Phase 8 (analytics and performance intelligence) and Phase 9 (production
packaging). It is the first version that installs and runs without an IDE, a terminal, Python, Node or
pnpm.

It is also the first **public, open-source** release, licensed under Apache-2.0, with a complete
visual redesign: a graphite-blue workspace, a deep-blue navigation rail, mint/rose/lavender accents,
one light feature panel, a purpose-built light theme, and a collapsible icon rail. The design system
is documented in [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md).

---

## What is in it

### Analytics (Phase 8, complete)

12 Analytics Center pages: Overview, Platform comparison, Account performance, Publication detail,
Content comparison, Campaign analytics, Video performance, Audience growth, Hashtag analysis,
Recommendations, Reports, Analytics settings.

Behind them: 129 provider metrics with verbatim provider names, an append-only observation ledger with
ten availability states (an unavailable metric is never a `0`), six performance windows, sample-gated
recommendation rules with mandatory evidence, 53 authenticated API routes, three deterministic agent
workflows, and two independent human gates (accepting a recommendation is not a publishing approval).

New in this release, relative to 0.7.x:

* **Report formats** — JSON, CSV and PDF from one content model; the digest is taken from the bytes
  written, so what the desktop verifies is what was generated.
* **Retention** — a per-category policy that expires analytics history and never touches business
  history. Preview first, execute only after typing `DELETE`, audited, batched, integrity-checked.
* **Version watch** — records the pinned provider version and the latest documented one, flags an
  observation older than 90 days as stale, and has no code path that upgrades the pin.
* **Diagnostics** — `/version` and `/analytics/diagnostics` report capability as facts or as an
  explicit "never run".

### Packaging (Phase 9)

* A real Windows installer: per-user, no administrator prompt, Desktop and Start Menu shortcuts with
  the CreatorOS icon, an entry under *Installed apps*, and an uninstaller that leaves your data alone.
* A branded application icon, rendered at every size from one SVG (`assets/brand/`).
* The backend ships as a signed Python interpreter plus a pure-Python payload, so the installed app
  starts its own API with nothing else installed.
* The shell resolves the backend correctly when packaged, migrates the database before starting the
  API, takes a single-instance lock, and shows the real error plus the log path if it cannot start.

---

## Installing

```
CreatorOS-Setup-0.8.0.exe
```

### ⚠ This build is unsigned

There is no code-signing certificate for it. On Windows 11 with **Smart App Control** enabled — the
default for new installations — Windows refuses to start a newly built unsigned executable. That is a
block, not a warning: there is no "run anyway". This was measured directly: with Smart App Control at
its default (`VerifiedAndReputablePolicyState = 1`) the same installer exited at spawn with "an
application control policy has blocked this file" (CodeIntegrity 3118), and electron-builder could not
even generate the uninstaller because that step executes the freshly built stub. To run the final
acceptance the machine owner turned Smart App Control off first; the 17/17 install result below was
measured after that, so it does **not** claim that an SAC-enabled machine will run the installer.

Two supported ways forward:

1. **Install without the installer** (identical result, no NSIS wrapper):

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-unpacked.ps1 -Launch
   ```

2. **Sign the build** and rebuild:

   ```powershell
   $env:CSC_LINK = 'C:\path\to\certificate.pfx'
   $env:CSC_KEY_PASSWORD = '…'
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-release.ps1
   ```

   `build-release.ps1` signs the installer, the uninstaller and the app executable, verifies
   the signature and the chain, and only then writes `SHA256SUMS.txt`. See `docs/CODE_SIGNING.md`,
   including why signing the app executable does not overwrite a vendor signature here: the Electron
   npm binary is itself unsigned.

CreatorOS does not ask you to weaken a security setting: option 1 needs no security change, and option
2 is the correct long-term fix. Turning Smart App Control off only to run an unsigned build is not a
supported distribution practice, and the clean-machine test on an SAC-enabled Windows 11 installation
remains outstanding.

---

## Verified on this machine

| | |
| --- | --- |
| Backend test suite | **1577 passed, 9 skipped** (243 s, 62 warnings) |
| Frontend test suite | **200 passed** |
| `ruff check src tests` | clean |
| `tsc --noEmit`, `vite build` | clean; initial shell 544 kB (137 kB gzip), analytics core 159 kB (34 kB gzip) loaded only when an Analytics page is opened |
| Installer | built, installed, launched, uninstalled: **17/17 checks** (2026-11-25, after the machine owner disabled Smart App Control) |
| Installed app | migrated, started its backend from `resources\backend\python.exe`, rendered the dashboard and the Analytics Reports / Settings pages from the live database |
| Production health (Phase 10) | **35/35** checks: all 49 pages open with no error card, the DPAPI vault seals and destroys a probe secret, the window closes with no orphan process, and a launch/close cycle changes no data |
| Upgrade and uninstall (Phase 10) | **45/45** with the upgrade path and uninstall - every row in place afterwards |
| Data | a separate acceptance data root was preserved across install, upgrade and uninstall; the user's existing data root was not touched |

## Known limits

* **No live provider verification.** There is no Meta app, no TikTok app and no connected social
  account on this machine, so no Facebook, Instagram or TikTok metric has ever been collected for real.
  Everything provider-facing is fixture-tested; `docs/evidence/P8.md` states this in its first
  paragraph.
* **No hardware video encoding.** FFmpeg runs with libx264; NVENC was rejected by a test encode
  (Phase 5 evidence).
* **No AI models are bundled.** The app expects a local Ollama installation; without it the AI pages
  report the gateway as unavailable and everything else works.
* **FFmpeg is a data-root tool, not part of the installer.** It lives at
  `<DATA_ROOT>\tools\ffmpeg\bin`; a brand-new data root that has not run the setup scripts reports
  "FFmpeg not found" (measured: production health 34/35 in that state) and media operations that need
  it are unavailable until it is installed.
* **Unsigned installer**, as above - and with no certificate available, `scripts/sign-release.ps1 -Check`
  reports `CSC_LINK: none`; the signing path is proven only with a throwaway certificate (`-SelfTest`).
* Report *scheduling* is not implemented; a report is generated on request and kept.
