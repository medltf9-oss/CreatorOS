# Media processing (Phase 5)

Everything here is done by FFmpeg 9.1 and ffprobe on this machine, with argv arrays (never a
shell string), a hard timeout, a kill-on-cancel path, and a verification step that reads the
produced file back.

## Probing

`services/media/probe.py` runs ffprobe with `-print_format json` and parses it strictly.

| Fact | Source | Notes |
|---|---|---|
| `kind` | streams, not extension | any video stream → `video`; audio only → `audio`; image codec → `image` |
| `duration_ms` | container, then stream fallback | `None` for still images — a still has no duration, and inventing 0 is a lie |
| `width/height` | video stream + rotation | rotation 90/270 swaps display dimensions |
| `fps` | `avg_frame_rate`, then `r_frame_rate` | VFR files report the average |
| `rotation` | side data, then tags | 0/90/180/270 |
| `is_hdr` | transfer/primaries | PQ or HLG, or BT.2020 primaries |
| `container` | `format_name` + brand | MOV vs MP4 is decided by `ftyp` brand, not by the `.mov` extension |
| `mime_type` | container + brand | used for `Content-Type` when serving |

Parsers are unit-tested against real ffprobe output **and** against hostile strings
(`"N/A"`, `"0/0"`, empty, non-numeric). A parse failure degrades one field, never the file.

`PROBE_VERSION` (`p5.1`) is stored per asset: when the parser improves, rows can be
re-probed and compared, and a stale probe is visible rather than invisible.

## Thumbnails

`ensure_thumbnail()` avoids the classic "black first frame" bug by **measuring** four
candidates instead of hoping:

1. Candidate positions are 8 %, 25 %, 50 % and 75 % of the duration.
2. Each candidate is scored with FFmpeg `signalstats`: mean luma plus chroma variance.
3. The highest-scoring frame wins; ties go to the earlier frame.
4. If no candidate scores (a genuinely dark clip), the midpoint is used and the reason is
   recorded in the derivative's `meta_json`.

Other rules:

* **Never upscale.** The scale filter is `min(size, iw)` / `min(size, ih)` with
  `force_original_aspect_ratio=decrease`. A 360p source produces a 360p thumbnail.
* **Two-pass scale chain.** FFmpeg 9.1 silently produced zero frames when `min()` was
  combined with `force_original_aspect_ratio` in one expression; the chain is therefore split.
* Output is WEBP (`libwebp` verified present) at quality 76, or JPEG when WEBP is missing.
* Timeline frames use the same pipeline at 240 px, 10 by default (max 40), one derivative row
  per frame with `position_ms` in its metadata.
* The produced image is re-read to confirm it is a real image with non-zero dimensions.

## Waveforms

`ensure_waveform()` streams decoded audio to **mono 8 kHz signed 16-bit PCM** and folds
min/max into N buckets (400 default). This is bounded — about 16 KB of PCM per second of
audio — instead of decoding the whole file into memory or parsing hundreds of thousands of
`astats` lines.

The resulting JSON contains `buckets`, `samples`, `sample_rate`, `duration_ms` and
`peaks[]`, with `peaks[i] = max(|min|, |max|) / 32768` clamped to `0..1`. A 440 Hz tone
measures ≈0.118 at the fixture's amplitude; silence measures 0.0. That difference is asserted
in the tests, so the waveform cannot become a decorative constant.

`ensure_waveform_image()` renders a PNG with `showwavespic` for players that should not draw
a canvas — audio assets get a real poster instead of a grey box.

## Transcode profiles

Five profiles, versioned (`PROFILE_VERSION = 2`), defined in `services/media/profiles.py`:

| id | Container | Video | Audio | Purpose |
|---|---|---|---|---|
| `fast_preview` | MP4 | H.264 CRF 23, ≤1280×720 | AAC 128k | scrubbing and review |
| `editing_proxy` | MP4 | H.264 CRF 26, ≤480p | AAC 96k | long or heavy sources |
| `normalized` | MP4 | H.264 CRF 20, ≤1080p | AAC 192k | delivery copy |
| `audio_wav` | WAV | — | PCM 16-bit | full-quality track for editing |
| `audio_aac` | M4A | — | AAC 192k | compact review track |

Invariants:

* Profiles only ever **downscale**; a 360p source stays 360p even if 1080p is requested.
* The fps cap only **lowers**; it never duplicates frames to reach a target.
* `audio_*` on a silent asset and video profiles on audio assets raise
  `ProfileNotApplicable` (HTTP 409), rather than producing a broken file.
* The derivative is written to `staging/`, verified (duration within tolerance, non-zero
  dimensions, non-zero size, correct container), and only then promoted.
* Derivative identity is `(asset_id, kind, profile, version)`, unique in the database, so a
  repeat request is a cache hit (`first.id == second.id` in the tests).

## Encoder selection, measured not assumed

`choose_h264_encoder()` runs a real 0.5 s test encode with `h264_nvenc`. On this machine that
**fails**, verbatim:

```
h264_nvenc: FAILED (returncode=4294967256)
[vost#0:0/h264_nvenc] [enc:h264_nvenc] Could not open encoder before EOF
[vost#0:0/h264_nvenc] Task finished with error: Invalid argument
```

Driver 582.78 provides NVENC API 13.0; FFmpeg 9.1 requires 13.1. The library therefore
reports `h264_encoder = libx264` and `encoder_verification.h264_nvenc = {listed: true,
verified: false}`, and the GUI says so. Advertising GPU encoding because the encoder appears
in `-encoders` would have been a lie. The decision is cached for an hour per process and
logged at INFO.

## Real timings (this machine)

20 s 1080p30 H.264 source, `libx264`, `veryfast`, CPU-only:

| Profile | Wall time | Speed | Output |
|---|---|---|---|
| `fast_preview` | 2.68 s | 7.5× realtime | 1280×720, 3.3 MB |
| `editing_proxy` | 1.88 s | 10.6× realtime | 854×480, 916 KB |
| `normalized` | 7.99 s | 2.5× realtime | 1920×1080, 16.2 MB |
| `audio_wav` | 0.19 s | 106× realtime | 3.4 MB |
| `audio_aac` | 0.51 s | 39× realtime | 463 KB |

Generated by `python scripts/bench_media.py`, which deletes each derivative before timing so
every number is a real encode, not a cache hit.

## Progress, timeout and cancellation

* `-progress pipe:1 -nostats` is parsed in the runner; `out_time_us` / `out_time_ms` are turned
  into a fraction of the known duration and pushed to the job row (throttled, on a separate
  session so a long encode never holds a read transaction).
* Hard timeout per operation: thumbnails 60 s, transcodes 3 600 s, probe 30 s, scene scan 30 s.
* `should_cancel` is polled between reads; on cancel the child process tree is killed
  (`taskkill /T` on Windows) and the derivative row returns to `QUEUED`, so a cancel is not a
  failure and does not burn a retry.
* Concurrency is 1 (`media_max_concurrent_ffmpeg`): one FFmpeg at a time on a 4-core,
  4 GB-VRAM laptop. The UI never presents a second "start" as free capacity.

## Quality checks that are arithmetic, not opinion

`media.quality.report` (agent tool) checks an asset against published platform limits:
duration, file size, minimum short edge, 9:16 band, audio presence, rights status. It returns
`issues` (blocking), `warnings`, and a verdict — no model is involved, and the same input
always produces the same verdict.
