# v1.0.0 release gate matrix

**Status date:** 2026-11-25
**Release candidate:** none. Public release remains **v0.8.0** (source only).
**Readiness commit:** `1eac7b3` on `release/v1.0.0-readiness` (PR #6, draft).
**Rule:** a mandatory gate passes only with evidence for the exact release commit and artifact.
`FAIL`, `BLOCKED`, `NOT RUN` and `UNKNOWN` all block the release. This matrix is the authoritative
summary; details live in the linked documents.

| # | Gate | Status | Evidence | What closes it |
| ---: | --- | --- | --- | --- |
| 1 | Source integrity | **PASS (readiness commit)** | `release/v1.0.0-readiness` at `1eac7b3`; `git fsck --full` reports only dangling blobs (normal after commits); PR #6 head matches the pushed commit | Re-run on the final release commit; keep the tree clean |
| 2 | CI on the exact commit | **PASS for `787af16`; rerun for `1eac7b3`** | GitHub Actions run on the pushed readiness commit; earlier run `45541888270` passed at `787af16` | Watch the run for `1eac7b3` to completion; require it before merge |
| 3 | Backend/frontend tests | **PASS (local `1eac7b3` content)** | 1,617 passed, 9 skipped (`pytest -q`); desktop 208 passed (`pnpm test`) | Re-run on the final commit; CI repeats both |
| 4 | Typecheck, build, lint | **PASS (local)** | `ruff check src tests` clean; `pnpm typecheck` clean; `pnpm build` succeeded | Re-run on the final commit |
| 5 | CA-issued certificate | **BLOCKED** | No CA-issued certificate with a private key in either Windows store; `sign-release.ps1 -Check` reports `CSC_LINK : none` | Owner orders a certificate (`OWNER_CERTIFICATE_ACTION.md`) |
| 6 | Private-key protection | **BLOCKED** | No token, HSM or signing service exists | Certificate issued to hardware or a CA signing service |
| 7 | Signature inventory | **PASS (baseline)** | `docs/release/SIGNATURE_INVENTORY.md`: 64 PE files measured, 44 vendor-signed, 20 unsigned, sign policy defined | Regenerate on the signed release candidate and record the result |
| 8 | Authenticode on every CreatorOS-owned PE | **NOT RUN** | No signed build exists | Sign `CreatorOS.exe`, installer and uninstaller; `signtool verify /pa /all` |
| 9 | RFC 3161 timestamp | **BLOCKED** | Nothing to timestamp | `/tr` + `/td SHA256` after signing; verify the token |
| 10 | Publisher identity matches owner approval | **BLOCKED** | Publisher name not yet chosen | Owner approves the exact subject (`OWNER_CERTIFICATE_ACTION.md` §2) |
| 11 | Smart App Control enforced | **BLOCKED** | Audit machine: build 26300, `VerifiedAndReputablePolicyState = 0` (off); assembler refuses | Clean SAC-on machine installs the signed artifact |
| 12 | Defender active, no detection, no exclusions | **UNKNOWN** | Capture tooling exists (`capture-security-state.ps1`); the audit machine is not the clean test environment | Run the capture on the clean SAC-on machine during acceptance |
| 13 | SmartScreen behaviour | **NOT RUN** | No signed, published asset exists; no claim is made | Record the actual prompt on the clean machine; do not promise "no warnings" |
| 14 | Clean install of the exact artifact | **BLOCKED** | Depends on #5, #11 and the final artifact | Install the downloaded signed installer on the clean machine |
| 15 | App startup and health | **PASS for 0.8.0; NOT RUN for 1.0.0** | 0.8.0: `verify-install.ps1 -Uninstall` 17/17; `production-health.ps1` 35 checks (`docs/evidence/P9.md`) | Repeat on the release candidate |
| 16 | Prerequisites: versions, hashes, signatures, consent | **TOOLING PASS; FINAL NOT RUN** | `prerequisites.v1.json` pins Ollama and FFmpeg; `/setup` routes and 24 tests; manifest gate passes | Verify the exact prerequisite bytes during acceptance |
| 17 | Data preservation across upgrade/uninstall | **PASS for 0.8.0; NOT RUN for 1.0.0** | 0.8.0 install/uninstall kept the data root (17/17) | Run `verify-install.ps1 -IncludeUpgrade -IncludeUninstall` on 1.0.0 |
| 18 | FFmpeg rights | **BLOCKED** | GPLv3 build with libx264, unsigned, not approved for bundling; no legal clearance | Owner approves a route: compliant redistribution, alternative build, external prerequisite, or user-consented download (`V1_RELEASE_BLOCKERS.md` §2.1) |
| 19 | Ollama rights | **BLOCKED** | Official installer verified but `approved_for_bundling: false`; no written redistribution approval | Owner obtains approval or keeps the official-source online route |
| 20 | Offline/online payload measured | **BLOCKED** | Payload strategy undecided; the 0.8.0 installer is 143,055,052 bytes without Ollama or FFmpeg | Owner chooses the distribution design; then measure the real bytes |
| 21 | SBOM (source and distribution) | **TOOLING PASS; ARTIFACT MISSING** | `generate_sbom.py` produces deterministic CycloneDX 1.5 (69 components); gate validates format, version, fields and duplicate refs | Generate from the release commit and from the signed payload; attach both |
| 22 | Checksums for final assets | **NOT RUN** | No final assets | Publish `SHA256SUMS.txt` covering every attached asset |
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
