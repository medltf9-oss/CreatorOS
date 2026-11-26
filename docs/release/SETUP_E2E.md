# First-run setup: process and desktop end-to-end verification

Wave 3 / specialist A14, integrated by the Director. This document records what was actually
executed, the exact commands, the counts, what is genuinely end-to-end, what was substituted,
and the one real integration defect the desktop journey exposed — together with its fix and
re-run. The defect is no longer open: the click-through override journey now passes.

- Repository: `D:\CreatorOS`
- Branch: `release/v1.0.0-readiness`
- Commit tested: `d65b8d6008bf750c5db3a0e03e772ccbd3dba540` (HEAD moved from `2076cf0` while
  Wave 3 ran; the files under test were not modified by A14)
- Host: Windows 11, Python 3.12.10, FastAPI 0.165.0, uvicorn 0.54.0, Node 26.13.1,
  Electron 45.4.1 (from `app/desktop/node_modules/electron`)
- Recorded: 2026-11-25 (host clock)

## Verdict

| Area | State |
| --- | --- |
| Backend process E2E (real uvicorn, real manifest, real FFmpeg bytes) | **GREEN**: 117/117 checks, 2 journeys |
| Pytest selection requested by the brief | **GREEN**: 19 passed, 2 warnings |
| Desktop E2E: present -> verify -> file changed -> 409 visible | **GREEN**: real Electron, real renderer, real backend |
| Desktop E2E: 409 -> "Complete anyway" -> completion -> audited override | **GREEN (fixed)**: the button was unreachable (SETUP-UI-OVERRIDE-1); after the `setupModel.ts` fix the real click completes and the audit row records it |
| Desktop E2E: UI reflects a completion recorded by another writer | **GREEN**: completion sent through the renderer API (an out-of-band writer), then observed after Re-check |

The desktop defect was not a harness limitation. The real page rendered and the real backend
answered; the renderer never entered its override branch because it looked for a string shape
the backend does not send. The fix is in `app/desktop/src/setupModel.ts` (recognise the
`{code, message}` envelope), with a regression test in `tests/setupWizard.test.mjs` built from
the real `ApiRequestError` shape.

## Exact commands

All commands were run from the stated directory in PowerShell.

Backend process harness (creates its own clean temporary data root and removes it):

```powershell
cd D:\CreatorOS
app\backend\.venv\Scripts\python.exe scripts\e2e_setup.py
```

Backend pytest selection (the command required by the brief):

```powershell
cd D:\CreatorOS\app\backend
.\.venv\Scripts\python.exe -m pytest tests\test_setup_e2e.py tests\test_setup_verification.py -q
```

Desktop UI suite:

```powershell
cd D:\CreatorOS\app\desktop
node --test tests\setupE2E.test.mjs
```

The suite skips itself (three skipped tests, with the missing prerequisite in the reason) when
the backend virtualenv, the Electron binary, the renderer build or the pinned FFmpeg is absent,
so CI's unit-test job does not fail for a missing GUI prerequisite. On the release machine all
four exist and every journey runs.

Optional override of the interpreter used by the UI test (defaults to the backend venv):

```powershell
$env:CREATOROS_E2E_PYTHON = 'D:\CreatorOS\app\backend\.venv\Scripts\python.exe'
```

Single combined command (backend journeys, then the desktop suite; non-zero if any part fails):

```powershell
cd D:\CreatorOS
app\backend\.venv\Scripts\python.exe scripts\e2e_setup.py --with-ui
```

Other harness options: `--keep` retains the temporary roots, `--python PATH` selects the
interpreter, `--skip-backend` runs only the UI suite. The harness exits 0 on success, 1 on a
failed check or failed UI suite, and 2 when the environment itself cannot run it (missing venv,
missing real FFmpeg payload, missing built renderer).

## Results (observed)

| Run | Result | Time |
| --- | --- | --- |
| `scripts/e2e_setup.py` | 117/117 checks, 20 real HTTP requests, 2 journeys, exit 0 | 28.70 s internal / 28.98 s wall (an earlier run took 38.15 s; cold file scans vary) |
| `pytest tests/test_setup_e2e.py tests/test_setup_verification.py -q` | 19 passed, 2 warnings, exit 0 | 35.53 s |
| `node --test tests/setupE2E.test.mjs` (before the fix) | 3 tests: 2 passed, 1 failed (the defect test), exit 1 | 30.79 s |
| `node --test tests/setupE2E.test.mjs` (after the fix) | 3 tests: 3 passed, exit 0 | 48.9 s |
| `scripts/e2e_setup.py --with-ui` (after the fix) | backend 117/117, UI 3 passed, exit 0 | see the gate run in this branch |

The two pytest warnings are pre-existing: Alembic `path_separator` deprecation and the
Starlette test-client httpx deprecation. The 17 tests in `test_setup_verification.py` are the
previous baseline; the two new tests are the process-level journeys.

## Backend process E2E: what actually ran

`scripts/e2e_setup.py` uses only the standard library. For every journey it:

1. Creates a fresh `CREATOROS_ROOT` under the system temp directory.
2. Runs `python -m creatoros migrate` in `app/backend` (real Alembic migrations, real SQLite).
3. Spawns `python -m creatoros serve --host 127.0.0.1 --port <free port>` as a separate
   process and waits for `/health/live`.
4. Talks to it over real authenticated HTTP (`Authorization: Bearer`), then terminates the
   process. Any residual `CREATOROS_*` variables in the parent environment are stripped first.
5. Reads the resulting SQLite database read-only to verify `app_setting` and `audit_event`.

The manifest is the one inside the package,
`app/backend/src/creatoros/resources/prerequisites.v1.json`, unmodified. The success case
copies the repository's real `tools/ffmpeg/bin/ffmpeg.exe` and the harness first proves that
its SHA-256 and size equal the manifest pins. Observed pin:

```text
SHA-256 ed8f66cb877087a96f5bbb40a2944f0e764f2ad8870d5cb647360d912e62bc07
size    110590976
```

No setup route, setup service, hashing function, manifest loader or HTTP layer is mocked.
The failure case writes a synthetic tampered file on purpose and hashes it for real.

### Journey 1: failing required check -> 409 -> explicit override -> restart -> upgrade (91 checks)

Temporary root deliberately has no `tools/ffmpeg/bin`.

- Fresh status: version `0.8.0`, `setup_complete=false`, `first_run.completed=false`,
  `manifest_error=null`, manifest updated `2026-11-25`; FFmpeg `state=missing`,
  `verified=false`, no verification record; readiness `media.ffmpeg=fail`,
  `data.root=ok`, `prerequisites.manifest=ok`.
- Verify a tampered file: HTTP 200 with `ok=false`, the observed SHA-256 recorded, record
  `verification.ok=false`, `file.sha256=fail`. A later status reports `state=unverified` and
  keeps the failed record.
- Verify a relative path: HTTP 422, code `PATH_NOT_ABSOLUTE`.
- Verify the real copied payload: HTTP 200, `ok=true`, hash and size equal the pins,
  record `ok=true` and `current=true`; status then reports `state=verified`, `verified=true`,
  the recorded path, `current=true`, and the catalog agrees. Detail text explicitly says the
  result is recorded, not a live re-hash.
- Complete without override: HTTP 409 with the real envelope
  `{"code":"REQUIRED_CHECKS_FAILING","message":"media.ffmpeg"}` (plus correlation id).
  A following status proves nothing was recorded.
- Complete with `{"override_required_checks": true, "notes": "..."}`: HTTP 200,
  `setup_complete=true`, `already_completed=false`, `first_run.version=0.8.0`,
  `version_changed=false`, timestamp recorded.
- Status afterwards: still complete, same timestamp, `media.ffmpeg` still `fail` (the
  override does not fake the check), verification still recorded.
- SQLite: `setup.first_run` contains completed, the sticky override flag, notes,
  `completed_version=0.8.0` and the timestamp; `setup.verification` contains the ffmpeg record
  under `verifications.ffmpeg` with the pinned hash and verified path.
- Audit: exactly two `setup.prerequisite.verify` events (failure then success, success
  metadata carries the pinned SHA-256), and one `setup.first_run.complete` event with
  `override_required_checks=true`, `failed_checks=["media.ffmpeg"]` and the version.
- Process restart (new process, same root and token): complete, same timestamp, verification
  still reported, currency recomputed from the real file, no version change.
- Upgrade (`CREATOROS_VERSION=9.9.9` on the same root): status reports `9.9.9`, setup stays
  complete, `first_run.version` remains `0.8.0`, `version_changed=true`, and a
  `setup.version_changed` warning check says the wizard does not reopen.
- Re-complete on the upgraded process: HTTP 200, `already_completed=true`, original
  timestamp and completed version preserved; completion audit events accumulate (2).

### Journey 2: present/unverified -> verify -> complete without override (26 checks)

Temporary root has the real `ffmpeg.exe` and `ffprobe.exe` under `tools/ffmpeg/bin`.

- Status: FFmpeg present (`state=ok`) but `verified=false` with no record; detail says
  integrity is unverified until `/verify` runs; `media.ffmpeg=ok`; no required check fails.
- Verify with an empty body (discovery): HTTP 200, `ok=true`, discovered the app-path copy,
  hash and size equal the pins.
- Status then reports verified.
- Complete without override: HTTP 200, not a repeat, version recorded; status stays complete;
  `setup.first_run` has **no** override flag; one completion audit event with
  `failed_checks=[]` and `override_required_checks=false`.

## Pytest wrapper

`app/backend/tests/test_setup_e2e.py` loads the harness with `importlib` and exposes two
tests (one per journey). It adds the module to `sys.modules` before execution because
`dataclasses` resolves string annotations there. It does not use the TestClient and does not
reimplement any part of the journey. The requested command therefore runs the two real
process journeys plus the 17 existing setup-verification tests.

## Desktop UI E2E: what actually ran

`app/desktop/tests/setupE2E.test.mjs` uses `node:test`, no browser-test dependency.

- Launches the real `node_modules/electron/dist/electron.exe .` with a free CDP port, after
  removing any inherited `ELECTRON_RUN_AS_NODE` (otherwise it runs as plain Node).
- `CREATOROS_DEV_URL` points at the built renderer,
  `file:///D:/CreatorOS/app/desktop/dist/index.html`, so this is the real built UI, not the
  Vite dev server.
- Electron main spawns the real backend with the temp root; the test connects over CDP and
  evaluates DOM expressions, dispatches real element clicks, and reads the visible text.
- Each test gets its own migrated temp root. The FFmpeg test copies the real 110 MB binary;
  the other two roots simply lack it, which makes `media.ffmpeg` fail for real.

### UI test 1 (PASS): present -> verify -> file changed -> 409

Observed in the real window:

- Initial FFmpeg card: `Present, not verified`, `not verified`, pinned version
  `9.1-essentials_build-www.gyan.dev`.
- Click `Verify`: card shows `recorded verification` and the pinned SHA-256
  `ed8f66cb...bc07`.
- Delete `ffmpeg.exe` from the running app's root, click `Re-check`: card shows
  `Unverified — file changed` and the readiness row `media.ffmpeg` is `fail`.
- Click `Finish setup`: the Finish card shows the backend 409 text (bare `media.ffmpeg`),
  and a direct page fetch of `/setup/status` confirms `setup_complete=false` and
  `first_run.completed=false`. A refused completion records nothing.
- Override panel after that 409: **true** once the fix is in the build (it was `false` before;
  the same DOM observation is logged by this test and asserted in test 2).

### UI test 2 (PASS): 409 -> Complete anyway -> recorded override -> wizard leaves

Test source: `app/desktop/tests/setupE2E.test.mjs` ("the 409 decision point offers Complete
anyway..."). The temp root has no FFmpeg, so the backend really refuses completion.

1. Click `Finish setup`: HTTP 409, and the Finish card now renders
   `Completion needs an explicit override`, the warning
   `Required checks are failing: media.ffmpeg`, and the `Complete anyway` button.
2. Before pressing it, `GET /setup/status` proves nothing was recorded
   (`setup_complete=false`, `first_run.completed=false`).
3. Click `Complete anyway`: HTTP 200. The shell's `onDone` immediately switches to the
   overview, so the wizard (and its "Setup is complete" line) is intentionally gone; the test
   waits for the wizard to disappear instead of asserting a line that is no longer mounted.
4. `GET /setup/status`: `setup_complete=true`, `first_run.completed=true`,
   `skipped_ai=false`, `version_changed=false`.
5. `GET /audit?action=setup.first_run.complete`: exactly one event, with
   `metadata_redacted_json.override_required_checks=true` and
   `failed_checks=["media.ffmpeg"]` — the override is recorded, not merely accepted.
6. Click the shell's `First-run setup` entry: the wizard reopens and shows
   `Setup is complete. Re-running the wizard only refreshes these checks.` with
   `First run recorded` set to the timestamp.

Observed before the fix (kept as the historical reproduction): the Finish card showed only
`media.ffmpeg`, no `Completion needs an explicit override`, no `Complete anyway`, and the
assertion failed at the panel check.

### UI test 3 (PASS, explicitly SUBSTITUTED): the UI reflects a recorded override

This test does **not** click `Complete anyway`, because that element does not exist in the
DOM. After the same real 409, it calls `POST /setup/complete` from inside the renderer using
`window.creatoros.bootstrap()` (the real API base and session token), gets HTTP 200, clicks
`Re-check`, and observes:

- `First run recorded` with the completion timestamp.
- `Setup is complete. Re-running the wizard only refreshes these checks.`
- `media.ffmpeg` still `fail` (the override did not hide the check).

This is the closest executable proof of the remaining UI behavior. It is not a click-through
override journey and must not be reported as one.

## Defect (RESOLVED): the override branch read the pre-envelope string

**ID:** SETUP-UI-OVERRIDE-1. **Status: fixed and regression-tested** in this branch by the
Director; the click-through journey above passes against the rebuilt renderer.

**Expected:** after a 409 from `POST /setup/complete`, the Finish card explains that required
checks are failing, names them, and offers `Complete anyway`; pressing it completes setup and
the completion is recorded and audited.

**Actual before the fix:** only the raw message `media.ffmpeg` was shown; the override branch
never rendered.

Causal chain (all verified):

1. `app/backend/src/creatoros/api/routes/setup.py:497-500` raises
   `HTTPException(409, "REQUIRED_CHECKS_FAILING: media.ffmpeg")`.
2. `app/backend/src/creatoros/main.py:481-503` (split at lines 499-502) normalizes every
   HTTPException into one envelope, splitting a `"CODE: message"` detail into
   `{code: "REQUIRED_CHECKS_FAILING", message: "media.ffmpeg", correlation_id: ...}`.
   The wire message is the bare ids; the code is a separate field. This is the documented
   backend contract.
3. `app/desktop/src/api.ts:15-20` constructs `ApiRequestError` with
   `message = "media.ffmpeg"` and `code = "REQUIRED_CHECKS_FAILING"`.
4. `app/desktop/src/setupModel.ts:278-296` defines
   `REQUIRED_PREFIX = "REQUIRED_CHECKS_FAILING:"` and line 290 requires
   `error.message.startsWith(REQUIRED_PREFIX)`. The bare message cannot match, so
   `requiredChecksFailure()` returns null and `code` is never consulted.
5. `app/desktop/src/pages/Setup.tsx:250-258` therefore never sets `pendingOverride`; the
   panel at `Setup.tsx:419` and the `Complete anyway` button at `Setup.tsx:436-443` are
   unreachable. Instead `Setup.tsx:418` prints `finishError`, which is the bare id.

Why the existing unit test misses it: `app/desktop/tests/setupTruthModel.test.mjs:133-156`
fabricates `new Error("REQUIRED_CHECKS_FAILING: media.ffmpeg, data.root")` with
`status = 409`. That is the pre-envelope detail string, not what `ApiRequestError` receives
from the real API, so the test passes while the integrated path fails.

**Applied fix (Director, after A14's report).** `requiredChecksFailure` in
`app/desktop/src/setupModel.ts` now treats `code === "REQUIRED_CHECKS_FAILING"` as the primary
signal and still accepts the legacy `REQUIRED_CHECKS_FAILING:` message prefix; the failing ids
are parsed from the wire message. A behavioural test in `app/desktop/tests/setupWizard.test.mjs`
builds the error from the real envelope shape (`{status: 409, code, message: "media.ffmpeg"}`)
rather than a synthetic prefixed `Error`. The desktop build was rebuilt (`pnpm build`) because
Electron loads `dist`, and the click-through journey above then passed. The backend, manifest
and `Setup.tsx` needed no change.

## Genuinely end-to-end vs substituted

Real in the backend journeys:

- Separate `python -m creatoros serve` process; real uvicorn/FastAPI middleware, auth,
  settings, Alembic migrations, SQLite and audit writes.
- Shipped pinned manifest, unmodified; real repository FFmpeg bytes; real SHA-256 hashing of
  a 110 MB file; verification records and completion state read back from SQLite.
- Real HTTP over loopback; real process exit/restart; real version change driven by
  `CREATOROS_VERSION`.

Controlled/substituted in the backend journeys:

- Temporary `CREATOROS_ROOT`, a free loopback port, a random session token, and the
  synthetic tampered file used to force verification failure.
- `CREATOROS_DISABLE_WORKERS=1` and `CREATOROS_SOCIAL_CALLBACK_ENABLED=0` are test-isolation
  knobs only (no background poller, no fixed OAuth callback port). No setup route reads them.
- The upgrade version `9.9.9` is injected; no new build is installed.
- FFmpeg is not executed and no media is transcoded; verification asserts the pinned size and
  SHA-256. Signer fields are recorded in the verification record but this harness does not
  assert their values separately.

Real in the desktop suite:

- Real Electron binary, real main process, real backend child process, real built renderer,
  real React state, real DOM, real HTTP, real 110 MB hashing on the verify click.
- Real file deletion to produce the stale fingerprint.

Substituted/limited in the desktop suite:

- Development Electron app pointed at the built renderer via `CREATOROS_DEV_URL`; the
  installed/asar-packaged app was not exercised.
- Temporary root and ports; CDP `element.click()` instead of a physical mouse; no pixels are
  asserted (the assertions are DOM text and API state).
- UI test 3 completes through the renderer API. It is deliberately the out-of-band case: it
  proves that a completion recorded outside the wizard is reflected after `Re-check`. The
  in-wizard path, including the `Complete anyway` click, is UI test 2 and is fully driven through
  the DOM. (The earlier substitution, where test 2 could not reach the button at all, was the
  defect fixed above and is no longer why test 3 uses the API.)

## Not run / out of scope

- Installed/asar-packaged application, installer execution, auto-update and code-signature
  verification at launch.
- Real acquisition/download of FFmpeg or Ollama; the Ollama `/verify` path; `Skip AI setup`.
- The `version_changed` notice in the UI (the backend journey asserts the warning; the UI
  tests do not restart Electron with a new version).
- Process restart *inside* the Electron session (backend restart is covered separately).
- Keyboard-only navigation, screen readers, multiple display sizes/themes, non-Windows hosts.
- Visual screenshot comparison; no image or pixel assertions were made.
- CI wiring: the commands are documented but not added to `scripts/release-gate.ps1`
  (owned by the Director/another specialist).

## Files added by A14

- `scripts/e2e_setup.py` - backend process harness (also runs the desktop suite with `--with-ui`).
- `app/backend/tests/test_setup_e2e.py` - two pytest entry points for the two journeys.
- `app/desktop/tests/setupE2E.test.mjs` - Electron/CDP desktop suite (3 pass after the fix).
- `docs/release/SETUP_E2E.md` - this record.

A14 changed no production file, manifest, version, package or dependency and committed
nothing. The Director's integration adds the `setupModel.ts` fix, its regression test, the
rewritten click-through test and this document update.

## Director integration notes

- The backend harness is release-gate ready today: command above, exit 0, about 30-40 s,
  needs the backend venv and the real `tools/ffmpeg/bin` payload (it copies it twice).
- The combined `--with-ui` command is the one-command proof and exits 0 after the fix.
- To run only the click-through override journey:
  `node --test --test-name-pattern "409 decision point" tests/setupE2E.test.mjs` from
  `app/desktop` (about 11 s). To run the present->verify and out-of-band journeys:
  `node --test --test-name-pattern "present-unverified|recorded outside the wizard"`.
- The UI test creates a fresh migrated root per test and therefore takes roughly 9-11 s per
  test on this host; budget about 35 s for the suite.
- Keep the backend and UI suites separate in the gate output: the backend green result and the
  desktop red result must not be averaged into one status.
