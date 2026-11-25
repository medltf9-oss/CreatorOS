# Agent Architecture — 111 agents, one engine

CreatorOS runs a fixed workforce of **111 logical agents** on **one local model at a time**.
The agents are not 111 processes and not 111 loaded models: they are 111 stored definitions
(id, prompt, tools, contract, temperature) executed by a single orchestration engine that
calls the local Ollama gateway through the resource governor.

```
User objective
      │
      ▼
┌──────────────────┐   depth 0   one call, output contract `plan`
│ Chief Orchestrator│  chooses up to 3 departments and states what each must return
└────────┬─────────┘
         │  task per department (no inter-department dependency)
         ▼
┌──────────────────┐   depth 1   output contract `plan`
│ Department leader │  splits work into up to 3 specialist tasks for *its own* department
└────────┬─────────┘
         │
         ▼
┌──────────────────┐   depth 2   leaf, contract from its role (`analysis`/`copy`/...)
│ Specialist agent  │  receives the objective, the department brief and tool output as data
└────────┬─────────┘
         │
         ▼
  deterministic tools (max 3 per task)   →   AiService → gateway → governor → Ollama
```

## Layers and who may do what

| Layer | Count | May delegate | May use tools | Output contract |
|---|---|---|---|---|
| Chief | 1 | to leaders only | 5 (read + report + approval request) | `plan` |
| Leader | 10 | to its own specialists only | its department's tools | `plan` |
| Specialist | 100 | never | its department's tools | role contract |

Depth is enforced by the engine (`Budgets.max_depth = 3`, hard cap 4). A leader cannot
create work in another department: `_expand_department` filters every requested specialist
against the department's own catalogue and records the rejected ones in the report.

## One engine, three roles

`services/orchestrator.py` contains a single class, `Orchestrator`. The difference between
chief, leader and specialist is data, not code:

- which `AgentDefinition` row is loaded (prompt, profile, tools, temperature, contract),
- the task's `depth` (0/1/2) and `task_type`,
- the output contract the artifact is validated against.

This is deliberate: a second code path for "leaders" would drift from the first one, and
the failure modes (budget overrun, contract violation, orphan task) would be fixed twice.

## Hierarchy of trust

1. **The human is the root of authority.** Objectives are submitted by a person; agents
   cannot create objectives, enable themselves, grant themselves tools or approve anything.
2. **The chief plans but does not execute.** Its artifact is a plan: departments, assignments,
   acceptance criteria. It has no tool that mutates content.
3. **Leaders decompose, they do not publish.** Their plans are constrained to their own
   department and their specialist list.
4. **Specialists produce validated artifacts.** Their output must satisfy the contract's
   required fields; an artifact that does not is repaired once and then rejected.
5. **Quality is reviewed by a different department.** `quality_governance` reviews the
   completed work after production, so a reviewer with the same blind spots as the author
   is not the only check, and review never starves production.

## Request lifecycle (synchronous objective)

```
POST /agents/objectives {objective, run_now: true}
  ├── objective row created (state=pending), request stays open
  ├── Orchestrator.create_objective → enqueue (durable queue) or claim directly
  ├── state=planning, root task `chief_orchestrator` created (depth 0)
  ├── plan phase: chief artifact → 2-3 department tasks (depth 1) + quality task
  ├── drain loop: ready tasks only, concurrency 1
  │     for each task: deterministic tools → model call → contract validation
  │                    → artifact + memory write + upstream results linked
  ├── quality phase tasks run on completed results (depth 1 → 2)
  ├── compile: deliverable text + per-task summaries + verdict + honest gaps
  └── state=succeeded|failed|partial|cancelled, final_report_json always written
```

`run_now=false` returns immediately and the durable queue worker executes the objective;
progress is visible on **Live monitor** and the **Task graph**.

## Budgets (enforced, not advisory)

| Budget | Value | Hard cap | Enforced in |
|---|---|---|---|
| Delegation depth | 3 | 4 | `TaskGraph.validate`, `Budgets` |
| Model calls per objective | 30 | 60 | `_invoke` counter, `objective.calls_used` |
| Revisions per task | 2 | 3 | `_invoke` attempt loop |
| Concurrency | 1 | 1 | `_drain` processes one ready task at a time |
| Wall clock | 900 s | — | deadline check before each task |
| Departments per objective | 3 | 5 | `_plan` truncation with a recorded warning |
| Specialists per department | 3 | 5 | `_expand_department` truncation |
| Tools per task | 3 | — | tool selection |

Exceeding a budget never silently truncates the answer: the objective report lists the
truncation under `warnings` and in `unfinished` when work was dropped.

## Model routing

Each definition names a profile (`router_light`, `specialist`, `planner`, `coding`, `vision`).
Routing is resolved by the existing Phase 3 `ModelRouter`:

- the profile's model must be installed (`/api/tags`) and generative,
- the governor must admit the call (VRAM/RAM/queue pressure),
- context is clamped to the profile's ceiling (planner = 2048 on this 4 GiB GPU),
- the call uses `format=json` so small models return a parseable object, `think=false` for
  qwen3 models (reasoning traces are not part of the artifact).

### Explicit fallback, never silent fallback

A definition carries `fallback_profiles_json` (defaults: planner→specialist, specialist→
router_light, router_light→none). If the primary model call fails with a *recoverable* code
(`OLLAMA_UNAVAILABLE`, `MODEL_LOAD_FAILED`, `MODEL_NOT_FOUND`, `MODEL_NOT_GENERATIVE`,
`GENERATION_TIMEOUT`), the engine retries the same task on the declared fallback and writes
the fact into three places:

- `agent_task.output_json.fallback_note`,
- `agent_task.evidence_refs_json` (`fallback:<note>`),
- `final_report_json.warnings` ("planner failed (OLLAMA_UNAVAILABLE); using specialist").

No artifact is ever reported as produced by a model that did not produce it.

## Data model (36 tables; Phase 4 added 5)

| Table | Purpose |
|---|---|
| `agent_department` | 10 departments, enable flag, default profile, weekly budget |
| `agent_definition` | 111 agents: prompt, versions, profile + fallbacks, contract, tools, limits |
| `agent_task` | one row per unit of work: depth, role, state, tokens, artifact, evidence refs |
| `task_edge` | DAG edges (`depends` / `review` / `data`) between tasks |
| `agent_run` | per-call record: model, prompt/completion tokens, duration, finish reason |
| `agent_memory` | scoped memory (global/long_term/project/department/agent/task) with expiry |
| `agent_objective` | user objective: state, budgets, calls/tokens used, final report |
| `approval_request` | hash-bound, single-use human approvals (intent only in Phase 4) |
| `tool_registry` | 12 tools: schema, tier, required grant, timeout, enabled flag |
| `prompt_template` | versioned prompts, one row per agent version |

`agent_task` and `agent_objective` are the durable truth. The catalogue is a *seed*:
seeding is insert-only, so a human prompt edit or a disabled agent is never overwritten by
a restart or a catalogue update.

## What the agents cannot do

- run shell commands, write arbitrary files, or make arbitrary HTTP requests (no such tools exist),
- read secrets (`secrets.read` is explicitly forbidden and memory writes reject secret-shaped content),
- publish or approve anything (`publish.plan.generate` produces a *draft*; `human.approval.request`
  creates an approval request whose decision is only ever a human action),
- create objectives, agents, departments, tools or grants,
- exceed depth, call, revision, department, specialist or time budgets,
- pass unvalidated text to another agent: every hand-off is a validated Pydantic artifact.

## Failure semantics

| Failure | Result |
|---|---|
| Model unavailable | task failed with the provider code; declared fallback tried first |
| Malformed JSON | one repair attempt with the validation error, then `MALFORMED_AGENT_OUTPUT` |
| Contract violation | same path as malformed output; nothing is stored as a valid artifact |
| Tool error | task failed with the tool's error code, tool output recorded |
| Tool not granted | `TOOL_FORBIDDEN`, audited, task failed |
| Objective cancelled | running task marked cancelled, objective cancelled, report explains |
| Backend restart mid-run | `mark_interrupted` sweeps non-terminal objectives; `requeue_pending` resumes queued work |

Every one of those paths writes a `final_report_json` that names what happened, so an
objective can never look successful when it was not.

## GUI surface

Nine pages read only from real backend data (no placeholders, no sample rows):

1. **Workforce Overview** — counts, department table, recent objectives, live metrics.
2. **Organization** — 1 chief, 10 leaders, hierarchy tree with live task counts.
3. **Directory** — all 111 agents, searchable, filter by department/level/state.
4. **Agent detail** — profile, contract, tools, grants/forbidden list, prompt editor with
   version history, temperature, tokens, timeout, enable/disable.
5. **Task graph** — persisted DAG for one objective, node detail, stored artifact.
6. **Live monitor** — 2 s poll of running objectives, tasks, tokens, warnings.
7. **Activity log** — task events with agent, depth, state, error code, model.
8. **Playground** — run an objective, run one tool directly, write/query memory.
9. **Settings** — governor limits, memory retention, approvals, safety statement.

## Verification

- 358 backend tests (8 skipped), ruff clean, coverage on Phase 4 modules 86 %.
- One real end-to-end objective on this machine: 5 tasks, 9 model calls, 1110 tokens,
  succeeded, quality verdict `pass`, with a recorded planner fallback.
- The GUI pages were exercised against that real objective (screenshots in `docs/evidence/P4.md`).
