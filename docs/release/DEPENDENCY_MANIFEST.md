# v1.0.0 dependency and payload manifest

**Audit date:** 2026-11-25  
**Purpose:** record exactly which third-party payload was checked, where it came from, and whether
it is approved for redistribution in the v1.0.0 installer.

This is an audit record, not a redistribution approval. A hash match proves the downloaded bytes are
the bytes that were checked; it does not grant a licence.

## 1. Runtime dependencies

### Ollama

| Field | Value |
| --- | --- |
| Version | `0.40.18` |
| Official release | <https://github.com/ollama/ollama/releases/tag/v0.40.18> |
| Installer asset | `OllamaSetup.exe` |
| Download URL used | <https://github.com/ollama/ollama/releases/download/v0.40.18/OllamaSetup.exe> |
| Size | 1,953,587,080 bytes (1.82 GiB) |
| SHA-256 | `AD2BB47AA969B21CF8DD81B2D9F9A6D5030270ED242EF55087118C37C510ACC2` |
| Official checksum file | <https://github.com/ollama/ollama/releases/download/v0.40.18/sha256sum.txt> |
| Authenticode | `Valid` |
| Signer | `CN=Ollama Inc., O=Ollama Inc., L=Toronto, S=Ontario, C=CA` |
| Issuer | `CN=DigiCert G5 CS ECC SHA384 2021 CA1` |
| Timestamp | DigiCert SHA256 RSA4096 Timestamp Responder 2026 1 |
| Installer technology | Inno Setup |
| Silent install | `OllamaSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` |
| Default install | Per-user under `%LOCALAPPDATA%\Programs\Ollama` |
| Licence | MIT for Ollama; the installer also carries GPU runtime components under their own terms |
| Redistribution status | **Not approved for v1.0.0 bundling yet.** The official installer is signed and hash-verified, but bundling a 1.82 GiB installer that contains NVIDIA/AMD runtime components requires a licence review and a product size decision. |

The hash was checked against Ollama's published `sha256sum.txt`; both matched. The local copy is
ignored by Git and must be re-downloaded and re-verified for a build.

### FFmpeg and ffprobe

| Field | Value |
| --- | --- |
| Version | `9.1-essentials_build-www.gyan.dev` |
| Build page | <https://www.gyan.dev/ffmpeg/builds/> |
| Local file | `<DATA_ROOT>\tools\ffmpeg\bin\ffmpeg.exe` |
| `ffmpeg.exe` size | 110,590,976 bytes |
| `ffmpeg.exe` SHA-256 | `ED8F66CB877087A96F5BBB40A2944F0E764F2AD8870D5CB647360D912E62BC07` |
| `ffprobe.exe` size | 110,374,912 bytes |
| `ffprobe.exe` SHA-256 | `DF0199F606FAA88E9AD192732A7C3987ACFB647663534C297772E17730611A08` |
| Authenticode | Not signed |
| Licence | **GPLv3** (`--enable-gpl --enable-version3 --enable-libx264`) |
| Source reference | <https://github.com/FFmpeg/FFmpeg/commit/74e4393595> |
| Redistribution status | **Blocked for bundling.** GPLv3 requires corresponding source and notices for the binary and its statically linked libraries. The repository has no approved source archive or build scripts for this exact build. |

The application uses `libx264` for H.264 output. An LGPL FFmpeg build is not a drop-in replacement
because it would omit that encoder; changing the encoder is product work, not a packaging tweak.

### CPython

| Field | Value |
| --- | --- |
| Version | `3.12.10` |
| Source | Python Software Foundation Windows installer |
| Authenticode | `Valid`, `CN=Python Software Foundation` |
| Redistribution | PSF licence permits redistribution; the interpreter and DLLs are copied unmodified by `scripts/build-backend.ps1` |
| Status | Approved for the existing interpreter-payload layout, subject to the PSF licence notice |

### Electron / Node / pnpm

| Component | Version | Role |
| --- | --- | --- |
| Electron | 45.4.1 | Runtime shell; Electron binary is redistributed under its own licence |
| Node.js | 24 (CI); 26.13.1 on audit machine | Build only |
| pnpm | 12 | Build only |
| Vite / React / TypeScript | See `app/desktop/pnpm-lock.yaml` | Build/renderer |

The exact frontend dependency graph is in `app/desktop/pnpm-lock.yaml`; the Python graph is in
`app/backend/pyproject.toml` and the installed environment.

## 2. What this means for the installer

The v1.0.0 brief asks for a full offline installer that bundles approved FFmpeg/ffprobe and manages
Ollama. As of this audit:

- Ollama: payload exists and is verified, but is **not approved** for bundling and is not integrated
  into the NSIS build.
- FFmpeg: payload exists, but its **GPLv3 redistribution is unresolved**, so it must not be silently
  copied into an Apache-2.0 installer.
- Therefore the current source does **not** satisfy the full offline installer requirement, even
  before the signing gate is considered.

The release gate must not accept a build whose dependency manifest does not record an approval for
every bundled component.
