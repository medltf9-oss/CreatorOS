# Task Graph

The DAG is not an in-memory plan that disappears when the process dies. It is
`agent_task` rows joined by `task_edge` rows in SQLite, which means the graph is
inspectable while it runs, after a crash, and in the GUI (Task graph page).

## Schema

```sql
agent_task(id, objective_id, parent_task_id, agent_id, department_id, depth, task_type,
           state, priority, input_json, output_json, error_code, error, model_tag,
           config_version, tokens_used, duration_ms, evidence_refs_json, created_at, finished_at)

task_edge(from_task_id, to_task_id, edge_type, created_at, updated_at)
          PRIMARY KEY (from_task_id, to_task_id, edge_type)
          FOREIGN KEY(from_task_id) REFERENCES agent_task(id) ON DELETE CASCADE
          FOREIGN KEY(to_task_id)   REFERENCES agent_task(id) ON DELETE CASCADE
```

`edge_type` values: `depends` (hard ordering), `review` (review of a subject), `data`
(an artifact is passed downstream). Only `depends` gates `ready_task_ids()`; the others
are recorded relationships and are drawn in the GUI.

## Validation before any model call

`TaskGraph.validate()` runs on load and raises rather than "handles":

| Check | Failure |
|---|---|
| every edge endpoint exists in the graph | `TaskGraphError` (`TASK_GRAPH_INVALID`) |
| the dependency edges form a DAG | `CycleDetected` (`TASK_GRAPH_CYCLE`) |
| every task's `depth == parent.depth + 1` | `DepthExceeded` (`DELEGATION_DEPTH_EXCEEDED`) |
| `depth <= max_depth` (3, hard 4) | `DepthExceeded` |
| no task depends on itself | `CycleDetected` |

The engine never calls the model first and validates later: an invalid graph costs zero
tokens and the objective fails with a precise error code.

## Readiness rules

```
ready   ⇔ state == pending ∧ every `depends` predecessor == succeeded
blocked ⇔ some `depends` predecessor ∈ {failed, cancelled, blocked, skipped}
waiting ⇔ state == waiting (review tasks whose subject is not complete yet)
```

Consequences, deliberately chosen:

- A failed department **blocks** its downstream work instead of skipping it. The objective
  report lists the blocked tasks and the failing ancestor; nothing is reported as done.
- Ready tasks are processed one at a time (`concurrency = 1`) in creation order, because
  the GPU can serve exactly one resident model.
- Independent department tasks have **no edges between them**, so one department cannot
  delay another, and the graph is shallow by design (depth 3 total).

## Shape produced for a real run

```
chief_orchestrator                 depth 0   plan
├── strategic_intelligence         depth 1   plan
│   └── audience_strategy_agent    depth 2   analysis
└── quality_governance             depth 1   plan
    └── metadata_quality_agent     depth 2   review
```

That is the persisted graph from the verified live objective: 5 tasks, 9 model calls,
1110 tokens, `succeeded`, verdict `pass`.

## Progress and inspection

`progress()` returns terminal / total per objective; the GUI shows it as a bar and the
API exposes it at `GET /agents/objectives/{id}/graph`:

```json
{
  "objective_id": "01M80RY6Q3JGJ2HM5NABS1FMB7",
  "state": "succeeded",
  "nodes": [{"id": "...", "agent_id": "chief_orchestrator", "depth": 0, "state": "succeeded", ...}],
  "edges": [{"from_task_id": "...", "to_task_id": "...", "edge_type": "depends"}],
  "progress": {"terminal": 5, "total": 5},
  "depth": 2
}
```

## Cancellation and retry

- Cancelling an objective marks `pending`/`ready`/`assigned`/`running` tasks `cancelled`,
  which automatically blocks their dependants with an explicit reason.
- `resume` requeues non-terminal tasks (`pending` again) and keeps every succeeded artifact,
  so a retry costs only the work that actually failed.
- Deleting a task is impossible by design: the graph is an audit record. Rows are
  cancelled, never removed (except by `ON DELETE CASCADE` when the whole objective is
  deleted by a future retention job).

## Tests

`tests/test_agent_task_graph.py` (12 tests): topological order, self-cycle, two-node cycle,
three-node cycle, missing endpoint, depth overflow, readiness with mixed states, blocked
propagation, progress counters, `as_dict` shape, priority ordering, empty graph.
