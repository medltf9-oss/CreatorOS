# Content library API (Phase 5)

Base URL `http://127.0.0.1:<port>`, token from the desktop shell, `Authorization: Bearer
<token>` on every call. Errors use the project-wide envelope:

```json
{"code": "ASSET_NOT_FOUND", "message": "media asset … does not exist", "correlation_id": "01M…"}
```

Request bodies reject unknown fields (`extra="forbid"`); validation failures return
`422 {"code": "VALIDATION_ERROR", …}`.

## Inventory (verified against the running OpenAPI document)

25 paths, 28 operations:

| Method | Path | Purpose |
|---|---|---|
| GET | `/library/stats` | counts, bytes, disk, recent imports, measured capabilities |
| GET | `/library/storage` | per-area files/bytes + disk + policy note |
| GET | `/library/assets` | list with `kind`, `state`, `q`, `sort`, `order`, `limit`, `offset` |
| GET | `/library/assets/{id}` | full detail: probe, derivatives, jobs, project count |
| GET | `/library/assets/{id}/probe` | stored probe + deterministic profile recommendation |
| POST | `/library/assets/{id}/inspect` | re-probe the file on disk now |
| PATCH | `/library/assets/{id}` | tags, rights status, archive state |
| DELETE | `/library/assets/{id}?confirm=<id>[&force=true]` | delete original + derivatives |
| GET | `/library/assets/{id}/thumbnail` | signed URL only |
| GET | `/library/assets/{id}/poster` | signed URL only (waveform image for audio) |
| GET | `/library/assets/{id}/stream` | signed URL only, HTTP Range supported |
| GET | `/library/assets/{id}/derivative/{derivative_id}` | signed URL only |
| POST | `/library/import/preview` | classify paths as new / probable duplicate / rejected |
| POST | `/library/import` | queue import jobs (IPC-signed when the shell is present) |
| POST | `/library/assets/{id}/thumbnail` | queue thumbnail / timeline job |
| POST | `/library/assets/{id}/preview` | queue `fast_preview` |
| POST | `/library/assets/{id}/proxy` | queue `editing_proxy` |
| POST | `/library/assets/{id}/transcode` | queue a named video profile |
| POST | `/library/assets/{id}/audio` | queue audio extraction (`wav` / `aac`) |
| POST | `/library/assets/{id}/waveform` | queue waveform peaks + image |
| GET | `/library/profiles` | the five profiles with their real parameters |
| DELETE | `/library/derivatives/{id}?confirm=true` | remove one derivative, keep the original |
| GET | `/library/jobs` | media jobs with state filter and pagination |
| GET | `/library/jobs/{id}` | one job |
| POST | `/library/jobs/{id}/cancel` | request cancellation |
| POST | `/library/jobs/{id}/retry` | re-queue a failed/dead-letter job |
| POST | `/library/recovery/scan` | staging sweep, quarantine flags, missing files |
| GET | `/library/capabilities` | ffmpeg/ffprobe versions, verified encoder, policy, profiles |

## Signed media URLs

`<img>` and `<video>` cannot set an `Authorization` header, so the library returns signed
URLs instead of weakening authentication:

```
/library/assets/<id>/thumbnail?exp=1764000000&sig=<32 hex>
```

* HMAC-SHA256 over `asset_id:resource:extra:expires`, keyed by the per-launch session token.
* Binds the asset, the resource (`thumbnail`, `poster`, `stream`, `derivative`) and, for
  derivatives, the derivative id (`i=`).
* Ten-minute TTL. Expired, tampered or unsigned requests are `403` with `MEDIA_URL_*`.
* Never contains a filesystem path, so it cannot be turned into arbitrary file access.

`test_thumbnail_requires_a_signature`, `test_tampered_signature_is_rejected` and
`test_expired_url_is_rejected` pin these properties.

## Streaming with Range

`GET /library/assets/{id}/stream` returns the original bytes with `Accept-Ranges: bytes`. A
`Range: bytes=0-99` request returns `206` with `Content-Range`. The video player seeks without
copying the file, and `test_stream_serves_the_original_bytes` proves the served bytes hash to
the stored SHA-256.

## Examples

Import (from the desktop shell; `x-creatoros-ipc` is added by the main process):

```http
POST /library/import
{"paths":[{"path":"<DATA_ROOT>\\data\\media\\incoming\\clip.mp4","original_filename":"clip.mp4"}]}

202 Accepted
{"jobs":[{"job_id":"01M…","path":"…","original_filename":"clip.mp4","size_bytes":18350080}],
 "duplicates":[],"rejected":[]}
```

Transcode:

```http
POST /library/assets/01M…/transcode
{"profile":"editing_proxy","height":480}

200 OK
{"job_id":"01M…","job_type":"media.transcode"}
```

Stats (trimmed):

```json
{
  "assets_total": 3, "by_kind": {"video": 2, "audio": 1},
  "bytes_total": 41234567, "derivatives_total": 11,
  "jobs_active": 1, "jobs_failed": 0, "jobs_completed": 14,
  "disk": {"free_gb": 864.9, "total_gb": 931.5, "percent_used": 7.2},
  "capabilities": {
    "h264_encoder": {"name": "libx264", "hardware": false, "verified": true,
                     "note": "NVENC is unavailable on this driver; encoding runs on the CPU"},
    "encoder_verification": {"h264_nvenc": {"listed": true, "verified": false}}
  }
}
```

## Honest capability reporting

`GET /version` is the authoritative map:

```json
{"api": "phase-5", "version": "0.5.0",
 "capabilities": {"media_library": "available", "media_probe": "available",
                  "media_thumbnails": "available", "media_waveforms": "available",
                  "media_transcode": "available",
                  "media_hardware_encoding": "unavailable",
                  "media_agent_tools": "available",
                  "oauth": "not_implemented", "publishing": "not_implemented"}}
```

`media_hardware_encoding` is `unavailable` because a real test encode fails on this driver,
not because nobody checked.

## Tests

`tests/test_media_api.py` — 32 tests: auth, stats, filtering/sorting/paging, detail shape,
signed URLs (unsigned/tampered/expired), Range requests, original byte integrity, waveform
poster, IPC signature enforcement, signed import, duplicate detection, validation errors,
job listing/cancel/retry, 404s, derivative deletion gating, asset deletion confirmation and
in-use protection, metadata validation, re-probe, storage/recovery, capabilities and
`/version`.
