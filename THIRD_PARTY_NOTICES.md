# Third-party notices

CreatorOS is licensed under the [Apache License 2.0](LICENSE). That licence
covers the original source code and documentation in this repository. It does
not replace or alter the licences of third-party software.

This file summarises the principal third-party components. It is a notice, not
a complete dependency manifest. The authoritative version and licence records
are:

- `app/desktop/pnpm-lock.yaml` and each package's `LICENSE` under
  `app/desktop/node_modules/` after installation.
- `app/backend/pyproject.toml` and the installed distributions' metadata
  (`python -m pip show <package>` or the `.dist-info` directories).
- The licence files shipped with Electron, Chromium, Node.js, Python, FFmpeg and
  Ollama in the locations described below.

## Runtime and build components

| Component | Used for | Licence | Distribution notes |
| --- | --- | --- | --- |
| Electron | Desktop shell | MIT | Bundles Chromium (BSD-3-Clause and other licences) and Node.js (MIT). Electron's `LICENSE` and `LICENSES.chromium.html` are included with the installed package. |
| Chromium | Renderer runtime (inside Electron) | BSD-3-Clause plus component licences | See Electron's `LICENSES.chromium.html`. |
| Node.js | Electron runtime and build tooling | MIT | Electron ships its own Node runtime. |
| React, React DOM | Renderer UI | MIT | |
| Vite, Rolldown | Renderer build | MIT | Development dependency. |
| Tailwind CSS | Styling | MIT | Development dependency. |
| TypeScript | Type checking | Apache-2.0 | Development dependency. |
| lucide-react / Lucide | Interface icons | ISC | |
| CPython | Bundled backend interpreter | PSF-2.0 | The packaged application bundles a Python runtime. |
| FastAPI | Backend HTTP API | MIT | |
| Uvicorn | ASGI server | BSD-3-Clause | |
| SQLAlchemy, Alembic | Persistence and migrations | MIT | |
| Pydantic, pydantic-settings | Settings and schemas | MIT | |
| HTTPX | HTTP client | BSD-3-Clause | |
| psutil | Resource telemetry | BSD-3-Clause | |
| Jinja2 | Templating | BSD-3-Clause | |
| structlog | Structured logging | Apache-2.0 OR MIT | Dual-licensed. |
| python-multipart | Multipart uploads | Apache-2.0 | |
| tzdata | Windows time-zone data | Apache-2.0 | |
| cryptography | Credential vault primitives | Apache-2.0 OR BSD-3-Clause | |
| pytest and test tooling | Tests only | MIT / Apache-2.0 | Not shipped in the runtime. |

## External tools that are not part of this repository

The source repository does not contain FFmpeg binaries, Ollama binaries, model
weights, or user media.

- **FFmpeg / ffprobe.** CreatorOS invokes an FFmpeg installation from its data
  root. The verified build (gyan.dev "essentials", FFmpeg 9.1) is compiled with
  `--enable-gpl --enable-version3` and is therefore **GPL-3.0-or-later**. The
  build is not distributed by this repository. If you redistribute a package
  that includes FFmpeg, you are responsible for the GPL source and notice
  obligations, including the corresponding-source offer. An LGPL build can be
  substituted if its feature set is sufficient; re-run the media verification
  suite after changing builds.
- **Ollama.** Installed separately and licensed MIT. The application talks to
  its local HTTP API at `127.0.0.1:11434` by default.
- **Models.** Model weights are downloaded separately from the Ollama registry.
  Their licences are set by their publishers (for example Apache-2.0 for the
  Qwen models, and the Gemma Terms of Use for Gemma models). Model weights are
  not covered by CreatorOS's Apache-2.0 licence and are not included here.
- **Fonts.** The interface uses Segoe UI Variable and Cascadia Mono from the
  host Windows installation. These fonts are not redistributed by CreatorOS.

## Brand assets

The CreatorOS name, wordmark, application icon and product screenshots are
project assets. The Apache-2.0 licence grants copyright and patent rights in the
code; it does not grant trademark rights in the project name or logos. See
`docs/LICENSING.md`.

## Reporting an omission

If a component is missing or a notice is inaccurate, please open an issue with
the package name, version and the path where it is used. Do not include secrets
or private media in a report.
