# Troubleshooting the agent workforce

Every entry below is a failure that actually happened on this machine, with the command that
reproduces the diagnosis. Read the error code first: it names the layer.

## Quick triage

```powershell
cd <PROJECT_ROOT>
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\diagnose.ps1     # 34 checks
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_ollama.ps1  # daemon not running
Invoke-RestMethod http://127.0.0.1:11434/api/ps                              # what is resident
```

```powershell
# what happened to the last objective, in one query
python -c "import sqlite3;c=sqlite3.connect(r'<DATA_ROOT>\data\database\creatoros.db');print(c.execute('select id,state,error_code,calls_used,tokens_used from agent_objective order by created_at desc limit 1').fetchone())"
```

GUI: **Activity log** for task-level history, **Live monitor** for a run in progress,
**Playground** to reproduce a single agent call.

## `OLLAMA_UNAVAILABLE` on the chief (planner, qwen3:4b)

Symptom: objective fails at depth 0, `ai_execution` shows `qwen3:4b`, `failed`.

Observed causes, in the order they were found:

1. **No `format=json`.** The 4B model rambled until Ollama aborted with
   `prediction aborted, token repeat limit reached`. Fixed by sending
   `response_format="json"` for structured agents.
2. **Context too large.** `qwen3:4b` at `num_ctx = 4096` on a 4 GiB GPU killed llama-server
   (`wsarecv: connection closed by the host`). The planner profile is capped at **2048**.
3. **Model switch contention.** Swapping the resident 1.7B for the 4B drops the socket
   roughly half the time on this driver. `ensure_exclusive()` unloads other models and waits
   0.8 s after the unload list clears; `AiService` retries once after a switch.
4. **The 4B model still fails sometimes.** The engine now falls back to the declared
   profile chain (planner → specialist) and records it:
   `chief_orchestrator: planner failed (OLLAMA_UNAVAILABLE); using specialist`.
   That is a degraded run, and it says so.

Check:
```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/ps
python -c "import sqlite3;c=sqlite3.connect(r'<DATA_ROOT>\data\database\creatoros.db');[print(r) for r in c.execute('select model_tag,state,error_code,substr(error_detail,1,120),created_at from ai_execution order by created_at desc limit 5')]"
```

## `no such table: main.agent_task_p3`

A migration artefact: the first Phase 4 rebuild renamed `agent_task` before rebuilding the
tables that reference it, so `agent_run` and `task_edge` kept foreign keys pointing at the
scratch name. Fresh databases were fine, so tests did not see it; the first real objective
died with a SQL error.

Fixed by migration `b7f1c02d4e61` (repairs any table whose SQL references the scratch name)
and prevented by `test_migrated_schema_has_no_dangling_foreign_keys`, which asserts every
`REFERENCES` target exists and `pragma foreign_key_check` is empty.

```powershell
cd <PROJECT_ROOT>\app\backend
.\.venv\Scripts\alembic.exe upgrade head
python -c "import sqlite3;c=sqlite3.connect(r'<DATA_ROOT>\data\database\creatoros.db');print(c.execute('select version_num from alembic_version').fetchone());print([n for n,s in c.execute(\"select name,sql from sqlite_master where sql like '%agent_task_p3%'\")])"
```

## `MALFORMED_AGENT_OUTPUT`

The model answered, but not with a valid artifact for the contract.

- The engine already retried once with the validation error included in the repair prompt.
- Look at `agent_task.output_json` — the raw text is kept there for diagnosis.
- Small models are literal: if the prompt asks for "a JSON object with fields summary and
  steps" they comply; if it asks for "a plan", they write prose.
- `coerce_artifact` normalises representation (key aliases, `str` → `[str]`, section wrapping)
  but never invents values. A missing required field is a failure, by design.

## `TOOL_FORBIDDEN`

The agent does not hold the grant in `agent_definition.allowed_tools_json`, or the tool is
disabled in `tool_registry`.

```powershell
Invoke-RestMethod "http://127.0.0.1:64625/agents/registry/chief_orchestrator" -Headers @{Authorization="Bearer $token"} | ConvertTo-Json -Depth 4
```

The GUI (Agent detail) shows grants and the explicit forbidden list; the audit log records
every denied attempt.

## `AI_UNAVAILABLE` (HTTP 503) from the Playground

`run_now` runs a preflight against the governor and gateway. 503 means: daemon down, no
model installed, or the governor refuses admission (VRAM/RAM pressure, queue full).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <PROJECT_ROOT>\scripts\start_ollama.ps1
Invoke-RestMethod http://127.0.0.1:11434/api/tags | ConvertTo-Json -Depth 2 | Select-String qwen3
```

## Objective stuck in `planning`

If the process was killed mid-run, the row stays non-terminal. The backend sweeps on boot
(`mark_interrupted`), and `POST /agents/objectives/{id}/resume` requeues non-terminal tasks
while keeping succeeded artifacts.

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:64625/agents/objectives/$id/resume" -Headers $h
```

## Objective succeeded but the deliverable looks thin

Check `final_report_json.warnings` and `unfinished`. Common honest outcomes:

- `chief did not name a usable department; falling back to strategic intelligence`,
- `strategic_intelligence: delegated 1 specialist task(s)` (the leader asked for less),
- `planner failed (OLLAMA_UNAVAILABLE); using specialist` (a 1.7B plan, not a 4B plan),
- truncation warnings when the plan exceeded department/specialist budgets.

A thin but successful run is a model-capability result, not a hidden failure. The
deliverable says which model produced each section (`task.model_tag`).

## Backend will not start

```powershell
cd <PROJECT_ROOT>
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\diagnose.ps1
Get-Content <DATA_ROOT>\logs\backend\creatoros.log -Tail 40
```

The desktop app strips a host `ELECTRON_RUN_AS_NODE=1` for the backend child only; if you
start the backend by hand, unset it first or the process will behave like Node.

## Environment reminders

- Models live in `<DATA_ROOT>\models\ollama` (`OLLAMA_MODELS`); a daemon started without
  that variable sees an empty model list.
- `TEMP`/`TMP` are injected per child process, never machine-wide (ADR-05).
- One generation at a time (`CREATOROS_EXCLUSIVE_MODEL=0` opts out, and will OOM on 4 GiB).
