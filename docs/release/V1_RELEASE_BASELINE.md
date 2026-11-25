# CreatorOS v1.0.0 release baseline

**Recorded:** 2026-11-25  
**Repository:** `https://github.com/medltf9-oss/CreatorOS`  
**Public branch:** `main`  
**Commit at audit:** `fc2a3e2`  
**Current release:** `v0.8.0` (source-only GitHub Release)  
**Target:** `v1.0.0` signed Windows x64 installer

This baseline records what exists before any v1.0.0 change. It deliberately contains no
machine-specific paths, account names or identity data.

## 1. Application and build stack

| Item | Baseline |
| --- | --- |
| Desktop version | `0.8.0` in `app/desktop/package.json` |
| Backend version | `0.8.0` in `pyproject.toml` and `core/settings.py` |
| Shell | Electron 45.4.1, React 19, TypeScript 7, Vite 8, Tailwind 4 |
| Backend | Python 3.12, FastAPI, SQLAlchemy, Alembic, SQLite |
| Installer | electron-builder 26.18.0, NSIS, per-user, x64 |
| Licence | Apache-2.0 for CreatorOS; third-party notices in repository |
| Public CI | Backend and desktop checks green on `fc2a3e2` |

The backend is shipped as a **signed Python interpreter plus pure-Python payload**, not as a
newly compiled executable. The layout is:

```text
installer_builds/backend/
  python.exe, python312.dll, python3.dll, vcruntime*.dll
  DLLs/  Lib/  Lib/site-packages/
  alembic.ini  migrations/
  src/creatoros/
```

The rationale is recorded in `docs/ADR/ADR-34-Release-Packaging.md`: Windows Smart App Control
blocks newly built unsigned executables, whereas the Python Software Foundation interpreter
retains its Authenticode signature.

## 2. Current installer artifact

The local, previously tested artifact is:

| Field | Value |
| --- | --- |
| File | `CreatorOS-Setup-0.8.0.exe` |
| Size | 143,055,052 bytes |
| SHA-256 | `AD8B7772B6F72AD5D1FC51C196B46308CB20ACD2001F3A0AC420875D073A7A4B` |
| Signature | Not signed |
| Install scope | Per-user, no administrator prompt |
| Data on uninstall | Preserved |

It is **not attached** to the public v0.8.0 release. It predates the licence/NOTICE
`extraResources` change and has no trusted timestamp.

## 3. Signing state

At audit time:

- `CSC_LINK` is unset.
- `CSC_KEY_PASSWORD` is unset.
- `TIMESTAMP_URL` is unset.
- No code-signing certificate for the publisher identity is present.
- `scripts/sign-release.ps1` supports a PFX or certificate-store thumbprint and verifies the
  resulting chain and timestamp, but there is no CA-issued publisher certificate to use.
- `scripts/sign-release.ps1 -SelfTest` uses a throwaway self-signed certificate only to prove
  the local signing, timestamping and tamper-detection path. It is **not** release signing.

A CA-issued certificate, its private key and the publisher identity verification are owner
actions. The release cannot be labelled signed until that exists.

## 4. Windows security baseline

The audit machine is Windows 11 x64, build 26300.

| Control | State |
| --- | --- |
| Smart App Control (`VerifiedAndReputablePolicyState`) | `0` — **off** |
| Defender real-time protection | On |
| Defender antivirus | On |

This machine is therefore **not** valid evidence for the v1.0.0 Smart App Control gate. The
previous installer acceptance was performed after Smart App Control was disabled and is not
clean-machine evidence.

## 5. Dependency state

| Dependency | Baseline | Signature | Used by |
| --- | --- | --- | --- |
| Ollama `0.40.18` | Installed outside the repository; official installer downloaded to ignored `installer_builds/vendor` and verified | Valid, Ollama Inc. (DigiCert), RFC 3161 timestamp | Local model inference |
| FFmpeg `9.1-essentials_build-www.gyan.dev` | `<DATA_ROOT>\tools\ffmpeg\bin` | Not signed | Media probing, transcode, thumbnails |
| Python 3.12.10 | Bundled interpreter from python.org install | Valid, Python Software Foundation | Backend |
| Node/Electron | Build-time only; Electron runtime ships in installer | Vendor signature retained | Desktop shell |

FFmpeg build facts:

- Configuration: `--enable-gpl --enable-version3 --enable-libx264 ...`
- Reported licence: **GPLv3**, not LGPL.
- Source reference: `https://github.com/FFmpeg/FFmpeg/commit/74e4393595`
- The binaries are large static builds and are not Authenticode-signed.

This build may be used as an external, user-installed data-root tool, but it is **not**
approved for silent bundling into the v1.0.0 installer without a complete GPL compliance
decision (corresponding source, build scripts, and notices). That decision is unresolved.

## 6. Data locations

| Data | Current location |
| --- | --- |
| Application files | Per-user install directory chosen by NSIS |
| Data root | `CREATOROS_ROOT`, else `<USERPROFILE>\CreatorOS` |
| Database | `<DATA_ROOT>\data` (SQLite) |
| Media | Under the configured data root |
| Models | Ollama's own store; `OLLAMA_MODELS` can redirect it |
| Logs | `<DATA_ROOT>\logs` |
| Credentials | Electron `userData/creatoros-desktop` DPAPI vault |

The installer does not currently offer data-root selection, prerequisite detection or
first-run setup. Those are v1.0.0 work.

## 7. Gap against the v1.0.0 brief

| Area | Baseline | v1.0.0 requirement |
| --- | --- | --- |
| Version | 0.8.0 | 1.0.0 in desktop, backend, docs |
| Signing | No certificate; unsigned installer | CA-issued Authenticode + RFC 3161 timestamp |
| Smart App Control | Off on audit machine | Clean Windows 11, SAC enforcement on |
| Offline dependencies | User must install FFmpeg; Ollama external | Bundled/managed, pinned and verified |
| Installer UX | Standard NSIS pages, no prerequisite pages | Branded wizard with checks, locations, components |
| First run | No readiness wizard | Health/dependency/model setup with real inference test |
| FFmpeg licensing | GPLv3 binary, untracked | Resolved, pinned, notices, corresponding source |
| SBOM/provenance | Not generated | SPDX/CycloneDX SBOM, checksums, provenance |
| Release gate | Manual unsigned build | Fail-closed signed release pipeline |

## 8. Baseline conclusion

The source and unsigned local installer are usable for continued development. The mandatory
v1.0.0 gates — a real publisher certificate, a clean SAC-on Windows test, a resolved FFmpeg
redistribution, and a complete offline prerequisite payload — are not met. No v1.0.0 signed
installer can be truthfully published from this baseline.

## 9. Verification added after the baseline

- The official Ollama `0.40.18` installer was downloaded from the GitHub release. Its size is
  1,953,587,080 bytes and its SHA-256 is
  `AD2BB47AA969B21CF8DD81B2D9F9A6D5030270ED242EF55087118C37C510ACC2`, matching Ollama's published
  `sha256sum.txt`. Authenticode is `Valid`, signed by `Ollama Inc.` and timestamped by DigiCert.
- `scripts/release-gate.ps1` now enforces the Source, Artifact and Release gates and fails closed.
- `docs/release/V1_RELEASE_BLOCKERS.md` records each blocker and the owner action.
- `docs/release/DEPENDENCY_MANIFEST.md` records the verified payload hashes and the unresolved
  FFmpeg/Ollama redistribution decisions.
- Branch protection was verified: required checks are `Backend tests (Windows)` and
  `Desktop checks (Windows)`, strict, with force pushes disabled. The latest `main` CI run at
  `fc2a3e2` succeeded.

No source version was changed to `1.0.0`, because doing so would create an apparent release that the
mandatory gates cannot support. The next release commit must be made only after the owner supplies
the certificate and clean-machine environment, or explicitly approves a source-only milestone.
