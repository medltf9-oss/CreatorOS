# ADR-34 — Release packaging: signed interpreter bundle, vendor-signed shell, per-user installer

**Status:** Accepted, **implemented and verified on this machine** (installer build, install, launch,
analytics pages from the live database, uninstall with data preserved — 17/17 checks).

> **Correction (2026-11-25):** the Smart App Control value cited in Context item 2 is wrong for the
> audit machine, which reports `VerifiedAndReputablePolicyState = 0` (off, Windows build 26300). The
> packaging decision stands — it is the conservative reading either way — but the install and launch
> evidence above was **not** collected with Smart App Control enforced, and no clean SAC-on evidence
> exists yet. See `docs/release/V1_RELEASE_BLOCKERS.md` §1.

## Context

Three facts about this machine and this product collided:

1. **The backend is Python.** Shipping it means shipping either a compiled executable or an
   interpreter.
2. **Smart App Control is ON** (`HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy` →
   `VerifiedAndReputablePolicyState = 1`). It refuses to start *any* newly built executable it has no
   reputation for — not with a warning, but with `Code Integrity` event 3077 and
   "did not meet the Enterprise signing level requirements". There is no per-file allow: the user
   cannot click through it, and a self-signed certificate does not satisfy it either.
3. **There is no code-signing certificate** for this build (ADR-14: v1 ships unsigned).

The first packaging attempt was PyInstaller (onedir, 39 MB). It worked — twice — and then every
subsequent build was blocked. That is the worst possible failure mode for a release process: it
succeeds until it does not.

## Decision

**Ship copies of binaries that already have a publisher signature, and never ship a newly built one.**

| Artefact | What ships | Why it runs |
| --- | --- | --- |
| App shell | `CreatorOS.exe` = a byte-identical copy of `electron.exe` (`sha256` equal to the vendor file) | Electron's release binary is signed and reputable; an unmodified copy keeps that |
| Backend | `python.exe` + `python3.dll` + `python312.dll` + `DLLs/` + `Lib/` from the installed Python 3.12 (PSF-signed), plus a pure-Python payload in `Lib/site-packages` | the interpreter is signed and reputable; the payload contains no new PE image |
| Installer | NSIS setup exe built by electron-builder | **this is the one unsigned, newly built executable** — see Consequences |

Concretely:

* `electron-builder.yml` sets `signAndEditExecutable: false` and `disableAsarIntegrity: true`. Either
  one alone is enough to rewrite `electron.exe` and break its signature; with both off the file is
  untouched. The cost is that the exe carries Electron's icon and version metadata, so the brand is
  applied where it is actually seen: `resources/icon.ico` for the window and taskbar
  (`electron/paths.cjs`) and `build/installer.nsh` recreates both shortcuts with that icon.
* `scripts/build-backend.ps1` assembles the interpreter bundle from the venv's own `sys.base_prefix`,
  asserts `Get-AuthenticodeSignature` reports `Valid`, prunes the stdlib of dev-only packages, and
  then **runs** the result against an empty data root: `version`, `migrate` to head, start the API,
  read `/health/live`.
* The backend is an `extraResources` directory, not part of `app.asar`: a process cannot be spawned
  out of an archive.
* `scripts/install-unpacked.ps1` exists because of the installer gap below. It performs exactly what
  the installer does — copy, Desktop shortcut, Start Menu shortcut, "Installed apps" entry — without
  the NSIS wrapper, so the application can be installed on a machine that blocks unsigned installers.
  `scripts/uninstall-unpacked.ps1` reverses it and never touches the data root.

## Consequences

**The installer is the only artefact that Smart App Control may refuse.** It is a newly built unsigned
executable, so on this machine it is blocked intermittently: two builds ran, later builds were refused
outright (`cmd.exe … attempted to load …\CreatorOS-Setup-0.8.0.exe`, Device Guard policy). Nothing in
the application can fix that. The two honest remedies are:

* **Sign the build.** Supply `CSC_LINK` (a `.pfx`) and `CSC_KEY_PASSWORD`, and set
  `signAndEditExecutable: true` / remove `disableAsarIntegrity`. electron-builder signs the installer,
  the uninstaller and the app exe; Smart App Control then accepts the build and the exe regains its
  own icon and metadata. This is the correct path for distribution.
* **Install without the installer** on a machine that blocks it: `scripts\install-unpacked.ps1`,
  documented in `docs/INSTALLATION.md` and `docs/RELEASE_NOTES_0.8.0.md`.

Other consequences:

* Backend bundle size is about 62 MB rather than 39 MB (measured 64,623,559 bytes after the dev-package prune; the 0.8.0 payload shipped 98,640,439 bytes because it copied the developer virtualenv). A traceback from the shipped backend names a real
  file, and an operator can start it by hand (`resources\backend\python.exe -m creatoros serve`).
* The shipped backend is the same Python version the test suite runs against, because the bundle is
  built from the venv's own base interpreter rather than a downloaded one.
* Nothing here changes a Windows security setting. Smart App Control stays ON; the build adapts to it.
* If the project later adopts signing, three things flip together: `signAndEditExecutable`,
  `disableAsarIntegrity`, and the shortcut-icon workaround in `installer.nsh` (which stays harmless but
  becomes unnecessary).

### Correction (Phase 10, measured)

This ADR assumed that keeping `signAndEditExecutable: false` preserves "the Authenticode signature
Electron ships". Measured on the build machine, that premise is wrong: the Electron binary that npm
installs carries **no** Authenticode signature at all.

```
app/desktop/node_modules/electron/dist/electron.exe   sha256 a547b53f...   Get-AuthenticodeSignature: NotSigned
releases/0.8.0/win-unpacked/CreatorOS.exe             sha256 a547b53f...   Get-AuthenticodeSignature: NotSigned
```

The *decision* stands, for a different and still valid reason: `signAndEditExecutable: false` keeps the
packaged executable byte-identical to the file the vendor shipped, which is verifiable (the two hashes
above) and is what makes the build reproducible from npm's artefact. What changes is the consequence for
signing: signing `CreatorOS.exe` would not overwrite a vendor signature, because there is none to
overwrite. `scripts/sign-release.ps1` therefore signs it by default when a certificate exists, and skips
it only if it is already validly signed by someone else. See `docs/CODE_SIGNING.md` section 3.

The Smart App Control paragraph above is also now recorded as measured rather than assumed: the **same**
artefact (`sha256 0ac5865c...`) was refused at launch in Phase 9 and installed with 12/12 checks in
Phase 10. An unsigned build cannot be distributed on that basis, which is the argument for signing
rather than against it.

**Post-acceptance measurement (2026-11-25).** The final artefact (`sha256 AD8B7772...`) was refused at
spawn while `VerifiedAndReputablePolicyState = 1`, and packaging itself stopped because electron-builder
executes that stub to generate the uninstaller. After the machine owner turned SAC off (state 0) the same
build packaged and `scripts/verify-install.ps1 -Uninstall` passed 17/17. This does not relax the decision:
the shipping artefact is still unsigned, and an SAC-enabled clean machine remains untested. See
`docs/evidence/P9.md` §F.
