# ADR-17 — One resident model per generation, with explicit fallback

- **Status:** accepted
- **Date:** 2026-11-22
- **Phase:** 4
- **Related:** ADR-09 (model pool and measured throughput), ADR-15 (gateway), ADR-16 (governor),
  `docs/TROUBLESHOOTING_AGENTS.md`

## Context

The workforce routes different agents to different profiles: the chief and leaders plan on
`qwen3:4b` (planner), specialists produce on `qwen3:1.7b`, cheap classification runs on
`qwen3:0.6b`. One objective therefore asks the daemon to serve several models in sequence.

Measured on this laptop (GTX 1050 Ti Max-Q, 4096 MiB, ~3.3 GiB usable, driver 582.78):

| Situation | Result |
|---|---|
| `qwen3:1.7b` alone | 45.4 tok/s, stable |
| `qwen3:4b` alone at `num_ctx=2048` | ~16 tok/s, stable in ~2 of 3 calls |
| `qwen3:4b` alone at `num_ctx=4096` | llama-server killed, `wsarecv: connection closed by the host` |
| 1.7B resident, then load 4B without unloading | `out of memory` / dropped socket |
| 1.7B unloaded, 4B loaded immediately | **fails about half the time** — the driver has not released VRAM yet |

So the daemon must never hold two models for one objective, and a switch is inherently
racy on this GPU. Two naive fixes are wrong:

- raise concurrency or run models in parallel → guaranteed OOM;
- retry forever → the run never terminates and hides a real hardware limit.

## Decision

1. **Exclusivity.** `OllamaGateway.ensure_exclusive(model)` runs before every generation
   that needs a model change: it calls `/api/ps`, unloads any other resident model
   (`keep_alive=0`), polls until the list is clear, and then **waits a further 0.8 s**
   because `/api/ps` clearing does not mean the driver released the VRAM. It is skipped
   while another generation is in flight (`_inflight > 0`) so it can never evict the model
   it is about to use. `CREATOROS_EXCLUSIVE_MODEL=0` opts out (and will OOM on 4 GiB).

2. **Profile context ceilings are measured, not aspirational.** `qwen3:4b` is capped at
   `num_ctx = 2048`; `ModelRegistry.ensure_seeded` reconciles stored profiles and
   `_clamp_context()` enforces the ceiling at call time, so an old 4096 value in the DB
   cannot crash the daemon.

3. **One retry after a switch.** If a generation fails with `OLLAMA_UNAVAILABLE`
   immediately after a model switch, `AiService` retries once after a 1.5 s settle delay
   and a fresh `ensure_exclusive`. A second failure is reported as a failure.

4. **Explicit fallback chain.** Every agent definition carries `fallback_profiles_json`
   (defaults: planner → specialist, specialist → router_light, router_light → none). When a
   call fails with a recoverable code (`OLLAMA_UNAVAILABLE`, `MODEL_LOAD_FAILED`,
   `MODEL_NOT_FOUND`, `MODEL_NOT_GENERATIVE`, `GENERATION_TIMEOUT`), the engine retries the
   **same task** on the declared fallback and records it in three places:
   `agent_task.output_json.fallback_note`, `agent_task.evidence_refs_json`, and
   `final_report_json.warnings`. Nothing is silent.

5. **`format=json` for structured agents.** Without it, `qwen3:4b` loops tokens until
   Ollama aborts (`prediction aborted, token repeat limit reached`). With it, the call
   either returns a parseable object or fails honestly.

6. **HTTP 200 with an `error` field is a failure.** Ollama reports load/abort failures
   inside a 200 body; the gateway raises `classify_error_message()` instead of returning an
   empty generation that would later look like a malformed artifact.

## Consequences

- An objective runs at roughly one model switch per department; a 5-task run took 144 s
  wall clock on this machine.
- Some objectives will run at reduced quality when the planner keeps failing; the report
  says so, and the deliverable names the model per section. Quality is never claimed
  beyond what the recorded `model_tag` supports.
- The system stays usable on 3.3 GiB of VRAM without asking the user to buy hardware.
- A stronger GPU would simply stop triggering the retry and the fallback; no code change
  is needed to benefit from it.

## Verification

- `tests/test_agent_orchestration.py::test_declared_fallback_is_used_and_recorded` proves the
  fallback is attempted and recorded.
- Real run after the repair migration: 5 tasks, 9 calls, 1110 tokens, `succeeded`,
  verdict `pass`, warning
  `chief_orchestrator: planner failed (OLLAMA_UNAVAILABLE); using specialist`.
- Probe script: `scripts/probe_ai.py` measures tok/s per model on demand.
