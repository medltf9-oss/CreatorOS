# Workforce GUI

Nine pages under **Agent workforce** in the desktop app. Every number on every page comes
from a backend endpoint; there is no sample data, no placeholder row, and no button that
does nothing. When a capability is unavailable the control is disabled and the page states
why.

| Page | File | Endpoints used |
|---|---|---|
| Workforce Overview | `pages/workforce/WorkforceOverview.tsx` | `/agents/registry/summary`, `/agents/metrics/summary`, `/agents/objectives` |
| Organization | `Organization.tsx` | `/agents/departments`, `/agents/registry` |
| Directory | `Directory.tsx` | `/agents/registry` |
| Agent detail | `AgentDetail.tsx` | `/agents/registry/{id}`, `/agents/tasks`, `PATCH /agents/registry/{id}`, `POST /agents/registry/{id}/state` |
| Task graph | `TaskGraph.tsx` | `/agents/objectives`, `/agents/objectives/{id}/graph`, `/agents/tasks/{id}` |
| Live monitor | `LiveMonitor.tsx` | `/agents/monitor` (2 s poll) |
| Activity log | `ActivityLog.tsx` | `/agents/activity` |
| Playground | `AgentPlayground.tsx` | `POST /agents/objectives`, `/agents/tools`, `POST /agents/tools/{id}/run`, `/agents/memory` |
| Settings | `AgentSettings.tsx` | `/ai/limits`, `/ai/governor`, `/agents/approvals`, `/agents/memory/stats` |

## Page notes

**Workforce Overview** — 111 agents / 10 departments / enabled counts, department table with
leader and live task counts, the five most recent objectives with state and verdict, and the
governor snapshot. A run in progress shows up here within one refresh.

**Organization** — the hierarchy as it is: 1 chief, 10 departments, each leader with its
specialist count. Clicking a department opens its leader; clicking the chief opens the chief.

**Directory** — all 111 definitions with search (name, id, mission), department filter,
level filter and "enabled only". Columns: agent, department, profile, contract, tool count,
config version, state.

**Agent detail** — the only page that writes agent configuration:
- prompt editor with version history (saving bumps `config_version` and creates a new
  `prompt_template` row rather than overwriting history),
- temperature, max tokens, timeout, max retries,
- model profile and declared fallbacks,
- tool grants and the explicit forbidden list (`shell.exec`, `filesystem.write`,
  `http.fetch`, `secrets.read`),
- recent tasks for that agent,
- enable/disable, which is also reflected on the Organization and Directory pages.

Validation is real: out-of-range temperature or token values are rejected by the API with
`422` and the field error is shown, not swallowed.

**Task graph** — the persisted DAG for one objective, laid out left to right by delegation
depth. Each node is coloured by state (succeeded / failed / running / assigned / ready /
pending / blocked / cancelled / waiting / review). Selecting a node shows its role, depth,
type, config version, model, tokens, state, error code, and the stored artifact as JSON —
the same bytes the compiler used.

**Live monitor** — polls every 2 s while open. Shows running and recent objectives with
progress counters, per-task state, tokens used, and warnings (including "planner failed …
using specialist"). Polling stops when the component unmounts.

**Activity log** — task-level events, newest first, with agent, department, depth, task type,
state, error code, model tag and duration. This is the page to open when an objective failed.

**Playground** — three real actions:
1. run an objective (`run_now`), blocking, with the compiled result and the deliverable
   rendered in place,
2. run one tool directly as a chosen agent — still authenticated, still validated, still
   audited; a denied tool shows the denial,
3. write and query memory in a chosen scope; a secret-shaped write is rejected with the
   pattern name, not the secret.

If the local model is unavailable, "run objective" returns `503 AI_UNAVAILABLE` with the
reason, and the page shows it rather than pretending to run.

**Settings** — governor limits (read + persisted updates), memory retention with enforce
button, approval queue with approve/reject/cancel, and a plain-language safety statement
of what agents can never do.

## States and honesty

- Buttons that would exceed a budget are disabled with the budget named.
- Any `4xx`/`5xx` shows the error envelope's `code` and correlation id, so it can be traced
  in `logs/backend/creatoros.log`.
- A failed objective is displayed as failed, with its error code and the failing agent —
  never as "completed with warnings" when the deliverable is empty.

## Verified against a real run

The screenshots in `docs/evidence/P4.md` were taken after a live objective
("Turn one long video into three platform-specific shorts about hooks") completed with
`succeeded · 5 tasks · 9 calls · 1110 tokens · quality verdict pass`, and the Task graph
page rendered that objective's stored DAG.

## Build

```
cd <PROJECT_ROOT>\app\desktop
pnpm typecheck
pnpm build     # 322.78 kB JS, 92.28 kB gzip
pnpm dev       # Electron + Vite against the local backend
```
