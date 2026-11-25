# Media library (Phase 5)

The library is the single source of truth for every file CreatorOS owns. It imports,
identifies, probes, derives and serves media — and it never guesses.

* Everything lives under `<DATA_ROOT>\data\media\`.
* A file's path is derived from its **content hash**, never from its filename.
* Every long operation is a job on the Phase 2 durable queue (queue name `media`).
* Every derivative row is backed by a file that exists, or it is marked failed.
* The original is never modified, moved or deleted by processing.

This is built on the Phase 2 `media_asset` table (extended, not replaced) and the Phase 2
queue (a second named queue, not a second queue implementation).

## Storage layout

```
data/media/
├── originals/     <sha[0:2]>/<sha256><ext>      imported source, never auto-deleted
├── thumbnails/    <sha[0:2]>/…-thumbnail-480px-v2.webp
│                  <sha[0:2]>/…-timeline-t00-v2.webp
├── previews/      <sha[0:2]>/…-fast_preview.mp4     ≤720p H.264 for scrubbing
├── proxies/       <sha[0:2]>/…-editing_proxy-480p.mp4
├── transcodes/    <sha[0:2]>/…-normalized.mp4
├── audio/         <sha[0:2]>/…-audio_wav.wav, …-audio_aac.m4a
├── waveforms/     <sha[0:2]>/…-400b-v2.json, …-waveform.png
├── staging/       in-flight copies and encodes (swept on recovery)
└── quarantine/    files that failed validation (kept, never served)
```

Each area maps to a `PathKind` in `core/settings.py`, so a directory cannot be created in
the wrong place and a component cannot invent a path. Sharding by the first two hash
characters keeps directories small on NTFS.

### Why hash paths

* Two files with the same name never collide.
* The same file imported twice resolves to the same path, so dedupe is a lookup, not a guess.
* A filename cannot be used to escape the root, because the filename is not part of the path.
* Paths stay stable across renames in the UI, which only changes metadata.

`MediaStorage.resolve_stored()` re-checks containment (`is_inside_root`) before any read, so
even a corrupted database row cannot make the process read outside the media root.

## Atomic import pipeline

`MediaLibrary.import_file()`:

1. **Validate the source** — must exist, be a regular file, not be a symlink, be inside the
   CreatorOS root, and be below `media_max_import_bytes` (4 GiB).
2. **Check space** — `require_space(size)` refuses before copying if free space would drop
   below `media_min_free_gb` (1 GB) plus headroom.
3. **Hash first** — SHA-256 of the source, streamed in 1 MiB chunks. This makes dedupe and
   the destination path available before anything is written.
4. **Dedupe** — an existing asset with the same hash is returned as `duplicate`; nothing is
   copied. A same-name/same-size asset is reported as `probable_duplicate` by the API
   preview so the UI can ask instead of silently importing.
5. **Copy to staging** — 1 MiB chunks, hashing the bytes that are actually written and
   aborting if the digest differs from the source digest (detects a file changing mid-copy).
6. **Promote atomically** — `os.replace()` into `originals/`, which is atomic on NTFS within
   a volume. A crash before the rename leaves only a staging file; a crash after it leaves a
   complete file.
7. **Probe** — ffprobe, strict parsing, stored as `probe_json` with `probe_version`.
8. **Register** — insert the `media_asset` row with real measured facts and
   `intake_state=ready`.
9. **Derivatives** — thumbnail and waveform are generated when applicable; a failure here is
   recorded on the derivative row and does **not** fail the import.

The renderer cannot trigger an import. Paths come from the Electron main process
(`creatoros:pickMedia`) and the request is HMAC-signed (`api/ipc_auth.py`); see
[MEDIA_SECURITY.md](MEDIA_SECURITY.md).

## Idempotency and crash recovery

* Import jobs are enqueued with `idempotency_key = media:import:<sha256-prefix>`, so a retry
  after a crash re-queues nothing that is already open.
* `POST /library/recovery/scan` (and the startup sweep) removes staging files older than a
  threshold, quarantines assets stuck in a non-terminal `intake_state`, and marks derivative
  rows whose file vanished as `FAILED` with `DERIVATIVE_MISSING`.
* Quarantine is a real state: a rejected file is preserved under `quarantine/` and its row
  records `quarantine_reason`, because silently deleting user media is not acceptable.

## Measured on this machine

| Operation | Source | Time | Result |
|---|---|---|---|
| ffprobe | 20 s 1080p MP4 | 73 ms (median of 5) | kind, streams, rotation, HDR flags |
| Import | 17.5 MB, 20 s 1080p | 519 ms | copied, hashed, probed, registered |
| Duplicate import | same file | 299 ms | SHA-256 hit, no copy |
| Thumbnail (480 px, 4-frame scoring) | 1080p | 1.25 s | 480×270 WEBP |
| Timeline (8 frames) | 1080p | 2.60 s | 8 WEBP frames |
| Waveform (400 buckets) | 1080p + AAC | 137 ms | 1 385 bytes of peaks |

Reproduce with `python scripts/bench_media.py` (a real file, a real encode, real timings).

## Files

| Path | Responsibility |
|---|---|
| `services/media/storage.py` | paths, streaming copy, atomic promote, space guard, sweep |
| `services/media/probe.py` | ffprobe + strict parsing + classification |
| `services/media/library.py` | import, derivatives, transcode, delete, capabilities |
| `services/media/jobs.py` | queue handlers, progress, cancellation, recovery |
| `services/media/profiles.py` | the five transcode profiles |
| `services/media/ffmpeg.py` | argv-only runner, progress parsing, encoder verification |
| `services/media/agent_grants.py` | one-time media-tool grants for Phase 4 agents |
| `api/routes/library.py` | 25 authenticated paths under `/library` |

## Related documents

* [MEDIA_PROCESSING.md](MEDIA_PROCESSING.md) — probe, thumbnails, waveforms, transcodes
* [MEDIA_JOBS.md](MEDIA_JOBS.md) — job types, retries, cancellation, recovery
* [MEDIA_API.md](MEDIA_API.md) — every endpoint
* [MEDIA_SECURITY.md](MEDIA_SECURITY.md) — the sandbox around untrusted files
* [CONTENT_LIBRARY_GUI.md](CONTENT_LIBRARY_GUI.md) — the seven pages
* [ADR-19](ADR/ADR-19-Media-Storage-Layout.md), [ADR-20](ADR/ADR-20-Media-Processing.md),
  [ADR-21](ADR/ADR-21-Media-Job-Pipeline.md)
