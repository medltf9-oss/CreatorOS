# Signed v1.0.0 release pipeline

This is the exact order for turning the readiness branch into a published v1.0.0 release. It is an
engineering runbook, not evidence that any step has already passed. As of 2026-11-26 there is no
CA-issued certificate, no signed CreatorOS artifact, no v1.0.0 tag and no v1.0.0 GitHub release.

Nothing here may be skipped, reordered or simulated. A gate failure is a release blocker.

## 0. Preconditions (owner-only)

| # | Precondition | Why it cannot be automated here |
| ---: | --- | --- |
| 1 | CA-issued code-signing certificate with private key in a token/HSM/store | No certificate or private key exists in this environment; never generate a self-signed substitute for release evidence |
| 2 | Approved publisher subject (exact CN/O) | A legal identity decision, not a build setting |
| 3 | FFmpeg distribution route approved | GPLv3 redistribution or a lawful alternative (`DISTRIBUTION_DECISIONS.md`) |
| 4 | Ollama redistribution route approved, or the official-source online route confirmed | `approved_for_bundling` is false; no written approval |
| 5 | Clean Windows 11 x64 machine/VM with Smart App Control enforced (`VerifiedAndReputablePolicyState = 1`), Defender on, no developer tools, no prior CreatorOS install | The audit machine has SAC off, so it cannot produce this evidence |
| 6 | A second clean snapshot with 0.8.0 plus user data, for upgrade and uninstall tests | Required for data-preservation evidence |
| 7 | Version decision: bump `app/desktop/package.json`, `app/backend/pyproject.toml`, `settings.py`, `CHANGELOG.md`, release notes to 1.0.0 | The source is deliberately still 0.8.0 until the gates can close |

## 1. Source stage (this branch, before merge)

```powershell
cd D:\CreatorOS
git status --porcelain          # must be empty
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Source -ReleaseVersion 1.0.0
```

The Source stage validates versions, required documents, the tracked-secret scan, a clean tree, and
the source and prerequisite SBOMs. Missing SBOMs are generated from the checkout; the artifact SBOM is
intentionally absent at this stage. A fresh clone reaches the same result after the documented
bootstrap (`CLEAN_CLONE_GATE.md`).

Then run the full local suites on the same commit:

```powershell
cd app\backend; .\.venv\Scripts\python.exe -m pytest -q; .\.venv\Scripts\python.exe -m ruff check src tests
cd ..\desktop; pnpm typecheck; pnpm test; pnpm build
```

## 2. Merge and CI

1. Open/refresh the PR against `main`; wait for both required checks (`Backend tests (Windows)`,
   `Desktop checks (Windows)`) on the exact head commit.
2. Merge only a green commit. The Release gate checks the latest `main` CI run at the tagged HEAD.
3. Re-run the Source gate on `main` after merge so the recorded HEAD is the release commit.

## 3. Signed build (one command, signing happens before hashing)

`scripts\build-release.ps1` builds the backend payload, renderer and installer, signs when a
certificate is supplied, then writes `SHA256SUMS.txt` and all three SBOMs from the signed bytes.
Do not sign after this command without re-sealing the manifest and SBOMs.

```powershell
$env:CSC_LINK = 'C:\secure\creatoros-codesign.pfx'   # or use -CertificateThumbprint for a store/HSM key
$env:CSC_KEY_PASSWORD = '<from the secret store, never committed>'
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1 -RequireSignature
```

Expected outputs under `releases\1.0.0\`:

- `CreatorOS-Setup-1.0.0.exe` and its blockmap, signed with an RFC 3161 timestamp;
- `SHA256SUMS.txt` covering every shipped asset;
- `sbom-source.cdx.json`, `sbom-prerequisites.cdx.json`, `sbom.cdx.json` (installer-bound).

Before those files are treated as release inputs, inventory the packaged payload:

```powershell
app\backend\.venv\Scripts\python.exe scripts\runtime-inventory.py `
  --payload releases\1.0.0\win-unpacked\resources\backend `
  --sbom releases\1.0.0\sbom.cdx.json `
  --json-out releases\1.0.0\runtime-inventory.json `
  --markdown-out releases\1.0.0\runtime-inventory.md
```

Exit 0 is required. Today the artifact SBOM is generated from `app/backend/.venv`, so a clean payload
reports development components as `absent-from-payload`; that is a release blocker until the SBOM is
generated from the payload itself. See `PRODUCTION_RUNTIME.md` for the measured difference.

If `CSC_LINK` is a token/HSM thumbprint, use `-CertificateThumbprint <sha1>` instead and keep the
private key non-exportable. `-RequireSignature` makes an unsigned build fail before packaging.

Verify the signature independently before any install:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check
powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Verify -Path releases\1.0.0\CreatorOS-Setup-1.0.0.exe
```

`-Verify` must report `Valid`, the approved publisher, a trusted chain and `timestamped`. `-SelfTest`
proves only the local signing machinery with a throwaway certificate; it is never release evidence.

## 4. Artifact stage

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Artifact -ReleaseVersion 1.0.0 `
  -ExpectedPublisher '<approved subject>'
```

This re-hashes the installer, every file named in `SHA256SUMS.txt`, the Authenticode signature and
timestamp, the publisher, and the installer hash recorded in `sbom.cdx.json` (`artifact.sbom`). It
also checks that the artifact SBOM contains every source component. A failure means the bytes are not
the bytes the documents describe: rebuild, do not edit the documents.

## 5. Clean-machine evidence (owner-only)

On the clean SAC-on machine, using the exact installer whose SHA-256 matches the Artifact gate:

1. Capture the security state with `scripts\capture-security-state.ps1` (SAC=1, Defender real-time,
   no exclusions) and the signed installer's `Get-AuthenticodeSignature`.
2. Install with the per-user installer; do not elevate unless the product explicitly requires it.
3. Run the acceptance matrix (`V1_ACCEPTANCE_MATRIX.md`), including first-run setup, upgrade from
   0.8.0 with user data, repair, uninstall preserving data, and the required-check/tamper rows.
4. Record the actual SmartScreen/SAC prompt; never claim a warning did not appear.
5. Write `docs/evidence/v1.0.0-clean-sac.json` in the schema `docs/evidence/SCHEMA.md` defines, with
   `artifact_sha256` equal to the installer hash, `sac.state=1`, `installer.signature_status=Valid`,
   `tests.installed/launched/backend_healthy/first_run_completed/uninstall_preserved_data=true`,
   and a date within 30 days.

Evidence from a different artifact hash, an unsigned build, or the audit machine's SAC-off state
does not close this stage.

## 6. Release stage

Only after sections 3–5 are complete and the merged commit is green:

```powershell
git tag -s v1.0.0 -m 'CreatorOS v1.0.0'
git push origin v1.0.0
gh release create v1.0.0 --title 'CreatorOS v1.0.0' --notes-file docs\RELEASE_NOTES_1.0.0.md `
  releases\1.0.0\CreatorOS-Setup-1.0.0.exe releases\1.0.0\CreatorOS-Setup-1.0.0.exe.blockmap `
  releases\1.0.0\SHA256SUMS.txt releases\1.0.0\sbom.cdx.json releases\1.0.0\sbom-source.cdx.json `
  releases\1.0.0\sbom-prerequisites.cdx.json
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Release -ReleaseVersion 1.0.0 `
  -ExpectedPublisher '<approved subject>' -EvidencePath docs\evidence\v1.0.0-clean-sac.json
```

The Release gate requires the tag on HEAD, the latest `main` CI run green at that HEAD, branch
protection enforcing both checks, a published (non-draft, non-prerelease) release, and the published
installer digest equal to the local artifact. A tag without the evidence is not a release; delete it
and fix the build rather than publishing a second explanation.

Finally re-download the published installer and verify its SHA-256 against `SHA256SUMS.txt` and
`sbom.cdx.json`; record that check in the release notes.

## 7. Failure handling

- **No certificate / publisher not approved:** stop at section 0. Do not publish a version-number-only
  release or an unsigned installer.
- **SAC blocks the signed installer on the clean machine:** capture the exact policy event and stop.
  Never disable SAC or add an exclusion to make acceptance pass.
- **Signature/timestamp/gate failure:** fix the build and re-run; never hand-edit `SHA256SUMS.txt`,
  an SBOM or the evidence JSON.
- **CI green only on an older commit:** wait for CI on the exact tagged commit. The gate compares the
  run's `headSha` with HEAD.
- **Published asset digest mismatch:** delete the release immediately (the tag may stay for audit),
  fix the upload, and document the incident. Do not overwrite the asset silently.

## 8. What remains manual by design

Certificate custody and publisher identity, FFmpeg/Ollama rights decisions, clean-machine execution
and Smart App Control observation, and the final publish decision. Everything else — builds, hashes,
SBOMs, signatures where a key exists, gate checks and acceptance automation — is scripted and
reproducible.