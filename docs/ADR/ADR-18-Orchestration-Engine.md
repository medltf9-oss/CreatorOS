# ADR-18 — One orchestration engine, contracts at every hand-off

- **Status:** accepted
- **Date:** 2026-11-22
- **Phase:** 4
- **Related:** ADR-04 (SQLite/Alembic), ADR-08 (durable queue), ADR-15 (gateway),
  ADR-16 (governor), ADR-17 (model exclusivity), `docs/AGENT_ARCHITECTURE.md`

## Context

The brief asks for 111 agents: 1 chief, 10 department leaders, 100 specialists, organised as
User → Chief → Leader → Specialist → Tools → Ollama, with a persistent task DAG, typed
inter-agent communication, scoped memory, about a dozen secure tools, hash-bound approvals
and a 9-page GUI.

Three implementation strategies were available:

1. a separate agent runtime per role (chief runtime, leader runtime, specialist runtime),
2. a graph framework dependency (LangGraph-style) with its own state model,
3. one engine that reads role configuration from the database.

Strategy 1 triples the failure modes (budget overflow, orphan tasks, contract violations)
and guarantees the three paths drift. Strategy 2 adds a runtime whose state model would
have to be reconciled with the durable queue and the SQL schema that phases 2–3 already
verify, and it hides scheduling behind a library on a machine where the binding constraint
is a single GPU slot.

There is also a correctness problem specific to small local models: they produce prose when
asked for structure, they repeat tokens under pressure, and they happily invent fields. A
free-form "agent graph" makes those failures invisible until the compiled result is wrong.

## Decision

1. **One engine, three roles.** `services/orchestrator.py` holds a single `Orchestrator`.
   Chief, leader and specialist differ only by the `AgentDefinition` row, the task `depth`,
   the task type and the output contract. There is no second code path to keep in sync.

2. **The database is the truth; the catalogue is a seed.** `agents/catalog.py` declares the
   111 agents; seeding is insert-only, so a human prompt edit, a disabled agent or a changed
   temperature is never overwritten by a restart or a catalogue bump. Each task records the
   `config_version` it started with, so an artifact can be traced to the exact prompt.

3. **Persistence on the existing queue.** An objective is a `queue_job` (`agents.objective`)
   executed by the Phase 2 worker; the DAG lives in `agent_task` + `task_edge`. No new
   scheduler, no in-memory graph, no framework. Crash recovery, idempotency and lease
   handling are the ones already tested in Phase 2.

4. **Typed communication only.** Every hand-off is a Pydantic model
   (`AgentTaskPayload`, `AgentArtifact`, `AgentResult`, `DelegationRequest/Response`,
   `ReviewRequest/Result`, `FinalObjectiveResult`). Model text is parsed into an artifact and
   validated against the contract's required fields; it is never executed, never pasted into
   another agent's prompt as instructions, and never stored as a valid result on trust.

5. **Structured output is mandatory, with exactly one repair.** Parse → validate → on failure
   one repair call that includes the validation error → on failure `MALFORMED_AGENT_OUTPUT`.
   No silent retries, no "close enough" artifacts. `coerce_artifact` normalises
   *representation* (key aliases, scalar → list, section wrapping) and never invents values.

6. **Deterministic tool selection.** The engine maps task type → at most three tool ids,
   intersects with the agent's grants, executes them *before* the model call and passes the
   result in as data. The model cannot request a tool, so a hallucinated tool call is
   impossible rather than discouraged.

7. **Hierarchical budgets, enforced.** depth 3 (hard 4), 30 calls (hard 60), 2 revisions
   (hard 3), concurrency 1, 900 s wall clock, 3 departments, 3 specialists per department.
   Exceeding a budget truncates with a recorded warning; it never silently reshapes the
   answer.

8. **Quality governance is a sibling, not an ancestor.** Review tasks are scheduled after
   production completes and depend only on the results they review, so a slow or failing
   reviewer cannot starve the departments that produce content — and the reviewer is always a
   different department from the author.

9. **Approvals are hash-bound and single-use, and intent-only in Phase 4.** An approval
   stores `sha256(canonical payload)`; use requires re-supplying the payload, re-hashing, and
   stamps `consumed_at`. No Phase 4 code performs an external action on an approval; the
   capability map states `human_approvals: intent_only`.

10. **Late-bound AI service.** `services/ai_runtime.py` holds the factory used by the
    orchestrator, set in `main.py`, which avoids a `main` ↔ `orchestrator` circular import and
    lets read-only registry/memory endpoints work with no inference available.

## Consequences

- Adding an agent is a catalogue entry plus a seed; adding a role *type* would require engine
  work, which is the correct friction.
- Because the engine owns tool choice, an agent cannot discover a new tool mid-task. That is
  the intended restriction, not a limitation to fix.
- Because review is a sibling, an objective can end `partial` with a deliverable and a
  `needs_revision` verdict. That is more honest than pretending the work is finished.
- Everything is inspectable with SQL: the graph, the artifacts, the tokens, the model used
  per task, the fallbacks, the errors. No library state to unwind.

## Alternatives rejected

| Alternative | Why rejected |
|---|---|
| Separate runtime per role | triples failure modes, guaranteed drift |
| LangGraph-style dependency | duplicates the queue and the schema; hides scheduling on a single-GPU box |
| Free-form agent chat / free tool choice | untraceable, unvalidated, prompt-injectable |
| In-memory DAG | lost on crash, invisible to the GUI, unverifiable |
| Auto-retry until valid | hides capability limits and burns the wall clock |
| Auto-escalating concurrency | OOM on 3.3 GiB VRAM (ADR-16, ADR-17) |

## Verification

- 358 backend tests (8 skipped), 87 % coverage across the Phase 4 modules, ruff clean.
- Real objective on this machine: 5 tasks, 9 model calls, 1110 tokens, `succeeded`,
  verdict `pass`, with a recorded planner fallback.
- Migration roundtrip `base → head` clean, 36 tables, no dangling foreign keys.
