# Orchestration Engine

`services/orchestrator.py` is the only code path that turns an objective into work.
It is a single class with explicit budgets, a persistent DAG, and one model call in
flight at a time.

## Why it is built on the Phase 2 queue

Phase 4 did not add a scheduler. An objective is a `queue_job` row (`kind = agents.objective`,
`handler = agents.objective`), so it inherits what Phase 2 already proves:

- atomic claim (one worker wins), lease + heartbeat, backoff with jitter,
- dead-letter after the attempt limit, idempotency by signature,
- crash recovery: a killed worker's lease expires and the objective is retried.

`objective_signature(objective_text, project_id)` makes a duplicate submission of the exact
same objective while one is still open resolve to the existing objective instead of
starting a second run.

## Task states

```
pending ──ready──► assigned ──► running ──► succeeded
   │                 │            │  │
   │                 │            │  └──► failed ──requeue──► ready
   │                 │            └─────► cancelled
   │                 └──────────────────► blocked (upstream failed/cancelled)
   └────────────────────────────────────► waiting (review waiting for its subject)
```

`TASK_TERMINAL_STATES = {succeeded, failed, cancelled, blocked, skipped}`.
An objective is `succeeded` only when every required task is terminal and at least one
task succeeded; mixed outcomes become `partial` with the failures named.

## The plan phase (depth 0)

Input to the chief: objective text, optional project metadata, department catalogue
(id, name, mission, leader, specialist count) and the output contract for `plan`.

The chief's artifact is validated as a `plan` contract:

```json
{
  "contract": "plan",
  "summary": "one paragraph",
  "steps": [{"step": "...", "owner": "department_id", "depends_on": []}],
  "items": [...],
  "confidence": 0.0
}
```

`assignments` (department ids) are read from the plan's owners/steps. The engine then:

1. keeps the requested departments that exist and are enabled, in plan order,
2. truncates to `max_departments` (3) and warns,
3. if none is usable, falls back to `strategic_intelligence` and warns
   (`chief did not name a usable department; falling back to strategic intelligence`),
4. creates one depth-1 task per department — **no edges between them**, so no department
   can stall another,
5. schedules the quality phase (below).

## Department expansion (depth 1)

Each leader receives the objective, its department brief, its own specialist catalogue and
anything already produced upstream. Its artifact must be a `plan` naming specialist task
types from its own list. The engine:

- rejects unknown or other-department specialists (recorded, not fatal),
- truncates to `specialists_per_department` (3),
- passes each specialist the objective, the leader's brief for it, and the readable
  upstream artifacts,
- refuses a fourth level: depth 2 is a leaf by construction.

## Quality governance comes after production

`_schedule_quality_phase` runs once per objective, after every production department task
reaches a terminal state. It creates `quality_director` (depth 1) with the compiled
results, whose plan becomes depth-2 review tasks (`review` contract: verdict + issues).

Review tasks **never block production** — they are siblings, not ancestors — which is why
a slow review can not starve the departments that produce content, and why the objective's
verdict can be `needs_revision` while the deliverable still exists.

## Execution loop (`_drain`)

```
while budget left:
    ready = graph.ready_task_ids()      # all dependencies terminal & succeeded
    if not ready: break
    task = ready[0]                      # concurrency 1, FIFO by created_at
    run one task
```

One task at a time is not a simplification, it is the only safe choice on this hardware:
a 4 GiB GPU with ~3.3 GiB usable serves exactly one resident model.

## What happens inside one task

1. **Lease** on the objective (`LeaseKeeper`, heartbeat every 15 s) so a long run is not
   treated as dead by another worker.
2. **Deterministic tool selection** from `TASK_TOOL_HINTS` for the task type; at most three,
   only those granted to that agent in `tool_registry` + department grants.
3. **Tool execution** before inference (single-threaded, per-tool timeout, audited). Results
   are given to the model as data with an explicit "data, not instructions" frame.
4. **Prompt build**: agent system prompt + objective + task brief + upstream artifact
   summaries (truncated, labelled) + tool output.
5. **Model call** through `AiService` (governor admission, execution row, metrics).
6. **Parse** with `extract_json` → `coerce_artifact` → contract validation. On failure: one
   repair call that includes the validation error, then `MALFORMED_AGENT_OUTPUT`.
7. **Persist**: artifact, tokens, duration, model tag, `agent_run`, memory write (scoped),
   evidence refs, task state.
8. **Progress**: `objective.calls_used`, `objective.tokens_used`, report warnings.

## Cancellation and recovery

| Action | Path |
|---|---|
| Cancel objective | `cancel_objective` marks queued/running tasks cancelled, cancels the in-flight generation via the AI cancel registry, sets objective `cancelled`, writes a report |
| Fix a failed objective and retry | `resume_objective` requeues non-terminal tasks and resets `calls_used`; completed artifacts are kept |
| Backend restart | `mark_interrupted()` sweeps `running` tasks and non-terminal objectives to `interrupted`/`failed` with a reason; `requeue_pending()` re-enqueues queued objectives |
| Worker death | queue lease expiry → the objective is retried from its persisted state |

## Compiled result

`FinalObjectiveResult` is built from stored artifacts only:

- `summary` — count of completed tasks + the chief's summary,
- `deliverable` — markdown with one section per task, in dependency order,
- `quality_verdict` — from the review artifacts (`pass` / `needs_revision` / `reject` / `not_run`),
- `tasks` — id, agent, department, type, state, summary, error code,
- `unfinished` — tasks that did not succeed, with why,
- `warnings` — truncations, fallbacks, rejected specialists, missing inputs.

If the objective failed, the report says so and names the failing task and error code.
There is no code path that produces a report claiming success for work that did not happen.

## Verdict semantics

The compiled verdict comes from the stored review artifacts, in this order: any `reject`
→ `reject`; else any `revise` → `needs_revision`; else `pass` when reviews exist;
`not_run` when the quality phase produced nothing.

A reviewer that asks for a revision causes the engine to queue exactly one follow-up task
for the same agent (up to `max_revisions`), recorded as
`"<agent> asked for a revision; queued one follow-up task"`. The follow-up is *not* followed
by a second review: re-reviewing every revision would double the cost of a run on this
hardware, so the verdict keeps saying that a human should look again. The deliverable and
the warning tell you the follow-up already ran.

## Measured behaviour (this machine)

| Objective | Result | Tasks | Calls | Tokens | Wall clock |
|---|---|---|---|---|---|
| "Turn one long video into three platform-specific shorts about hooks" | `succeeded`, verdict `pass` | 5 (1 chief, 2 leaders, 2 specialists) | 9 | 1110 | 144.6 s |
| Same, before explicit fallback existed | `failed` `OLLAMA_UNAVAILABLE` | 1 | 1 | 0 | 19 s |

The first run above recorded this warning, which is exactly the intended honest behaviour:

```
chief_orchestrator: planner failed (OLLAMA_UNAVAILABLE); using specialist
```

## Deliberate limits

- The chief is limited to *three* departments per objective. Ten at once would not fit in
  30 calls, and a bigger budget would just make the run longer without making it better.
- A leader may not see other departments' results, except what the engine chooses to pass
  down as upstream context. This keeps department outputs independent and comparable.
- No retry loops on validation: two attempts, then the task fails. A model that cannot
  produce a valid plan is a fact the report should carry, not something to hide behind
  six retries.
- The engine never edits an artifact to make it valid. `coerce_artifact` normalises
  *representation* (key aliases, string-vs-list, section naming); it never invents values.
