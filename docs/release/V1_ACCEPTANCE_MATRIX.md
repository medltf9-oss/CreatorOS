# v1.0.0 acceptance matrix

**Status:** Wave 3, 2026-11-26. **No v1.0.0 release candidate exists.** The audit machine has Smart
App Control off (`VerifiedAndReputablePolicyState = 0`) and no code-signing certificate, so no row
here is claimed as v1.0.0 acceptance evidence.

Three classes are kept apart on purpose:

- **Executed (agent-executable now):** run against the unsigned local build; proves the code and the
  tooling, not a signed artifact.
- **Agent-executable on the RC:** the command is ready and must be run by an agent or engineer once a
  signed release candidate exists. Not run today because the artifact does not exist.
- **Owner-only:** needs the certificate/publisher identity, the clean SAC-on machine, or the final
  publish decision. An agent cannot produce this evidence and must not simulate it.

## A. Executed in Wave 3

All commands run from `D:\CreatorOS`. Full logs are in the session and the results are repeated in
`WAVE3_RELEASE_REPORT.md`.

| ID | Scenario | Command | Result (2026-11-26) |
| --- | --- | --- | --- |
| S1 | First-run backend journeys: 409 override, verify, stale fingerprint, restart, upgrade | `app\backend\.venv\Scripts\python.exe scripts\e2e_setup.py` | **PASS** 117/117 checks, 2 journeys, 23.4 s |
| S2 | First-run desktop UI: present-unverified, real 110 MB hash, changed file, 409 card, **Complete anyway** click, override audit row, out-of-band completion | `... scripts\e2e_setup.py --with-ui` | **PASS** 3/3 UI tests, 27.9 s |
| S3 | Packaged runtime minimization and inventory | `... scripts\runtime-inventory.py --payload installer_builds\wave3-check --no-sbom` | **PASS** exit 0; 34 distributions, 0 development-only, 2,674 files, 63,384,618 B |
| S4 | Artifact guard: hash, duplicate/missing manifest entries, unsigned refusal, signed path | `pwsh scripts\tests\artifact-guard.tests.ps1` | **PASS** 10/10 |
| S5 | Source release gate on the working tree | `pwsh scripts\release-gate.ps1 -Stage Source -ReleaseVersion 0.8.0` | **14 passed, 1 failed**; the only failure is `git.clean` with the expected uncommitted Wave 3 files |
| S6 | Desktop unit suite | `pnpm test` in `app\desktop` | **PASS** 227/227 |
| S7 | Backend suite | `.venv\Scripts\python.exe -m pytest -q` in `app\backend` | **PASS** 1,636 passed, 30 skipped, 0 failed (4 min 5 s); 21 skips are migration tests that need a production database on the machine, which this audit machine does not have |
| S8 | Source gate from a completely clean clone | `git clone`, `scripts\bootstrap.ps1 -Root <clone> -Apply`, `scripts\release-gate.ps1 -Stage Source -ReleaseVersion 0.8.0` | **PASS** on the final commit; details in `CLEAN_CLONE_GATE.md` |
| S9 | Artifact SBOM vs packaged payload | `... scripts\runtime-inventory.py --payload installer_builds\wave3-check --sbom releases\0.8.0\sbom.cdx.json` | **FINDING** 17 development components in the SBOM are absent from a clean payload; open 1.0.0 requirement (`PRODUCTION_RUNTIME.md`) |

S1/S2 do not prove SAC-on installation, signed-app launch or a packaged (asar) renderer; they drive
the development Electron shell against the built renderer, as documented in `SETUP_E2E.md`.

## B. Agent-executable once the signed RC exists

| ID | Scenario | Procedure | Expected observable |
| --- | --- | --- | --- |
| A1 | Clean install on a machine with no prior CreatorOS | `scripts\verify-install.ps1 -RequireSignature` (or run the signed installer, verify hash + signature first) | install exit 0; both shortcuts target the installed app; backend ready; `/setup/status` 200; DB at head |
| A2 | Upgrade preserves data | `scripts\production-health.ps1 -IncludeUpgrade -RequireSignature` on the 0.8.0 snapshot | 39+ checks pass; row counts unchanged; schema at head; first-run completion preserved; `version_changed` shown once |
| A3 | Repair | install the same signed build over itself | exit 0; data unchanged; wizard not reopened |
| A4 | Uninstall keeps user data | `scripts\production-health.ps1 -IncludeUninstall -RequireSignature` | install dir, shortcuts and ARP entry gone; data root and `%APPDATA%\creatoros-desktop` kept |
| A8 | Offline install | disable the VM network, install and launch, open the wizard | install and launch succeed; prerequisites report offline without hanging |
| A9 | Low disk | 2 GB free, install | installer fails with a clear message, or completes with a documented warning; no partial database |

These need the signed RC because the harnesses refuse an artifact unless its bytes match
`SHA256SUMS.txt` and its signature satisfies `-RequireSignature`. `-AllowUnsigned` exists for local
development only and its output is never release evidence.

## C. Owner-only

| ID | Scenario | Why it cannot be delegated |
| --- | --- | --- |
| A11 | SmartScreen/SAC behaviour for the signed installer | Requires the owner's certificate, publisher identity and a machine that has never trusted that publisher; capture the real prompt, never promise its absence |
| A12 | Missing/invalid RFC 3161 timestamp is rejected | Requires signing material to build a deliberately timestamp-less artifact; the audit machine has no certificate |
| O1 | Certificate and publisher identity | CA issuance and legal identity choice (`OWNER_CERTIFICATE_ACTION.md`) |
| O2 | Clean Windows 11 SAC-on install | `VerifiedAndReputablePolicyState = 1`; the audit machine is 0 and must not be changed |
| O3 | Defender state and exclusions on the clean machine | Owner's test environment; capture with `capture-security-state.ps1` |
| O4 | FFmpeg and Ollama distribution rights | Legal decision; recommendations and exact obligations in `DISTRIBUTION_DECISIONS.md` |
| O5 | Screenshots from the installed RC | Must show the real released UI, not a development shell |
| O6 | Publish, re-download and hash the asset | Owner-only release action; no tag before every gate passes |

## Rules

- A row passes only with evidence for the exact artifact SHA-256. A screenshot or log from a different
  build is not evidence.
- Do not disable Smart App Control, Defender or the firewall to make a row pass. Record the block.
- Do not install with administrator rights to "be safe"; the per-user installer must work without
  elevation.
- A failed row is a release blocker, not a documentation task.
