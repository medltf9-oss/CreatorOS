# Artifact verification before execution

The execution harnesses verify the release artifact immediately before they start it. This page
documents what is checked, when, the exact commands, the difference between an unsigned development
build and a signed release, and the gap the guard does not close.

**Current state on this machine (2026-11-25): no code-signing certificate is available.** No
Authenticode signature or RFC 3161 timestamp is claimed here for any CreatorOS artifact. The
`0.8.0` installer is `NotSigned` and its `SHA256SUMS.txt` is stale, so the guard refuses it (see
section 6).

---

## 1. What is verified, and when

`scripts/lib/artifact-guard.ps1` defines one function, `Assert-ArtifactVerified`. It runs in
`scripts/verify-install.ps1` after the installer path and version are resolved and immediately
before the first `Start-Process` of the installer. `scripts/production-health.ps1` calls it in the
same position for `-IncludeUpgrade` and again immediately before the reinstall at the end of
`-IncludeUninstall`.

Before the artifact is executed the guard proves, in this order:

1. **The artifact exists.** A missing file is refused.
2. **A digest manifest exists.** Default: `SHA256SUMS.txt` in the artifact's own release directory
   (`releases/<version>/`). `-ChecksumFile <path>` selects an explicit manifest instead. A missing
   manifest is refused.
3. **Exactly one SHA-256 entry names the artifact's basename.** The accepted format is the one
   `scripts/build-release.ps1` writes and the GNU coreutils format:

   ```
   <64 hex chars><two spaces><filename><two spaces>(annotation)
   <64 hex chars><space><asterisk><filename>                 (binary marker)
   ```

   The optional parenthetical annotation (for example `(136.4 MB)`) is ignored. Blank lines and
   `#` comment lines are ignored. Zero matching entries is refused; more than one matching entry is
   refused rather than choosing one.
4. **The bytes hash to the manifest value.** The guard calls `Get-FileHash -Algorithm SHA256`
   itself. It has no parameter that accepts a caller-supplied digest, so the expected value can only
   come from the manifest. A mismatch is refused and both hashes are reported.
5. **The Authenticode state is acceptable for the requested mode.** The guard calls
   `Get-AuthenticodeSignature`. See section 3 for the policy.

The guard runs at execution time on purpose. Checking at build time, or trusting a hash printed
earlier, would not detect an artifact that was replaced after the manifest was written.

---

## 2. Commands

### Self-test (dependency-free, no Pester)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\tests\artifact-guard.tests.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\tests\artifact-guard.tests.ps1
```

Both exit non-zero when a case fails and print a `passed / failed / NOT RUN` summary. The signed
case is `NOT RUN` on a machine with no validly signed, timestamped file.

### Install verification: unsigned local development build

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-install.ps1 -AllowUnsigned
```

`-AllowUnsigned` is required for a local unsigned build: without it the guard refuses an unsigned
installer. When it is passed, the guard prints a warning that the build is unsigned and must not be
published, and the check line says `UNSIGNED dev build, must not be published`.

### Install verification: release evidence

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-install.ps1 -RequireSignature
```

`-RequireSignature` requires `Status Valid`, a signer certificate, and an RFC 3161 timestamp
(`TimeStamperCertificate`). Without a signature it refuses and no installer runs. A valid signature
with no timestamp also fails in this mode.

An explicit manifest can be supplied in either mode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-install.ps1 -RequireSignature -ChecksumFile releases\1.0.0\SHA256SUMS.txt
```

### Production health: upgrade and uninstall/reinstall

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\production-health.ps1 -IncludeUpgrade -AllowUnsigned
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\production-health.ps1 -IncludeUninstall -AllowUnsigned
```

For release evidence use `-RequireSignature` instead of `-AllowUnsigned`. The upgrade check is named
`installer artifact verified`; the reinstall check is named `reinstall installer verified`.

`verify-install.ps1` and `production-health.ps1` report a refused artifact as a normal `[FAIL]`
check, print the guard's message, and exit 1 without starting the installer.

---

## 3. Modes: unsigned development vs signed release

| Authenticode result | default (no flag) | `-AllowUnsigned` | `-RequireSignature` |
| --- | --- | --- | --- |
| `NotSigned` | refused | **accepted**, loud unsigned warning | refused |
| `Valid`, signer, RFC 3161 timestamp | accepted | accepted | **accepted** |
| `Valid`, no timestamp | accepted, with a warning | accepted, with a warning | refused |
| `HashMismatch`, `NotTrusted`, `UnknownError`, other | refused | refused | refused |

`-AllowUnsigned` never weakens the SHA-256 check: an unsigned development build still needs exactly
one matching manifest entry. It also never accepts a broken or untrusted signature.

`-RequireSignature` and `-AllowUnsigned` are mutually exclusive.

---

## 4. Contract

```powershell
Assert-ArtifactVerified -Path <artifact> [-ChecksumFile <SHA256SUMS.txt>] [-AllowUnsigned | -RequireSignature]
```

Returns a `PSCustomObject` with `Path`, `Name`, `Sha256`, `ChecksumFile`, `SignatureStatus`,
`Signed`, `Timestamped` and `Unsigned`.

Every refusal throws an error whose message starts with `ARTIFACT GUARD:` and names the artifact,
so callers can report the refusal without executing the file. Refusal cases:

| Condition | Message begins with |
| --- | --- |
| artifact missing | `ARTIFACT GUARD: artifact not found: <path>` |
| manifest missing (default) | `ARTIFACT GUARD: SHA256SUMS.txt not found for <name>: expected <path>` |
| manifest missing (explicit) | `ARTIFACT GUARD: checksum file not found: <path> (artifact: <name>)` |
| no entry | `ARTIFACT GUARD: no SHA-256 entry for <name> in <path>` |
| duplicate entries | `ARTIFACT GUARD: duplicate SHA-256 entries for <name> in <path> (...)` |
| hash mismatch | `ARTIFACT GUARD: SHA-256 mismatch for <name>: manifest says ..., file hashes to ...` |
| unsigned, not allowed | `ARTIFACT GUARD: <name> is unsigned; pass -AllowUnsigned ...` |
| invalid signature | `ARTIFACT GUARD: <name> has an invalid Authenticode signature (status=...)` |
| require mode, unsigned | `ARTIFACT GUARD: -RequireSignature: <name> is not validly signed (status=...)` |
| require mode, no timestamp | `ARTIFACT GUARD: -RequireSignature: <name> is signed but has no RFC 3161 timestamp ...` |

---

## 5. Self-test coverage

`scripts/tests/artifact-guard.tests.ps1` builds throwaway artifacts and manifests under `%TEMP%`
and proves:

- a valid hash with `-AllowUnsigned` passes (and the annotation is ignored);
- the GNU binary marker and `-ChecksumFile` are accepted;
- tampered bytes are refused;
- a missing manifest is refused;
- a missing entry is refused;
- a duplicate entry is refused;
- `-RequireSignature` refuses an unsigned artifact;
- an unsigned artifact is refused without `-AllowUnsigned`;
- a non-PE file is never accepted by `-AllowUnsigned` either;
- the signed release path is accepted only when a real validly signed, timestamped file exists on
  the machine. Otherwise the case prints `[NOT RUN]`; no signature is ever fabricated.

The unsigned fixture is a minimal but structurally valid PE image with no certificate table, so
Authenticode genuinely returns `NotSigned` for it. It is never executed.

---

## 6. Measured state on this machine

```
releases\0.8.0\CreatorOS-Setup-0.8.0.exe
  sha256   ad8b7772b6f72ad5d1fc51c196b46308cb20acd2001f3a0ac420875d073a7a4b
  Authenticode Status  NotSigned

releases\0.8.0\SHA256SUMS.txt records
  b9686c6e0eeab95c9d5cff0117f469b4242000baa27c56a9837e5ff1f896c4e0
```

The manifest is stale (it describes bytes that no longer exist), so the guard refuses the 0.8.0
installer even though it is present. This is the intended behaviour: a stale manifest is exactly
the failure mode the guard exists to stop. The local 0.8.0 leftover must not be reused; see
`docs/release/V1_RELEASE_BLOCKERS.md` section 2.4.

No CreatorOS Authenticode or RFC 3161 evidence is claimed. The self-test's signed case copies
Microsoft's signed `powershell.exe` only to exercise the guard's signed branch; it is not evidence
about a CreatorOS artifact.

---

## 7. What this does not do (residual gap)

- **It does not sign anything, and it is not gate evidence.** It adds an execution-time precondition.
  Signing remains `scripts/sign-release.ps1` with an owner-supplied certificate; release acceptance
  remains `scripts/release-gate.ps1`.
- **It does not bind the SBOM.** The guard consumes `SHA256SUMS.txt`. It does not compare
  `sbom.cdx.json`, its `creatoros:artifact` digest, or the artifact bytes across those records.
- **Timestamp presence is not a full RFC 3161 audit.** `-RequireSignature` requires
  `TimeStamperCertificate` and `Status Valid`; it does not independently validate the timestamp
  token's policy or the certificate chain beyond what `Get-AuthenticodeSignature` reports.
- **No publisher identity check.** Any certificate Windows trusts passes `Status Valid`. There is no
  thumbprint or subject allow-list.
- **Time-of-check/time-of-use window.** The hash and signature cover the bytes at the moment of the
  check. A local attacker who can replace the file between the guard returning and `Start-Process`
  is not stopped; the file is not held open or locked. The window is small but not zero.
- **Only the installer is covered.** The uninstaller that `verify-install.ps1 -Uninstall` starts is
  not in `SHA256SUMS.txt` and is not guarded.
- **It does not prevent execution by other tooling.** Any script or person that starts the installer
  without calling the guard bypasses it.

---

## 8. Integration notes for the release owner

- Files: new `scripts/lib/artifact-guard.ps1`, new `scripts/tests/artifact-guard.tests.ps1`, edited
  `scripts/verify-install.ps1` and `scripts/production-health.ps1`. No change to
  `scripts/build-release.ps1`, `scripts/sign-release.ps1`, `scripts/release-gate.ps1`, the SBOM
  generator, or any application source.
- New parameters on both harnesses: `-AllowUnsigned`, `-RequireSignature`, `-ChecksumFile`.
  Existing invocations that used an unsigned build must add `-AllowUnsigned`; release evidence must
  use `-RequireSignature`.
- Check accounting: one check is added before the first installer execution
  (`installer artifact verified`), one more before the `-IncludeUninstall` reinstall
  (`reinstall installer verified`). All existing checks are unchanged.
- Build ordering matters: `scripts/build-release.ps1` writes `SHA256SUMS.txt` after signing. Any
  rebuild after that manifest makes the guard refuse the artifact, as it does for 0.8.0. Publish the
  signed bytes and the manifest that describes those exact bytes together.
- The guard and the release gate are complementary: the gate checks the manifest statically; the
  guard re-checks at execution time.
