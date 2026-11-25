# ADR-37 — First-run setup: pinned prerequisites, verification before trust

**Status:** Accepted, implemented. Backend tests and the desktop source suite cover the behaviour
described here; no real Ollama download, signature or install was performed as part of this change.

## Context

The `0.8.0` product documents Ollama and FFmpeg as external prerequisites, but it gave the user no
way to answer three different questions:

1. Is a local runtime actually installed, and is it answering?
2. Is the file I downloaded the exact build the release was tested against?
3. What does the product still need that only the user can supply?

The pinned facts existed only in `docs/release/DEPENDENCY_MANIFEST.md` — a human-readable audit record
with no machine-readable counterpart and no code that consumed it. That produced two failure modes: a
user could install any Ollama build and assume it was the tested one, and a future change could edit
the markdown without anything noticing. The v1.0.0 plan also requires that bundle-ability decisions
(today: Ollama and FFmpeg are **not** approved for bundling) stay visible in the product rather than
living only in release notes.

## Decision

**Pin the prerequisites in a versioned manifest shipped with the backend, verify on request, and never
turn "could not check" into a pass.**

| Concern | Decision |
| --- | --- |
| Where the facts live | `app/backend/src/creatoros/resources/prerequisites.v1.json`, schema `creatoros.prerequisites.v1`, loaded by `services/setup/manifest.py` with strict validation |
| What is pinned | component id, kind (`vendor-installer` / `local-tool`), acquisition mode, https URL, bare filename, positive size, lowercase SHA-256, signer subject, installer arguments, consent requirement, licence, redistribution approval |
| API | `GET /setup/status`, `GET /setup/prerequisites`, `POST /setup/prerequisites/{id}/verify`, `POST /setup/complete` |
| Verification | chunked SHA-256, size comparison, Authenticode status, publisher match, RFC 3161 timestamp when required |
| Failure semantics | a check is `pass`, `fail`, `unknown` or `skipped`; `ok` requires every check to be pass or skipped |
| Detection | bounded HTTP probe of `127.0.0.1:11434`, install-path discovery, `ffmpeg -version`, disk free space and a real write test |
| UI | a first-run page that shows observations, links to the official download, verifies a local file on request, and records a decision |
| Acquisition | the app never downloads or executes a prerequisite; the user keeps the consent step and the vendor keeps its elevated prompt |

### Why the manifest ships inside the package

`scripts/build-backend.ps1` copies `src\creatoros` into the shipped interpreter bundle, so a JSON file
under `resources/` travels automatically with the installed backend. The alternative — reading
`docs/release/*.md` at runtime — would ship documentation into the installer and make a prose table a
parsing dependency.

### Why verification is explicit

Hashing the Ollama installer means reading 1.8 GiB and takes seconds. Doing it on every status request
would make the wizard slow and hide the cost. Presence is cheap and labelled as presence; hashing is a
user action with an audit record (`setup.prerequisite.verify`, outcome success/failure).

### Why "unknown" blocks

If PowerShell cannot read a signature, the file has not been proven. Reporting `ok: true` because the
hash matched would let a stripped or unsigned binary pass on a machine where signature tooling is
broken. `VerificationReport.ok` therefore requires every check to be `pass` or `skipped`, and the UI
colours `unknown` as a caution, not a success.

### What finishing means

`POST /setup/complete` writes `setup.first_run` to `app_setting` (completed, timestamp, version,
`skipped_ai`, actor) and an audit event. It does not install, start, download or enable anything. The
wizard is skippable on purpose: opening the application must never depend on a model download.

## Consequences

- Upgrading a prerequisite is now a two-part change: edit the manifest, and update
  `docs/release/DEPENDENCY_MANIFEST.md` when the release record changes. The gate validates the JSON.
- Signature verification needs a PowerShell host. Without one the report says so instead of guessing;
  that is acceptable for a Windows-only product but must stay explicit in the UI.
- The first wizard paint performs an HTTP probe (1.5 s timeout) and one FFmpeg subprocess. On a machine
  with neither, both fail fast and the page still renders.
- Tests never touch the network or a real Ollama: probes and signature readers are injectable and are
  monkeypatched. The real FFmpeg hash test skips when the pinned build is absent.
- `redistribution.approved_for_bundling` remains `false` for both components. The wizard displays that
  fact; it does not change it. Resolving it is an owner decision recorded in the blockers document.
