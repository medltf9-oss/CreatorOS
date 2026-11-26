# v1.0.0 release gate matrix

**Status date:** 2026-11-25
**Release candidate:** none. Public release remains **v0.8.0** (source only).
**Readiness commit:** `fda7efc` on `release/v1.0.0-readiness` (PR #6, draft).
**Rule:** a mandatory gate passes only with evidence for the exact release commit and artifact.
`FAIL`, `BLOCKED`, `NOT RUN` and `UNKNOWN` all block the release. This matrix is the authoritative
summary; details live in the linked documents.

| # | Gate | Status | Evidence | What closes it |
| ---: | --- | --- | --- | --- |
| 1 | Source integrity | **PASS (readiness commit)** | `release/v1.0.0-readiness` at `fda7efc`; `git fsck --full` reports only dangling blobs (normal after commits); PR #6 head matches the pushed commit | Re-run on the final release commit; keep the tree clean |
| 2 | CI on the exact commit | **PASS at `fda7efc`** | GitHub Actions run `45660131410`: backend tests and desktop checks both succeeded (earlier runs at `47516c7` and `68e3f3c` also green). The run at `1eac7b3` failed only because the pinned-FFmpeg byte test ran against a different CI FFmpeg build; the guard was fixed in `6cee073` | Require a fresh green run on the final release commit |
| 3 | Backend/frontend tests | **PASS (local and CI at `fda7efc`; Wave 3 local)** | Wave 3: backend **1,636 passed, 30 skipped, 0 failed** (21 skips need a production database absent here); desktop **227 passed**; setup E2E **117/117** backend checks and **3/3** UI tests. CI at `fda7efc` was green | Re-run on the final commit and require fresh CI |
| 4 | Typecheck, build, lint | **PASS (local and CI at `fda7efc`)** | `ruff check src tests` clean; `pnpm typecheck` clean; `pnpm build` succeeded | Re-run on the final commit |
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
| 15 | App startup and health | **PASS for 0.8.0; NOT RUN for 1.0.0** | 0.8.0: `verify-install.ps1 -Uninstall` 17/17; `production-health.ps1` 35 checks (`docs/evidence/P9.md`). The minimized bundle now passes `runtime-inventory.py` (34 distributions, 0 development-only, exit 0) and its own migrate + `/health/live` smoke test | Repeat on the release candidate; re-run the inventory on its payload |
| 16 | Prerequisites: versions, hashes, signatures, consent | **TOOLING PASS; FINAL NOT RUN** | `prerequisites.v1.json` pins Ollama and FFmpeg; `/setup` routes and 42 setup tests (persisted verification, stale fingerprint, bounded verify, 409 override, exact RDN publisher matching); manifest gate passes | Verify the exact prerequisite bytes during acceptance |
| 17 | Data preservation across upgrade/uninstall | **PASS for 0.8.0; NOT RUN for 1.0.0** | 0.8.0 install/uninstall kept the data root (17/17). `production-health.ps1` now verifies the installer bytes and signature before the upgrade install and the uninstall reinstall | Run `production-health.ps1 -IncludeUpgrade -RequireSignature` and `-IncludeUninstall -RequireSignature` on 1.0.0 |
| 18 | FFmpeg rights | **BLOCKED** | GPLv3 build with libx264, unsigned, not approved for bundling; no legal clearance. `DISTRIBUTION_DECISIONS.md` records the exact GPLv3 §1/§4/§5/§6 obligations and five architectures | Owner approves a route: external prerequisite (recommended for 1.0.0), user-consented verified download, compliant redistribution with corresponding source, or an alternative build |
| 19 | Ollama rights | **BLOCKED** | Official installer verified but `approved_for_bundling: false`; no written redistribution approval. The model weights and GPU runtimes are separate licences and are not covered by Ollama's MIT code licence (`DISTRIBUTION_DECISIONS.md`) | Owner obtains written approval for the exact installer plus runtime inventory, or keeps the official-source online route |
| 20 | Offline/online payload measured | **BLOCKED (measurements complete)** | 0.8.0 installer 143,055,052 B; Ollama installer 1,953,587,080 B; FFmpeg pair 220,965,888 B; combined single asset 2,317,608,020 B exceeds GitHub's 2 GiB per-file limit, while separate assets are legal (1,000 assets, no total cap). The minimized backend payload is now 63,384,618 B in 2,674 files (down from 98,640,439 B in the shipped 0.8.0 payload). Online (hash-verified official download) is the only lawful route today | Owner approves FFmpeg/Ollama rights and the online-vs-separate-assets design; then measure the real installer |
| 21 | SBOM (source, prerequisites and distribution) | **SOURCE/PRE-ARTIFACT PASS; ARTIFACT NOT RUN** | `generate_sbom.py` has `source`, `prerequisites` and `artifact` profiles. On 0.8.0 the Source gate generated 66 source components and 3 prerequisite components; the artifact document binds the installer hash and must contain every source `bom-ref`. Open defect: the artifact profile reads Python components from `app/backend/.venv`, so a clean payload reports 17 development components as `absent-from-payload` (`PRODUCTION_RUNTIME.md`). Until the artifact SBOM is payload-bound, the Artifact gate must not treat it as proof of the shipped inventory | Generate the three documents from the release commit and signed payload; bind the artifact SBOM to the packaged payload, not the build venv; attach them |
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
