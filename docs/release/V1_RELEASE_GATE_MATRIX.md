# v1.0.0 release gate matrix

**Status date:** 2026-11-26
**Release candidate:** none. Public release remains **v0.8.0** (source only).
**Readiness code commit:** `ab4499a` on `release/v1.0.0-readiness` (PR #7, draft); later commits on
this branch are release documents only.
**Rule:** a mandatory gate passes only with evidence for the exact release commit and artifact.
`FAIL`, `BLOCKED`, `NOT RUN` and `UNKNOWN` all block the release. This matrix is the authoritative
summary; details live in the linked documents.

| # | Gate | Status | Evidence | What closes it |
| ---: | --- | --- | --- | --- |
| 1 | Source integrity | **PASS (readiness commit)** | `release/v1.0.0-readiness` at `ab4499a`; `git fsck --full` reports only dangling blobs (normal after commits); Source gate **15/15** on the readiness tree and clean clone **13/13** at `5812915`; later commits are tests/docs | Re-run on the final release commit; keep the tree clean |
| 2 | CI on the exact commit | **PASS at `ab4499a`** | GitHub Actions runs `45730731847` (pull request) and `45730732090` (dispatch): backend **1,630 passed, 36 skipped**; desktop checks succeeded. The earlier run at `5812915` failed on two environment assumptions (source-SBOM venv resolution and the setup E2E fixture), both fixed in `ab4499a` | Require a fresh green run on the final release commit |
| 3 | Backend/frontend tests | **PASS (local at `ab4499a`; CI at `ab4499a`)** | Wave 3 local: backend **1,636 passed, 30 skipped, 0 failed**; desktop **227 passed**; setup E2E **117/117** backend checks and **3/3** UI tests. CI: backend **1,630 passed, 36 skipped** (the additional skips are the environment-bound tests); desktop unit suite green with the 3 GUI journeys skipped | Re-run on the final commit and require fresh CI |
| 4 | Typecheck, build, lint | **PASS (local and CI at `ab4499a`)** | `ruff check src tests` all checks passed; `pnpm typecheck` exit 0; `pnpm build` exit 0; desktop 227/227; CI lint and type-check steps green | Re-run on the final commit and require fresh CI |
| 5 | CA-issued certificate | **BLOCKED** | No CA-issued certificate with a private key in either Windows store; `sign-release.ps1 -Check` reports `CSC_LINK : none` | Owner orders a certificate (`OWNER_CERTIFICATE_ACTION.md`) |
| 6 | Private-key protection | **BLOCKED** | No token, HSM or signing service exists | Certificate issued to hardware or a CA signing service |
| 7 | Signature inventory | **PASS (baseline)** | `docs/release/SIGNATURE_INVENTORY.md`: 64 PE files measured at 0.8.0 (PyInstaller payload), 44 vendor-signed, 20 unsigned, sign policy defined. The current minimized backend bundle has 51 PE files (1 `python.exe`, 31 extension modules, 19 DLLs) and no PyInstaller bootloaders; the installer adds Electron's own PE set | Regenerate on the packaged release candidate |
| 8 | Authenticode on every CreatorOS-owned PE | **NOT RUN** | No signed build exists. The install harnesses now refuse to execute an artifact unless its bytes match `SHA256SUMS.txt` and its signature satisfies `-RequireSignature` (or `-AllowUnsigned` for local development only) | Sign `CreatorOS.exe`, installer and uninstaller; `signtool verify /pa /all` |
| 9 | RFC 3161 timestamp | **BLOCKED** | Nothing to timestamp | `/tr` + `/td SHA256` after signing; verify the token |
| 10 | Publisher identity matches owner approval | **BLOCKED** | Publisher name not yet chosen | Owner approves the exact subject (`OWNER_CERTIFICATE_ACTION.md` §2) |
| 11 | Smart App Control enforced | **BLOCKED** | Audit machine: build 26300, `VerifiedAndReputablePolicyState = 0` (off); assembler refuses | Clean SAC-on machine installs the signed artifact |
| 12 | Defender active, no detection, no exclusions | **UNKNOWN** | Capture tooling exists (`capture-security-state.ps1`); the audit machine is not the clean test environment | Run the capture on the clean SAC-on machine during acceptance |
| 13 | SmartScreen behaviour | **NOT RUN** | No signed, published asset exists; no claim is made | Record the actual prompt on the clean machine; do not promise "no warnings" |
| 14 | Clean install of the exact artifact | **BLOCKED** | Depends on #5, #11 and the final artifact | Install the downloaded signed installer on the clean machine |
| 15 | App startup and health | **PASS for the 0.8.0 payload; NOT RUN for 1.0.0** | Fresh payload 2,645 files / 62,513,538 B / 33 distributions / 0 development-only; inventory exit 0; bundled `creatoros version` 0.8.0, migrate to head `b8c9d0e1f2a3`, `/health/live` live. The 0.8.0 install evidence remains `verify-install.ps1 -Uninstall` 17/17 and `production-health.ps1` 35 checks (`docs/evidence/P9.md`) | Repeat on the signed release candidate and its exact payload |
| 16 | Prerequisites: versions, hashes, signatures, consent | **TOOLING PASS; FINAL NOT RUN** | `prerequisites.v1.json` pins Ollama and FFmpeg; `/setup` routes and 42 setup tests (persisted verification, stale fingerprint, bounded verify, 409 override, exact RDN publisher matching); manifest gate passes | Verify the exact prerequisite bytes during acceptance |
| 17 | Data preservation across upgrade/uninstall | **PASS for 0.8.0; NOT RUN for 1.0.0** | 0.8.0 install/uninstall kept the data root (17/17). `production-health.ps1` now verifies the installer bytes and signature before the upgrade install and the uninstall reinstall | Run `production-health.ps1 -IncludeUpgrade -RequireSignature` and `-IncludeUninstall -RequireSignature` on 1.0.0 |
| 18 | FFmpeg rights | **BLOCKED** | GPLv3 build with libx264, unsigned, not approved for bundling; no legal clearance. `DISTRIBUTION_DECISIONS.md` records the exact GPLv3 §1/§4/§5/§6 obligations and five architectures | Owner approves a route: external prerequisite (recommended for 1.0.0), user-consented verified download, compliant redistribution with corresponding source, or an alternative build |
| 19 | Ollama rights | **BLOCKED** | Official installer verified but `approved_for_bundling: false`; no written redistribution approval. The model weights and GPU runtimes are separate licences and are not covered by Ollama's MIT code licence (`DISTRIBUTION_DECISIONS.md`) | Owner obtains written approval for the exact installer plus runtime inventory, or keeps the official-source online route |
| 20 | Offline/online payload measured | **BLOCKED (measurements complete)** | Fresh 0.8.0 installer 139,888,520 B; backend payload 62,513,538 B in 2,645 files; Ollama installer 1,953,587,080 B; FFmpeg pair 220,965,888 B; a combined single asset would exceed GitHub's 2 GiB per-file limit, while separate assets are legal (1,000 assets, no total cap). Online (hash-verified official download) remains the only lawful route today | Owner approves FFmpeg/Ollama rights and the online-vs-separate-assets design; then measure the real 1.0.0 installer |
| 21 | SBOM (source, prerequisites and distribution) | **TOOLING AND 0.8.0 ARTIFACT PASS; NOT RUN FOR 1.0.0** | Source 48 components, reproducible byte-for-byte in a venv-free minimal tree (serial `55491264-…`, SHA-256 `7E61D01D…`); prerequisites 3 components. Artifact 51 components (33 Python) is bound to installer SHA-256 `d3a73f60…`, payload tree `49d3fb31…`, 2,645 files; inventory matches it with 0 absent in either direction. The Wave 3 defect (artifact components read from the venv) is closed: `--payload` is required and every Python component comes from payload `METADATA`/`RECORD` | Generate and attach all three documents for the signed 1.0.0 release commit and artifact |
| 22 | Checksums for final assets | **TOOLING PASS; FINAL NOT RUN** | `artifact.manifest-scope` re-hashes every file named in `SHA256SUMS.txt`; on the 0.8.0 leftovers it correctly fails both the installer and the blockmap. No final assets exist yet | Publish `SHA256SUMS.txt` covering every attached asset |
| 23 | Screenshots | **NOT RUN** | None captured from a 1.0.0 build | Capture 10–14 genuine screenshots from the installed release candidate |
| 24 | README and release metadata | **PARTIAL** | 0.8.0 README is accurate for the source release; no v1.0.0 notes, download table or gallery | Write the v1.0.0 README/release notes after the artifact exists |
| 25 | Published asset re-downloaded and hashed | **BLOCKED** | No v1.0.0 release | Download the public asset on a second machine, hash-match, re-install |
| 26 | `v1.0.0` tag on the approved commit | **BLOCKED** | Gates 5–25 are not closed | Tag only after every mandatory gate passes |

## Summary

- **Passing now:** source integrity on the readiness commit, local test/typecheck/lint/build, signature
  inventory baseline, prerequisite tooling, SBOM tooling and gate hardening.
- **Blocked by the owner:** certificate and publisher identity, clean SAC-on machine, FFmpeg
  redistribution, Ollama redistribution and the payload design.
- **Not started because the artifact cannot exist yet:** Authenticode, timestamp, clean install,
  Defender/SmartScreen evidence, checksums, screenshots, published-asset verification and the tag.

The release stays at **v0.8.0** until the blocked and not-run gates pass. This is the honest state,
not a temporary report to be replaced by optimism.
