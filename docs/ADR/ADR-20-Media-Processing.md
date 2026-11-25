# ADR-20 — Media processing pipeline and encoder selection

* Status: Accepted
* Date: 2026-11-22
* Phase: 5

## Context

Phase 5 runs FFmpeg and ffprobe on untrusted user files on a 4-core i5-8300H with a
GTX 1050 Ti Max-Q (4 GB, ~3.3 GiB usable, Pascal CC 6.1, driver 582.78). The machine is used
for AI inference at the same time. Three pressures shape the design:

* **Trust:** a media file is attacker-controlled input; a shell string or a raw path is a
  vulnerability.
* **Predictability:** the UI must show what is happening, including for a 20-minute encode, and
  must be able to stop it.
* **Honesty:** "GPU accelerated" is a claim that must be measurable.

## Decision

**A fixed set of versioned profiles, a single argv-only runner, and encoder selection by real
test encode.**

1. **Profiles, not free-form settings.** Five profiles (`fast_preview`, `editing_proxy`,
   `normalized`, `audio_wav`, `audio_aac`) with `PROFILE_VERSION` stored on every derivative.
   The UI cannot construct an arbitrary ffmpeg command; it can only name a profile and
   optionally request a smaller height.
2. **Profiles only downscale.** Scale filters use `min()` against the source dimensions and a
   fps cap that can only lower the rate. Upscaling a 360p clip to 1080p wastes time and
   produces a worse file; refusing it is a feature.
3. **argv arrays, never a shell.** `FfmpegRunner` builds a list, runs with `shell=False`,
   `CREATE_NO_WINDOW`, and a hard timeout; on cancel the child tree is killed.
4. **Verify the output, don't trust the exit code.** A produced derivative is re-probed
   (dimensions, duration within tolerance, non-zero size, container/mime) before its row
   becomes `ready`. `test_invalid_output_is_rejected_and_cleaned_up` corrupts a successful
   encode's output and asserts it is not accepted.
5. **One FFmpeg at a time.** `media_max_concurrent_ffmpeg = 1`, enforced by the media queue
   lane. A run-away transcode cannot starve the AI governor or the desktop.
6. **Encoder by measurement.** `choose_h264_encoder()` runs a 0.5 s test encode per candidate.
   On this machine `h264_nvenc` fails (API 13.0 vs 13.1), so `libx264` is used and
   `media_hardware_encoding` is reported `unavailable` everywhere — including the GUI, which
   shows `encoder_verification.h264_nvenc = {listed: true, verified: false}`.
7. **Thumbnails chosen by measurement.** Four candidate frames are scored with `signalstats`
   (mean luma + chroma variance); the best wins, and the chosen timestamp is stored in the
   derivative's metadata.
8. **Waveforms from decoded PCM.** Mono 8 kHz PCM streamed and folded into N buckets, rather
   than parsing `astats` for every frame. Bounded memory, real peaks, 137 ms for 20 s.

## Consequences

**Good**

* Every encode is reproducible from a profile id and two measured facts (source, height).
* No shell injection surface; no unbounded decode; no silent failure.
* The capability report cannot drift from reality, because it is produced by running the
  encoder.
* Progress, cancellation and timeouts behave identically for thumbnails, previews and
  transcodes.

**Costs**

* Five profiles will not cover every future need; adding one is a code change plus a version
  bump, not a user setting. That is deliberate: the agent and UI must not invent encoding
  parameters.
* Encoder verification costs ~70 ms once per process (cached an hour).
* Hardware encoding is unused on this machine; CPU encoding is 2.5×–10.6× realtime, which is
  acceptable for short-form video and is reported honestly rather than papered over.

## Verification

* `tests/test_media_probe.py` (36) — parsing, classification, rotation, HDR, still images.
* `tests/test_media_derivatives.py` (17) — no upscaling, black-frame avoidance, timeline,
  waveform measured against silence, failure recording.
* `tests/test_media_transcode.py` (18) — every profile verified, cache hits, cancellation,
  invalid output, profile applicability, source integrity.
* `scripts/bench_media.py` — the numbers quoted in [MEDIA_PROCESSING.md](../MEDIA_PROCESSING.md).
