# Code signing

Everything about the digital signature of a CreatorOS build: what it proves, why one Windows policy
setting decides whether an installer runs at all, how to sign, how the result is verified, and what a
signature still does not buy.

**Current state:** no certificate is available on this machine, so the shipped installer is **unsigned**.
`scripts/sign-release.ps1 -Check` prints exactly that, and `SHA256SUMS.txt` states it in the release
directory rather than leaving it to be guessed.

---

## 1. What a signature proves, and what it does not

An Authenticode signature over an installer proves two things:

1. the bytes you have are the bytes the publisher signed (any change invalidates it - verified below);
2. the file was signed by the holder of the private key of the certificate chain shown in the signature.

It does **not** prove the software is safe, and it does **not** by itself make a new build run on every
Windows machine. Two independent mechanisms get confused here:

| Mechanism | What it decides | How it is satisfied |
| --- | --- | --- |
| **Authenticode** | who published these bytes, and whether they were altered | a signature from a certificate Windows trusts |
| **SmartScreen** | whether to warn a *user* about a download | reputation earned over downloads and time |
| **Smart App Control (SAC)** | whether an executable or installer may run at all | a signature Windows trusts **and** a reputation it accepts; an unsigned file it has no reputation for is refused outright |

A brand-new certificate starts with no reputation. Signing therefore moves a build from "unsigned" to
"signed, no reputation yet", which is better on SAC and identical on SmartScreen until downloads accrue.

---

## 2. The Smart App Control constraint on this machine

```
Smart App Control : off at the 2026-11-25 acceptance run
                                       VerifiedAndReputablePolicyState = 0
                     it was 1 (enforced) when the unsigned build was refused
```

SAC is a Windows security setting and **nothing in this project changes it**. Not the build, not the
installer, not a script, not a troubleshooting step. The build adapts to it instead:

* `app/desktop/electron-builder.yml` sets `signAndEditExecutable: false` and `disableAsarIntegrity: true`,
  so the packaged executable is not rewritten;
* the installer is per-user under `%LOCALAPPDATA%\Programs\CreatorOS`, needs no elevation, and can be
  replaced by `scripts/install-unpacked.ps1` when a policy refuses the NSIS launcher;
* a refusal is detected and explained instead of producing a stack trace
  (`scripts/verify-install.ps1` prints both remedies).

### What actually happened here, recorded

| When | Result |
| --- | --- |
| Phase 9, first two builds | installer ran, 17/17 checks |
| Phase 9, later builds of the same artefact | refused at launch, Device Guard `CodeIntegrity 3077` |
| Phase 10, `releases/0.8.0/CreatorOS-Setup-0.8.0.exe` (sha256 `0ac5865c…`), re-tested | **installed, 12/12 checks** |
| 2026-11-25, final build `AD8B7772…`, `VerifiedAndReputablePolicyState = 1` | refused at spawn ("application control policy has blocked this file", CodeIntegrity 3118); `npm run package` also failed because the uninstaller step executes that stub |
| 2026-11-25, same final build, machine owner turned SAC off (state 0) | packaging finished; `verify-install.ps1 -Uninstall` = **17/17** |

The same bytes were refused on one day and accepted on another. That is the honest description of
SAC-driven behaviour for an unsigned installer: the decision is made by a reputation service that this
project does not control and cannot query. It is also exactly why an unsigned build **cannot be offered
for distribution** - it may work for the person who built it and fail for everyone else. Signing is the
only thing that turns this from a coin flip into a decision the publisher makes.

---

## 3. Which files get signed

| File | Signed by default | Why |
| --- | --- | --- |
| `CreatorOS-Setup-<version>.exe` (NSIS installer) | yes | this is the file a user runs |
| `Uninstall*.exe` inside the packaged tree | yes | so uninstalling is attributable to the same publisher |
| `win-unpacked\CreatorOS.exe` (the app) | **yes if unsigned**, skipped if already validly signed | see below |
| `resources\backend\python.exe` | never | it is a copy of the PSF-signed CPython interpreter; re-signing it would replace that signature |
| `resources\backend\**` payload | never | pure Python sources; nothing executes them directly |

### The packaged app executable is not vendor-signed

Phase 9's ADR-34 assumed the packaged app keeps the signature Electron ships. Measured on this machine,
that assumption is false:

```
app\desktop\node_modules\electron\dist\electron.exe   sha256 a547b53f…
releases\0.8.0\win-unpacked\CreatorOS.exe             sha256 a547b53f…   (byte-identical, as intended)
Get-AuthenticodeSignature electron.exe                NotSigned
```

The Electron npm distribution binary carries no Authenticode signature here. So signing
`CreatorOS.exe` with a CA-issued certificate does not overwrite a vendor signature - it adds the only
signature that file would have. `scripts/sign-release.ps1` therefore signs it by default, and skips it
only if it already has a valid signature from someone else (pass `-IncludeAppExe` to override).

The reason `signAndEditExecutable` stays `false` is unchanged and unrelated: it stops electron-builder
from rewriting the version block and the icon into the executable, which keeps the bytes identical to
the file the vendor shipped. If you later want icon and version metadata stamped into the executable,
turn it on **and** ensure signing runs afterwards - that is an ADR-34 decision to revisit, not a detail.

---

## 4. Signing a build

### 4.1 What you need

* a **CA-issued** code-signing certificate. Since June 2023 the CA/Browser Forum requires the private
  key to live in FIPS 140-2 Level 2 / Common Criteria EAL 4+ hardware (a token, card or HSM); a
  plain exportable `.pfx` is no longer how public CAs issue these certificates. Individual (IV), OV
  and EV certificates can all sign an installer; EV is not required for NSIS and does not guarantee
  instant SmartScreen reputation.
* the Windows SDK signing tools (`signtool.exe`), which this machine already has at
  `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe`.
* the procurement options, current prices and the owner actions for Algeria are in
  [`CERTIFICATE_PROCUREMENT.md`](CERTIFICATE_PROCUREMENT.md).

### 4.2 Where the secret lives

Two environment variables, exactly as electron-builder expects:

```powershell
$env:CSC_LINK = 'C:\keys\creatoros-codesign.pfx'   # a path, a store thumbprint, or the base64 blob
$env:CSC_KEY_PASSWORD = '<the .pfx password>'      # read by signtool, never printed or written
```

Rules the script enforces:

* **refuses** to use a certificate file that lives inside `<PROJECT_ROOT>` (it would be committed);
* **refuses** to run with no `CSC_LINK`, with a message rather than a silent unsigned build;
* passes the password to `signtool` as a process argument and never echoes, logs or copies it;
* `.pfx`, `.p12`, `.cer`, `.key` and `.pvk` are in `.gitignore`, and no certificate has ever been
  committed to this repository.

### 4.3 One command

```powershell
$env:CSC_LINK = 'C:\keys\creatoros-codesign.pfx'
$env:CSC_KEY_PASSWORD = '<password>'
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1 -RequireSignature
```

With a hardware token in the Windows certificate store, use the thumbprint instead of exporting a
PFX:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1 -RequireSignature -CertificateThumbprint '<SHA-1 thumbprint>'
```

`-RequireSignature` aborts before building when neither input is present, so a release cannot silently
fall back to an unsigned installer.

`build-release.ps1` now signs between packaging and the manifest, so:

1. `scripts/sign-release.ps1 -Sign` signs the installer, the uninstaller and the app executable with
   `/fd sha256 /tr http://timestamp.digicert.com /td sha256` (RFC-3161 timestamp, so the signature stays
   valid after the certificate expires);
2. every artefact is verified with `Get-AuthenticodeSignature` **and** an explicit `X509Chain` build;
3. a single invalid result aborts the release;
4. only then is `SHA256SUMS.txt` written, so the digests describe the signed bytes;
5. the manifest records the signature state and the Smart App Control value of the build machine.

Signing an existing build without rebuilding:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Sign
powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Verify     # exits 1 if anything is unsigned
powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check      # report only, always exits 0
```

A signature is necessary but not sufficient for release. The mandatory gate is enforced separately
and fails closed:

```powershell
# before tagging: versions, docs, SBOM, tracked-secret scan, clean tree
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Source

# after signing: digest, manifest, publisher, RFC 3161 timestamp, signtool chain
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Artifact -ExpectedPublisher "<validated publisher>"

# before publishing: clean SAC-on evidence, tag, CI, branch protection, published asset digest
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Release -ExpectedPublisher "<validated publisher>"
```

The Release stage refuses to pass unless `docs/evidence/v1.0.0-clean-sac.json` records an installer
run on a clean Windows 11 machine with Smart App Control enforced (`state = 1`), Defender real-time
protection on, the same artifact SHA-256, a valid timestamp, a completed first run, and data preserved
through uninstall. That file does not exist yet and must not be invented.

`-Check` today:

```
  Smart App Control : off
  signtool          : C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe
  CSC_LINK          : none
  CSC_KEY_PASSWORD  : not set
  timestamp server  : reachable: http://timestamp.digicert.com

  current signatures:
    CreatorOS-Setup-0.8.0.exe        NotSigned    no timestamp   n/a
    CreatorOS.exe                    NotSigned    no timestamp   n/a
```

---

## 5. Proof that the signing path works

`scripts/sign-release.ps1 -SelfTest` creates a throwaway self-signed certificate in
`Cert:\CurrentUser\My`, signs a **copy** of the real installer under `%TEMP%`, verifies it, tampers with
it, verifies again, then removes the certificate and the copies. It never touches a release artefact and
never modifies a trust store.

```
self-test: signing a COPY of <PROJECT_ROOT>\releases\0.8.0\CreatorOS-Setup-0.8.0.exe
  created throwaway certificate BF26B393686FB761576DFBCA2081897824BA81FD
  before: NotSigned / no timestamp
  after : UnknownError / timestamped - signer CN=CreatorOS Self-Test (throwaway)
  chain : UntrustedRoot
  chain : UntrustedRoot, which is exactly what a self-signed certificate should produce
  tampered copy: HashMismatch

SELF-TEST PASSED: signing, verification, chain building and tamper detection all work.
This does NOT mean Smart App Control accepts a self-signed installer - it does not.
Only a CA-issued certificate, and an installation test, can establish that.
  cleaned up: certificate removed from CurrentUser\My, temporary files deleted
```

`UnknownError` with `UntrustedRoot` is the correct outcome for a self-signed certificate: the signature
is present and the chain is understood, but nothing vouches for the root. `HashMismatch` is the correct
outcome for the tampered copy, and the test asserts that it is *specifically* `HashMismatch` rather than
any error - a weaker check would pass if the file had simply stopped parsing.

One measured subtlety, worth knowing before writing a signature test: the Authenticode hash excludes the
PE checksum field and the appended signature blob, so flipping a byte **near the end of the file** does
**not** invalidate the signature. The self-test therefore modifies the middle of the image.

---

## 6. After signing: what still has to be tested

A valid signature is necessary, not sufficient. After the first signed build:

1. `powershell -File scripts\verify-install.ps1 -RequireSignature` - verifies the signed bytes against
   `SHA256SUMS.txt`, then installs, checks both shortcuts, launches the app, confirms the bundled
   backend and the database revision (12 checks);
2. `powershell -File scripts\verify-install.ps1 -Uninstall -RequireSignature` - adds 5 checks that
   uninstalling removes the shortcuts and leaves the data root intact;
3. install it on a **clean** Windows 11 machine with Smart App Control on and **no** development tools.
   This machine cannot be that machine: it has Python, Node, the Windows SDK and the build tree, all of
   which change what SAC sees. Only a clean machine answers "will a stranger be able to run this?".

Until step 3 has been done with a CA-issued certificate, the honest statement is: *signed and verified
locally; distribution readiness not yet proven on a clean machine.*

---

## 7. If a policy still refuses the build

The unpacked layout is the supported recovery path and is documented in `docs/INSTALLATION.md`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-unpacked.ps1
```

It copies the same packaged tree into `%LOCALAPPDATA%\Programs\CreatorOS`, creates the same two
shortcuts with the same icon, and never needs the NSIS launcher. It does not sign anything, and it does
not claim to work where a policy forbids the application executable itself - if `CreatorOS.exe` is
refused, no installation method helps and only a trusted signature will.
