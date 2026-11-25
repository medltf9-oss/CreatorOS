# Clean-machine evidence: schema and honesty rules

This document defines the JSON file that `scripts/release-gate.ps1 -Stage Release` reads as the
clean-machine attestation for a CreatorOS release. It is the contract for the file, not a
description of what the gate would like to be true.

The file is written by `scripts/build-sac-evidence.ps1` from three inputs:

1. a read-only capture taken **before** the install (`scripts/capture-security-state.ps1`),
2. the tester's five install observations (`-InstallResults`),
3. a read-only capture taken **after** the uninstall (`scripts/capture-security-state.ps1`).

The assembler hashes the artifact and reads its Authenticode signature itself. It never installs,
signs, or runs anything, and it never writes `true` into `windows.clean_install` or `tests.*`:
those values come from the capture heuristic and from the tester's file, or they are refused.

Schema version: **1** (capture JSON `capture_version`: 1).

## The pipeline

```powershell
# 1. on the fresh Windows 11 x64 machine, before downloading or installing anything
powershell -ExecutionPolicy Bypass -File scripts\capture-security-state.ps1 `
    -Output C:\evidence\before.json

# 2. download the installer from the published release, install it normally, complete first-run,
#    uninstall it, and write the five observations to install-results.json
#    (copy docs\evidence\install-results.example.json and replace every null)

# 3. after the uninstall
powershell -ExecutionPolicy Bypass -File scripts\capture-security-state.ps1 `
    -Output C:\evidence\after.json

# 4. assemble; refuses (exit 1) unless every precondition below is observed
powershell -ExecutionPolicy Bypass -File scripts\build-sac-evidence.ps1 `
    -ReleaseVersion 1.0.0 `
    -Artifact C:\evidence\CreatorOS-Setup-1.0.0.exe `
    -Before C:\evidence\before.json -After C:\evidence\after.json `
    -InstallResults C:\evidence\install-results.json `
    -Tester 'Jane Doe' -Date 2026-11-25 `
    -Output docs\evidence\v1.0.0-clean-sac.json
```

Run both captures from an **elevated** session. Defender reports exclusion lists only to an
administrator, and some policy registry trees are not enumerable by a standard user. A capture
taken without elevation still succeeds, but those fields are `null` and the assembler fails them.
Elevation is for reading only; neither script changes a security setting.

## Fields

`null` means **not observed**. It never means false, zero, or acceptable.

| Path | JSON type | Passing value | Written from |
| --- | --- | --- | --- |
| `release` | string | the release version exactly (`1.0.0`, no leading `v`) | `-ReleaseVersion` |
| `artifact_sha256` | string | lowercase SHA-256 hex of the exact installer | `Get-FileHash -Algorithm SHA256` on `-Artifact` |
| `date` | string | `yyyy-MM-dd` or ISO 8601, within 30 days | `-Date`; when omitted, the assembly time (printed as a note) |
| `tester` | string | a name or role that identifies who observed the run | `-Tester` |
| `windows.edition` | string or null | informational | after capture, else before capture |
| `windows.build` | integer or null | `>= 22000` | after capture, else before capture |
| `windows.architecture` | string or null | informational | after capture, else before capture |
| `windows.clean_install` | boolean or null | `true` | **before** capture, `clean_install.value` |
| `sac.state` | integer or null | `1` (enforced) | after capture, else before capture |
| `sac.source` | string or null | provenance only, not gated | capture `sac.registry_path` + `sac.value_name` |
| `defender.realtime_protection` | boolean or null | `true` | after capture, else before capture |
| `defender.antivirus_enabled` | boolean or null | `true` | after capture, else before capture |
| `defender.signature_age_hours` | number or null | informational | after capture, else before capture |
| `installer.signature_status` | string or null | `Valid` | `Get-AuthenticodeSignature(...).Status` |
| `installer.publisher` | string or null | non-empty certificate subject | `SignerCertificate.Subject` |
| `installer.timestamped` | boolean or null | `true` | `TimeStamperCertificate` is present |
| `tests.installed` | boolean | `true` | `-InstallResults` |
| `tests.launched` | boolean | `true` | `-InstallResults` |
| `tests.backend_healthy` | boolean | `true` | `-InstallResults` |
| `tests.first_run_completed` | boolean | `true` | `-InstallResults` |
| `tests.uninstall_preserved_data` | boolean | `true` | `-InstallResults` |

### `release`, `artifact_sha256`, `date`, `tester`

`release` is the version the evidence is for. The gate also requires it to equal its own
`-ReleaseVersion` and to match the artifact hash, so a file cannot be reused across releases.
`artifact_sha256` is computed from the local file at assembly time; the gate recomputes it and
compares. It proves which bytes were hashed, not which bytes were installed; keeping the downloaded
file untouched from download to assembly is part of the tester's honesty.

`date` defaults to the assembly time, which is recorded and printed when `-Date` is omitted. A
supplied date must parse and be no more than one day in the future. The gate accepts anything
newer than 30 days old; the assembler additionally requires it within 30 days.

`tester` is an accountability field, not an identity check. `TBD`, `todo`, `unknown`, `n/a`,
`tester`, `placeholder` and `<...>` are refused. The gate also rejects an empty or placeholder value
and records `evidence.schema`, but it cannot tell whether the name is true; the assembler is the
stricter writer, and a hand-edited file is an integrity incident either way. Do not record an email
address.

### `windows.*`

`build` is the operating system build from CIM, not the application's. Windows 11 starts at 22000;
the gate requires 22000 or later. `edition` and `architecture` are informational: the gate does
not read them, but they should agree with the machine and are useful to a reviewer.

`clean_install` is copied **only from the before capture**. It is a heuristic, and its output is
deliberately conservative:

- `false` when contamination is found: developer tooling (git, python, node, code, signtool), a
  CreatorOS source/build tree, an existing per-user installation, or an existing data root;
- `true` only when none of that is found and the Windows install date is readable and at most 90
  days old; confidence is then `medium`, never `high`;
- `null` when no contamination is found but the install date is unreadable or older than 90 days.

The full `checked`, `contradictions`, `supporting` and `reason` lists are in the capture, not in the
evidence file. The evidence only carries the `true` this precondition requires. The heuristic
cannot see a cleaned-up machine, a second user profile, a reused VM snapshot, or tools installed
after the capture; the tester's statement that the machine was fresh remains the primary claim.

### `sac.state`

Read from `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy\VerifiedAndReputablePolicyState`.
`0` is off, `1` is enforced, `2` is evaluation. An absent key or value is **not** 0 and not 1: the
capture records `configured: false`, `state: null` and `interpretation: not_configured`, and the
evidence carries `null`, which fails closed. `sac.source` repeats the value path from the capture
for provenance; the gate does not read it.

Smart App Control cannot be turned on programmatically on a machine where Windows did not enable
it. If the machine reports `0`, the only honest outcome is a failed evidence file: provision a
fresh VM whose Windows installation enables SAC, or record the blocker. Editing `state` to `1` is
a false attestation.

### `defender.*`

`realtime_protection` and `antivirus_enabled` come from `Get-MpComputerStatus` in both captures and
must be `true` in both. If the Defender module is unavailable, the capture writes `null` plus a
reason in `collection_errors`, and the evidence fails.

`signature_age_hours` is informational (the gate does not read it). A `null` there does not fail the
assembler, but it should be reviewed: an old signature is not evidence of a disabled Defender.

Exclusions are not stored in the evidence file, but they are a precondition on both captures:
`Get-MpPreference` must return readable path, extension and process lists, and all three must be
empty. A non-administrator capture receives Defender's `N/A: Must be an administrator to view
exclusions` sentinel; the collector records `null` with a reason, and the assembler refuses. That
is intentional: an unreadable exclusion list is not an empty one.

### Recent third-party certificates

The evidence file does not carry certificate details, but the assembler requires
`certificates.recent_third_party.total` to be 0 in both captures. The collector enumerates
`LocalMachine\Root` and `LocalMachine\TrustedPublisher` and reports a certificate only when its
`NotBefore` is within the last 30 days, not more than 24 hours in the future, and its `Subject`
does not contain the word Microsoft. Windows does not record import time, so `NotBefore` is the
only date used: "recent" means recently issued, not provably recently imported. `Issuer` is
recorded but is not an exclusion reason; a third-party certificate issued by a Microsoft CA is
still a third-party trust change. Both stores must be readable; an unreadable store yields null
counts and fails closed.

### `installer.*`

`signature_status` is Windows' own string from `Get-AuthenticodeSignature`: `Valid`, `NotSigned`,
`HashMismatch`, `UnknownError`, `NotTrusted`, and so on. The assembler never maps an unknown status
to `Valid`; the gate requires the literal `Valid`.

`timestamped` is `true` only when Windows returns a non-null `TimeStamperCertificate`. A signed but
untimestamped installer fails, because its signature stops being verifiable when the certificate
expires.

`publisher` is the full `SignerCertificate.Subject`, not a friendly name. The gate checks
`-like "*$ExpectedPublisher*"`, a substring match; a lookalike certificate whose subject happens to
contain the expected string could pass that check. Review the full subject and confirm the
organization, and prefer a publisher string specific enough that a lookalike cannot contain it.

### `tests.*`

These five values are the tester's observations and nothing else. The assembler copies them from
`-InstallResults` and refuses the file if a key is missing, null, a string (`"true"`), or a number.
There is no default and no coercion. A template with nulls is refused by design.

| Key | Meaning | What would make it dishonest |
| --- | --- | --- |
| `installed` | the installer ran to completion and reported success | marking true after a rollback, a partial install, or a manual copy of the app files |
| `launched` | the installed application window opened | marking true when the process started and immediately crashed, or when the unpacked build was launched instead of the installed one |
| `backend_healthy` | the backend health endpoint answered successfully | marking true because the process existed, or by testing the development backend |
| `first_run_completed` | the first-run setup finished and the app became usable | skipping setup, or completing it on a different data root |
| `uninstall_preserved_data` | uninstalling removed the app but left the data root (database, media, models, logs) | marking true when the uninstaller was never run, when data was restored from a backup, or when only the shortcuts were removed |

All five must be `true` for the gate. A failed step is recorded as `false`, the file is still
written, and the gate fails; that is the correct outcome.

## Preconditions (fail-closed)

Once every input parses, the assembler **always writes the evidence file** and then exits `1` if
any line below is `FAIL`. It exits `0` only when all pass. An unmet precondition is printed on its
own line with the observed values, followed by a `REFUSED` summary.

| Precondition id | What it proves |
| --- | --- |
| `windows.build` | the capture machine is Windows 11 (build 22000+) |
| `windows.clean_install` | the before capture found no contamination and a fresh-enough OS |
| `sac.enforced` | SAC was `1` in both captures |
| `defender.realtime_protection` | real-time protection was on in both captures |
| `defender.antivirus_enabled` | the antivirus engine was on in both captures |
| `defender.exclusions` | both captures have readable exclusion lists and all are empty |
| `certificates.recent_third_party` | both captures report zero recently issued third-party root/trusted-publisher certificates (see the certificate window below) |
| `captures.same_machine` | both captures name the same machine |
| `captures.before_freshness` | the before capture is at most `-AllowStaleHours` old (default 24) and not more than one hour in the future |
| `captures.ordering` | the after capture does not predate the before capture |
| `captures.after_freshness` | the after capture is at most `-AllowStaleHours` old and not more than one hour in the future |
| `tests.observed` | all five install observations are `true` |
| `artifact.sha256` | the artifact was hashed |
| `installer.signature_status` | Windows reports `Valid` |
| `installer.timestamped` | Windows reports a timestamp certificate |
| `installer.publisher` | Windows reports a non-empty signer subject |
| `date.recent` | the evidence date is within 30 days and not more than one day in the future |

Refusal behaviour:

- missing, unreadable or malformed **inputs** (including a capture without `capture_version`, a
  bad date, or an install-results key that is not a real boolean) -> `REFUSED`, exit `1`, and **no
  evidence file is written**;
- inputs parse but a precondition fails -> the file is written, `FAIL` lines and a `REFUSED`
  summary are printed, exit `1`;
- the file cannot be written -> exit `2`.

## The two capture files

The captures are working files. They contain the machine name, BIOS serial and disk models, the
subjects, thumbprints and serial numbers of recently issued third-party certificates, policy keys,
and redacted user paths. Keep them in an access-controlled evidence archive; **do not commit
them**. The committed artifact is only the assembled JSON, which contains the tester name, the
publisher subject and the build.

`collection_errors` lists every field the collector could not read, with the exception type. A
capture with entries is still valid JSON and still records everything else; the assembler prints
the before/after counts and then evaluates the preconditions on the observed values. Do not delete
entries to make a capture look complete.

The collector is read-only: it reads CIM, the registry, Defender status and certificate stores, and
never writes a registry value, changes a security setting, installs anything, or runs the installer.

## What this file does not prove

- It is not signed and not tied to the tester or the machine by any cryptographic mechanism.
- The hash binds the file that was hashed, not the file that was installed. Only the tester's
  process connects them.
- SAC and Defender were in the recorded state at capture time. They could be changed afterwards.
- The clean-install flag is a heuristic, and `true` carries only `medium` confidence in the capture.
- A capture taken between the two points could miss a Defender exclusion that was added and removed
  entirely within the install window.
- The publisher check is a substring match. Review the full certificate subject.
- A false attestation is a release-integrity incident, not a gate workaround.

## Versioning

Add fields; do not repurpose them. The gate's `evidence.schema` check requires every documented
top-level and nested key to be present, each value to have the documented type (or `null` where the
table allows it), and rejects unknown top-level keys. An additive field is therefore not safe until
the gate is updated in the same change. Changing the meaning of an existing field requires a new
schema version here and a matching change in both the assembler and the gate. `docs/evidence/README.md`
shows an illustrative example that includes optional fields; this document defines what the assembler
actually writes and what the gate actually reads.

## Related file

`docs/evidence/install-results.example.json` is a template, not a result. Its five values are
`null` and it carries `_template` / `_not_a_result` markers, so passing it to the assembler is
refused. Copy it, replace every `null` with an observed `true` or `false`, and keep the result
outside the repository.
