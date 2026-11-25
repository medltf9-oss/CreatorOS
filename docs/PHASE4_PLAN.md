# Phase 4 implementation plan — the 111-agent orchestration engine

Status: in progress · Target: `<PROJECT_ROOT>` · Baseline: commit `9ae38e1` (Phase 3)

This plan follows the Phase 4 brief and the existing architecture. Nothing from
Phases 1–3 is rewritten: the agent engine reuses the Ollama gateway, the resource
governor, the durable queue, the database and the Electron/React shell.

## What already exists (verified before writing code)

| Piece | State |
| --- | --- |
| Tables | 33, incl. `agent_definition`, `agent_task`, `task_edge`, `agent_run`, `agent_memory`, `prompt_template`, `tool_registry`, `approval_record`, `queue_job` |
| Queue | `services/queue.py` — atomic claim, lease + heartbeat, backoff + jitter, dead letter, idempotency |
| Worker | `workers/runner.py` — handler registry, background thread |
| Inference | `services/ai.py` (`AiService.generate/chat/stream`) + `integrations/ollama` gateway |
| Governor | `services/governor.py` — 1-slot admission, telemetry, pressure |
| Router | `services/models.py` — six profiles, fallback chain with reasons |
| API | 60 routes, token auth, one error envelope, correlation ids |
| Desktop | Sidebar shell + Overview / AI infrastructure / Model Lab / Playground / Settings |
| Tests | 202 passing, 8 live (opt-in), 85 % coverage |

## Gaps this phase must close

1. `agent_definition` has no departments, system prompt, task types, fallback
   profiles, restricted tools, token budget, temperature, hierarchy level or
   config version.
2. `agent_task` has no department, task type, config version, model, tokens,
   duration or objective link; `TaskState` lacks pending/ready/assigned/waiting/review.
3. `agent_memory` has no project/task attribution; `MemoryScope` lacks
   global/department/agent.
4. No objective table, no approval request table, no tool executor, no
   orchestration engine, no agent API, no agent UI.

## Design decisions

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | **Persist the organisation, don't hardcode it in the runtime.** 111 definitions are seeded from a Python catalogue into `agent_definition`; the runtime always reads the database. | Enables editing, disabling and adding agents without code changes; execution keeps the config version it started with. |
| D2 | **One engine, three roles.** `chief` → `dept leader` → `specialist` are the same code path with different `hierarchy_level`, prompt and budget. | Avoids three divergence-prone implementations, satisfies depth limit 3. |
| D3 | **The queue stays the only executor.** An objective enqueues one `agents.objective` job; the handler drives the graph. Ready *tasks* are scheduled inside that job by a deterministic selector. | Reuses leases/retries/dead-letter; no second queue. |
| D4 | **Every inference goes through `AiService.generate`.** | Gets governor admission, `ai_execution` rows, cancellation and honest failures for free. |
| D5 | **Structured output is mandatory.** Agents must return JSON matching a pydantic model; one repair attempt, then `MALFORMED_AGENT_OUTPUT`. | Model text is never executed as instructions and never parsed loosely. |
| D6 | **Tools are a closed allow-list.** 12 registered tools, each a plain Python function with a strict input schema, no shell, no arbitrary paths, no HTTP, no secrets. | Least privilege per agent, audited, testable. |
| D7 | **Approvals are hash-bound and single-use.** A decision binds `action + target + payload_hash`; a changed payload invalidates it. | Same invariant as publishing (INV-9); Phase 4 records intent but performs no external action. |
| D8 | **Memory is scoped and scanned.** Five scopes, per-scope size caps, TTL, and a secret detector that rejects tokens/keys/passwords. | Prevents leakage across projects and credential storage. |
| D9 | **No fake progress.** The UI polls real task rows and shows counts, states and timings; no invented percentages. | Product rule from Phase 1. |

## Stages (each one ends green)

| Stage | Work | Verification |
| --- | --- | --- |
| S1 | Enums (`TaskState`, `MemoryScope`, `ObjectiveState`, `ApprovalState`), new tables (`agent_department`, `agent_objective`, `approval_request`), column extensions, reversible migration | `alembic upgrade head`, `downgrade -1`, `upgrade head`, schema dump |
| S2 | Agent catalogue: 10 departments, 111 definitions (1 chief + 10 leaders + 100 specialists), idempotent seeding, config versioning, enable/disable | registry tests (counts, IDs, dept mapping, idempotency, version bump) |
| S3 | Structured contracts (`AgentArtifact`, `TaskResult`, `ReviewVerdict`, `FinalReport`), safe JSON extraction, schema rejection | parsing tests (fences, prose, wrong types, truncated) |
| S4 | Tool registry executor: 12 tools, permission + risk + timeout + audit, secret-free | permission/denied/schema/timeout/audit tests |
| S5 | Memory service: scope isolation, caps, TTL, secret rejection, retrieval | memory tests |
| S6 | Approval service: create/approve/reject/expire/hash binding | approval tests |
| S7 | Task graph service: dependency validation (cycles), ready selection, transitions, cancellation, recovery | graph tests |
| S8 | Orchestration engine: objective → chief plan → leader plan → specialists → leader review → chief compile, with budgets | orchestration tests with mocked inference |
| S9 | Queue handler `agents.objective` + recovery on boot | queue/restart test |
| S10 | API: agents, objectives, tasks, monitoring, memory, tools, approvals (~30 routes), `/version` → 0.4.0 | API tests |
| S11 | Desktop: **Agent Workforce** section, 9 pages | typecheck, build, live screenshots |
| S12 | Docs (10 files), ADR-17/18/19, README, evidence | review |
| S13 | Full verification: tests, ruff, tsc, build, diagnose, migration roundtrip, live objective, restart, security | command log + report |

## Budgets and limits (defaults, configurable within validated bounds)

| Limit | Default | Range |
| --- | --- | --- |
| Hierarchy depth | 3 | 2–4 |
| Agent calls per objective | 30 | 3–60 |
| Revisions per task | 2 | 0–3 |
| Concurrent inference | 1 (governor) | 1–4 |
| Objective wall clock | 900 s | 60–3600 |
| Departments per objective | 3 | 1–5 |
| Specialists per department task | 3 | 1–6 |
| Memory chars per scope | 200 000 | 10 000–1 000 000 |

## Out of scope (documented, not faked)

- Any external platform action: Phase 4 prepares and records approvals only.
- Video processing (Phase 5), OAuth (Phase 6), publishing (Phase 7).
- 111 concurrent processes: one governor slot, 111 registered roles.
- Autonomous background objectives: every objective starts from an explicit user action.
