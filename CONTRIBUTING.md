# Contributing to CreatorOS

Thanks for helping improve CreatorOS. This project is a Windows-first, local-first
Electron + FastAPI application. Contributions are accepted under the
[Apache License 2.0](LICENSE).

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- Search existing issues and pull requests.
- For a security problem, **do not open a public issue**; follow
  [SECURITY.md](SECURITY.md).
- For a large change, open an issue first so the design and data-contract impact
  can be discussed.

## Development setup

Prerequisites: Windows 10/11 x64, Git, Node.js 22+ with pnpm 9+, Python 3.12+.
Ollama and FFmpeg are optional for tests but required for manual AI and media
checks.

```powershell
git clone <your-fork-url> CreatorOS
cd CreatorOS

cd app\desktop
pnpm install

cd ..\backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

Create a local data root and apply the safe user-scope environment settings:

```powershell
cd ..\..
powershell -ExecutionPolicy Bypass -File scripts\set_env.ps1 -Root (Get-Location) -Apply
```

Run the desktop shell with `cd app\desktop; pnpm dev`. The Electron main
process starts the local backend; do not run the backend on a public interface.

## Checks required before a pull request

```powershell
cd app\backend
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests

cd ..\desktop
pnpm typecheck
pnpm test
pnpm build
```

For UI changes also run the route audit with the app running and
`CREATOROS_DEBUG_PORT` set (see `scripts/ui_drive.py` and
`docs/DESIGN_SYSTEM.md`). Include the before/after result for both themes.

## What we look for

- **Correctness over volume.** Keep the diff focused; do not refactor unrelated
  code in the same pull request.
- **No fabricated state.** Do not insert placeholder metrics, fake provider
  connections, synthetic analytics, or a "success" path that has not run.
  Labelled empty, unavailable and stale states are always preferable.
- **Preserve invariants.** Read `docs/ADR/` and the security, publishing,
  media and data-root documentation before changing those areas. Paths stay
  under `CREATOROS_ROOT`; the renderer never receives provider secrets; publish
  actions stay human-approved.
- **Tests.** Add or update tests for behaviour changes. Backend tests use pytest;
  renderer tests use `node --test`. Bugs need a regression test where practical.
- **Types and style.** TypeScript is strict; avoid `any` unless the boundary is
  genuinely untyped and documented. Python follows Ruff at 110 columns.
- **Accessibility.** Keyboard reachability, visible focus, accessible names for
  icon buttons, and 4.5:1 body-text contrast are required.
- **Privacy.** Never commit `config/secrets/`, `.env`, databases, media, model
  weights, logs, tokens or real account identifiers. Redact logs in issues.

## Commits and pull requests

- Write imperative commit subjects (for example `Fix approval queue retry`).
- Explain the problem, the approach, the tests run, and any remaining limitation
  in the pull request description.
- Do not force-push a shared branch. Do not rewrite published history.
- Keep generated artefacts and build output out of commits.
- A maintainer may ask for changes; review is about the code, not the person.

## Licensing of contributions

Unless you explicitly state otherwise, any contribution you intentionally submit
for inclusion in CreatorOS is licensed under Apache-2.0, without additional terms
or conditions, as described in section 5 of the licence. You confirm you have the
right to submit the work. Do not contribute code copied from a project with an
incompatible licence.
