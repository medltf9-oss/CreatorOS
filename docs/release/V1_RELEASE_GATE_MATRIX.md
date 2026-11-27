# v1.0.0 release gate matrix

**Status date:** 2026-11-27
**Release candidate:** unsigned 0.8.0 build at `35924e5`; final code HEAD `386d322`; not publishable. Public release remains
**v0.8.0** (source only).
**Wave 5 code commit:** `386d322` on `release/v1.0.0-readiness` (PR #7, draft). The installer was
built at `35924e5`; `386d322` adds the independent-QA test/comment/doc fixes and does not change
product code.
**Rule:** a mandatory gate passes only with evidence for the exact release commit and artifact.
`FAIL`, `BLOCKED`, `NOT RUN` and `UNKNOWN` all block the release. This matrix is the authoritative
summary; details live in the linked documents.

| # | Gate | Status | Evidence | What closes it |
| ---: | --- | --- | --- | --- |
| 1 | Source integrity | **PASS at `386d322`** | Source gate **13/13 PASSED** in the Artifact run on 2026-11-27; clean tracked tree; source SBOM 48 components, serial `b9d20cd7…`, SHA-256 `8722fbd7…`. A fresh-clone rerun was not repeated for this commit; the Wave 4 clean-clone evidence is `b10fe00` | Re-run in a fresh clone for the signed final commit |
| 2 | CI on the exact commit | **PASS at `386d322`** | GitHub Actions run `45995301334` at the final code-bearing commit `386d322`: **SUCCESS**, backend and desktop jobs. Run `45992648710` at `35924e5` was also green; the run at `aee8e4c` failed one environment-dependent setup assertion, fixed in `35924e5` | Re-run on the signed final commit |
| 3 | Backend/frontend tests | **PASS at `386d322`** | Local backend **1,672 passed, 30 skipped, 0 failed** in 273.86s on the final test tree, including the runtime-manifest, provenance, inventory-error and setup tests. Desktop **227 passed, 0 failed**; setup E2E passed | Re-run on the signed final commit |
| 4 | Typecheck, build, lint | **PASS at `386d322`** | `ruff check src tests` and all changed release scripts pass; `pnpm typecheck` passes; desktop tests **227 passed**; `pnpm build` ran in the release build. `scripts\e2e_setup.py`/`scripts\normalize_eol.py` retain pre-existing findings outside the product lint scope | Re-run on the signed final commit |
| 5 | CA-issued certificate | **BLOCKED** | No CA-issued certificate with a private key in either Windows store; `sign-release.ps1 -Check` reports `CSC_LINK : none` | Owner orders a certificate (`OWNER_CERTIFICATE_ACTION.md`) |
| 6 | Private-key protection | **BLOCKED** | No token, HSM or signing service exists | Certificate issued to hardware or a CA signing service |
| 7 | Signature inventory | **PASS (baseline); NOT RUN on a signed candidate** | `docs/release/SIGNATURE_INVENTORY.md`: 64 PE files in the 0.8.0 unpacked tree (Electron plus backend), 44 vendor-signed, 20 unsigned. The minimized backend accounts for 51 PE files (1 `python.exe`, 39 `.pyd`, 11 DLLs) and has no PyInstaller bootloader; the installer adds Electron's own PE set | Regenerate on the packaged release candidate |
| 8 | Authenticode on every CreatorOS-owned PE | **NOT RUN** | No signed build exists. Policy: sign the installer and uninstaller; `CreatorOS.exe` intentionally keeps Electron's valid vendor signature unless `sign-release.ps1 -IncludeAppExe` is explicitly passed (replacing it can stop the app starting). The install harnesses refuse an artifact unless its bytes match `SHA256SUMS.txt` and its signature satisfies `-RequireSignature` (or `-AllowUnsigned` for local development only) | Sign the installer and uninstaller; `signtool verify /pa` on the installer; record the app-EXE policy and verify every CreatorOS-owned inner PE |
| 9 | RFC 3161 timestamp | **BLOCKED** | Nothing to timestamp | `/tr` + `/td SHA256` after signing; verify the token |
| 10 | Publisher identity matches owner approval | **BLOCKED** | Publisher name not yet chosen | Owner approves the exact subject (`OWNER_CERTIFICATE_ACTION.md` §2) |
| 11 | Smart App Control enforced | **BLOCKED** | Audit machine: build 26300, `VerifiedAndReputablePolicyState = 0` (off); assembler refuses | Clean SAC-on machine installs the signed artifact |
| 12 | Defender active, no detection, no exclusions | **UNKNOWN** | Capture tooling exists (`capture-security-state.ps1`); the audit machine is not the clean test environment | Run the capture on the clean SAC-on machine during acceptance |
| 13 | SmartScreen behaviour | **NOT RUN** | No signed, published asset exists; no claim is made | Record the actual prompt on the clean machine; do not promise "no warnings" |
| 14 | Clean install of the exact artifact | **BLOCKED** | Depends on #5, #11 and the final artifact | Install the downloaded signed installer on the clean machine |
| 15 | App startup and health | **PASS for the Wave 5 payload; INSTALL NOT RUN** | Fresh payload 2,645 files / 62,520,376 B / 33 distributions / 0 development-only; inventory exit 0; bundled `creatoros version` 0.8.0, migrate to head `b8c9d0e1f2a3`, `/health/live` live. No install was run against the `a45ffb1e…` installer; the 17/17 `verify-install.ps1` result belongs to the older `AD8B7772…` artifact | Run `verify-install.ps1 -Uninstall` and `production-health.ps1` on the signed candidate |
| 16 | Prerequisites: versions, hashes, signatures, consent | **TOOLING PASS; FINAL NOT RUN; D-7/D-8 CLOSED** | `prerequisites.v1.json` pins Ollama, FFmpeg and ffprobe as three components with exact sizes and SHA-256. Presence is stat-only; detection never executes FFmpeg. Ollama consent is rendered, recorded through `POST /setup/prerequisites/ollama/consent`, required before the download link and included in completion audit metadata. D-7/D-8 are closed with regression tests; the owner gates in #18/#19 still decide redistribution | Verify the exact prerequisite bytes during clean-machine acceptance |
| 17 | Data preservation across upgrade/uninstall | **PASS for the superseded 0.8.0 build; NOT RUN for the Wave 5 candidate** | The 17/17 install/uninstall data-root result belongs to the superseded `AD8B7772…` installer. `production-health.ps1` verifies installer bytes and signature before upgrade/uninstall, but it has not been run against `a45ffb1e…` | Run `production-health.ps1 -IncludeUpgrade -RequireSignature` and `-IncludeUninstall -RequireSignature` on the signed candidate |
| 18 | FFmpeg rights | **BLOCKED** | GPLv3 build with libx264, `approved_for_bundling: false`; no written redistribution approval or corresponding-source plan. `DISTRIBUTION_DECISIONS.md` records the GPLv3 §1/§4/§5/§6 questions and five architectures | Owner approves a route: external prerequisite (recommended for 1.0.0), user-consented verified download, compliant redistribution with corresponding source, or an alternative build |
| 19 | Ollama rights | **BLOCKED** | Official installer verified but `approved_for_bundling: false`; no written redistribution approval. The model weights and GPU runtimes are separate licences and are not covered by Ollama's MIT code licence (`DISTRIBUTION_DECISIONS.md`) | Owner obtains written approval for the exact installer plus runtime inventory, or keeps the official-source online route |
| 20 | Offline/online payload measured | **BLOCKED (measurements complete)** | Wave 5 0.8.0 installer 139,882,940 B; backend payload 62,520,376 B in 2,645 files; Ollama installer 1,953,587,080 B; FFmpeg pair 220,965,888 B. Total **2,314,435,908 B**, which is **166,952,260 B over** GitHub's 2,147,483,648-byte per-file limit; the earlier 2,317,608,020 total was arithmetic error and 2,314,441,488 used the superseded installer. GitHub permits separate assets (1,000 assets, no total cap). Online (hash-verified official download) is the only route with current approvals; separate assets still need the redistribution rights in #18/#19 | Owner approves FFmpeg/Ollama rights and the online-vs-separate-assets design; then measure the real 1.0.0 installer |
| 21 | SBOM (source, prerequisites and distribution) | **PASS for the Wave 5 0.8.0 candidate; NOT RUN FOR 1.0.0** | Source 48 components, serial `b9d20cd7…`, SHA `8722fbd7…`; prerequisites 3 components, SHA `eda64f9f…`; artifact 51 components (33 Python), serial `ca732e3e…`, SHA `3442b0cc…`. Artifact binds installer `a45ffb1e…`, payload tree `0475d2b1…`, 2,645 files, runtime manifest `e6b25a84…` and source commit `35924e5`. The gate re-ran the inventory: 0 absent in either direction. The old 2,644/2,645 mismatch is closed | Generate and attach all three documents for the signed 1.0.0 release commit and artifact |
| 22 | Checksums for final assets | **PASS for the 0.8.0 rebuild; FINAL NOT RUN** | `artifact.manifest-scope` re-hashes every file named in `SHA256SUMS.txt`; the Wave 5 installer (`a45ffb1e…`, 139,882,940 B) and blockmap match the manifest. No final 1.0.0 assets exist yet | Publish `SHA256SUMS.txt` covering every attached asset |
| 23 | Screenshots | **NOT RUN** | None captured from a 1.0.0 build | Capture 10–14 genuine screenshots from the installed release candidate |
| 24 | README and release metadata | **PARTIAL** | 0.8.0 README is accurate for the source release; no v1.0.0 notes, download table or gallery | Write the v1.0.0 README/release notes after the artifact exists |
| 25 | Published asset re-downloaded and hashed | **BLOCKED** | No v1.0.0 release | Download the public asset on a second machine, hash-match, re-install |
| 26 | `v1.0.0` tag on the approved commit | **BLOCKED** | Gates 5–25 are not closed | Tag only after every mandatory gate passes |

## Summary

- **Passing now:** source integrity and the local full regression at `386d322`; D-5..D-13 tooling
  and tests; runtime manifest; source/prerequisites/artifact SBOMs and their bindings; payload
  inventory; artifact manifest; build provenance; signature inventory baseline.
- **Artifact gate:** **25 passed, 6 failed**. All six failures are signing-related: no certificate,
  publisher, signature, timestamp or `signtool` verification.
- **Blocked by the owner:** certificate and publisher identity, clean SAC-on machine, FFmpeg
  redistribution, Ollama redistribution and the payload design.
- **Not started because no signed artifact exists:** Authenticode, timestamp, clean install,
  Defender/SmartScreen evidence, published-asset verification and the tag. CI at `386d322` is
  green (run `45995301334`), but that is not a signed-release pass.

The release stays at **v0.8.0** until the blocked and not-run gates pass. The Wave 5 candidate is
a real unsigned build, not a releasable artifact.
