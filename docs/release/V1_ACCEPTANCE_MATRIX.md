# v1.0.0 clean-machine acceptance matrix

**Status: NOT RUN.** Every row below requires a signed release candidate and a clean Windows 11
machine with Smart App Control enforced. The audit machine is not that machine
(`VerifiedAndReputablePolicyState = 0`), so nothing here is claimed as passed.

**Owner of execution:** the release owner. **Evidence storage:** `docs/evidence/` following
`docs/evidence/SCHEMA.md` (30-day freshness, artifact hash match). Attach the captured files to the
release record; do not paste hashes without the files that produced them.

## Preconditions

1. A signed installer whose Authenticode signature and RFC 3161 timestamp validate on a machine that
   has never trusted the publisher, and whose SHA-256 matches `SHA256SUMS.txt` and the bound
   `sbom.cdx.json` (`artifact.sbom`).
2. A Windows 11 x64 VM created from a clean image, with Smart App Control **on**
   (`VerifiedAndReputablePolicyState = 1`), Defender real-time protection on, no exclusions, no
   developer tools, and no prior CreatorOS install.
3. A second VM (or a snapshot) with CreatorOS 0.8.0 installed plus user data: at least 10 media rows,
   one published item, one scheduled post, and a model manifest entry.
4. `capture-security-state.ps1` available on the test machine.

## Matrix

| ID | Scenario | Precondition | Steps | Expected observable | Evidence | Auto/Manual | Risk |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | Clean install | fresh VM, SAC on, signed RC | verify hash + signature, run `/S`, launch | installer exit 0; both shortcuts target the installed app; backend ready; `/setup/status` returns 200; database at head; no SAC block | hash, `Get-AuthenticodeSignature`, install log tail, status JSON | Automated + manual | High |
| A2 | Upgrade preserves data | 0.8.0 with user data | install RC over 0.8.0 | exit 0; row counts unchanged; schema revision = head; `setup.first_run` preserved; `version_changed` shown once, onboarding not repeated | before/after row counts + state JSON | Automated | High |
| A3 | Repair | RC installed | reinstall same build | exit 0; data unchanged; wizard does not reappear | state JSON, row counts | Automated | Medium |
| A4 | Uninstall keeps user data | RC installed with data | `Un_A /S` | install dir, shortcuts and ARP entry gone; data root and `%APPDATA%\creatoros-desktop` kept | directory listing before/after, `production-health.ps1` post-uninstall snapshot | Automated | High |
| A5 | Required prerequisite failing | clean VM, FFmpeg absent | open wizard, attempt complete | 409 `REQUIRED_CHECKS_FAILING`; UI names the failing check; **Complete anyway** sends `override_required_checks: true` and the override is recorded | API response, audit row | Automated | Medium |
| A6 | Present but tampered FFmpeg | FFmpeg present, equal size, wrong bytes | verify | `ok=false`, state `unverified`, never green; no code executed | verify response | Automated | High |
| A7 | Verified then modified | verified FFmpeg | replace file (same size) | status reports `unverified` because the fingerprint changed; detail says what changed | status JSON | Automated | High |
| A8 | Offline install | no network | install + launch + open wizard | install succeeds; app starts; wizard reports prerequisites offline without hanging | screenshots, timing | Manual | Medium |
| A9 | Low disk | 2 GB free | install | installer fails with a message, or completes with a documented warning; no partial database | free-bytes capture, error text | Manual | Medium |
| A10 | Cancel | during prerequisite verify | cancel | request stops; no half-written verification record; verify can be re-run | audit rows | Automated | Low |
| A11 | Wrong hash / wrong publisher | tampered installer | install | SmartScreen/SAC refuses or warns; no silent install; documented exact prompt | screenshot of prompt | Manual | High |
| A12 | Timestamp/signature failure | sign without timestamp (test build) | inspect | gate fails `artifact.timestamp`; release blocker recorded; build not published | gate output | Automated | High |

## Rules

- A row passes only with the evidence file for the exact artifact hash. A screenshot of a different
  build is not evidence.
- Do not disable Smart App Control, Defender or the firewall to make a row pass. Record the block.
- Do not run the installer with administrator rights "to be safe"; the per-user installer must work
  without elevation. Elevation is only tested where the product explicitly requires it.
- A failed row is a release blocker, not a documentation task.
