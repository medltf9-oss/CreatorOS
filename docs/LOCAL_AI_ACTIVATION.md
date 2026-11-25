# Local AI activation — measured on this machine

Everything here was measured on the installed application (`%LOCALAPPDATA%\Programs\CreatorOS`)
talking to the local Ollama daemon. Nothing was simulated: each row is a real HTTP call through the
product's own routes, and the numbers come from the responses.

Activation date: **2026-11-24**. Baseline commit: `33bc269`.

## 1. What was broken before activation

The product was reporting zero installed models while 9.25 GB of model weights sat on D:.

| Check | Before | After |
| --- | --- | --- |
| `ollama list` | empty | 6 models |
| `GET /api/tags` | `{"models":[]}` | 6 models |
| `GET /ai/health` (`ollama.online`) | `false` in the products's AI pages | `true`, latency 2.3 ms |
| Model directory on D: | 9.25 GB, 31 blobs, 6 manifests | unchanged (nothing was downloaded or moved) |

### Root cause

The Windows Ollama *tray app* keeps its own `models` path in
`%LOCALAPPDATA%\Ollama\db.sqlite` (table `settings`, column `models`). That stored value wins over the
`OLLAMA_MODELS` environment variable, and it still held `D:\NEXUS_DATA\models` from an earlier
installation — a directory that exists but contains no weights. Its server log line was decisive:

```
OLLAMA_MODELS:D:\NEXUS_DATA\models
```

So the daemon was healthy and honest: it served the (empty) directory it had been told to serve.

### Fix

1. `scripts/start_ollama.ps1` now **verifies what the daemon can actually see** instead of just starting it.
   If models are missing it stops the app-managed daemon and runs `ollama serve` with `OLLAMA_MODELS` set
   for the server process (which wins over the stored setting), then re-checks and reports. `-Status`
   prints the daemon version, the on-disk model size and the visible tags.
2. `scripts/fix_ollama_path.ps1` fixes the durable setting the tray app uses at login. It stops the app,
   copies `db.sqlite` to `db.sqlite.creatoros-backup-<timestamp>`, updates the one column, restarts the
   app, and reports. Without `-Apply` it only reports. Both scripts are idempotent and exit 0 when
   there is nothing to do.

Neither script downloads anything, moves model files, or changes a Windows setting. The model weights
stay at `<DATA_ROOT>\models\ollama`.

## 2. Profiles and models

All six default profile tags are installed; **nothing needed to be downloaded.**

| Profile | Tag | Size on disk | Role |
| --- | --- | --- | --- |
| `router_light` | `qwen3:0.6b` | 0.52 GB | routing, classification, extraction |
| `specialist` | `qwen3:1.7b` | 1.4 GB | captions, hashtags, drafting, default worker |
| `planner` | `qwen3:4b` | 2.5 GB | planning, reasoning, critique |
| `coding` | `qwen2.5-coder:3b` | 1.9 GB | code, tests, debugging |
| `vision` | `gemma3:4b` | 3.3 GB | image and frame understanding |
| `embedding` | `nomic-embed-text:latest` | 0.27 GB | embeddings |

## 3. Inference, streaming, cancellation, governor

| What was verified | How | Result |
| --- | --- | --- |
| Real generation through the product | `POST /ai/generate`, profile `specialist` | routed to `qwen3:1.7b`, sensible answer, 39.05 tok/s |
| Direct daemon throughput | `POST /api/generate`, `qwen3:1.7b`, 60-token budget | **37.4 tok/s**, 4.1 s wall |
| GPU placement | `GET /api/ps` | `size_vram 1.71 GB / size 1.71 GB` → **100 % GPU** |
| Streaming | `POST /ai/stream` | `token` events then a `meta` event with the model, timing and token counts |
| Cancellation | `POST /ai/executions/{id}/cancel` during a stream | `200 {"cancelled_in_flight": true}`, stream closed with a `cancelled` event carrying the partial text |
| Governor limits | `GET /ai/limits` | `max_concurrent_generations 1`, `max_ram_percent 85`, `min_free_ram_gb 2`, `max_vram_percent 92`, `max_queue_depth 25`, `generation_timeout_s 300`, `allow_concurrent_models false` |
| Health | `GET /ai/health` | `ollama.online true`, version 0.40.17, latency 2.3 ms |

### The GPU caveat, stated exactly

Ollama's log says:

```
skipping CUDA device NVIDIA GeForce GTX 1050 Ti with Max-Q Design: compute capability 6.1 not in
compiled architectures [750 800 860 890 1000 1200]; OLLAMA_VULKAN:true
```

This build has no Pascal CUDA kernel, so it reaches the GPU through Vulkan instead. Placement is still
100 % in VRAM and throughput is consistent with it. **No CUDA or driver configuration was changed** —
the working setup was left exactly as found. `qwen3:4b` is the model that spills
(~69 % placement), which is why it is reserved for planning.

## 4. One whole objective, end to end

The objective, submitted through `POST /agents/objectives` from the installed app:

> "Write three short captions for a 30-second morning-routine video, each under 90 characters, each
> with one hashtag."

Result: **`succeeded`**, `quality_verdict: pass`, 15 tasks, 17 model calls, 2626 tokens. The compiled
deliverable contained nine usable captions with hashtags, for example:

```
### creative_writing — caption_writer_agent
Three original 30-second morning-routine captions with hashtags, each under 90 characters.

- Sunrise, skincare, productivity – wake up fresh. [hashtag: #MorningMood]
- Skincare first, then the day – start strong. [hashtag: #GentleWakeUp]
- Breakfast boosts energy, productivity starts here. [hashtag: #EnergyBoost]

### quality_governance — content_quality_agent
The captions are within character limits and include one hashtag each, meeting the basic requirements.
```

## 5. Four defects found by running it, and what changed

An activation that only starts the daemon would have reported "local AI working" while every objective
ended `partial`. These are the defects the real run exposed.

| # | Symptom | Cause | Change |
| --- | --- | --- | --- |
| 1 | Every objective failed with `OBJECTIVE_NOT_RUNNABLE` / `MALFORMED_AGENT_OUTPUT` | qwen3:4b plans with `dependencies` instead of `depends_on`, single-character step ids, and `assignments` as a mapping; the contract rejected all three | `services/structured.py` maps dependency aliases and normalises assignment shapes; `PlanStep.step` minimum length 3 → 1 |
| 2 | A copy task failed on `items.0.hashtag: Extra inputs are not permitted` | the model writes `{"caption": "...", "hashtag": "#..."}`; `ArtifactItem` has no `hashtag` field, so the hashtag — the thing the objective asked for — was thrown away | unknown text-bearing fields are preserved in `notes` under the model's own key (`hashtag: #MorningMood`); structural noise (`confidence`, `score`) is dropped, not quoted |
| 3 | The compiled deliverable described the work instead of containing it | it rendered each task's `summary` only, and a copy task's captions live in `artifact.items` | `_deliverable_body` renders items and report sections; placeholder items are not rendered |
| 4 | Every objective ended `partial` with `verdict: reject`, reviewers saying "no content provided" | quality tasks have no dependency edges, so their payload was the objective text only | a quality task with no edges now reviews the objective's own succeeded work, and upstream context carries the rendered artifact rather than the summary |

Tests: `tests/test_agent_structured.py` (24) and `tests/test_agent_orchestration.py` (26) — 50 passing,
built from the real model output captured in the database, not from invented fixtures.

## 6. What did not change

- No model was downloaded, moved or deleted; no CUDA, driver or Ollama installation setting was changed.
- No Windows security setting was touched (Smart App Control is still on).
- The Resource Governor is unchanged: one generation at a time, 85 % RAM ceiling, 2 GB free-RAM floor.
- No credentials, accounts or publishing behaviour were touched in this milestone.

## 7. Known limits of the local AI layer on this hardware

1. **Small models plan better than they execute.** The 0.6–1.7b models reliably restate a task; the
   copy they produce is usable but generic, and the `qwen3:4b` planner is what makes the plan valid.
   This is a capability ceiling, not a defect.
2. **One model at a time.** A 4 GB card plus the governor's single slot means a 4b planning call and a
   1.7b drafting call cannot overlap; a long objective is serial and takes minutes.
3. **One transient daemon crash was observed** during a first run: `OLLAMA_UNAVAILABLE` with
   `error reading llama-server response ... connection closed by host`, recorded honestly as a failed
   task rather than retried into a false success. It did not recur in later runs. If it recurs, the
   mitigation is fewer model swaps (`OLLAMA_MAX_LOADED_MODELS=1`).
4. **No embeddings-based retrieval was exercised** beyond the profile being installed.
