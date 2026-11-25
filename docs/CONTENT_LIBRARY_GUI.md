# Content Library GUI (Phase 5)

Seven pages in the existing CreatorOS Studio shell, under the sidebar group
**Content library**. Every value comes from `/library/*`; there is no mock data, and an action
that cannot work is disabled with a stated reason.

| Page | Route id | What it does |
|---|---|---|
| Overview | `lib-overview` | counts, bytes, disk, recent imports, measured capabilities, storage areas |
| Media grid | `lib-grid` | paginated grid, kind filter, sort, search, signed thumbnails |
| Import manager | `lib-import` | native picker, duplicate preview, signed import, live job mirror |
| Media detail | `lib-detail` | player, facts, probe, derivatives, actions, tags/rights, delete |
| Processing centre | `lib-processing` | every media job, progress, cancel/retry, payload + error |
| Transcode studio | `lib-transcode` | pick source + profile, height override, encoder capability |
| Storage | `lib-storage` | per-area footprint, recovery scan, original-preservation policy |

## Overview

* Four headline cards: assets (with kind breakdown), derivatives (by kind), jobs
  (active/completed/failed), disk (free/total/used).
* Recent imports rendered with real signed thumbnails; clicking opens the detail page.
* "Measured capabilities" shows `libx264` and states that hardware encoding is unavailable on
  this driver — sourced from `encoder_verification`, not from a hardcoded claim.
* Storage areas list files and bytes per purpose directory.
* Refreshes every 15 s; a failed poll shows the error envelope instead of stale numbers.

## Media grid

* Filters: kind (all/video/audio/image), sort (newest/name/size/duration), direction, free-text
  search over filename and tags.
* Page size 12/24/48/96 with prev/next; "showing N of M" always from the server total.
* Cards show thumbnail (or waveform poster for audio), duration, dimensions, size, age, intake
  state, rights status and up to two tags.
* Lazy `loading="lazy"` images; a missing thumbnail shows the kind instead of a broken icon.

## Import manager

* **Choose files** opens the OS picker in the Electron main process. Files are filtered to
  regular, non-symlink files before the renderer sees them.
* A **dry run** (`POST /library/import/preview`) classifies each file as new, probable
  duplicate (same name and size, with the existing asset id) or rejected (with the reason).
  The import button counts only the new files and is disabled when there are none.
* The real import is signed in main (`x-creatoros-ipc`) and posted by main, so the renderer
  never handles a path with authority.
* Results are split into queued jobs, duplicates (with *open* buttons) and rejections with
  the backend's reason verbatim.
* The recent-jobs panel mirrors the media queue every 4 s including progress and stage.

## Media detail

* `video` element with a signed `src`, `preload="metadata"` and the thumbnail as poster;
  `audio` element with the waveform image; images show the thumbnail.
* Facts panel: size, duration, resolution, fps, audio, bitrate, MIME, import/processed time,
  project count, plus a *re-probe* action.
* Storage panel: the actual stored path and SHA-256, and a quarantine reason when present.
* Four tabs:
  * **Overview** — thumbnails (with per-frame dimensions), generate thumbnail, timeline of 8
    frames, and processing actions (preview, proxy, audio WAV/AAC, waveform). Image assets
    hide the transcode actions and explain why.
  * **Derivatives** — table of every derivative: kind, profile, state, size, dimensions,
    version, plus open/job/delete per row and the asset's recent jobs.
  * **Technical** — parsed probe fields (container, codecs, profile/level, pixel format, bit
    depth, colour range, HDR, rotation, audio layout) and the raw probe JSON in a collapsible.
  * **Metadata** — editable tags, rights status buttons, archive state, rule-based profile
    recommendation, and the available video profiles.
* Danger zone: delete requires a browser confirmation, sends `?confirm=<asset id>`, and a
  separate *force* path for assets referenced by projects.

## Processing centre

* State filter (all/queued/running/succeeded/failed/dead letter/cancelled), refreshed every 3 s.
* Each row: job type, state badge, stage or error, age, progress bar, attempts/max attempts.
* Selecting a job shows its id, payload result JSON, verbatim `last_error`, and contextual
  actions: cancel while queued/running, retry when failed/dead-letter/cancelled, open asset.
* Arriving from a derivative row highlights that job (`focusJobId`).

## Transcode studio

* Source list from the library (newest first) with facts for the selected asset.
* All five profiles with their purpose; the selected profile shows container, codecs, CRF,
  preset and fps cap.
* Height override is numeric-only, disabled for audio profiles, and documented as
  downscale-only.
* The queue button is disabled with an explanation when the profile does not apply to the
  asset (e.g. a video profile on an audio file).
* Size estimate is explicitly labelled an estimate derived from the profile's bitrate budget.
* Encoder capability panel lists `libx264` (works, CPU) and `h264_nvenc` (listed by the build,
  rejected by a real encode) with the measured detail string.

## Storage

* Disk card with free/total and the minimum-free policy.
* Footprint card: originals vs derivatives vs total, with the "originals are never
  auto-deleted" policy stated.
* Area table with files, bytes and the purpose of each directory.
* Recovery scan with a minimum-age input (so an active import is not disturbed) and the
  returned counters: staging removed, bytes freed, assets quarantined, derivatives missing.
* Largest-assets list for quick review, each opening the detail page.

## Honesty rules applied in the UI

* No "Published" anywhere; nothing in Phase 5 publishes.
* Hardware encoding is shown as unavailable, with the measured reason.
* Estimates are labelled estimates; measured values are labelled measured.
* Buttons that cannot act are disabled and explain why (`bridgeAvailable`,
  `mismatch`, `newCount === 0`).
* Every error path renders the backend's `code: message` envelope, including the correlation
  id when present, instead of a generic failure toast.

## Build facts

`pnpm typecheck` clean, `pnpm build` clean:

```
dist/assets/index-*.js   377.11 kB │ gzip: 104.05 kB
dist/assets/index-*.css   32.22 kB │ gzip:   6.57 kB
```

Eleven new modules: `src/apiLibrary.ts`, seven pages under `src/pages/library/`, and the
existing shell extended with nav, typing and the Electron picker/import bridge.
