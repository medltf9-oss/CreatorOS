# Signature inventory and signing policy

**Measured:** 2026-11-25 on `releases\0.8.0\win-unpacked` (the payload the 0.8.0 installer was
built from)
**Purpose:** define exactly which portable executables (PE files) a release signing pass must cover,
which must keep their third-party signature, and which must not ship at all.
**Related:** `docs/release/CERTIFICATE_PROCUREMENT.md`, `scripts/sign-release.ps1`,
`scripts/build-release.ps1`, `docs/CODE_SIGNING.md`.

This is a baseline inventory, not evidence for v1.0.0. Regenerate it from the exact signed release
candidate; a signature on a different build proves nothing.

## 1. How it was produced

```powershell
$base = "releases\0.8.0\win-unpacked"
Get-ChildItem $base -Recurse -File -Include *.exe,*.dll,*.pyd |
  ForEach-Object {
    $s = Get-AuthenticodeSignature $_.FullName
    [pscustomobject]@{
      Path   = $_.FullName.Substring($base.Length + 1)
      Status = $s.Status
      Signer = $s.SignerCertificate.Subject
    }
  } | Sort-Object Status, Path
```

Result: **64 PE files** — 44 carry a valid vendor signature (41 Python Software Foundation, 3
Microsoft), 20 are unsigned.

## 2. What CreatorOS must sign

| Artifact | Produced by | Rule |
| --- | --- | --- |
| `CreatorOS-Setup-<version>.exe` | NSIS build (`build-release.ps1`) | Sign and RFC 3161 timestamp. This is the file users download. |
| `Uninstall CreatorOS.exe` | NSIS, if generated | Sign and timestamp; it executes with the installer’s privileges. |
| `win-unpacked\CreatorOS.exe` | Electron packaging | Sign and timestamp. It currently carries no signature; `sign-release.ps1` skips it only if a valid signature already exists. |
| Any future CreatorOS-built `.exe`/`.dll` | project build | Sign and timestamp; add it to this table in the same change. |

`scripts\sign-release.ps1` signs every top-level `*.exe` in the release directory, the NSIS
uninstaller if present, and `CreatorOS.exe` when it is not already validly signed. `signtool verify
/pa /all` and a timestamp check follow every signature; any failure aborts the run.

## 3. Third-party binaries that must keep their own signature

Do **not** re-sign these with the CreatorOS certificate: doing so discards the vendor chain, implies
CreatorOS authored them, and can breach the vendor’s terms.

| Class | Count | Signer | Notes |
| --- | ---: | --- | --- |
| Python 3.12 runtime under `resources\backend` | 41 | `CN=Python Software Foundation, O=Python Software Foundation` | `python.exe`, `pythonw.exe`, `python312.dll`, `vcruntime140*.dll` and the rest of the interpreter. The bundled interpreter is deliberately the PSF-signed build (ADR-34), not a PyInstaller one-file executable. |
| Microsoft runtime pieces | 3 | `CN=Microsoft Windows...` | Shipped by the interpreter/OS toolchain; leave untouched. |

## 4. Unsigned PE files in the 0.8.0 payload

| Class | Files | Decision |
| --- | --- | --- |
| Electron runtime DLLs (5) | `dxcompiler.dll`, `electron_wer.dll`, `ffmpeg.dll`, `vk_swiftshader.dll`, `vulkan-1.dll` | Electron ships these unsigned. They are not CreatorOS code and not re-signed. `ffmpeg.dll` here is Electron’s media library, not the FFmpeg CLI. Record their hashes in the SBOM and leave them untouched. |
| `resources\elevate.exe` (1) | electron-builder’s elevation helper | Unsigned upstream. Decide before release: remove it if the per-user installer never elevates, or keep it only if its hash is pinned and its use is documented. It executes with elevation, so shipping it unsigned is a real risk. |
| `CreatorOS.exe` (1) | the app shell | **Must be signed.** Currently unsigned. |
| Python extension modules (9) | `coverage\tracer`, `httptools\parser` (2), `markupsafe\_speedups`, `psutil\_psutil_windows`, `pydantic_core\_pydantic_core`, `watchfiles\_rust_notify`, `websockets\speedups`, `yaml\_yaml` | Native `.pyd` files from wheels; most projects do not sign them. They are loaded into the backend process, so their hashes must be in the SBOM. Do not sign them; do remove the development-only ones. |
| PyInstaller bootloaders (4) | `PyInstaller\bootloader\Windows-64bit-intel\run*.exe` | **Must not ship.** PyInstaller is not used by the packaged product (the backend is the plain interpreter plus `creatoros`). Their presence proves the payload still contains development dependencies. |

### Finding: the payload carries development dependencies

The 0.8.0 `win-unpacked` payload includes `pytest`, `coverage`, `PyInstaller` hooks and bootloaders,
and other development packages under `resources\backend\Lib\site-packages`. They inflate the
installer (143,055,052 bytes) and add executable surface that is not part of the product. Before
v1.0.0, the packaging step must install only runtime dependencies into the bundled interpreter and
prove the result with `pytest`-free, `coverage`-free site-packages. Removing them also removes the
four unsigned bootloader executables above.

This is a packaging task, not a signing task: do not try to make the inventory acceptable by signing
files that should not be present.

## 5. What verification must record

1. The exact artifact SHA-256 and file size.
2. `Get-AuthenticodeSignature` output for every file in section 2: status, signer subject, issuer,
   thumbprint, timestamp presence and timestamp subject.
3. `signtool verify /pa /all` for the installer and the app shell.
4. A post-signing hash of every signed file, so a later change is detectable.
5. Confirmation that the files in section 3 still verify against their vendor chain (a re-signed
   `python.exe` is a failure, not a pass).
6. The clean-machine install result for that exact hash (`docs/evidence/SCHEMA.md`).

## 6. Limits of this inventory

- It describes the 0.8.0 payload. File sets change with Electron, Python and dependency upgrades;
  regenerate the table on the release commit and update this document with the measured counts.
- An Authenticode signature proves who signed the bytes and that they were not modified after
  signing. It does not prove the bytes are free of defects, and it does not by itself earn SmartScreen
  reputation.
- Smart App Control evaluates the launched image and its trust chain; signing `CreatorOS.exe` and the
  installer is necessary, but the only proof that SAC accepts the release is a clean SAC-on install
  (see `V1_RELEASE_BLOCKERS.md`).
