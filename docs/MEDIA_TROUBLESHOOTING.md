# Media troubleshooting

Every entry below was hit while building Phase 5, with the exact symptom and the fix that
worked. Nothing here is hypothetical.

## `no such table: main.agent_task_p3`

**Where:** Phase 4 migration on the real database.
**Cause:** a table rebuild renamed `agent_task`, and child foreign keys were rewritten to
point at the scratch name.
**Fix:** migration `b7f1c02d4e61` rebuilds any table whose SQL mentions `agent_task_p3`, and
`test_migrated_schema_has_no_dangling_foreign_keys` guards it.

## Migration works on a fresh DB, fails on the real one

Always test against a copy of the real database. `tests/test_media_migration.py` copies
`<DATA_ROOT>\data\database\creatoros.db`, upgrades it, and checks `integrity_check`,
`foreign_key_check`, dangling references, row preservation (111 agents), CHECK enforcement and
downgrade. A fresh in-memory schema proves nothing about a shipped file.

## FFmpeg produced 0 frames with a scale filter

**Symptom:** exit 0, output with no video frames.
**Cause:** FFmpeg 9.1 mishandles `scale=w=min(1280,iw):h=min(720,ih):force_original_aspect_ratio=decrease`
as a single expression.
**Fix:** two-step chain — cap width and height with `force_original_aspect_ratio=decrease`,
then a second `scale` to trim to even dimensions for yuv420p. Pinned by
`test_landscape_thumbnail_keeps_aspect_ratio_and_does_not_upscale`.

## `Could not open encoder before EOF` (h264_nvenc)

**Symptom:** every NVENC encode fails with return code `4294967256`.
**Cause:** FFmpeg 9.1 requires NVENC API 13.1; driver 582.78 exposes 13.0.
**Fix:** none available on this machine — `choose_h264_encoder()` verifies with a real test
encode and falls back to `libx264`, reporting `media_hardware_encoding = unavailable`. Do not
"fix" this by trusting `ffmpeg -encoders`: the encoder is listed and still unusable.

## `database is locked` in tests

**Symptom:** `sqlite3.OperationalError` while a worker claims a job.
**Cause:** the test session held an open read transaction while the worker wrote on another
connection.
**Fix:** `session.commit()` (and `expire_all()`) before handing work to the worker. Production
is unaffected because the API commits per request.

## Media URLs 403 in the browser but fine in tests

**Symptom:** `<img>` shows nothing, `MEDIA_URL_UNSIGNED`.
**Cause:** the URL was rebuilt by hand instead of using the one the API returned.
**Fix:** always use `thumbnail_url` / `poster_url` / `stream_url` / `derivatives[].urls.file`.
Signatures bind asset id, resource, derivative id and expiry; any edit invalidates them.

## Import returns `403 IPC_SIGNATURE_REQUIRED`

**Cause:** the backend has `CREATOROS_IPC_SECRET` set (it does when launched by the desktop
shell) and the request did not come through the Electron main process.
**Fix:** use the Import manager page, or call the import from a process that can sign. This is
the control working as designed; do not remove the secret to make it go away.

## A derivative stays `running` after a crash

**Fix:** run `POST /library/recovery/scan` (Storage page → *Run scan*). Rows whose file is
missing become `failed` / `DERIVATIVE_MISSING`; staging leftovers are swept. Then retry the
job from the Processing centre.

## Waveform is flat

Not a bug if the audio is silent. The tests assert that silence measures `< 0.01` while a
440 Hz tone measures ~0.118, so a flat waveform on a real tone would be a genuine defect —
check that the asset reports `has_audio` and that `audio.streams` shows a codec.

## Thumbnail is dark

The picker scores four candidate frames with `signalstats`; a very dark clip can still be dark
everywhere. Check `meta_json.frame_ms` on the derivative: the chosen frame is recorded. Force
regeneration with a different size or on a brighter source rather than assuming the picker
failed.

## Disk fills up

`require_space` blocks new copies and encodes before they start, but derivatives accumulated
from many exports still cost space. Use the Storage page to see area totals; delete
derivatives from the asset detail page (originals are preserved). The minimum-free threshold
is `media_min_free_gb` (1 GB default, `MEDIA_MIN_FREE_GB`).

## Ollama and FFmpeg together

The AI governor limits generations to 1, and the media queue runs exactly one FFmpeg at a
time. If an objective is running and a transcode starts, both are still serialised on the CPU;
expect both to be slower and do not raise concurrency on this machine.
