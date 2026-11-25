# v1.0.0 release blockers

**Status: NOT READY.** No v1.0.0 tag, installer or GitHub release has been published.  
**Audit date:** 2026-11-25  
**Baseline:** `fc2a3e2` (`0.8.0`), clean public source release.

This file records what prevents a truthful v1.0.0 release. A blocker is not closed by changing a
document, disabling a gate or shipping an unsigned installer as if it were signed.

## 1. Mandatory gates

| Gate | Status | Evidence | Owner action |
| --- | --- | --- | --- |
| Publicly trusted code-signing certificate issued to the publisher | **BLOCKED** | No CA-issued code-signing certificate with a private key in `CurrentUser\My` or `LocalMachine\My`; `CSC_LINK`, `CSC_KEY_PASSWORD` and `TIMESTAMP_URL` are unset. `scripts\sign-release.ps1 -Check` reports `CSC_LINK : none`. | Order an IV/OV code-signing certificate from a public CA; complete identity verification in the publisher's legal name; receive the hardware token or cloud-HSM credential; record the exact certificate subject and thumbprint. See `CERTIFICATE_PROCUREMENT.md`. |
| RFC 3161 timestamp from a public TSA | **BLOCKED** | Depends on a real signature. The timestamp endpoint is reachable, but there is no signature to timestamp. | Sign with `/tr` and `/td sha256` after the certificate is available. |
| Clean Windows 11 x64 machine with Smart App Control **enforced** | **BLOCKED** | Audit machine is Windows 11 build 26300 with `VerifiedAndReputablePolicyState = 0` (SAC off). No Hyper-V, VirtualBox, QEMU or VMware VM is installed. `scripts\capture-security-state.ps1` reports `sac off (raw 0)`, `clean_install=false`, and three unreadable fields; `scripts\build-sac-evidence.ps1` refuses this machine (6 unmet preconditions, including `sac.enforced`, `windows.clean_install` and `installer.signature_status NotSigned`) and writes no acceptable evidence. | Provide a clean Windows 11 x64 VM or physical machine with SAC on (fresh install, no developer tools), install the downloaded signed artifact, run the first-run flow, and record the evidence JSON consumed by `scripts\release-gate.ps1 -Stage Release`. |
| Installer actually downloaded from the published release and hash matched | **BLOCKED** | There is no published v1.0.0 release. The only local installer is the unsigned 0.8.0 build. | Publish only after the earlier gates pass; then download the attached asset on a second machine and compare SHA-256. |
| Signed installer attached to the GitHub release | **BLOCKED** | No signed v1.0.0 installer exists. | Build, sign, verify, attach the exact signed bytes, and publish `SHA256SUMS.txt`. |
| SBOM for the released artifact | **TOOLING READY, ARTIFACT MISSING** | `scripts/generate_sbom.py` produces a deterministic CycloneDX 1.5 document from real inputs (68 components on this checkout, including the FFmpeg binary hash) and `scripts/release-gate.ps1` now rejects an SBOM whose format, spec version, component fields or `metadata.component.version` do not match the release. No v1.0.0 SBOM is checked in because no v1.0.0 build exists. | Run `python scripts\generate_sbom.py --output releases\1.0.0\sbom.cdx.json --version 1.0.0 --prerequisites app\backend\src\creatoros\resources\prerequisites.v1.json --check` on the release commit, review the document, and attach it to the release. |
| Secret scan | **PASS (tracked text)** | `scripts\release-gate.ps1 -Stage Source` finds no private keys, GitHub tokens, AWS keys or literal signing passwords in tracked non-test text files. | Re-run on the release commit; also scan the built artifact and logs. |
| CI green on the release commit | **PASS FOR 0.8.0, NOT APPLICABLE TO V1.0.0 YET** | Latest `main` CI run at `fc2a3e2` is `completed/success`; no v1.0.0 commit exists. | Run the full CI workflow on the release PR and require it before merge. |
| Branch protection / required checks | **PASS (verified 2026-11-25)** | `gh api` shows `strict: true`, no force pushes, and required contexts `Backend tests (Windows)` and `Desktop checks (Windows)`. | Keep the checks required; do not bypass them for the release PR. |

## 2. Product-completeness blockers

These are independent of signing and would still block a truthful v1.0.0 installer:

### 2.1 FFmpeg GPLv3 redistribution

The current FFmpeg 9.1 essentials build is GPLv3 and unsigned. It statically links `libx264`, which
the product uses. Bundling it in an Apache-2.0 installer without corresponding source and notices
would misrepresent the licence. An LGPL build is not a drop-in replacement because it omits
`libx264`.

**Owner/legal action:** choose one:

1. ship the GPLv3 build with complete corresponding source, build scripts, written offer and notices;
2. fund an approved LGPL-compatible build and change the encoder path; or
3. obtain a written distribution approval that covers the exact binary.

Until then the dependency manifest records `Redistribution status: Blocked for bundling`.

### 2.2 Offline prerequisite handling: verified on request, not automated

The official Ollama `0.40.18` installer was downloaded and verified:

- 1,953,587,080 bytes;
- SHA-256 `AD2BB47AA969B21CF8DD81B2D9F9A6D5030270ED242EF55087118C37C510ACC2`;
- Authenticode `Valid`, signed by `Ollama Inc.`, DigiCert timestamp;
- silent install documented as `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`.

**What now exists** (implemented and tested in this audit):

- `app/backend/src/creatoros/resources/prerequisites.v1.json` pins Ollama and FFmpeg with exact sizes,
  SHA-256 digests, publisher subjects, installer arguments, consent requirements and an explicit
  `redistribution.approved_for_bundling: false` for both.
- `GET /setup/status` reports presence only (install path, bounded loopback probe, models, FFmpeg
  `-version`, disk free space and a real write test) and never claims a file is verified.
- `POST /setup/prerequisites/{id}/verify` hashes a file the user already has and checks size,
  Authenticode status, publisher subject and RFC 3161 timestamp; `unknown` is never reported as pass.
- `POST /setup/complete` records the decision (finished or skipped AI) in the database and the audit log.
- The first-run page shows those results, links to the official download, and lets the user finish or
  skip AI setup without a terminal.
- `scripts/release-gate.ps1` validates the manifest and cross-checks every pinned digest against
  `docs/release/DEPENDENCY_MANIFEST.md`.

**What still does not exist:**

- The application does not download or execute a prerequisite; the user runs the vendor installer and
  keeps its elevated prompt. The wizard verifies, it does not install.
- It does not start Ollama, pull model weights, or choose a model on the user's behalf.
- The Ollama installer is not integrated into `CreatorOS-Setup-*.exe`; the payload strategy is still
  unresolved (see 2.3).
- No run on a clean machine exists, so the wizard's behaviour there is unverified.

**Owner action:** approve Ollama redistribution (including its bundled GPU runtime terms), decide the
installer size/payload strategy, and provide a clean machine for the wizard's acceptance run.

### 2.3 GitHub's 2 GiB per-asset limit conflicts with a single fully offline installer

GitHub documents a hard limit: each release asset must be under **2 GiB**. The current components
already total more than that before NSIS overhead:

| Component | Bytes |
| --- | ---: |
| CreatorOS 0.8.0 installer | 143,055,052 |
| Ollama 0.40.18 installer | 1,953,587,080 |
| FFmpeg + ffprobe 9.1 | 220,965,888 |
| **Total** | **2,317,608,020** |

That is about 2.16 GiB, roughly 170 MB over the limit. Compression may reduce it, but no compressed
payload has been measured, and the installer would still be extremely large. A single
`CreatorOS-Setup-1.0.0-x64.exe` containing all three is therefore **not demonstrated to be publishable
as one GitHub release asset**, which is exactly what the brief asks for.

**Owner action:** choose an explicit distribution design: a compressed offline payload under 2 GiB
with measured evidence, a separate verified prerequisites bundle (not a single .exe), or external
hosting with a signed manifest. Do not claim a single fully offline installer until its final byte
size is measured.

### 2.4 Version and release artifacts

The source is still `0.8.0`. No `1.0.0` version bump, release notes for 1.0.0, SBOM artifact, signed
build or tag exists. This is deliberate: releasing `1.0.0` without the gates above would be a
version-number claim, not a release. The tooling to produce the SBOM and to validate the prerequisite
manifest now exists; it is not a substitute for running it on the release commit and attaching the
result.

## 3. What was completed in this audit

- `docs/release/V1_RELEASE_BASELINE.md`: measured baseline of the 0.8.0 source and installer.
- `docs/release/CERTIFICATE_PROCUREMENT.md`: CA options, prices, hardware requirements and exact
  owner steps for a publisher in Algeria.
- `docs/release/DEPENDENCY_MANIFEST.md`: verified Ollama hash/signature, FFmpeg hashes and licence
  status, CPython signature.
- `scripts/release-gate.ps1`: fail-closed Source/Artifact/Release gate covering version consistency,
  docs, SBOM, tracked secrets, installer digest, manifest, Authenticode, RFC 3161 timestamp,
  publisher, clean SAC-on evidence, tag, CI, branch protection and the published asset digest.
- `app/backend/src/creatoros/resources/prerequisites.v1.json` plus `services/setup/` and the
  `/setup` routes: manifest validation, bounded presence detection, hash/Authenticode verification and
  recorded completion (24 backend tests).
- The first-run wizard (`app/desktop/src/pages/Setup.tsx`) and its typed API client, with 8 desktop
  tests; it reports presence, verification and "could not check" as different states.
- `docs/INSTALLATION.md` (updated for the first-run flow; it already satisfied the gate) and ADR-37,
  which records why the manifest ships in the package and why "unknown" never counts as a pass.
- `scripts/generate_sbom.py`, `app/backend/tests/test_release_sbom.py` and `docs/release/SBOM.md`:
  a deterministic CycloneDX 1.5 document built from real inputs, and a gate that rejects a shape-only
  or wrong-version SBOM.
- `scripts/release-gate.ps1`: the `release.sbom` gate now validates format, spec version, component
  fields, duplicate refs and the release version; new `release.prerequisites` and
  `release.prerequisites-hashes` gates validate the pinned manifest and cross-check the audit record.
- `scripts/capture-security-state.ps1` and `scripts/build-sac-evidence.ps1`, with `docs/evidence/SCHEMA.md`:
  a read-only collector (SAC, Defender, policy, certificates, clean-install heuristic) and a fail-closed
  assembler that refuses this machine rather than producing evidence it cannot support. Both were run
  here under Windows PowerShell 5.1; the assembler wrote an honest refusal and exited 1.
- Correction of the false statement that Smart App Control is on in the current build environment. The
  audit machine reports `VerifiedAndReputablePolicyState = 0` (off, build 26300); the stale
  `releases\0.8.0\SHA256SUMS.txt` comment claiming state 1 must not be carried into any new release.
- Current 0.8.0 artifact audited: `NotSigned`, no timestamp. It must not be attached to v1.0.0.

## 4. Verdict

**v1.0.0 NOT READY — CERTIFICATE, CLEAN SAC-ON TEST, FFmpeg LICENCE AND OFFLINE PAYLOAD ARE UNRESOLVED.**

The first hard blocker is certificate procurement: without a publicly trusted certificate issued in
the publisher's name, no amount of building can produce a signed installer that passes the mandatory
gate. The second is a clean SAC-enforced machine. Both require owner action outside this repository.

Do not publish an unsigned v1.0.0 installer, do not describe the 0.8.0 installer as v1.0.0, and do not
tag `v1.0.0` until `scripts\release-gate.ps1 -Stage Release` passes with real evidence.
