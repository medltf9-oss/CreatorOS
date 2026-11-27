# Wave 5 - final engineering defect closure and release-candidate readiness

**Date:** 2026-11-27. **Branch:** `release/v1.0.0-readiness` (PR #7, draft). **Product
version:** 0.8.0, unchanged. **Code/build commit:**
`35924e536590c1aa61cab6f0bf2d418363f7f3c1` (2026-11-27T12:42:08+01:00). The installer was
built from this clean commit; later commits on the branch are test/comment/documentation evidence only and do not change product code.

## Verdict

**NOT READY for v1.0.0.** The nine Wave 5 engineering defects D-5 through D-13 are closed with
code and regression tests, and a fresh unsigned release candidate was built and inspected.
The candidate still fails six mandatory Artifact-stage checks, all consequences of having no
code-signing certificate: certificate, publisher, installer signature, RFC 3161 timestamp,
publisher identity and `signtool verify`. No clean-machine install, Smart App Control (SAC)
acceptance, FFmpeg/Ollama redistribution approval, tag or publication exists. This report does
not turn any of those into a pass.

## 1. What is closed

| ID | Severity | Owner | Implementation | Acceptance evidence |
| --- | --- | --- | --- | --- |
| D-5 | Medium | Release engineering | Removed the direct `package`/`package:dir` electron-builder scripts and the builder comment that recommended the bypass. `build-release.ps1` is the only supported path and writes `release-provenance.json`; the Artifact gate requires and validates it. | `test_release_provenance.py` proves no package script invokes electron-builder, the stamp binds artifact/commit/payload/runtime/SBOMs, a dirty tracked tree is refused, and the gate requires the stamp. The real build produced the stamp. |
| D-6 | Medium | Release engineering / security | Committed `app/backend/runtime-manifest.json` (47 files: `python.exe`, core DLLs and all `DLLs/*`), SHA-256 and size for each. `build-backend.ps1` refuses a non-Valid or non-Python-Software-Foundation signer and verifies the base and assembled trees, including extra `.exe`/`.dll`/`.pyd`. The inventory verifies the packaged tree and the Artifact gate requires it. | `test_runtime_manifest.py` (same-size tamper, extra PE, missing DLL, malformed manifest, committed coverage); `test_runtime_inventory_errors.py`; `artifact.interpreter` passed on the candidate (47 files). |
| D-7 | Medium | Prerequisite tooling | `ffprobe` is a separate pinned component with its own size and SHA-256. Setup detection is stat-only and never executes FFmpeg; SBOM generation hashes bytes and takes the version from the manifest, never `-version`. A missing or mismatched ffprobe fails the media check, and `/setup/verify` verifies both binaries. | `test_setup_prerequisites.py`, `test_setup_verification.py`, `test_release_sbom.py` (static no-exec assertion), `e2e_setup.py`. The setup path now pins both; the runtime media probe still assumes setup verification and is not re-hashed per call. |
| D-8 | Medium | Product / setup | The Ollama card renders the manifest consent summary, blocks the official download until an explicit checkbox is accepted, records consent through `POST /setup/prerequisites/ollama/consent`, and includes the decision in completion audit metadata. Declining hides the download; nothing auto-downloads. | Setup API/state/UI tests and `test_setup_api.py`; the consent record survives restart and is absent from the FFmpeg card. |
| D-9 | Low | Product / prerequisites | Removed both dead `install` blocks and `companion_filenames`; `DEPENDENCY_MANIFEST.md` records the historical silent-install command as audit prose only. | Manifest/schema tests and a repository search show no consumer; the Source gate still passes the 3-component prerequisites document. |
| D-10 | Low | Tooling | `runtime-inventory.py` now requires `--payload`; the stale `releases/0.8.0` defaults are gone. | `test_runtime_inventory_errors.py::test_payload_is_required` asserts exit 2 and a usage message; no `0.8.0` default remains. |
| D-11 | Low | Tooling | Traversal, `stat`, open and hashing failures are wrapped as `InventoryError`; `main` returns exit 2, one line, no traceback and no partial JSON. Exit 1 remains for actual findings. | `test_runtime_inventory_errors.py` covers required/missing payload, a mid-walk `OSError` and a missing manifest; clean and findings paths still return 0/1. |
| D-12 | Low | Product | The missing-local 409 now carries code `NO_LOCAL_FILE`, the exact expected path and the pinned official source in `details.guidance`, while `message` remains the bare required-check ID. The UI shows the publisher build page and verifies the downloaded file. | `test_missing_local_binary_names_the_expected_path_and_official_source`; the E2E override journey still sees `message="media.ffmpeg"` and the happy journey sees the verify-first detail. |
| D-13 | Low | Prerequisite tooling | `prerequisites.v1.json` is the single source for FFmpeg/ffprobe size, SHA-256, licence and source. SBOM, deploy checks, setup routes and tests read that document; no second digest table is authoritative. | `test_setup_prerequisites.py`, `test_release_sbom.py`, `artifact` SBOM properties; the two binaries match the pins (`ed8f66cb...bc07`, `df0199f6...1a08`). |

## 2. Release candidate evidence

| Item | Value |
| --- | --- |
| Installer | `CreatorOS-Setup-0.8.0.exe`, **139,882,940 bytes** |
| Installer SHA-256 | `a45ffb1e4e499a9c1ae055232774d339e8fe109f52717cf5b42befa7c66d7352` |
| Authenticode | `NotSigned`; no timestamp |
| Payload | **2,645 files / 62,520,376 bytes / 33 distributions** |
| Payload tree SHA-256 | `0475d2b19897da44df336280b86ef4f44637ec0a8d1a282253d6fcc49d549941` |
| Runtime manifest | 47 files, SHA-256 `e6b25a84725b2bf3375131fc36f5b4757aa64430c35251da8e95f50ef2ff7b0d` |
| Source SBOM | 48 components, serial `urn:uuid:b9d20cd7-8dc8-5a56-86c9-4e68e06db4fc`, SHA-256 `8722fbd7...c113` |
| Prerequisites SBOM | 3 components, serial `urn:uuid:5937cb04-9cba-5f42-ad48-b8ace7914ff5`, SHA-256 `eda64f9f...0954` |
| Artifact SBOM | 51 components (33 Python), serial `urn:uuid:ca732e3e-ad78-527c-a30f-a9a0a22e1383`, SHA-256 `3442b0cc...5e1a` |
| Provenance stamp | `releases/0.8.0/release-provenance.json`, schema `creatoros.release-provenance.v1`, `signed: false`, `clean_tree: true` (tracked files only; ignored build outputs are not part of that claim) |
| Bundled smoke test | `creatoros version` 0.8.0; migrate empty -> `b8c9d0e1f2a3`; `/health/live` live |

The artifact SBOM binds the installer name/hash/size, payload tree/count/bytes, runtime manifest
hash/count, source SBOM and source commit. The gate re-ran the independent payload inventory:
**0 components absent in either direction**, **0 RECORD size/hash mismatches**, **0 declared
dependencies missing**, and the interpreter manifest verified.

## 3. Tests and CI

* Backend full suite: **1,672 passed, 30 skipped, 0 failed in 273.86s** with `CREATOROS_ROOT`
  pointed at a clean temporary root. This is the final test tree. The 30 skips include the real-DB
  migration tests: the clean-root configuration does not exercise a populated production database,
  so this is not a migration pass against real data.
* Desktop: `pnpm typecheck` passed; `pnpm test` **227 passed, 0 failed**.
* Release build: `scripts\build-release.ps1 -SkipIcons` succeeded from the clean commit and wrote
  the installer, all three SBOMs, inventory and provenance stamp.
* GitHub Actions: run **`45992648710`** at `35924e5`, **SUCCESS**; both `Backend tests (Windows)`
  and `Desktop checks (Windows)` passed. Documentation commits after it do not change product code.

## 4. Gate result

`release-gate.ps1 -ReleaseVersion 0.8.0 -Stage Artifact` ran at `35924e5`:

**25 passed, 6 failed. RELEASE GATE: NOT READY.** All non-signing checks passed, including
`git.clean`, source/prerequisites/artifact SBOMs, SBOM consistency, payload inventory, payload/SBOM
tree and count, interpreter manifest, prerequisites, artifact manifest, artifact SBOM binding and
build provenance. The six failures are `signing.certificate`, `signing.publisher`,
`artifact.signature`, `artifact.timestamp`, `artifact.publisher` and `artifact.signtool`.

The install and upgrade checks were **not run** against this candidate. Their earlier 17/17
pass belongs to the superseded `AD8B7772...` installer, not these bytes.

Residual evidence caveats, independently confirmed by A09:

* `clean_tree: true` means the **tracked** tree was clean. `releases/` is untracked build output and
  is intentionally excluded from that check; the stamp is a local JSON record, not a cryptographic
  attestation.
* `SHA256SUMS.txt` carries a human-readable size annotation on each line. The project gate and the
  artifact guard parse that format, but GNU `sha256sum -c` does not accept it as written.
* The 30 skipped backend tests include populated-database migration tests. The clean-root suite is
  green, but it does not prove migrations against a real populated database.
* The runtime media probe assumes setup verification; it does not re-hash `ffprobe.exe` on every
  individual probe call. The setup verification path is the enforced gate.
* The orphan `releases/1.0.0` SBOMs are not part of this candidate and were quarantined outside the
  repository.

## 5. Task-force record

| Agent | Assignment | Delivered / status |
| --- | --- | --- |
| A01 | Read-only defect audit and D-5..D-13 register | `D:\P\wave5\A01-A10-defect-owner-register.md`; independently found the 2,644/2,645 SBOM mismatch and mapped every defect to code. |
| A02 | D-6 interpreter digest pinning | Implemented by the Director against the A01 brief; no separate agent report (the delegate was rate-limited). |
| A03 | D-7 FFmpeg/ffprobe pins and no-exec detection | Implemented and tested by the Director; A06 independently verified the pinned bytes and GPLv3 facts. |
| A04 | Source/artifact SBOM binding | Implemented by the Director; the real build and Artifact gate prove the binding. |
| A05 | Installer/updater review | Director review: supported build is `build-release.ps1` only; electron-builder retains the vendor-signed Electron exe; no updater/feed is configured. No install was executed. |
| A06 | FFmpeg distribution and licensing options | `D:\P\wave5\A06-ffmpeg-distribution-options.md`; recommends external present-only plus guided download; confirms both binaries match the pins, GPLv3, unsigned, not bundled, and corrects the 2 GiB arithmetic. |
| A07 | Ollama official-download/consent flow | Implemented and tested by the Director; no redistribution approval is claimed. |
| A08 | Security/Windows acceptance | A worktree diff exists (`D:\P\wave5\a08\worktree.diff`) but no independent report. No SAC/Defender/SmartScreen test was run; this remains owner/clean-machine work. |
| A09 | Independent QA | `D:\P\wave5\A09-independent-qa.md`: D-5..D-13 **PASS** in code and focused tests; release-ready **FAIL**. It found the stale D-13 row, the stale SBOM row, the missing D-11 permission/IO test, orphan `releases/1.0.0` SBOMs and the hard-coded provenance `clean_tree`. The first two are corrected here, the D-11 test was added, the orphan SBOMs were quarantined, and the `clean_tree` limitation is stated above. |
| A10 | Gate matrix, evidence and owner handoff | Director updated `V1_RELEASE_GATE_MATRIX.md`, `V1_RELEASE_BLOCKERS.md`, this report and `WAVE4_RELEASE_REPORT.md`; exact commands/results are in `D:\P\wave5`. |

## 6. Owner-only actions still blocking release

1. Obtain a publicly trusted code-signing certificate in the publisher's legal name, with a
   hardware token or cloud HSM; record the exact subject and thumbprint.
2. Sign the installer and uninstaller, timestamp with RFC 3161, and keep `CreatorOS.exe` on
   Electron's vendor signature unless `-IncludeAppExe` is explicitly chosen and tested.
3. Provide a clean Windows 11 x64 machine with SAC enforced; install the signed downloaded
   artifact and record genuine SAC, Defender and SmartScreen evidence.
4. Decide FFmpeg redistribution (external prerequisite recommended) and Ollama redistribution;
   obtain written approval or keep the official-source online route. Do not bundle without it.
5. Run the install, upgrade and uninstall acceptance against the signed candidate.
6. Re-run CI and the Release gate on the signed commit, then decide whether to bump the version,
   tag and publish. None of those actions was taken here.

## 7. Not claimed

* No signed or timestamped CreatorOS artifact; the candidate is `NotSigned`.
* No SAC, Defender, SmartScreen or clean-install pass.
* No FFmpeg/Ollama redistribution or legal approval.
* No v1.0.0 version bump, tag, release asset, publication or merge.
* No A08 independent security report was delivered. A09 delivered an independent QA report, but its
  verdict is "release-ready FAIL"; that is not a release pass.

## 8. Commits

| Commit | Purpose |
| --- | --- |
| `1c8239b` | Code and docs for D-5..D-13, runtime manifest, provenance, tests |
| `affa8cb` | First process-wait fix (superseded by `aee8e4c`) |
| `aee8e4c` | Deterministic `ProcessStartInfo` exit-code handling; first complete candidate build |
| `35924e5` | Test-only CI portability fix for an unverified local ffprobe; final candidate rebuilt from this commit |

The Wave 4 report now carries a correction banner; its artifact SBOM count and artifact claims
must not be used for this candidate.
