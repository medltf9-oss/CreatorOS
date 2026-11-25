# Media security (Phase 5)

Media files are **untrusted input**. A downloaded MP4 can be malformed, can contain an
absurd number of streams, can claim a duration of 400 hours, can have a filename that is a
path, or can be a symlink to `C:\Windows\System32\config\SAM`. The design assumes all of
that.

## Threat model

| Threat | Control | Test |
|---|---|---|
| Renderer asks the backend to read an arbitrary path | imports are IPC-signed by the Electron main process; renderer never holds the secret | `test_import_requires_the_ipc_signature_when_the_shell_is_present` |
| Symlink escapes the media root | `check_source` rejects symlinks and non-regular files | `test_media_storage.py`, `test_import_rejects_symlinks` |
| Path traversal via filename | paths derive from SHA-256; `resolve_stored` re-checks containment | `test_storage_rejects_paths_outside_the_root` |
| Crafted media triggers a shell injection | FFmpeg and ffprobe run with argv arrays, `shell=False`, `CREATE_NO_WINDOW` | `test_media_probe.py` (argument construction) |
| Media processing hangs forever | hard timeout per operation, child tree killed on expiry | `test_ffmpeg_runner_*` |
| Huge file fills the disk | `media_max_import_bytes` (4 GiB) + `require_space` before copy/encode | `test_rejects_oversized_source`, `test_insufficient_disk_space_fails_before_encoding` |
| `?sig=` URL leaked to another asset | signature binds asset id, resource, derivative id and expiry | `test_tampered_signature_is_rejected` |
| A corrupt row points outside the root | `is_inside_root()` before every serve/read | `test_media_api.py` stream tests |
| Malicious filename shown as HTML | filenames are only ever text/JSON; served files are `nosniff` | `test_api_security.py` |
| Agent uses media tools to read arbitrary files | tools take `asset_id` only, never a path; source is re-checked against the root | `test_no_media_tool_can_escape_the_sandbox` |

## Import authentication (fail closed)

`api/ipc_auth.py`:

* The main process generates a 32-byte secret per launch and passes it to the backend via
  `CREATOROS_IPC_SECRET`; the renderer never sees it (context isolation, sandbox on).
* The main process signs `"<unix_ts>." + canonical_json_body` with HMAC-SHA256 and sends
  `x-creatoros-ipc: <ts>.<hex>`.
* The backend rejects a missing header (`IPC_SIGNATURE_REQUIRED`), a stale timestamp
  (±300 s), or a digest mismatch — all `403`.
* When no secret is configured (tests, headless tooling) imports are allowed, because no
  renderer exists in that process. Configured means enforced.

The file picker lives in main (`dialog.showOpenDialog`) and returns only regular, non-symlink
files under a 100-file cap; the renderer receives names and sizes, not authority.

## Filesystem containment

* Every write goes through `PathKind` → a fixed directory under `data/media/`.
* `MediaStorage.resolve_stored()` normalises a stored relative path, joins it to the media
  root, resolves it, and refuses anything that is not inside (`UnsafePath`).
* `is_inside_root()` is used again at serve time (`_serve`) and inside the scene-scan and
  derivatives-list agent tools, so a tampered database row cannot widen access.
* Stored paths never contain a user-provided segment: `<sha256>` plus a generated suffix only.

## Untrusted content handling

* **Probe**: JSON parsing with strict coercion; a field that does not parse becomes `None` and
  is reported, never trusted.
* **Classification by streams, not extension** — a `.mp4` that is really audio is treated as
  audio, and a `.txt` renamed to `.mp4` fails the probe and is rejected/quarantined.
* **Duration sanity**: absurd durations do not cause unbounded loops; timeline positions are
  computed from the stored duration and capped at 40 frames.
* **Output verification**: a "successful" FFmpeg exit is not trusted on its own — the produced
  file is re-probed (dimensions, duration tolerance, size) before the derivative is marked
  `ready`. `test_invalid_output_is_rejected_and_cleaned_up` corrupts the output after a
  successful encode and asserts the row is not `ready`.
* **Quarantine, don't delete**: rejected files are preserved with a recorded reason.

## What the renderer cannot do

* Cannot read a local path directly (no `nodeIntegration`, no `fs` in the renderer).
* Cannot ask the API to read a path (`POST /library/import` is IPC-signed; every other route
  takes ids).
* Cannot fabricate a media URL (HMAC-signed, 10-minute TTL).
* Cannot delete an asset by accident (`?confirm=<asset id>` required; a project reference
  blocks deletion unless `force=true`).
* Cannot see the session token or the IPC secret in the DOM — both stay in main/preload
  scope, and the API token is minted per launch and never written to disk.

## Resource abuse

* One FFmpeg process at a time (`media_max_concurrent_ffmpeg = 1`), enforced by the queue
  lane plus the profile policy, so a runaway transcode cannot starve the AI governor.
* Scene scan is bounded (`max_scenes ≤ 30`, 25 s timeout, 30 s tool timeout).
* Agent media tools are read/compute only — no tool can start an encode, delete a file, or
  take a path. Anything destructive requires a human decision (Phase 7 approvals).

## Audit

Every media API action that mutates state writes an audit event (`media.import`,
`media.transcode`, `media.delete`, `media.recovery.scan`, `media.tool_grants`), and every
agent tool call records `agent.tool.call` with duration and outcome.
