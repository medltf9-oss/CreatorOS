# AGENTS.md

Guidance for AI coding agents working in this repository. Human contributors
should read [CONTRIBUTING.md](CONTRIBUTING.md) as well.

## Read first

1. `README.md` — product scope, requirements, build and test commands.
2. `docs/DESIGN_SYSTEM.md` — visual tokens, shell, components and UI gates.
3. `docs/ADR/README.md` — architecture decisions and invariants.
4. The documentation for the area you will touch: `docs/PUBLISHING_SAFETY.md`,
   `docs/SOCIAL_SECURITY.md`, `docs/MEDIA_SECURITY.md`,
   `docs/CREDENTIAL_VAULT.md`, `docs/ANALYTICS_ARCHITECTURE.md`.
5. `SECURITY.md` before handling credentials, provider payloads or auth.

## Non-negotiable rules

- **Never fabricate.** Do not invent metrics, accounts, provider approvals,
  screenshots, test results, live-publishing success or clean-machine results.
  If something is blocked, say exactly what is blocked and why.
- **Preserve the safety model.** The renderer never receives provider secrets.
  The API stays on loopback with the per-launch token. Publishing requires an
  explicit human approval bound to the exact payload. Media paths stay under
  `CREATOROS_ROOT`.
- **No destructive surprises.** Do not delete user data, media, models,
  databases or logs to make a check pass. Do not force-push or rewrite published
  history. Do not run `git add .`; stage only intended files.
- **No secrets in the repo.** Never commit `.env`, `config/secrets/`,
  certificates, databases, tokens, real account identifiers, private media or
  model weights. Redact logs before sharing them.
- **Keep dependencies honest.** Do not add a package to work around a local
  problem without checking its licence and maintenance status. Record new runtime
  dependencies in the lockfile and `THIRD_PARTY_NOTICES.md` if material.
- **Do not hide defects.** Removing a broken control, disabling a test or
  replacing real data with a mock is not a fix.

## Repository map

```
app/desktop/electron/   Electron main, preload, backend lifecycle, IPC
app/desktop/src/        React renderer (pages/, components/, api*.ts, styles.css)
app/desktop/tests/      node:test suites
app/backend/src/creatoros/
  api/                  FastAPI routes
  services/             domain logic, AI, media, social, publishing, analytics
  workers/              durable queue, scheduler, publisher, metrics collection
  db/                   SQLAlchemy models and sessions
  core/                 settings, paths, security, logging
app/backend/tests/      pytest suites
scripts/                bootstrap, diagnostics, release and verification tooling
docs/                   architecture, operations, design and evidence
```

## Commands

Use PowerShell and absolute or repository-relative paths.

```powershell
# Backend
cd app\backend
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check src tests

# Renderer
cd app\desktop
pnpm typecheck
pnpm test
pnpm build

# Repository health
cd ..\..
powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1 -Root (Get-Location)
powershell -ExecutionPolicy Bypass -File scripts\production-health.ps1
```

The release evidence also used `scripts/verify-install.ps1`, the 49-route UI
audit and a secret scan. Re-run the checks that cover your change; do not claim a
check you did not run.

## Coding conventions

- TypeScript strict mode. Avoid `any`; use the typed API clients in
  `src/api*.ts`. React function components only.
- Styling uses the mapped Tailwind palette and semantic `--app-*` tokens. Do not
  add raw colours to page markup; add a token in `styles.css` if the palette is
  missing a role.
- Icons come from `lucide-react`; use `aria-hidden="true"` for decorative icons
  and an accessible name for icon-only controls.
- Python 3.12, Ruff line length 110, type hints on public functions. Async
  boundaries follow the existing service and worker patterns.
- Keep migrations additive and reversible. Never edit an applied migration;
  create a new revision.
- Tests: pytest for backend, `node --test` for desktop. Prefer real local
  services where the suite already does; use test doubles only at external
  provider boundaries.

## UI changes

- Design both themes. Check 1280×800 and 1424×835.
- Run the route audit for the affected pages in dark and light. Contrast
  failures are defects; clipping is a defect unless it is an intentional
  ellipsis with a `title`.
- Reuse Card, Button, Field, Badge, Stat, EmptyState and ErrorNote.
- Preserve loading, empty, stale and error states. An honest empty state is
  better than a fabricated chart.

## Security and privacy checks

Before proposing publication:

```powershell
git status --short
git diff --cached --stat
git grep -n -I -E "(api[_-]?key|client[_-]?secret|access[_-]?token|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)"
```

Review the matches; do not paste secrets into the transcript. Confirm the staged
set excludes `data/`, `models/`, `logs/`, `temp/`, `config/secrets/`,
`releases/` and `installer_builds/`.

## Reporting

When you finish, state:

- what changed and where;
- what you verified, with exact commands and results;
- what remains unverified or externally blocked, with the owner action needed;
- any new dependency or licence obligation.

Do not describe a plan as completed work.
