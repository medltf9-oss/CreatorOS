# Troubleshooting the local AI layer

Every error the AI layer returns is one of the codes below, with an HTTP status
and a correlation id. The UI shows `<CODE>: <message>`; the same message is in
`logs/backend/creatoros.log` with the full context.

## Error codes

| Code | HTTP | Meaning | What to do |
| --- | --- | --- | --- |
| `OLLAMA_UNAVAILABLE` | 503 | the daemon is not listening on the configured address | start Ollama, or check `CREATOROS_OLLAMA_URL` |
| `MODEL_NOT_FOUND` | 404 | tag is not installed | pull it in Model Lab |
| `MODEL_LOAD_FAILED` | 507 | Ollama could not allocate memory for the model | free VRAM/RAM, use a smaller model, close other GPU apps |
| `MODEL_BUSY` | 409 | model is loaded while a delete was requested | retry with `force=true` or unload first |
| `MODEL_NOT_GENERATIVE` | 409 | embedding-only model asked to generate | use a completion model |
| `GENERATION_TIMEOUT` | 504 | the model did not answer within the timeout | raise `generation_timeout_s` or use a smaller model |
| `GENERATION_CANCELLED` | 409 | cancellation was requested | expected after a cancel |
| `GOVERNOR_BUSY` | 429 | no inference slot within `wait_s` | wait, or raise `max_concurrent_generations` (not recommended on 4 GB) |
| `RESOURCE_PRESSURE` | 429 | RAM/VRAM headroom below the configured floor | reduce the limit floor or free memory |
| `NO_MODEL_AVAILABLE` | 503 | no installed profile can serve the request | pull a model, or lower `needed_context` |
| `CONFIRMATION_REQUIRED` | 428 | destructive call without `confirm=true` | repeat with confirmation |
| `EXECUTION_NOT_CANCELLABLE` | 409 | the execution already finished | nothing to cancel |
| `VALIDATION_ERROR` | 422 | a field is out of range | the message names the field |

## Common symptoms

### "ollama offline" in the header, everything AI greyed out
```powershell
& <DATA_ROOT>\Programs\Ollama\ollama.exe list     # does it answer at all?
Invoke-RestMethod http://127.0.0.1:11434/api/version
```
If the second command fails, Ollama is not running. Start it from the Start menu
or run `<DATA_ROOT>\Programs\Ollama\ollama.exe serve`.

### The model list is empty although the weights are on D:
`ollama list` answers, but with nothing in it, and every AI page reports no installed model.
The daemon is serving a directory that has no weights in it - the Windows Ollama *app* stores its own
model path in `%LOCALAPPDATA%\Ollama\db.sqlite` (table `settings`, column `models`) and that value wins
over `OLLAMA_MODELS`. Measured on this machine (2026-11-24): the stored value was
`D:\NEXUS_DATA\models` from an earlier installation, and the daemon log said
`OLLAMA_MODELS:D:\NEXUS_DATA\models` while 9.25 GB of weights sat unused in
`<DATA_ROOT>\models\ollama`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_ollama.ps1 -Status   # what the daemon sees
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_ollama.ps1          # repair if missing
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix_ollama_path.ps1       # report the stored path
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix_ollama_path.ps1 -Apply # fix it for login
```

`start_ollama.ps1` serves `<DATA_ROOT>\models\ollama` for the running daemon.
`fix_ollama_path.ps1 -Apply` corrects the durable setting (after copying `db.sqlite` next to itself as
a backup), so a reboot does not undo it. Nothing is downloaded or moved. See
`docs/LOCAL_AI_ACTIVATION.md`.
### A generation returns an empty answer
Qwen3 models reason before answering. With a small `num_predict` the entire
budget can be consumed by hidden reasoning and `response` comes back empty.
CreatorOS disables thinking by default for `qwen3*` tags
(`default_think_for`) and reports `THINKING_BUDGET_EXHAUSTED` instead of calling
the run a success when it still happens. To see the reasoning, tick "allow
hidden reasoning" in the Playground; the reasoning appears in a collapsible
block and is never mixed into the answer text.

### Throughput collapsed to a few tokens per second
The model is running partly on the CPU. Check "Loaded models" in
**AI infrastructure → Loaded models**: a `vram_percent` below 99 means spill.
Causes: another GPU process, a too-large model for 4 GB, or a context window
above 32768. `qwen3:4b` is expected to show ~69 % placement on this laptop.

### "out of memory" / `MODEL_LOAD_FAILED`
Only one 4B-class model fits. Unload something (`POST /ai/models/{tag}/unload`),
or let the governor handle it: with `max_concurrent_generations: 1` the second
request is refused with `429` rather than crashing the runtime.

### Everything returns `429 GOVERNOR_BUSY`
Something long is running, or a previous generation died without releasing its
slot. The slot is released in a `finally` block, so a crashed request cannot
leak it; a killed *process* can. Check `GET /ai/resources` → `active_generations`
and restart the backend if a slot is stuck at 1 with no running execution.

### Executions stuck in `running` after a crash
On boot the service marks every `running` execution as `interrupted`
(`AiService.mark_interrupted`). If you see `running` for a row that cannot be
running, the backend is mid-boot or the row was written by a process that is
still alive.

### A queued AI job ends in `dead_letter`
Look at `last_error` in the job row. The worker retries with exponential backoff
and a jittered delay; `MODEL_NOT_FOUND` is not retryable in practice, so pull the
model and enqueue again (the idempotency key of the old job stays used — use a
new key).

### `nvidia-smi` errors in the UI
The governor reports `telemetry_errors` and continues; RAM limits are still
enforced. This happens on machines without a GPU, with a driver that blocks
`nvidia-smi`, or when `nvidia-smi` is not on `PATH` (it normally lives in
`C:\Windows\System32`).

### Model download appears to jump to 100 %
Ollama reports `success` without byte counters when all layers are already in
the local store. The UI then says "layers already present locally" instead of
faking a progress bar.

## Logs and evidence

| What | Where |
| --- | --- |
| Backend log (JSON, redacted) | `<DATA_ROOT>\logs\backend\creatoros.log` |
| Audit trail | `audit_event` table, also `GET /audit` |
| Execution history | `ai_execution` table, `GET /ai/executions` |
| Machine checks | `powershell -File <PROJECT_ROOT>\scripts\diagnose.ps1` |

## Running the tests

```powershell
cd <PROJECT_ROOT>\app\backend
.\.venv\Scripts\python.exe -m pytest -q                 # unit + API (no model runtime)
$env:CREATOROS_RUN_LIVE_AI=1
.\.venv\Scripts\python.exe -m pytest tests/test_ai_integration.py -v   # real Ollama
```

The live tests load the smallest installed model (`qwen3:0.6b` by default,
override with `CREATOROS_LIVE_LIGHT_MODEL`) and unload it afterwards.
