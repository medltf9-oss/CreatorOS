# v1.0.0 release blockers

**Status: NOT READY.** No v1.0.0 tag, installer or GitHub release has been published.  
**Audit date:** 2026-11-26  
**Baseline:** `0.8.0`; Wave 3 readiness work at `c912771` on `release/v1.0.0-readiness` (PR #7).
`main` is at `0683773` (`0.8.0`), the squash merge of PR #6.

This file records what prevents a truthful v1.0.0 release. A blocker is not closed by changing a
document, disabling a gate or shipping an unsigned installer as if it were signed.

The authoritative pass/blocked/not-run matrix is `V1_RELEASE_GATE_MATRIX.md`; this document keeps
 the evidence and owner actions behind each blocker.

## 1. Mandatory gates

| Gate | Status | Evidence | Owner action |
| --- | --- | --- | --- |
| Publicly trusted code-signing certificate issued to the publisher | **BLOCKED** | No CA-issued code-signing certificate with a private key in `CurrentUser\My` or `LocalMachine\My`; `CSC_LINK`, `CSC_KEY_PASSWORD` and `TIMESTAMP_URL` are unset. `scripts\sign-release.ps1 -Check` reports `CSC_LINK : none`. | Order an IV/OV code-signing certificate from a public CA; complete identity verification in the publisher's legal name; receive the hardware token or cloud-HSM credential; record the exact certificate subject and thumbprint. See `CERTIFICATE_PROCUREMENT.md`; the one-page owner checklist is `OWNER_CERTIFICATE_ACTION.md`. |
| RFC 3161 timestamp from a public TSA | **BLOCKED** | Depends on a real signature. The timestamp endpoint is reachable, but there is no signature to timestamp. | Sign with `/tr` and `/td sha256` after the certificate is available. |
| Clean Windows 11 x64 machine with Smart App Control **enforced** | **BLOCKED** | Audit machine is Windows 11 build 26300 with `VerifiedAndReputablePolicyState = 0` (SAC off). No Hyper-V, VirtualBox, QEMU or VMware VM is installed. `scripts\capture-security-state.ps1` reports `sac off (raw 0)`, `clean_install=false`, and three unreadable fields; `scripts\build-sac-evidence.ps1` refuses this machine (6 unmet preconditions, including `sac.enforced`, `windows.clean_install` and `installer.signature_status NotSigned`) and writes no acceptable evidence. | Provide a clean Windows 11 x64 VM or physical machine with SAC on (fresh install, no developer tools), install the downloaded signed artifact, run the first-run flow, and record the evidence JSON consumed by `scripts\release-gate.ps1 -Stage Release`. |
| Installer actually downloaded from the published release and hash matched | **BLOCKED** | There is no published v1.0.0 release. The only local installer is the unsigned 0.8.0 build. | Publish only after the earlier gates pass; then download the attached asset on a second machine and compare SHA-256. |
| Signed installer attached to the GitHub release | **BLOCKED** | No signed v1.0.0 installer exists. | Build, sign, verify, attach the exact signed bytes, and publish `SHA256SUMS.txt`. |
| SBOM for the released artifact | **PASS for the 0.8.0 payload; NOT RUN for 1.0.0** | `scripts/generate_sbom.py` produces deterministic CycloneDX documents: 48 source components from the committed runtime lock and Node licence manifest, 3 prerequisites, and 51 artifact components read from `releases\0.8.0\win-unpacked\resources\backend` (33 Python; 0/0 payload-vs-SBOM discrepancies; tree `49d3fb31…`; installer `d3a73f60…`). No v1.0.0 SBOM exists because no v1.0.0 build exists. | On the release commit and signed payload run the artifact profile with `--artifact` and `--payload releases\1.0.0\win-unpacked\resources\backend`, review it, and attach it. |
| Secret scan | **PASS (tracked text)** | `scripts\release-gate.ps1 -Stage Source` finds no private keys, GitHub tokens, AWS keys or literal signing passwords in tracked non-test text files. | Re-run on the release commit; also scan the built artifact and logs. |
| CI green on the release commit | **PASS at `ab4499a`; NOT YET ON A RELEASE COMMIT** | GitHub Actions runs `45730731847` and `45730732090` at `ab4499a`: backend **1,630 passed, 36 skipped**, desktop checks succeeded. The run at `5812915` failed for two environment assumptions, fixed in `ab4499a`. No v1.0.0 release commit exists yet. | Re-run the full workflow on the final 1.0.0 commit and require it before merge. |
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

GitHub's official documentation (verified 2026-11-25) states: up to **1,000 assets per release**,
**each file** must be under **2 GiB**, and there is **no limit on the total size** of a release. The
limit is therefore per asset, not on the sum. What matters is whether any single installer would
exceed 2 GiB, and the components already total more than that before NSIS overhead:

| Component | Bytes |
| --- | ---: |
| CreatorOS 0.8.0 installer (rebuilt 2026-11-26) | 139,888,520 |
| Ollama 0.40.18 installer | 1,953,587,080 |
| FFmpeg + ffprobe 9.1 | 220,965,888 |
| **Total** | **2,317,608,020** |

That is about 2.16 GiB, roughly 170 MB over the per-file limit. Compression may reduce it, but no
compressed payload has been measured. A single `CreatorOS-Setup-1.0.0-x64.exe` containing all three
is therefore **not demonstrated to be publishable as one GitHub release asset**. Publishing them as
separate assets is within GitHub's limits (the Ollama installer alone is about 1.82 GiB), but it is
only lawful if the redistribution rights in §2.1–§2.2 are cleared, and it is no longer a single-file
install.

**Owner action:** choose an explicit distribution design: a compressed offline payload under 2 GiB
with measured evidence, separately approved prerequisite assets, or external hosting with a signed
manifest. Do not claim a single fully offline installer until its final byte size is measured.

### 2.4 The 0.8.0 `SHA256SUMS.txt` is current; v0.8.0 still publishes no binaries

On 2026-11-25 the local manifest was stale: it recorded `b9686c6e…` while the installer on disk
hashed to `ad8b7772…`, and `release-gate.ps1` refused the pair. The 2026-11-26 rebuild replaced both:
`SHA256SUMS.txt` now records `d3a73f608d903e72de35a5f2def18d31ed367dccbc4fe922ac7f739580e9cc07`
for the 139,888,520-byte installer, and `artifact.manifest` passes. The stale bytes must not be
reused.

The published GitHub release `v0.8.0` has **no binary assets** — it is a source release — so no
public download is affected. The stale file is a local build leftover and must not be reused. Do not
silently rewrite a published record: if a binary is ever attached to 0.8.0, publish a dated erratum
with both hashes rather than replacing the file without comment.

### 2.5 First-run setup defects found by independent QA

An adversarial pass over the setup code (read-only) found behaviours the tests do not cover. One is
fixed here; the rest are recorded so they are not mistaken for release-ready.

| Defect | Severity | Evidence | Status |
| --- | --- | --- | --- |
| Publisher check used substring containment: `CN=Ollama Inc. Support, O=Evil` matched `CN=Ollama Inc., O=Ollama Inc.` | High | `services\setup\verify.py:129` | **Fixed**: RDN-exact comparison with quoted-comma parsing; regression test added |
| `verified` was hard-coded `False` and verification results were not persisted, so "present but unchecked" was the only reachable state | High | `api\routes\setup.py`, `state.py` | **Fixed**: verification records persist in `app_setting` (`setup.verification`), status reports `verified` only when the record passed and its size/mtime fingerprint still matches, and the detail says it is a recorded verification, not a live re-hash |
| FFmpeg presence compared size only; an equal-size file with different bytes reported `ok` | High | `api\routes\setup.py` | **Fixed**: presence now says the size matches but integrity is unverified until `/verify`; only a recorded hash sets `verified` |
| Setup could be completed while a required check was `fail` (e.g. `media.ffmpeg`) | Medium | `api\routes\setup.py`, `Setup.tsx` | **Fixed**: `POST /setup/complete` returns 409 `REQUIRED_CHECKS_FAILING`; the UI shows the ids and requires an explicit *Complete anyway* that sends `override_required_checks: true`, recorded in state and audit metadata |
| Completion overwrote `completed_at`, dropped `notes` when omitted, and stored but never compared `version` | Medium | `state.py` | **Fixed**: completion is idempotent (first `completed_at` and `completed_version` preserved, notes kept unless replaced); status reports `version_changed` and a non-blocking `setup.version_changed` warning. Decision: the recorded completion is kept on upgrade and the wizard does not reopen |
| Any Ollama version (or a reachable remote endpoint) reported `ok`; the pinned version was never compared | Medium | `api\routes\setup.py`, `detection.py` | **Fixed**: the component detail and an `ai.ollama.version` check state whether the installed version matches the pin (warn, not a hard gate). A reachable remote endpoint is still reported as reachable, not installed |
| `ffprobe` is never verified and `companion_filenames` is dead config | Medium | `prerequisites.v1.json:61`, `verify.py` | Open: the FFmpeg component verifies `ffmpeg.exe` only; a `ffprobe` row or an explicit companion-check needs a manifest/schema decision |
| Verify accepted any absolute path, with no timeout, cancellation or concurrency limit | Medium | `api\routes\setup.py`, `verify.py` | **Fixed**: symlink/reparse-point and non-regular targets are rejected with `VERIFY_*` 422 codes, files over 4 GiB are refused, runs are serialised and a 120 s deadline returns 504 without writing a record |
| `production-health.ps1` contained a literal 0x08 byte in the venv path, so post-uninstall checks aborted | High | `scripts\production-health.ps1:62` | **Fixed**: path restored, script parses |
| `current` is a size/mtime fingerprint, not a live hash: a same-size content swap with a forced-identical `st_mtime_ns` stays `verified` until the next `/verify` | Low (accepted) | `verify.py`, `state.py` | Accepted trade-off: status is a GET and must not re-hash 1.8 GiB. `/verify` always re-hashes; the UI says "recorded verification" |
| A refused or timed-out verification is not audited (no fingerprint exists and the exception rolls back) | Low | `api\routes\setup.py` | Open: would need an explicit commit before raising |
| A recorded failed verification does not carry the failing check ids; the UI must re-run Verify to show them | Low | `schemas\setup.py`, `verify.py` | Open: additive `failure_ids` field if operators need it |
| Fingerprint check and hash are not atomic (TOCTOU): the lock serialises verifies, not external writers | Low | `verify.py` | Accepted: hash is re-read by `/verify`; no privilege boundary is crossed |

### 2.6 Version and release artifacts

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
  recorded completion and persisted verification (42 setup backend tests).
- The first-run wizard (`app/desktop/src/pages/Setup.tsx`) and its typed API client, with 15 new
  desktop tests (223 total); it reports recorded verification, present-unverified, stale,
  verification-failed, missing and "could not check" as different states, and requires an explicit
  override when a required check fails.
- **Wave 2:** the setup truth model (persisted fingerprint, `verified` only while current, bounded
  verification, 409 completion gate, idempotent completion, upgrade notice), ADR-38, and the updated
  INSTALLATION/UPGRADING contracts. Twelve acceptance scenarios are recorded as **not run** in
  `docs/release/V1_ACCEPTANCE_MATRIX.md`.
- **Wave 2 release evidence:** `--artifact` SBOM binding, `artifact.sbom` and
  `artifact.manifest-scope` gates, an SBOM step in `build-release.ps1`, and the measured stale-0.8.0
  negative test (installer and blockmap both rejected).
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
