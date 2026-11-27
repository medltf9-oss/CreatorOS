# Evidence records

These are the written acceptance records for phases 1-11: what was exercised, what the
observed result was, and what remained open. They are kept because they document real
engineering decisions and known limitations.

The screenshots that originally accompanied them were **removed from this repository and
from its git history** before publication. They were captured from a development machine
and most of them show the local data-root path, the installed interpreter path, or internal
build labels. They are preserved outside the repository in the owner's local evidence
archive; the rows below still name the capture they refer to.

Fresh, clean product screenshots used for documentation live in `docs/assets/`.

---

# Clean-machine release evidence

`scripts/release-gate.ps1 -Stage Release` refuses to pass without a JSON attestation that the exact
signed installer was installed on a clean Windows 11 x64 machine with Smart App Control **enforced**.
The file is evidence, not a build artifact: a human tester must create it from observations on that
machine. Do not generate it from a template, copy it from another release, or edit it to make the gate
pass.

Expected path: `docs/evidence/v1.0.0-clean-sac.json`.

## Required schema

```json
{
  "release": "1.0.0",
  "artifact_sha256": "<lowercase sha256 of the exact installer>",
  "date": "2026-11-25T14:30:00Z",
  "tester": "<name or role, no personal address>",
  "windows": {
    "edition": "Windows 11 Pro",
    "build": 22631,
    "architecture": "x64",
    "clean_install": true
  },
  "sac": {
    "state": 1,
    "source": "HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy\\VerifiedAndReputablePolicyState"
  },
  "defender": {
    "realtime_protection": true,
    "antivirus_enabled": true,
    "signature_age_hours": 12
  },
  "installer": {
    "signature_status": "Valid",
    "timestamped": true,
    "publisher": "<certificate subject>"
  },
  "tests": {
    "installed": true,
    "launched": true,
    "backend_healthy": true,
    "first_run_completed": true,
    "uninstall_preserved_data": true
  }
}
```

## How to collect it

1. Start from a **fresh** Windows 11 x64 installation or VM. Do not install Python, Node, pnpm, the
   Windows SDK, Visual Studio, or the CreatorOS build tree. A machine that has them is not clean
   evidence.
2. Confirm Smart App Control is enforced, not merely present:

   ```powershell
   (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState
   ```

   `1` means enforced; `0` means off and is not acceptable; `2` means evaluation and is not acceptable.
3. Confirm Defender real-time protection is on:

   ```powershell
   Get-MpComputerStatus | Select-Object RealTimeProtectionEnabled, AntivirusEnabled, AntivirusSignatureAge
   ```

4. Download the installer from the **published GitHub release**, never from the build machine. Record
   its SHA-256 and compare it with the release asset digest.
5. Run the installer normally. Accept only the documented per-user default; do not disable SAC, add a
   Defender exclusion, or run an unpacked fallback.
6. Complete the first-run setup. Confirm the application window opens, the backend health endpoint is
   reachable, and a real local inference test succeeds if a model is available.
7. Uninstall through Settings → Apps. Confirm the application directory and shortcuts are removed and
   the data root still contains the database, media, models and logs.
8. Fill every field from what was observed. If a step failed, record `false`; do not omit it. The gate
   will fail and that is the correct outcome.

## What the gate does and does not prove

The gate checks that the attestation is internally consistent with the local artifact hash and the
required fields. It cannot cryptographically prove that the tester used a clean machine. The tester's
name, the machine build, and the recorded commands are the accountability trail. A false attestation is
a release-integrity incident, not a shortcut.
