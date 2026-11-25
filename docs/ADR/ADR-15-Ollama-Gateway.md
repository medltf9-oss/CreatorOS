# ADR-15 — A single Ollama gateway inside the backend

- **Status:** accepted
- **Date:** 2026-11-22
- **Phase:** 3
- **Supersedes:** nothing
- **Related:** ADR-09 (model pool), ADR-16 (resource governor)
- **Numbering note:** the Phase 3 brief called this "ADR-10", but ADR-10…ADR-14 are
  already taken by the platform decisions in the master plan, so the AI decisions
  were filed as ADR-15/16 to keep every existing cross-reference valid.

## Context

The renderer, the queue worker, the (future) agent runtime and any future
feature all need to call Ollama. Ollama is an HTTP daemon on `127.0.0.1:11434`
with no authentication. Three naive designs were available:

1. Call Ollama directly from the Electron renderer.
2. Call it from each backend feature that needs it.
3. Put every call behind one adapter owned by the backend.

Options 1 and 2 duplicate error handling, timeout policy, cancellation,
metrics extraction and redaction in every caller. They also make it trivial to
leak the daemon's address into the UI, and they make it impossible to enforce a
single concurrency budget.

## Decision

All model traffic goes through `creatoros.integrations.ollama.client.OllamaGateway`.
The renderer never learns the Ollama URL; it only sees `/ai/*` routes.

The gateway is the *only* module allowed to speak the Ollama wire protocol. It
owns:

| Concern | Implementation |
| --- | --- |
| Transport | one `httpx.AsyncClient` per running event loop, created lazily |
| Base URL | `CREATOROS_OLLAMA_URL`, default `http://127.0.0.1:11434` |
| Timeouts | connect 5 s; per-request read timeout; hard `asyncio.wait_for` envelope |
| Cancellation | `asyncio.Event` per execution, checked between stream chunks |
| Error taxonomy | `OllamaUnavailable`, `ModelNotFound`, `ModelLoadFailed`, `GenerationTimeout`, `GenerationCancelled`, `OllamaBadResponse`, `ModelBusy` |
| Metrics | tokens, durations and tok/s derived from Ollama's own counters |
| Redaction | `safe_error_payload()` scrubs tokens before anything reaches a log or the UI |
| Context window | `num_ctx_for()` clamps to 2048…32768 |
| Reasoning models | `default_think_for()` disables `think` for `qwen3*` so answers are visible |

### Endpoint contract we depend on

| Call | Purpose |
| --- | --- |
| `GET /api/version` | liveness + version |
| `GET /api/tags` | installed models |
| `POST /api/show` | architecture, context length, parameter count, capabilities, licence |
| `GET /api/ps` | resident models and their VRAM split |
| `POST /api/pull` (stream) | download progress |
| `DELETE /api/delete` | remove a model |
| `POST /api/generate` with `keep_alive: 0` | evict a model from memory |
| `POST /api/generate` / `POST /api/chat` | generation (streaming and non-streaming) |

`/api/tags` does not report capabilities, so `list_models(enrich=True)` fills
them from `/api/show` and caches the result per tag for the process lifetime.

## Consequences

**Good**

- One place to fix a protocol change, one place to redact, one place to enforce
  timeouts. Measured: 24 gateway contract tests pin behaviour with a mocked
  transport (`respx`), so wiring mistakes fail fast without a GPU.
- The renderer cannot accidentally reach the daemon, which keeps the "local
  only, no external provider" promise auditable: `GET /version` lists
  `local_inference` and there is no cloud client in the dependency tree.
- Both event loops (FastAPI's loop and the worker thread's loop) share one
  gateway safely. This was a real defect found by the queue integration test:
  an `httpx.AsyncClient` and an `asyncio.Condition` created in the API loop
  raised *"bound to a different event loop"* when the worker used them.

**Costs / limits**

- A pass-through adds one Python function call per request — irrelevant next to
  a 40 ms/token model.
- The gateway is stateful only for its client cache and capability cache; a
  restart costs one extra `/api/show` per installed model.
- Non-streaming requests cannot be cancelled mid-flight: Ollama has no cancel
  endpoint, so cancellation is only honest for streamed generations. The API
  says so: cancelling a finished execution returns `409 EXECUTION_NOT_CANCELLABLE`.
