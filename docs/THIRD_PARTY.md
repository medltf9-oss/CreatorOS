# Third-party components installed on this machine

Recorded at install time. Versions, sources and licence obligations are part of the
release record: an installer that redistributes these binaries must comply with the
notes in the "Redistribution" column.

For the public notice that ships with the source, see
[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

| Component | Version | Installed at | Source | Licence / redistribution |
|---|---|---|---|---|
| FFmpeg + ffprobe | 9.1-essentials_build (gyan.dev), built 2026 with gcc 16.2.0 | `<DATA_ROOT>\tools\ffmpeg\bin` | https://www.gyan.dev/ffmpeg/builds/ (`ffmpeg-release-essentials.zip`) | **GPL v3** (`--enable-gpl --enable-version3 --enable-libx264 --enable-libx265`). Redistributing the binaries obliges us to provide the corresponding source and licence text; the `LICENSE` file is kept next to the binaries. If a permissive build is required later, switch to an LGPL build and drop GPL-only filters. |
| Ollama | 0.40.18 | `<DATA_ROOT>\Programs\Ollama` | https://ollama.com/download/OllamaSetup.exe | MIT. Bundles its own CUDA/Vulkan runtime libraries under `lib\ollama`. |
| qwen3:0.6b | 522 MB | `<DATA_ROOT>\models\ollama` | Ollama registry | Apache-2.0 (Qwen3) |
| qwen3:1.7b | 1.4 GB | `<DATA_ROOT>\models\ollama` | Ollama registry | Apache-2.0 (Qwen3) |
| qwen3:4b | 2.5 GB | `<DATA_ROOT>\models\ollama` | Ollama registry | Apache-2.0 (Qwen3) |
| qwen2.5-coder:3b | 1.9 GB | `<DATA_ROOT>\models\ollama` | Ollama registry | Apache-2.0 (Qwen2.5-Coder) |
| gemma3:4b | 3.4 GB | `<DATA_ROOT>\models\ollama` | Ollama registry | Gemma Terms of Use (not OSI-open; usage restrictions apply) |
| nomic-embed-text | 274 MB | `<DATA_ROOT>\models\ollama` | Ollama registry | Apache-2.0 |
| Electron | 45.4.1 | `<PROJECT_ROOT>\app\desktop\node_modules\electron` | npm | MIT; bundled Chromium (BSD-style) and Node.js (MIT) |
| Node.js (build tooling) | v26.13.1 (system) | `C:\Program Files\nodejs` (pre-existing) | — | MIT |
| Python | 3.12.10 (system) | `%USERPROFILE%\AppData\Local\Programs\Python\Python312` (pre-existing) | — | PSF licence |
| pnpm | 12.21.0 | `<DATA_ROOT>\tools\npm-global` | npm | MIT |
| React / Vite / Tailwind / TypeScript | see `app/desktop/package.json` + `pnpm-lock.yaml` | `<PROJECT_ROOT>\app\desktop\node_modules` | npm | MIT / Apache-2.0 (lockfile is the record) |

## Known writes outside `<PROJECT_ROOT>`

The application itself writes only under its configured root (INV-6, enforced by
`creatoros.core.paths.PathPolicy`). The following are third-party behaviours we do
not fully control, recorded honestly rather than hidden:

| Path | What | Size seen | Mitigation |
|---|---|---|---|
| `%LOCALAPPDATA%\Ollama\` | Ollama's own `db.sqlite`, `server.log`, `ollama.pid`. Ollama resolves this through the Windows Known Folder API, so redirecting `LOCALAPPDATA` for the child process does not move it. | ~173 KB (grows with logs) | Model weights (the large part) are on D: via `OLLAMA_MODELS`. Log growth is bounded by restarting the server; a future phase can prune it. |
| `C:\Users\<user>\AppData\Local\Temp` | Chromium/Electron scratch files at startup; Windows itself. Machine `TEMP` is intentionally left alone (ADR-05). | transient | Electron user-data directory is redirected to `<DATA_ROOT>\cache\electron-profile` in the packaged build. |
| `C:\Users\<user>\.npmrc`, `%LOCALAPPDATA%\pnpm\config\config.yaml` | tool configuration pointers (not data) | < 1 KB | none needed; no user content |

## Deliberate deviations from the original blueprint

1. **`TEMP`/`TMP` are not set machine-wide to D:** (ADR-05). The blueprint's PowerShell
   snippet does this; it breaks Windows installers and unrelated applications.
   CreatorOS injects `TEMP`/`TMP` per child process instead.
2. **`OLLAMA_MODELS` was pre-set to `D:\NEXUS_DATA\models`** (a stale path that did not
   exist). It now points at `<DATA_ROOT>\models\ollama`; the old value is preserved in
   `config\env_backup.json` for `set_env.ps1 -Revert`.
3. **pnpm obtained via npm with `NPM_CONFIG_PREFIX` on D:** because Node 26 no longer
   bundles corepack.
4. **Node 26 kept for build tooling** instead of installing Node 24 LTS: verified working
   with Vite 8 / electron-builder toolchain. Electron ships its own Node, so the runtime
   is unaffected. Revisit if a native dependency fails to build.
