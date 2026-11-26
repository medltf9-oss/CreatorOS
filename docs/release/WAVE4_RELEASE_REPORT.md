# Wave 4 — production payload integrity and release decision closure

**Date:** 2026-11-26. **Branch:** `release/v1.0.0-readiness` (PR #7, draft). **Product version:**
0.8.0, unchanged. **Code commit for this wave:** `b10fe00` (2026-11-26T13:19:43+01:00); the final
commit on the branch is release evidence only. No tag, installer attachment, merge, signature, SAC
acceptance or licensing approval is claimed.

## Verdict

The four defects recorded in `WAVE3_RELEASE_REPORT.md` are closed with executable evidence:

1. the artifact SBOM is generated from the packaged payload, not the developer virtualenv;
2. first-party package metadata reports the shipped version 0.8.0, matching the desktop app;
3. the unrecorded `README.txt` is removed and every payload file is either owned by a distribution
   `RECORD` or explicitly classified;
4. the source SBOM is byte-reproducible from a fresh clone without a virtualenv or `node_modules`.

The release is still **not ready**. The certificate, publisher identity, clean SAC machine and the
FFmpeg/Ollama rights decisions remain owner-only blockers. This report does not convert any of them
into a pass.

## 1. The four defects

### 1.1 Artifact SBOM derives from the packaged payload

`scripts/generate_sbom.py --profile artifact` now requires `--payload` and reads every Python
component from `<payload>/*.dist-info/METADATA` and `RECORD`, never from `app/backend/.venv`.
`scripts/build-release.ps1` passes `releases/<v>/win-unpacked/resources/backend`. A payload
distribution outside `requirements-runtime.lock`, a lock distribution missing from the payload, an
unowned site-packages file, a stale first-party dist-info or a RECORD hash/size mismatch is fatal.

Fresh 0.8.0 payload: **2,645 files / 62,513,538 bytes / 33 distributions** (32 lock distributions +
`creatoros-backend`), **0 development-only**, canonical tree SHA-256
`49d3fb3126210bce11baeb46d069621e69f5441e6d3c83ea41e17f37397f225a`.

Artifact SBOM: **51 components** (33 Python, 18 non-Python), serial
`urn:uuid:8fa3f433-bbad-5548-a6ed-59835a2a1563`, installer SHA-256
`d3a73f608d903e72de35a5f2def18d31ed367dccbc4fe922ac7f739580e9cc07`. The separate
`runtime-inventory.py` report shows **0 Python components absent in either direction**, 0 size/hash mismatches and exit 0.

### 1.2 First-party version metadata

`scripts/payload_metadata.py` writes canonical `METADATA` (2.4), `WHEEL` and `RECORD` for the
actually shipped files, runs `--check` after the payload is assembled, and the build asserts desktop
and backend versions agree. The packaged metadata is `creatoros_backend-0.8.0.dist-info`; the build
fails on a stale `0.1.0` directory. The installed product remains 0.8.0 because no version bump was
authorised.

### 1.3 `README.txt` and complete payload provenance

`build-backend.ps1` removes CPython's unowned `Lib/site-packages/README.txt`; `payload_prune.py`
replays every distribution `RECORD`, removes out-of-lock distributions and rejects any remaining
unowned site-packages file. The inventory reports no unrecorded files. Its explicit exceptions are
counted, not hidden: **8 optional SQLAlchemy `.pyd` accelerators absent with pure-Python fallbacks**
and **9 `../../Scripts/*.exe` console wrappers outside the payload, not shipped**. Both are recorded
on the `creatoros-backend` component in the artifact SBOM.

### 1.4 Reproducible source SBOM

The source profile reads only `app/backend/requirements-runtime.lock` (32 pins) and the committed
`app/desktop/third-party-licenses.json` (14 direct dependencies with audited SPDX licences). It does
not read `.venv` or `node_modules`. Timestamp is the source commit time, not wall-clock time.

Fresh clone at `b10fe00`, no build output, no artifact SBOM: **48 components**, serial
`urn:uuid:55491264-5c86-556a-b907-4b689f343e40`, SHA-256
`38BCB7B8DBAA360C58800AB575632E2ED9EF00A2ED2A1514F733A877CD44D8DD`, byte-identical to the
development checkout at the same commit.

## 2. Final 0.8.0 build evidence

The Artifact results below come from `.\scripts\release-gate.ps1 -Stage Artifact -ReleaseVersion 0.8.0`;
the gate exits 1 on the six signing/certificate failures and has no expected-failure class.

| Item | Evidence |
| --- | --- |
| Installer | `CreatorOS-Setup-0.8.0.exe`, 139,888,520 bytes, SHA-256 `d3a73f60…e9cc07` |
| Blockmap | SHA-256 `1228dcf9…16ddd7` |
| Backend payload | 2,645 files, 62,513,538 bytes, tree `49d3fb31…f225a` |
| Runtime inventory | exit 0, 33 distributions, 0 violations, 0 development-only |
| Artifact SBOM | 51 components, 32 matched pypi distributions, 0/0 discrepancies |
| Source / prerequisites SBOM | 48 / 3 components |
| First-party metadata | `payload_metadata.py --check` passed, 0.8.0 |
| Backend suite | **1,653 passed, 30 skipped, 0 failed** (257 s) |
| Desktop checks | typecheck, unit tests and production build green in CI at `b10fe00`; no product-code change this wave |
| CI at `b10fe00` | run `45818643202`: backend **1,648 passed, 35 skipped**, 0 failed; desktop green |
| Clean clone | Source gate **13/13 PASSED**; `backend_lock.py --check` 32 distributions |
| Artifact gate (local) | all non-signing checks pass; **6 failures**, all signing/certificate (the gate has no expected-failure class and exits 1) |

The installer was packaged from `d80aedd`; `b10fe00` changes only verification scripts, tests and
release documents, so the packaged product bytes are unchanged. The artifact SBOM is bound to the
actual installer hash and payload tree and is not regenerated from a different tree.

## 3. Guard gaps found by independent review, then fixed

Specialist 3 probed the guard and found genuine fail-open behaviour. Fixed in `b10fe00`:

- `runtime-inventory.py` returned exit 0 when declared runtime dependencies were missing and when
  the SBOM claimed distributions absent from the payload. Both now enter `causes` and return 1; two
  regression tests cover them.
- `release-gate.ps1` never opened the payload. The Artifact/Release stages now run the inventory
  against `win-unpacked/resources/backend` and compare its tree hash and file count with
  `creatoros:payload-tree-sha256` / `creatoros:payload-files` (`artifact.payload-inventory`,
  `artifact.payload-sbom`).
- Clean-clone setup exposed that `backend_lock.py` needs the release-tooling `packaging` module,
  which is not a runtime dependency. `bootstrap.ps1` now installs it explicitly; it remains outside
  the lock and the payload.

## 4. Specialist deliverables

| # | Report | Contribution |
| ---: | --- | --- |
| 1 | `wave4/01-sbom-architecture.md` | Source/artifact SBOM architecture, provenance and reproducibility review |
| 2 | `wave4/02-metadata.md` | First-party metadata/wheel version audit |
| 3 | `wave4/03-runtime-prereq-artifact-guard.md` | Runtime composition, prerequisite workflow, artifact-guard fail-open probes and defect register |
| 4 | `wave4/04-ffmpeg-ollama-decision.md` | Dated FFmpeg/Ollama evidence, unknowns and three distribution options; no legal conclusion |
| 5 | `wave4/05-installer-architecture.md` | Installer layout, NSIS, signing authority, notices, prerequisites and update-path review |
| 6 | `wave4/06-clean-clone-ci-audit.md` | Independent clean-clone lock/gate/SBOM/CI audit; found the CI event mislabel and the uncommitted-SBOM caveat |
| 7 | `wave4/07-gate-evidence-audit.md` | Independent matrix/doc evidence audit; found stale Wave 3 install and signature-inventory claims |

Reports are retained outside the repository under `D:\P\wave4\`; they are reviews, not release
approvals.

## 5. FFmpeg / Ollama decision package

Verified facts (specialist 4, hashes recomputed):

- Nothing in `extraResources` or the NSIS script conveys FFmpeg or Ollama. The app opens the Ollama
  vendor URL; `ffmpeg.exe`/`ffprobe.exe` are local tools the wizard verifies.
- FFmpeg is a GPLv3 build (`--enable-gpl --enable-version3 --enable-libx264`, static libx264); the
  manifest says `approved_for_bundling: false` because corresponding source/build scripts/notices
  for that exact binary are not assembled. `ffprobe.exe` has no digest in `prerequisites.v1.json`
  even though the app executes it.
- Ollama's installer is validly signed by Ollama Inc.; `approved_for_bundling` is false. The MIT
  code licence does not establish rights to the installer's GPU runtime payload or model weights.
- A single asset containing installer + Ollama + FFmpeg would exceed GitHub's 2 GiB per-file limit;
  GitHub permits separate assets, but redistribute rights are still required. Offline bundling
  therefore requires a split, measured design.

**Director recommendation, not an owner decision:** keep FFmpeg as an external present-only
prerequisite; offer Ollama through a user-consented, hash- and signature-verified guided download
from the official source; do not enable bundling until the owner obtains written rights and a
corresponding-source plan. If full offline is mandatory, use separately signed, measured assets,
not one oversized installer. No redistribution flag was changed.

## 6. Recommended installer architecture

From specialist 5, for the eventual 1.0.0 build:

- Keep `resources\backend` as an `extraResources` directory (a spawned executable cannot live in
  `app.asar`); add a payload manifest bound to the inventory tree hash and tighten the filter so
  `-SkipBackend` cannot ship host leftovers.
- Per-user NSIS at `%LOCALAPPDATA%\Programs\CreatorOS`, `perMachine:false`, `allowElevation:false`;
  remove `allowToChangeInstallationDirectory` unless a Program Files path is actually supported.
- One signing authority at packaging time so setup and embedded uninstaller share a signer;
  `sign-release.ps1` becomes a verifier, not a second signer.
- Assert that `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md` are inside `resources\` before
  packaging, and surface them at install time.
- Manual signed, digest-verified reinstall for 1.0.0; keep `verifyUpdateCodeSignature` honest and do
  not enable auto-update.

## 7. Remaining owner actions

1. Obtain a CA-issued code-signing certificate and approve the publisher subject; protect the key.
2. Provide a clean Smart App Control-on machine for install, Defender and SmartScreen evidence.
3. Decide FFmpeg and Ollama rights/routes in writing; if bundling, supply corresponding source and
   notices and approve the separate-asset design.
4. Decide whether 1.0.0 requires full offline operation; that choice determines the asset layout.
5. Decide whether to bind the source SBOM to the commit: `releases/` is ignored, and the gate currently checks structure and a 30-day freshness window but not that the document's timestamp equals `HEAD`. A clean clone regenerates it correctly; an old SBOM in a developer checkout can still pass. Adding a timestamp/HEAD equality gate is the smallest fix.
6. Accept or schedule the residual engineering defects below. Severity is Director triage, not a
   risk acceptance or a release approval; none is fixed in Wave 4.

| ID | Defect | Severity | Owner | Disposition |
| --- | --- | --- | --- | --- |
| D-5 | Alternate `npm run package` path bypasses the release build/gate | Medium | Release engineering | Gate or remove before 1.0.0 |
| D-6 | Unsigned/substituted interpreter accepted; no digest pin | Medium | Release engineering / security | Enforce a pinned digest or hard signature policy |
| D-7 | `ffprobe.exe` is executed but not byte-verified; `companion_filenames` is dead config | Medium | Prerequisite tooling | Pin and verify, or refuse an unverified sibling |
| D-8 | Ollama `consent.required` is exposed but not rendered/recorded | Medium | Product / setup | Wire UI and audit record |
| D-9 | `install` block is dead configuration | Low | Product / prerequisites | Remove it or implement and guard the action |
| D-10 | Stale `releases/0.8.0` defaults in `runtime-inventory.py` | Low | Tooling | Derive from project version or require explicit arguments |
| D-11 | Mid-walk `OSError` exits 1, not the documented 2 | Low | Tooling | Align exit classification |
| D-12 | FFmpeg URL/409 override UX is ambiguous | Low | Product | Clarify before acceptance |
| D-13 | Two sources for the `ffprobe` digest | Low | Prerequisite tooling | Use one manifest source |

## 8. Not claimed

No v1.0.0 tag, no version bump, no signed installer, no RFC 3161 timestamp, no publisher approval,
no SAC/clean-machine acceptance, no FFmpeg/Ollama legal clearance, no installer attachment or
publication, and no merge of PR #7. The product remains v0.8.0.