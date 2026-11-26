# Wave 3 release-candidate report

**Date:** 2026-11-26  
**Branch:** `release/v1.0.0-readiness` (a short series of commits on top of merged `main` at `0683773`)  
**Reviewed code commit:** `ab4499a`  
**Pull request:** #7 (draft)  
**Product version:** `0.8.0`. No `v1.0.0` tag, installer or public release was created.

Wave 3 closed every priority that can be closed without the owner's certificate, clean machine,
licensing decisions and publish approval. This report separates what was executed and measured
from what is still blocked, and it does not upgrade a tooling pass into artifact evidence.

## 1. Priority outcomes

| # | Priority | State |
| ---: | --- | --- |
| 1 | End-to-end first-run setup wizard | **DONE**: backend journey harness 117/117; real Electron/CDP UI 3/3, including the `Complete anyway` click and the audit row |
| 2 | Harden verification before installer execution | **DONE**: artifact guard re-hashes exactly one manifest entry and enforces the signature policy before any process starts; 10/10 tests; wired into install and health harnesses |
| 3 | Reproducible Source gate from a clean clone | **DONE**: clean clone + bootstrap + gate = 13/13 PASS at `5812915`; gate no longer depends on the caller's directory; procedure and the CRLF caveat recorded in `CLEAN_CLONE_GATE.md` |
| 4 | Minimized runtime and packaged inventory | **DONE**: 34 runtime distributions, 0 development-only, 2,674 files, 63,384,618 B, inventory exit 0; three pruning defects fixed |
| 5 | Separate source, prerequisite and artifact SBOMs | **DONE as tooling + honest finding**: profiles exist and the gate checks them; the artifact profile still reads the build venv and reports 17 development components absent from the payload. This is a release blocker, not hidden |
| 6 | FFmpeg and Ollama distribution decision packages | **DONE**: verified facts, exact GPLv3 and MIT/GPU-runtime obligations, five architectures each, recommendations and owner decisions in `DISTRIBUTION_DECISIONS.md` |
| 7 | Agent-executable vs owner-only acceptance | **DONE**: `V1_ACCEPTANCE_MATRIX.md` now has executed rows S1-S9, RC-executable rows A1-A4/A8/A9, and owner-only rows A11/A12/O1-O6 |
| 8 | Remaining gates and signed-release pipeline | **DONE**: 26-gate matrix refreshed; step-by-step signed pipeline in `SIGNED_RELEASE_PIPELINE.md`; remaining blockers are owner actions |

## 2. Measured evidence

| Check | Command (from the stated directory) | Result |
| --- | --- | --- |
| Setup backend journeys | `app\backend\.venv\Scripts\python.exe scripts\e2e_setup.py` | **PASS** 117/117 checks, 2 journeys, 23.4 s |
| Setup desktop UI E2E | `node --test tests\setupE2E.test.mjs` in `app\desktop` | **PASS** 3/3, 27.9 s |
| Desktop unit suite | `pnpm test` in `app\desktop` | **PASS** 227/227, 29.6 s |
| Backend suite | `.venv\Scripts\python.exe -m pytest -q` in `app\backend` | **PASS** 1,636 passed, 30 skipped, 0 failed, 4 min 5 s |
| CI on the code commit | GitHub Actions `45730731847` / `45730732090` | **PASS**: backend 1,630 passed, 36 skipped; desktop checks succeeded |
| Type check | `pnpm typecheck` in `app\desktop` | **PASS** exit 0 |
| Production build | `pnpm build` in `app\desktop` | **PASS** exit 0, built in 0.77 s |
| Backend lint | `.venv\Scripts\python.exe -m ruff check src tests` | **PASS** all checks passed |
| Source gate, development checkout | `pwsh scripts\release-gate.ps1 -Stage Source -ReleaseVersion 0.8.0` | **PASS** 15/15 on the readiness tree; later commits change tests/docs only |
| Source gate, clean clone | clone, `bootstrap.ps1 -Root <clone> -Apply`, gate | **PASS** 13/13 at `5812915` (artifact checks correctly not required) |
| Runtime inventory | `python scripts\runtime-inventory.py --payload installer_builds\wave3-check --no-sbom` | **PASS** exit 0; 34 distributions; 0 development-only; 2,674 files; 63,384,618 B |
| Artifact guard self-tests | `pwsh scripts\tests\artifact-guard.tests.ps1` | **PASS** 10/10 |
| Runtime inventory tests | `pytest tests\test_runtime_inventory.py -q` | **PASS** 14/14 |

The clean clone was created at `1334fe9`, bootstrapped from an empty checkout (bootstrap exit 0,
`git status --porcelain` empty), and gated at 13/13. It was then fast-forwarded to `5812915` (the
CI-safe E2E skip, no runtime change), reset clean and gated again at 13/13. `c912771` changes only
`CLEAN_CLONE_GATE.md`.

## 3. Defects found and fixed

1. **The 409 override card was unreachable in the real renderer.** `requiredChecksFailure` only
   recognised a legacy prefixed string; the backend sends `{status:409, code:'REQUIRED_CHECKS_FAILING',
   message:'media.ffmpeg, ...'}`. The model now keys on the code and returns the ids; the real click
   completes, the override is persisted and the audit row is asserted. Regression test built from
   the real envelope.
2. **The first E2E test waited for a screen that navigation removes.** `onDone()` leaves the wizard
   for the overview; the test now asserts the wizard is gone, checks status/audit, then reopens the
   wizard through navigation.
3. **`build-backend.ps1` pruned the wrong names.** PEP 503 normalization (`foo-bar` vs `foo_bar`),
   `_pyinstaller_hooks_contrib`, `win32ctypes` and single-file modules (`pefile.py`, `peutils.py`,
   `py.py`) survived or were missed. The build now throws if development metadata remains.
4. **The Source gate was dependent on the caller's directory.** It invoked `scripts/generate_sbom.py`
   relative to the current directory, so a clean clone run from `D:\P` failed both SBOM checks.
   Fixed to resolve from the repository root; verified in the clean clone.
5. **The desktop E2E suite failed CI's unit job.** CI tests before building the renderer and without
   the backend venv or pinned FFmpeg. The suite now registers three skipped tests with the missing
   prerequisite in the reason; locally all four exist and it runs for real (227/227).
6. **A persistent `CREATOROS_ROOT` environment variable poisoned migration tests.** Left by an earlier
   clean-gate experiment, it made tests copy an empty scratch database (8 failures). The user value
   was removed and the suite is 1,636/0. A future clean-clone run must start without it; bootstrap
   does not set it.
7. **PR #6 had already been squash-merged**, so the old branch could not be reviewed as a clean PR.
   The Wave 2/3 tree was reparented onto `0683773` without changing content; PR #7 is mergeable.
8. **Two tests assumed the development environment and failed CI.** The source-SBOM test expected
   resolved versions from `app/backend/.venv`, and the setup E2E fixture failed instead of skipping
   when no venv interpreter exists (CI installs into the runner interpreter). Both now skip with the
   missing prerequisite in the reason; the release machine has the venv and runs them.

## 4. Open engineering defects (not owner-only)

- **Artifact SBOM is not payload-bound.** `generate_sbom.py` reads Python components from
  `app/backend/.venv`; a clean payload reports 17 development components as `absent-from-payload`
  (51 in SBOM, 33 matched, 0 bundle-only). The Artifact gate must not treat this document as proof
  of shipped inventory until it reads the packaged payload.
- **First-party metadata is stale.** The backend wheel metadata records `0.1.0` while
  `pyproject.toml` is `0.8.0`. Regenerate package metadata at the release version before 1.0.0.
- **One loose unrecorded payload file.** `Lib/site-packages/README.txt` (121 B) has no RECORD entry;
  harmless, but it is the only unexplained file in an otherwise balanced payload.
- **Source SBOM count varies with the environment** (49 components in the clean clone, 66 in the
  development checkout). The gate validates structure and required components, not a fixed count;
  this is recorded, not explained away.

## 5. Owner-only actions, in order

### 5.1 Code-signing certificate and publisher identity

1. Decide the public publisher string: exact legal name, or a legally registered entity. Do not use
   a pseudonym or "CreatorOS" alone.
2. Choose the route. Recommendation: **SSL.com Individual Validation + YubiKey FIPS**, about
   US$520-600 delivered plus customs; Algeria is accepted and no company is required. Certum Open
   Source (EUR 107-121) only if the project stays strictly non-commercial and Certum accepts the
   maintainer. Microsoft Artifact Signing is not available to an Algerian individual.
3. Order, pay, keep the receipt and order number, complete the CA's identity check in its portal.
   Never send identity documents, PINs, PFX files or private keys to an agent or into the repository.
4. Install the token middleware, set the PIN, confirm the certificate has a private key and the Code
   Signing EKU, then run `powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check`.
   It must report a CA-issued certificate.
5. Deliver to the release owner: thumbprint, subject, issuer, serial, validity dates, the public
   `.cer`, and the approved publisher string.

### 5.2 Clean Smart App Control machine

1. Prepare a Windows 11 machine or VM with Smart App Control **enforced** (not evaluation/off), no
   prior CreatorOS install, Defender active and no exclusions.
2. Install the signed release candidate after verifying its SHA-256 and signature.
3. Run acceptance rows A1-A4, A8, A9 and owner-only rows A11/A12/O1-O6 from
   `V1_ACCEPTANCE_MATRIX.md`; capture state with `scripts\capture-security-state.ps1` and
   `scripts\build-sac-evidence.ps1`; record results in `docs/evidence/install-results.json`.
4. Record the actual SmartScreen prompt and Defender result. Do not promise "no warnings".

### 5.3 FFmpeg distribution route

1. For v1.0.0, approve **option D: keep FFmpeg as an external, present-only prerequisite**. The app
   detects it and guides the user; no FFmpeg bytes are shipped, so no GPLv3 distribution obligation
   is triggered by CreatorOS.
2. If bundling or offering a download instead, approve option C (verified, consented download) or
   option B/A (redistribution), and provide: the exact corresponding source for the GPLv3 build
   (`--enable-gpl --enable-version3 --enable-libx264`), the scripts used to build it, the licence
   texts, and a written legal review. GPLv3 obligations under sections 1, 4, 5, 6 and 10 are listed
   in `DISTRIBUTION_DECISIONS.md`; an offer of source is not the same as shipping it.
3. Do not describe the current unsigned GPLv3 binary as approved for bundling; the manifest keeps
   `approved_for_bundling: false` until the owner changes it with evidence.

### 5.4 Ollama distribution route

1. Approve **option B: guided, hash- and signature-verified download from the official source with
   explicit consent**, or option C (detect and guide). The current manifest already pins the
   installer and keeps `approved_for_bundling: false`.
2. Bundling requires written redistribution approval for the exact installer **and** an inventory of
   the GPU runtimes and model weights, whose licences are separate from Ollama's MIT code licence.
3. The app must never silently install Ollama, and it must fail closed when the hash or signature
   does not match.

### 5.5 Payload and hosting

Approve the online-vs-separate-assets design. The measured combined single asset is
2,317,608,020 B (0.8.0 installer 143,055,052 B + Ollama 1,953,587,080 B + FFmpeg 220,965,888 B),
above GitHub's 2 GiB per-file limit; separate assets are legal (up to 1,000 assets, no total cap).
Measure the real 1.0.0 installer before publishing.

### 5.6 Final release (after 5.1-5.5)

1. Close the engineering defects in section 4, merge PR #7 after a green CI run, and bump every
   version to `1.0.0` with a changelog section.
2. Run `scripts\build-release.ps1 -RequireSignature` on the clean release commit, then the
   `Artifact` and `Release` gate stages with the signed installer and the clean-machine evidence.
3. Tag `v1.0.0` only on the commit whose artifact passed; publish the installer, `SHA256SUMS.txt`,
   SBOMs and notes; re-download on a second machine and hash-match.
4. Do not publish the 0.8.0 release notes as 1.0.0 notes, and do not move the tag after publishing.

The full command sequence is in `SIGNED_RELEASE_PIPELINE.md`; the gate-by-gate state is in
`V1_RELEASE_GATE_MATRIX.md`; the procurement detail is in `OWNER_CERTIFICATE_ACTION.md`.

## 6. What this report does not claim

- No CA-issued certificate, publisher identity, Authenticode signature, RFC 3161 timestamp or Smart
  App Control evidence exists. The audit machine reports `VerifiedAndReputablePolicyState = 0`.
- The unsigned 0.8.0 installer remains `NotSigned`; the artifact guard refuses it for release
  evidence and only permits it with the explicit local-development flag.
- No installer was built from the Wave 3 tree, published or tagged; the runtime measurements come
  from the clean bundle `installer_builds\wave3-check` produced by `build-backend.ps1`.
- FFmpeg and Ollama remain unapproved for bundling; no licence conclusion is asserted beyond the
  obligations cited in `DISTRIBUTION_DECISIONS.md`.
- CI is required on the code-bearing commit; documentation-only commits do not change the artifact.
