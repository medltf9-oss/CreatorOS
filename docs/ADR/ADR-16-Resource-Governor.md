# ADR-16 — Admission control instead of unbounded concurrency

- **Status:** accepted
- **Date:** 2026-11-22
- **Phase:** 3
- **Related:** ADR-09 (model pool and measured throughput), ADR-15 (gateway), INV-1
- **Numbering note:** the Phase 3 brief called this "ADR-11"; see the note in ADR-15.

## Context

The target machine is an **i5-8300H / 24 GB RAM / GTX 1050 Ti Max-Q 4 GB**. The
usable VRAM for Ollama is about **3.3 GiB**. Measured on this laptop (ADR-09):

| Model | Throughput | GPU placement |
| --- | --- | --- |
| `qwen3:0.6b` | 75.8 tok/s | 100 % |
| `qwen3:1.7b` | 45.4 tok/s | 100 % |
| `qwen3:4b` | 15.5 tok/s | 69 % (partial offload) |
| `gemma3:4b` | not benchmarked | does not fit alongside another 4B model |

Two concurrent 4B-class generations do not fit in 4 GB: Ollama will either spill
to CPU (an order of magnitude slower) or fail the load (`out of memory`). Once
CreatorOS has 111 agents, the temptation is to run many generations in parallel;
on this hardware that produces three Windows-visible symptoms: the desktop
stutters, the model load fails, and the queue retries something that will never
succeed.

## Decision

Introduce a **resource governor** that every generation must pass through:

```
request → governor.acquire() → gateway.generate() → governor.release()
```

- **Default concurrency is 1.** It is a user-editable limit, never an automatic
  escalation.
- Admission is refused, not queued forever: `429 GOVERNOR_BUSY` (no slot within
  the caller's `wait_s`) or `429 RESOURCE_PRESSURE` (memory headroom already
  below the floor).
- The governor samples CPU/RAM (`psutil`) and GPU utilisation, temperature,
  VRAM and power (`nvidia-smi`) every 3 s, keeps 120 samples for charts, and
  tracks peaks.
- Missing telemetry is **reported as missing** (`telemetry_errors`), never
  replaced with zeros that look like real readings.
- Limits live in `AppSetting key="resources.limits"` and are validated strictly:
  out-of-range values are rejected with `422 VALIDATION_ERROR`, not silently
  clamped. Clamping would mean the number on screen is not the number in force.

Enforced envelope (`ResourceLimits`):

| Limit | Default | Allowed |
| --- | --- | --- |
| `max_concurrent_generations` | 1 | 1…4 |
| `max_ram_percent` | 85 | 50…98 |
| `min_free_ram_gb` | 2.0 | 0.5…32 |
| `max_vram_percent` | 92 | 50…100 |
| `max_queue_depth` | 25 | 1…500 |
| `generation_timeout_s` | 300 | 10…1800 |

Admission is polled (0.25 s) rather than built on `asyncio.Condition`, because
the governor is shared by the API event loop and the worker thread's loop; a
`Condition` created in one loop raised *"bound to a different event loop"* when
released from the other. The counter is guarded by the GIL and both loops only
increment it inside `acquire()`.

## Consequences

**Good**

- The machine stays usable with a 4B model loaded: RAM stays under 85 %, the
  desktop keeps repainting, and a second request gets a clear 429 instead of a
  silent 20-second stall.
- Limits are auditable: every change writes an audit row and a settings row.
- Peak RAM/VRAM is recorded, so the next phase (media pipeline) can be planned
  from measurements instead of guesses.

**Costs / limits**

- Throughput is bounded by one generation at a time: a batch of 20 captions at
  ~45 tok/s takes minutes, not seconds. This is the honest cost of the GPU in
  this laptop; pretending otherwise would need a different card.
- The 0.25 s poll adds up to 0.25 s of latency when a slot frees up. Acceptable
  for interactive use and for a queue that is already seconds-scale.
- `nvidia-smi` can be absent or blocked by driver policy; the governor then
  reports GPU pressure as unknown and still enforces RAM limits.
