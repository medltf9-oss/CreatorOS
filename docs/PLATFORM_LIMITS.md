# Platform limits

Every rule below carries the documentation URL it came from, in
`integrations/social/publishing/metadata/limits.py`. `GET /publishing/limits?platform=&mode=` returns
the same table as data.

`null` means **the provider publishes no limit** (or no limit this project is willing to invent). A
wrong local cap silently blocks valid content; a missing one lets the provider's own error surface,
which the executor records. That trade was made deliberately in favour of `null`.

## Facebook Page video

| Item | Value |
| --- | --- |
| Caption | no documented cap (`null`) |
| Containers | mp4/mov family |
| Video codec | H.264 |
| Audio | AAC, optional |
| Duration | provider-defined; no local cap |
| Size | up to 4 GB, uploaded in 4 MiB chunks |
| Public URL needed | no (direct chunked upload) |

## Instagram Reels

| Item | Value |
| --- | --- |
| Caption | 2 200 characters, hashtags included |
| Aspect | 9:16 preferred (0.5625), tolerance documented |
| Duration | 3 s – 15 min, warning above 90 s |
| Size | ≤ 1 GB |
| Public URL needed | **yes** — this is why a delivery bucket is required (ADR-28) |

## TikTok Direct Post / Inbox

| Item | Value |
| --- | --- |
| Caption | 2 200 characters |
| Duration | 3 s – 10 min |
| Size | ≤ 4 GB; one chunk ≤ 64 MB, otherwise 10 MB chunks with the last absorbing the remainder |
| Privacy | per-creator options, read from `creator_info` before every direct post |
| Public URL needed | no (chunked upload to a provider-issued URL) |

## What the validators return

`validate_variant()` returns a `ValidationReport` with an outcome (`passed`, `warning`, `failed`,
`unsupported`, `requires_attention`) and typed findings, each with `code`, `severity`, `field` and a
message a person can act on. Rotation is honoured (a portrait video tagged 90° is validated as
portrait), and the ffprobe document is parsed rather than trusted blindly.

The composer stores the report on the variant and on the payload the approval hashes, so what a person
approved includes the verdict of the validators that ran.
