# Architecture Decision Record — index and measured evidence

Status values: **Accepted** (in force) · **Proposed** (needs your sign-off) · **Superseded**.

| ADR | Decision | Status |
|---|---|---|
| ADR-01 | Data root `<PROJECT_ROOT>`, configurable at first launch, resolved by `core/settings.py` | Accepted, **implemented** |
| ADR-02 | Keep Node 26.13.1 for build tooling (no LTS downgrade needed) | Accepted, **verified** — Vite 8.3.8, TS 7.0.2, Electron 45.4.1, typecheck + build clean |
| ADR-03 | Electron desktop shell (Tauri rejected) | Accepted, **implemented** |
| ADR-04 | SQLAlchemy 2 + Alembic + SQLite (WAL) | Accepted, **implemented + verified** (31 tables, reversible migrations) |
| ADR-05 | **Do not** redirect machine-wide `TEMP`/`TMP`; inject per child process | Accepted, **implemented** in `set_env.ps1` and `electron/main.cjs` |
| ADR-06 | Loopback-only API, random 32-byte session token per launch | Accepted, **implemented + tested** (401 without token) |
| ADR-07 | DPAPI token vault via Electron `safeStorage`; DB stores only a reference | Accepted, not implemented (Phase 6) |
| ADR-08 | SQLite-backed durable queue with leases/heartbeat/backoff | Accepted, **implemented + verified** (atomic claim, lease recovery, dead letter) |
| ADR-09 | Model library and per-role profiles | Accepted, **measured — see below** |
| ADR-10 | Facebook Pages only, Graph API, dev-mode private use first | Proposed (needs your Meta app decision) |
| ADR-11 | Instagram via Instagram Login (`instagram_business_*` scopes) first | Proposed |
| ADR-12 | TikTok manual export is the primary path; Direct Post is capability-gated | Proposed |
| ADR-13 | Public HTTPS privacy + data-deletion pages for Meta app review | Proposed (needs your hosting decision) |
| ADR-14 | v1 ships unsigned; SmartScreen warning documented | Accepted for v1 |
| ADR-15 | One Ollama gateway inside the backend; the renderer never speaks to the daemon | Accepted, **implemented + tested** (24 gateway contract tests) |
| ADR-16 | Admission control (governor) instead of unbounded concurrency | Accepted, **implemented + measured** (1 slot default, 429 on overload) |
| ADR-17 | One resident model per generation; measured context ceilings; explicit recorded fallback | Accepted, **implemented + measured** (switch retry, planner→specialist fallback observed live) |
| ADR-18 | One orchestration engine, three roles; DB is truth, catalogue is seed; typed hand-offs | Accepted, **implemented + verified** (111 agents, 5-task objective succeeded on this machine) |
| ADR-19 | Content-addressed media storage under `data/media/`, atomic promote, hash-only paths | Accepted, **implemented + verified** (real imports, duplicate = 299 ms, real-DB migration test) |
| ADR-20 | Versioned transcode profiles, argv-only FFmpeg, output verification, encoder chosen by test encode | Accepted, **implemented + measured** (libx264 2.5–10.6× realtime; nvenc rejected verbatim) |
| ADR-21 | Media work runs on the existing durable queue in a second lane (`media`), never a second queue | Accepted, **implemented + tested** (restart, cancel, dead letter, lane isolation) |
| ADR-22 | Credential vault lives in the Electron main process behind a scoped loopback broker; the backend holds no key, the renderer has no decrypt handle | Accepted, **implemented + tested** (18 vault/broker tests, reserve → seal → adopt → link) |
| ADR-23 | OAuth state is 32 random bytes, stored only as sha256, single use, consumed by a conditional UPDATE before the code exchange | Accepted, **implemented + tested** (replay loses the race) |
| ADR-24 | One canonical capability vocabulary of nine states; legacy strings normalized on write; unreachable capabilities reported `unsupported` rather than hidden | Accepted, **implemented + tested** (18 capability tests, real-API matrix) |
| ADR-25 | Publish destinations follow one 18-state machine with a single transition table; `partially_published` is a campaign rollup only | Accepted, **implemented + tested** (41 state tests, 6 migration tests) |
| ADR-26 | One path from approval to provider: `PublishExecutor` on its own queue lane (`publish`); nothing else may call a publisher | Accepted, **implemented + tested** (22 executor tests, publisher-import scan) |
| ADR-27 | Ambiguous outcomes become `unknown_remote_state` and are reconciled from evidence; no automatic resubmission | Accepted, **implemented + tested** (22 reconciliation tests, no-queue assertion) |
| ADR-28 | Instagram needs a configured S3-compatible bucket; without one it is `blocked: delivery_not_configured`, with no localhost workaround | Accepted, **implemented + tested** (27 delivery tests, 30 security tests) |
| ADR-29 | One analytics content model and three renderers (JSON/CSV/PDF); the digest is taken over the bytes written, and a format is offered only if a renderer exists | Accepted, **implemented + tested** (40 report/retention tests, 17 frontend tests) |
| ADR-30 | Analytics history expires, business history does not: a per-category retention policy with a reason per category, bounded batches, and `foreign_key_check` after every execution | Accepted, **implemented + tested** (preview/execute parity, capped batches, audit records) |
| ADR-31 | The version watch observes and warns; nothing in it changes the pinned provider version | Accepted, **implemented + tested** (staleness, env gate, no write path to the pin) |
| ADR-32 | Diagnostics report measured facts or an explicit gap — never a synthetic healthy state | Accepted, **implemented + tested** (12 diagnostics/version tests) |
| ADR-33 | Retention and version-check jobs share the existing `analytics` lane at lower priority; no fifth thread, no second queue | Accepted, **implemented + tested** (idempotent enqueue, `work_remaining` re-queue) |
| ADR-34 | Release packaging ships copies of signed binaries: the shell exe is a byte-identical copy of the vendor `electron.exe`, the backend is the PSF-signed interpreter plus a pure-Python payload, and the installer is per-user NSIS | Accepted, **implemented + verified** (17/17 install/uninstall checks; unsigned installer blocked by Smart App Control — documented with two remedies) |
| ADR-35 | Live provider verification is a staged, opt-in, twelve-step procedure whose publication step needs a typed phrase and which never approves anything | Accepted, **implemented + exercised** (20 tests through the real routes; first live run: 5 blocked, 4 skipped, 1 ok, exit 1) |
| ADR-36 | Production health is measured on the installed app, launched from the shortcut, with the page sweep failing on the app's own error surface and on unexpected data drift | Accepted, **implemented + exercised** (35/35 checks, 45/45 with upgrade and uninstall, re-measured 2026-11-25; sweep self-check proves it can fail) |
| ADR-37 | First-run setup ships a pinned prerequisite manifest, observes but never installs, and treats "unknown" as not a pass | Accepted, **implemented + tested** (setup API and wizard, manifest gate) |
| ADR-38 | A finished verification is recorded; `verified` requires a passing record that still matches the file; completion refuses failing required checks unless the override is explicit and audited | Accepted, **implemented + tested** (42 setup backend tests, 223 desktop tests) |

---

## Phase 8 — analytics decisions

Files: `ADR-29-Analytics-Report-Formats.md`, `ADR-30-Analytics-Retention.md`,
`ADR-31-Analytics-Version-Watch.md`, `ADR-32-Analytics-Diagnostics.md`,
`ADR-33-Analytics-Maintenance-Lane.md`. ADR-29 to ADR-33 were proposed in `docs/PHASE8_PLAN.md`
Appendix A and are recorded here with the implementation as built.

## Phase 9 — release packaging

File: `ADR-34-Release-Packaging.md`, with the build and install record in
`docs/PHASE9_RELEASE.md` and the evidence in `docs/evidence/P9.md`. ADR-34 carries a measured
correction from Phase 10: the packaged executable is byte-identical to the Electron npm binary, which
is itself unsigned, so signing it replaces nothing.

## Phase 10 — activation, verification, production health

Files: `ADR-35-Live-Verification-Protocol.md`, `ADR-36-Production-Health-Measurement.md`,
`ADR-37-First-Run-Setup-And-Pinned-Prerequisites.md`, `ADR-38-Setup-Verification-Truth-Model.md`. The signing
path, its limits and the Smart App Control behaviour are recorded in `docs/CODE_SIGNING.md` rather than
as a separate ADR, because the decision (sign when a certificate exists, never change a Windows
security setting) is a refinement of ADR-34.

---

## ADR-09 — Model selection and GPU verdict (measured on this laptop)

**Machine:** i5-8300H · 23.85 GB RAM · GTX 1050 Ti Max-Q 4 GB (Pascal, compute 6.1) · driver 582.78
**Ollama:** 0.40.17 · models on `<DATA_ROOT>\models\ollama`

### What actually happens with the GPU

Ollama 0.40.17 ships two CUDA libraries. The log is explicit:

```
skipping CUDA device — compute capability not in compiled architectures
  device="NVIDIA GeForce GTX 1050 Ti with Max-Q Design" cc=610 archs=[750 800 860 890 1000 1200]
  libDirs=[...\lib\ollama, ...\lib\ollama\cuda_v13]
...
inference compute id=0 library=CUDA compute=6.1 name=CUDA0
  description="NVIDIA GeForce GTX 1050 Ti with Max-Q Design" libdirs=ollama,cuda_v12
  total="4.0 GiB" available="3.3 GiB"
vram-based default context  default_num_ctx=4096
```

- **`cuda_v13` rejects Pascal.** `cuda_v12` accepts it, so GPU offload **works**.
- Default context is auto-set to 4096 from available VRAM. Keep it there by default.
- Vulkan enumerates only the Intel UHD 630 iGPU (dropped as integrated). Not useful.
- Ollama contacts `ollama.com` for cloud-model metadata; no content is sent. Set
  `OLLAMA_NO_CLOUD=1` if you want that chatter gone entirely.

### Benchmarks (single short prompt, `num_ctx=4096`, warm model)

| Model | Tokens/sec | Placement | Notes |
|---|---:|---|---|
| `qwen3:0.6b` | **75.8** | 100% GPU (930 MB) | Excellent router/classifier |
| `qwen3:1.7b` | **45.4** | 100% GPU (1.71 GB) | **Default worker** — fast enough for interactive drafts |
| `qwen3:4b` | **15.5** | 69% GPU (2.43 GB VRAM of 3.53 GB total) | Usable for short structured output; slow for long generations |
| `qwen2.5-coder:3b`, `gemma3:4b`, `nomic-embed-text` | not benchmarked yet | — | Phase 3 added the benchmark harness (`POST /ai/models/{tag}/benchmark`); run it in Model Lab to record values |

> Phase 3 status: the gateway is implemented in `app/backend/src/creatoros/integrations/ollama/`
> and the governor in `app/backend/src/creatoros/services/governor.py`. See
> [ADR-15](ADR-15-Ollama-Gateway.md) and [ADR-16](ADR-16-Resource-Governor.md).
> The Phase 3 brief labelled these "ADR-10" and "ADR-11"; those numbers already belong
> to the platform decisions above, so they were filed as 15/16.

### Decisions taken from the numbers

1. `fast_worker` → `qwen3:1.7b` (default for most agent tasks, 100% GPU).
2. `reasoning_primary` → `qwen3:4b` at `num_ctx=4096`, accepting partial offload
   (~15 tok/s). Use for planning only, never for bulk generation.
3. `router_worker` → `qwen3:0.6b` for classification/extraction where deterministic code
   is not possible.
4. `vision_worker` → `gemma3:4b`, sampled frames only (max 4 frames, ≤768 px).
5. Concurrency stays at **1 generation**. Phase 3 implemented the governor (ADR-11) and
   kept that default: VRAM headroom is ~0.9 GB after a 1.7B model, so two concurrent
   1.7B models do not fit. The limit is user-editable, never auto-raised.
6. Do **not** plan work around a 4B model at full speed. The laptop's realistic
   interactive ceiling is the 1.7B model.

**Implication for the plan:** risk R1 ("GTX 1050 Ti may be CPU-only") is **closed** — it
is GPU-capable, but only for models ≤ ~2 GB and only at 4K context. The plan's
conservative assumptions stand.
