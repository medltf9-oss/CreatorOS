# Model profiles

A **profile** is a named role ("who should answer this?") mapped to one
installed Ollama tag plus the context window and temperature that role needs.
Agents and API callers ask for a profile, never for a raw model tag, so the
mapping can change without touching calling code.

Profiles are stored in `model_profile` and seeded on first boot
(`creatoros/services/models.py::DEFAULT_PROFILES`). They can be edited in
**Settings → Model profiles** or with `PUT /ai/profiles/{name}`.

## The six default profiles

| Profile | Ollama tag | ctx | temp | Roles | Falls back to |
| --- | --- | --- | --- | --- | --- |
| `router_light` | `qwen3:0.6b` | 4096 | 0.2 | classification, routing, extraction, short_rewrite | `specialist` |
| `specialist` | `qwen3:1.7b` | 4096 | 0.7 | caption, hashtags, drafting, default_worker | `router_light` |
| `planner` | `qwen3:4b` | 4096 | 0.6 | planning, reasoning, critique | `specialist` |
| `coding` | `qwen2.5-coder:3b` | 4096 | 0.2 | code, tests, debugging | `specialist` |
| `vision` | `gemma3:4b` | 4096 | 0.4 | image_understanding, frame_analysis | `planner` |
| `embedding` | `nomic-embed-text` | 2048 | 0.0 | embeddings, semantic_search | — (none) |

Why these models on a 4 GB card:

- **0.6b** is fast enough (75.8 tok/s measured) for routing and classification,
  and small enough to stay resident while a bigger model loads.
- **1.7b** is the default worker at 45.4 tok/s with 100 % GPU placement — the
  best quality-per-second point on this hardware.
- **4b** produces noticeably better plans but only 15.5 tok/s at 69 % GPU
  placement, so it is reserved for planning, not for bulk caption writing.
- **qwen2.5-coder:3b** is the only model here trained for code.
- **gemma3:4b** is the only multimodal model installed; frame analysis has to
  use it, accepting the same partial-offload cost as the planner.
- **nomic-embed-text** is an embedding-only model (no text generation).

## How a model is chosen

`ModelRouter.route(complexity, preferred_profile=…, needed_context=…, task_type=…)`:

1. Start from the requested profile, else `COMPLEXITY_TO_PROFILE[complexity]`,
   else `limits.default_profile`.
2. Walk the fallback chain, skipping any profile whose tag is not installed or
   whose context window is too small. Tags are normalised, so
   `nomic-embed-text` matches `nomic-embed-text:latest`.
3. If the machine is under critical memory pressure and the chosen profile is
   not `router_light`, step down one fallback and say so in `warnings`.
4. Return the decision **with its reason**, the profiles considered, the
   alternatives and their installed state.

If nothing matches, the router raises `RoutingError` → `503 NO_MODEL_AVAILABLE`.
There is no hidden "just use whatever is there" path, and no cloud fallback:
grep the module for a provider name and the test
`test_no_cloud_provider_is_reachable_from_the_router_module` will fail.

## Benchmarks and provenance

`model_benchmark` rows carry a `source` column:

- `reference` — the ADR-09 readings taken on this laptop before Phase 3.
- `measured` — produced by the Model Lab benchmark button or
  `POST /ai/models/{tag}/benchmark`, which runs a **real** generation.

A `measured` row always wins over a `reference` row for the same profile, even
if both were written in the same second. The UI shows `ref` next to reference
values so a seeded number is never mistaken for a current measurement.

## Changing a profile

```powershell
# point the specialist profile at a different installed tag
curl -X PUT http://127.0.0.1:<port>/ai/profiles/specialist `
  -H "Authorization: Bearer $env:CREATOROS_SESSION_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{"ollama_tag":"gemma3:4b","context_len":8192,"temperature":0.5}'
```

Validation: `context_len` 512…131072, `temperature` 0…2, unknown profile → 404.
Context windows above 32768 are clamped by the gateway, because the KV cache
would not fit in 4 GB.
