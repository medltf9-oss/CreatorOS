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
| Clean Windows 11 x64 machine with Smart App Control **enforced** | **BLOCKED** | Audit machine is Windows 11 build 26300 with `VerifiedAndReputablePolicyState = 0` (SAC off). No Hyper-V, VirtualBox, QEMU or VMware VM is installed. | Provide a clean Windows 11 x64 VM or physical machine with SAC on (fresh install, no developer tools), install the downloaded signed artifact, run the first-run flow, and record the evidence JSON consumed by `scripts\release-gate.ps1 -Stage Release`. |
| Installer actually downloaded from the published release and hash matched | **BLOCKED** | There is no published v1.0.0 release. The only local installer is the unsigned 0.8.0 build. | Publish only after the earlier gates pass; then download the attached asset on a second machine and compare SHA-256. |
| Signed installer attached to the GitHub release | **BLOCKED** | No signed v1.0.0 installer exists. | Build, sign, verify, attach the exact signed bytes, and publish `SHA256SUMS.txt`. |
| SBOM for the released artifact | **MISSING** | No v1.0.0 SBOM exists. `docs/release/DEPENDENCY_MANIFEST.md` is an audit record, not an SBOM. | Generate CycloneDX or SPDX from the actual build inputs and attach it to the release. |
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

### 2.2 Full offline prerequisite handling is not implemented

The official Ollama `0.40.18` installer was downloaded and verified:

- 1,953,587,080 bytes;
- SHA-256 `AD2BB47AA969B21CF8DD81B2D9F9A6D5030270ED242EF55087118C37C510ACC2`;
- Authenticode `Valid`, signed by `Ollama Inc.`, DigiCert timestamp;
- silent install documented as `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`.

It is **not** integrated into the installer. There is also no implemented first-run setup/model wizard
that detects Ollama, starts it, offers a bounded model download, verifies FFmpeg/ffprobe, checks disk
space and writability, and completes without a terminal. The existing product documents Ollama as an
optional external prerequisite.

**Owner action:** approve Ollama redistribution (including its bundled GPU runtime terms), decide the
installer size/payload strategy, and fund the wizard work and its tests.

### 2.3 Version and release artifacts

The source is still `0.8.0`. No `1.0.0` version bump, release notes, changelog entry, SBOM, signed
build or tag exists. This is deliberate: releasing `1.0.0` without the gates above would be a
version-number claim, not a release.

## 3. What was completed in this audit

- `docs/release/V1_RELEASE_BASELINE.md`: measured baseline of the 0.8.0 source and installer.
- `docs/release/CERTIFICATE_PROCUREMENT.md`: CA options, prices, hardware requirements and exact
  owner steps for a publisher in Algeria.
- `docs/release/DEPENDENCY_MANIFEST.md`: verified Ollama hash/signature, FFmpeg hashes and licence
  status, CPython signature.
- `scripts/release-gate.ps1`: fail-closed Source/Artifact/Release gate covering version consistency,
  docs, SBOM, tracked secrets, installer digest, manifest, Authenticode, RFC 3161 timestamp,
  publisher, clean SAC-on evidence, tag, CI, branch protection and the published asset digest.
- Correction of the false statement that Smart App Control is on in the current build environment.
- Current 0.8.0 artifact audited: `NotSigned`, no timestamp. It must not be attached to v1.0.0.

## 4. Verdict

**v1.0.0 NOT READY — CERTIFICATE, CLEAN SAC-ON TEST, FFmpeg LICENCE AND OFFLINE PAYLOAD ARE UNRESOLVED.**

The first hard blocker is certificate procurement: without a publicly trusted certificate issued in
the publisher's name, no amount of building can produce a signed installer that passes the mandatory
gate. The second is a clean SAC-enforced machine. Both require owner action outside this repository.

Do not publish an unsigned v1.0.0 installer, do not describe the 0.8.0 installer as v1.0.0, and do not
tag `v1.0.0` until `scripts\release-gate.ps1 -Stage Release` passes with real evidence.
