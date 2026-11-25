# Phase 5 — Media intake, processing and content library · Implementation plan

Status: **plan approved by the brief; implementation in progress.**
Baseline: `96117ef` (Phase 4 complete). Nothing below replaces Phase 1–4 infrastructure;
every item either extends an existing component or is a new user-facing capability.

## 0. Repository findings that constrain the plan

| Finding | Consequence |
|---|---|
| `media_asset` already exists (Phase 2) with `sha256` UNIQUE, `source_path`, `thumbnail_path`, rights/tags/archive fields, 0 rows in the real DB | Extend it; never invent a second media table for the same concept |
| Phase 2 queue already has claim/lease/heartbeat/backoff/dead-letter/idempotency | Media jobs reuse it; no new scheduler |
| `QueueJob.payload_json` + `result_json` + `cancel_requested` exist | Real progress and cancellation without a new table |
| `PathPolicy` (`resolve_under_root`) is the single write gateway; `data/media/{incoming,working,exports,thumbnails,subtitles,archive}` already configured | Add the new media kinds to settings; keep old kinds for compatibility |
| Electron preload exposes only 3 functions; main spawns the backend with a per-launch token | Import paths must come from the main process, signed so the renderer cannot forge them |
| FFmpeg 9.1 static build with libx264/libwebp/libmp3lame/libopus/libvpx | Software encoding only (see measured NVENC failure below) |
| **Measured:** `h264_nvenc` fails on driver 582.78 (`nvenc API required 13.1, found 13.0`) | GPU encoding must be *probed*, never assumed; libx264 is the default |
| 111 agents, 12 tools, `tool_registry` + `agent_definition.allowed_tools_json` grants | New media tools are added to the catalogue and granted once, recorded in `app_setting` |
| Existing `/media/{asset_id}` CRUD route | New library API uses a distinct `/library` prefix (no ambiguity with `{asset_id}`) |
| `UtcDateTime` rejects naive datetimes | All new timestamps go through the existing type |
| Real DB head `b7f1c02d4e61`, 36 tables, `foreign_key_check` clean, 0 assets | Migration tested against a **copy** of that file, not only a fresh schema |

### Measured hardware facts (this machine, this session)

```
ffmpeg 9.1: 5 s of 1080p30 → 720p H.264 (libx264 veryfast, crf 23): 0.71 s wall  ≈ 7× realtime
            source generation 1080p30 5 s testsrc2: 1.02 s,  3.43 MB
h264_nvenc: fails — "Driver does not support the required nvenc API version.
            Required: 13.1  Found: 13.0 ... minimum required driver 610.00"
disk free:  932 GB on D:  (C: ~30 GB — never used)
```

GPUs present: GTX 1050 Ti Max-Q (Pascal). NVENC is **unavailable**, so the transcode engine
must (a) default to libx264, (b) probe for a working hardware encoder at first use and cache
the result, (c) never fail a job because hardware is missing.

## 1. Scope

Build a media subsystem with: deterministic storage, secure import, a real probe engine,
thumbnails/waveforms, four transcode profiles, durable queue integration, a content-library
GUI (7 pages), agent tools over real media analysis, and documentation/evidence — all local,
all on D:, preserving Phases 1–4.

Out of scope (later phases): timeline editing, subtitles burn-in/transcription, publishing.

## 2. Architecture

```
Electron main (native dialog / dropped paths)
   │  HMAC-signed request  (secret only the shell and backend know)
   ▼
POST /library/import ──► media_asset rows (intake_state) + queue jobs (media.import)
                              │
                              ▼
                   durable queue worker (single intensive slot)
     media.import → probe → thumbnail → waveform? → preview?/proxy?
                              │
                              ▼
        data/media/{originals,thumbnails,previews,proxies,transcodes,audio,waveforms,staging,quarantine}
                              │
                              ▼
    /library/assets/{id}/{thumbnail,stream,derivatives,history}  (token or signed URL)
                              │
                              ▼
              Content library GUI (7 pages) + agent tools
```

### Storage layout (deterministic)

```
data/media/originals/<sha[0:2]>/<sha256>.<ext>     never modified after import
data/media/thumbnails/<sha[0:2]>/<sha256>-<kind>-v<ver>.webp
data/media/previews/<sha[0:2]>/<sha256>-preview-v<ver>.mp4
data/media/proxies/<sha[0:2]>/<sha256>-proxy-<res>p-v<ver>.mp4
data/media/transcodes/<sha[0:2]>/<sha256>-<profile>-v<ver>.mp4
data/media/audio/<sha[0:2]>/<sha256>-audio.<ext>
data/media/waveforms/<sha[0:2]>/<sha256>-waveform-v<ver>.{json,png}
data/media/staging/<asset-or-job>.<rand>.part     deleted on failure/recovery
data/media/quarantine/<sha[0:2]>/<sha256>.<ext>   unreadable or policy-violating imports
```

Path is derived from the content hash, never from the user's filename; the original name is
metadata only. Import writes to `staging` and `os.replace()`s into place, so a crash can never
leave a half-written original.

### New database objects (minimal, justified)

`media_asset` (extend): `kind`, `mime_type`, `format_name`, `probe_json`, `probe_version`,
`intake_state`, `last_processed_at`, `quarantine_reason`. All nullable or defaulted → safe on the
existing file.

`media_derivative` (new): one row per generated file.
`id, asset_id FK→media_asset ON DELETE CASCADE, kind, profile, version, rel_path, size_bytes,
sha256, state, job_id, width, height, duration_ms, created_at`.
Unique `(asset_id, kind, profile, version)` → regeneration is a cache hit, not a duplicate.

No second queue, no second asset table, no duplicate checksum column.

### Queue jobs (all on the existing queue, queue name `media`)

| Job | Payload | Result |
|---|---|---|
| `media.import` | `{asset_id, staging_rel, source_path, options}` | `{asset_id, sha256, kind, probe_ok}` |
| `media.probe` | `{asset_id}` | summary + `probe_version` |
| `media.thumbnail.generate` | `{asset_id, size, timeline}` | derivative ids |
| `media.preview.generate` | `{asset_id}` | derivative id, verified duration |
| `media.proxy.generate` | `{asset_id, height, crf}` | derivative id |
| `media.transcode` | `{asset_id, profile, options}` | derivative id |
| `media.audio.extract` | `{asset_id, format}` | derivative id |
| `media.waveform.generate` | `{asset_id, buckets}` | derivative id |
| `media.recovery.scan` | `{older_than_minutes}` | cleaned counters |

Progress: the handler heartbeats and writes `result_json={"progress":0.42,"stage":"copying",...}`;
`GET /library/jobs` reads it. Cancel: `cancel_requested` is polled through a short-lived session;
the handler raises `JobCancelled(ValueError)` so the queue marks it `cancelled`, and partial
output is removed. A job is completed only after the output exists and was re-probed.

### Resource policy

- One intensive FFmpeg operation at a time (`media` queue, single worker slot, in-process lock
  that is also held across the direct (synchronous) API path).
- Copy/chunk size bounded (1 MiB), waveform buckets bounded (default 800) — no file is read
  into memory whole.
- Disk preflight: `free_bytes - size*1.25 > 1 GiB`, else `507 INSUFFICIENT_STORAGE`.
- Max import size (default 4 GiB) and max derivative count configurable.
- FFmpeg/ffprobe timeouts; kill on timeout; `CREATE_NO_WINDOW` so no console flashes.
- Media work never calls Ollama; it is independent of the AI governor but respects the same
  "one heavy operation" principle.

### Security

- No `shell=True`, argv arrays only, no user-supplied arguments reach FFmpeg.
- Import is only accepted from an HMAC-signed shell request (`x-creatoros-ipc`), so the renderer
  cannot ask the backend to read arbitrary paths; the shell only signs paths the user picked.
- Output paths always `resolve_under_root`; every served file is resolved and checked to be
  inside the media root, else 403.
- `<video>`/`<img>` URLs carry a short-lived HMAC access token (10 min) because HTML media
  elements cannot send an Authorization header. No path is accepted from the client.
- Filenames are sanitised for metadata display, never used as paths.
- Quarantine on: probe failure, no usable streams, size/duration over policy, path escape.
- Originals are never deleted automatically; deletion requires `confirm=<asset_id>` + audit.

## 3. API (new router `/library`, all authenticated)

```
GET    /library/stats                      totals, kinds, bytes, disk, job counters
GET    /library/assets                     filter: kind,state,q,sort,order; paged
GET    /library/assets/{id}                detail incl. probe summary + derivatives
GET    /library/assets/{id}/probe          stored probe document
GET    /library/assets/{id}/derivatives    all derivatives with state
GET    /library/assets/{id}/history        queue jobs for this asset (real events)
GET    /library/assets/{id}/thumbnail      bytes (signed URL) — primary/ timeline i
GET    /library/assets/{id}/poster         bytes (signed URL) — waveform/audio poster
GET    /library/assets/{id}/stream         bytes with Range (signed URL)
POST   /library/import                     shell-signed paths → jobs
POST   /library/assets/{id}/inspect        re-probe
POST   /library/assets/{id}/thumbnail      regenerate (force)
POST   /library/assets/{id}/preview        queue preview
POST   /library/assets/{id}/proxy          queue proxy (height, crf)
POST   /library/assets/{id}/transcode      queue profile
POST   /library/assets/{id}/audio          extract wav/aac
POST   /library/assets/{id}/waveform       generate waveform
DELETE /library/derivatives/{id}?confirm    remove one derived file
DELETE /library/assets/{id}?confirm=<id>   remove asset + files (never implicit)
GET    /library/jobs                       queue jobs of the media queue
GET    /library/jobs/{id}                  one job + progress
POST   /library/jobs/{id}/cancel           request cancellation
POST   /library/jobs/{id}/retry            requeue a dead-letter job
GET    /library/storage                    per-area usage + reclaimable
POST   /library/recovery/scan              staging sweep + integrity check
```

## 4. GUI (new section "Content library")

Seven pages, real data only: Library overview, Media grid (grid/list, search, sort, filters,
multi-select, bulk tag/rights), Import manager (native picker, folder, drag-drop, duplicate
dialog, progress, cancel, failures), Media detail (player, probe table, derivatives, jobs,
checksum), Processing center (queue, progress, cancel, retry), Transcode studio (profile picker,
options, start, monitor, compare original vs output, keep/discard), Storage (per-area usage,
free space, staging cleanup).

## 5. Agent integration

Four new tools (12 → 16), all deterministic and local:

| Tool | Grants | What it really does |
|---|---|---|
| `media.derivatives.list` | read-only (all agents) | lists real derivative rows for an asset |
| `media.scene.scan` | video_production, design_visual | runs FFmpeg scene detection, returns real cut timestamps |
| `media.transcode.recommend` | video_production, platform_adaptation | deterministic profile recommendation from probe facts |
| `media.quality.report` | quality_governance, video_production | runs blackdetect/freeze/volumedetect, returns measured issues |

Grants are applied once via `app_setting["media.tool_grants_v1"]` so a human who later removes a
grant is not overridden. `video_inspection_agent` may only claim visual inspection when a real
tool produced frame/scene data — enforced in its prompt and proven by an evidence ref in the task.

## 6. Testing

Fixtures are generated by FFmpeg at test start (tiny: 1–3 s, ≤ 400 KB each) into the isolated
test root: landscape mp4, portrait mp4 with audio, still png, still jpg, wav, mp3, a
rotated/odd-fps clip, a text file with `.mp4` extension (must be rejected by probe, not by name),
and a truncated mp4 (must fail cleanly).

Modules: `test_media_storage.py`, `test_media_probe.py`, `test_media_import.py`,
`test_media_derivatives.py` (thumbnails/waveform), `test_media_transcode.py`,
`test_media_queue.py` (idempotency, cancel, restart, dead letter), `test_media_api.py`,
`test_media_migration.py` (copy of the real DB), plus additions to `test_agent_tools.py`.

## 7. Delivery order

1. Settings/paths + storage service + probe engine + tests.
2. Migration (extend `media_asset`, add `media_derivative`) + copy-of-real-DB verification.
3. Import service + IPC signature + recovery scan.
4. Thumbnail/waveform/transcode services + queue handlers + progress/cancel.
5. `/library` API + signed media URLs + tests.
6. GUI (7 pages) + nav + typecheck/build.
7. Agent tools + grants + evidence contract.
8. Docs (9 files + ADR-19/20/21), evidence pack with measured numbers, full acceptance run, commit.

## 8. Risks

| Risk | Mitigation |
|---|---|
| ffmpeg 9.1 static build lacks something at runtime | probe features at startup (`-encoders`, `-filters`) and disable unsupported options with a stated reason |
| Long imports block the single worker thread | import runs in the worker, chunked, cancellable; API stays responsive (import returns immediately) |
| SQLite write contention between API and worker | short transactions, WAL already on, progress writes are single-row updates |
| Migration on the real DB | tested on a byte copy first, `foreign_key_check`, plus the Phase 4 dangling-FK test |
| Disk fills during transcode | preflight + post-check, partial output deleted on failure |
| NVENC believed available | actual encode test, cached, never assumed |
