# Model Lab

Model Lab is the model-management surface of CreatorOS Studio: **Model Lab** in
the sidebar, backed by the `/ai/models`, `/ai/pulls`, `/ai/profiles` routes.
Everything on the page is read from the backend — there is no fixture data in
the renderer.

## What each control does

| Control | Backend call | Notes |
| --- | --- | --- |
| Pull model | `POST /ai/pulls` then `GET /ai/pulls` (1 s poll) | real Ollama progress events |
| Cancel pull | `DELETE /ai/pulls/{pull_id}` | cancels the streaming task and marks the record `cancelled` |
| Benchmark | `POST /ai/models/{tag}/benchmark` | runs a real short generation, records `tokens_per_sec` |
| Unload | `POST /ai/models/{tag}/unload` | `keep_alive: 0`, then verifies with `/api/ps` |
| Delete… | `DELETE /ai/models/{tag}?confirm=true` | two-step in the UI; server requires `confirm=true` |
| Delete a loaded model | `…?confirm=true&force=true` | unloads first, then deletes |
| Model detail | `GET /ai/models/{tag}` | architecture, context length, parameter count, licence |
| Profile edit | `PUT /ai/profiles/{name}` | tag, context window, temperature |

Deleted and pulled models are audited (`ai.model.delete`, `ai.model.pull`,
`ai.model.pull.cancel`, `ai.model.benchmark`).

## Honesty rules the page follows

- **Loaded state** comes from Ollama's `/api/ps`, not from a flag we set.
- **VRAM placement** is `size_vram / size`, so a partially offloaded model shows
  e.g. `69% gpu` instead of pretending to be fully accelerated.
- **Context window** shows `ctx 4096 (loaded)` when read from the running model
  and `ctx 4096 (profile)` when it is the configured value for a model that is
  not resident.
- **Benchmark provenance**: `tok/s` is labelled `ref` when it is the seeded
  ADR-09 reading and unlabelled when measured on this machine.
- **Embedding-only models** (`nomic-embed-text`) have their benchmark button
  disabled with the reason in the tooltip; the API also refuses with
  `409 MODEL_NOT_GENERATIVE`.
- **Delete is never one click.** The first click reveals the size that will be
  removed and what will happen to a loaded model; the server independently
  refuses a request without `confirm=true` (`428 CONFIRMATION_REQUIRED`).

## Verified behaviour (2026-11-22, this machine)

| Action | Result |
| --- | --- |
| Benchmark `qwen3:0.6b` in the UI | 32 tokens at 72.73 tok/s, recorded as `measured`, model showed `loaded 100% gpu` |
| Pull `qwen3:0.6b` (already present) | `success`, "layers already present locally" |
| Delete without confirmation | `428 CONFIRMATION_REQUIRED` |
| Delete a loaded model | `409 MODEL_BUSY`; with `force=true` it unloads then deletes |
| Delete an unknown tag | `404 MODEL_NOT_FOUND` |
| Benchmark an embedding-only model | `409 MODEL_NOT_GENERATIVE` |

## Physical layout

Model weights live on D: — `<DATA_ROOT>\models\ollama` (`OLLAMA_MODELS`).
About 173 KB of Ollama's own bookkeeping (`db.sqlite`, logs) stays in
`%LOCALAPPDATA%\Ollama` because Windows Known Folder resolution does not allow
redirecting it; this is recorded in `docs/THIRD_PARTY.md`. Deleting a model
through Model Lab removes the weights from D: immediately.

## Limits of this screen

- There is no "download from a URL" or arbitrary GGUF import: only tags that
  exist in the Ollama library can be pulled. A local-file import would need an
  explicit Modelfile flow, which is not implemented.
- Model deletion cannot be undone; re-pulling costs the full download size.
- Benchmark runs one short generation (48 tokens by default). It measures
  prompt-eval + generation for that prompt, not a long-context workload.
