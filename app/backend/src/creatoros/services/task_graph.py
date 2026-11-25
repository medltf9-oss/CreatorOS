"""Persistent task DAG.

The graph lives in `agent_task` + `task_edge`, so it survives a crash, is visible in
the UI, and can be validated before a single model call is spent. Nothing here talks
to a model; it only decides what is allowed to run next.

Invariants enforced here:
- an objective's graph is a DAG (a cycle is rejected, never "handled");
- a task's depth matches its parent depth + 1 and never exceeds the budget;
- a task becomes READY only when every dependency succeeded;
- a failed dependency blocks downstream work instead of silently skipping it.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..db.enums import TASK_TERMINAL_STATES, TaskState
from ..db.models import AgentTask, TaskEdge

DEPENDENCY_EDGE = "depends"


class CycleDetected(ValueError):
    code = "TASK_GRAPH_CYCLE"


class DepthExceeded(ValueError):
    code = "DELEGATION_DEPTH_EXCEEDED"


@dataclass(slots=True)
class GraphEdge:
    from_task_id: str
    to_task_id: str
    edge_type: str = DEPENDENCY_EDGE


@dataclass(slots=True)
class GraphNode:
    id: str
    agent_id: str
    team_id: str
    state: str
    depth: int
    title: str | None = None
    task_type: str | None = None
    priority: int = 5
    parent_task_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    error_code: str | None = None


def validate_dag(nodes: list[str], edges: list[GraphEdge]) -> list[str]:
    """Return a topological order or raise CycleDetected."""
    known = set(nodes)
    adjacency: dict[str, set[str]] = defaultdict(set)
    indegree: dict[str, int] = {node: 0 for node in nodes}
    for edge in edges:
        if edge.from_task_id not in known or edge.to_task_id not in known:
            raise ValueError(
                f"edge references unknown task(s): {edge.from_task_id} -> {edge.to_task_id}"
            )
        if edge.to_task_id not in adjacency[edge.from_task_id]:
            adjacency[edge.from_task_id].add(edge.to_task_id)
            indegree[edge.to_task_id] += 1
    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for neighbour in sorted(adjacency[node]):
            indegree[neighbour] -= 1
            if indegree[neighbour] == 0:
                queue.append(neighbour)
    if len(order) != len(nodes):
        remaining = sorted(node for node in nodes if node not in set(order))
        raise CycleDetected(f"task graph contains a cycle among: {remaining}")
    return order


def maximum_depth(nodes: dict[str, GraphNode], edges: list[GraphEdge]) -> int:
    if not nodes:
        return 0
    depths = {task_id: node.depth for task_id, node in nodes.items()}
    parents: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        parents[edge.to_task_id].append(edge.from_task_id)
    order = validate_dag(list(nodes), edges)
    for task_id in order:
        if parents[task_id]:
            depths[task_id] = max(depths[parent] + 1 for parent in parents[task_id])
    return max(depths.values()) if depths else 0


class TaskGraph:
    """A read view over one objective's stored graph."""

    def __init__(self, session: Session, objective_id: str) -> None:
        self.session = session
        self.objective_id = objective_id

    # ------------------------------------------------------------------ load
    def tasks(self) -> list[AgentTask]:
        return list(
            self.session.scalars(
                sa.select(AgentTask)
                .where(AgentTask.objective_id == self.objective_id)
                .order_by(AgentTask.created_at, AgentTask.id)
            )
        )

    def edges(self) -> list[TaskEdge]:
        task_ids = sa.select(AgentTask.id).where(AgentTask.objective_id == self.objective_id)
        return list(
            self.session.scalars(
                sa.select(TaskEdge).where(
                    TaskEdge.from_task_id.in_(task_ids), TaskEdge.to_task_id.in_(task_ids)
                )
            )
        )

    def nodes(self) -> dict[str, GraphNode]:
        rows = self.tasks()
        dependency_map: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges():
            if edge.edge_type == DEPENDENCY_EDGE:
                dependency_map[edge.to_task_id].append(edge.from_task_id)
        return {
            row.id: GraphNode(
                id=row.id,
                agent_id=row.agent_id,
                team_id=row.team_id,
                state=str(row.state),
                depth=row.depth,
                title=row.title,
                task_type=row.task_type,
                priority=row.priority,
                parent_task_id=row.parent_task_id,
                depends_on=dependency_map.get(row.id, []),
                error_code=row.error_code,
            )
            for row in rows
        }

    # -------------------------------------------------------------- validate
    def validate(self, *, max_depth: int = 3) -> dict[str, Any]:
        nodes = self.nodes()
        edges = [GraphEdge(edge.from_task_id, edge.to_task_id, edge.edge_type) for edge in self.edges()]
        order = validate_dag(list(nodes), edges)
        depth = maximum_depth(nodes, edges)
        if depth > max_depth:
            raise DepthExceeded(f"graph depth {depth} exceeds the budget of {max_depth}")
        return {"task_count": len(nodes), "edge_count": len(edges), "max_depth": depth, "order": order}

    # ---------------------------------------------------------------- status
    def ready_task_ids(self) -> list[str]:
        state_by_id = {
            row.id: TaskState(row.state)
            for row in self.session.scalars(
                sa.select(AgentTask).where(AgentTask.objective_id == self.objective_id)
            )
        }
        dependency_map: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges():
            if edge.edge_type == DEPENDENCY_EDGE:
                dependency_map[edge.to_task_id].append(edge.from_task_id)
        ready: list[str] = []
        for task_id, state in state_by_id.items():
            if state not in (TaskState.PENDING, TaskState.READY):
                continue
            deps = dependency_map.get(task_id, [])
            if all(state_by_id.get(dep) is TaskState.SUCCEEDED for dep in deps):
                ready.append(task_id)
        priority = {
            row.id: row.priority
            for row in self.session.scalars(
                sa.select(AgentTask).where(AgentTask.objective_id == self.objective_id)
            )
        }
        return sorted(ready, key=lambda task_id: (-priority.get(task_id, 5), task_id))

    def blocked_task_ids(self) -> list[str]:
        """Pending tasks with at least one failed/cancelled dependency."""
        rows = self.tasks()
        state_by_id = {row.id: TaskState(row.state) for row in rows}
        dependency_map: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges():
            if edge.edge_type == DEPENDENCY_EDGE:
                dependency_map[edge.to_task_id].append(edge.from_task_id)
        blockers = {TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED}
        return [
            task_id
            for task_id, state in state_by_id.items()
            if state in (TaskState.PENDING, TaskState.READY)
            and any(state_by_id.get(dep) in blockers for dep in dependency_map.get(task_id, []))
        ]

    def progress(self) -> dict[str, Any]:
        rows = self.tasks()
        states: dict[str, int] = {}
        for row in rows:
            states[str(row.state)] = states.get(str(row.state), 0) + 1
        terminal = sum(count for state, count in states.items() if state in {str(s) for s in TASK_TERMINAL_STATES})
        return {
            "objective_id": self.objective_id,
            "task_count": len(rows),
            "states": states,
            "terminal": terminal,
            "remaining": len(rows) - terminal,
            "ready": len(self.ready_task_ids()),
            "blocked": len(self.blocked_task_ids()),
        }

    def as_dict(self) -> dict[str, Any]:
        nodes = self.nodes()
        return {
            "objective_id": self.objective_id,
            "nodes": [
                {
                    "task_id": node.id,
                    "agent_id": node.agent_id,
                    "team_id": node.team_id,
                    "state": node.state,
                    "depth": node.depth,
                    "title": node.title,
                    "task_type": node.task_type,
                    "priority": node.priority,
                    "parent_task_id": node.parent_task_id,
                    "depends_on": node.depends_on,
                    "error_code": node.error_code,
                }
                for node in nodes.values()
            ],
            "edges": [
                {"from": edge.from_task_id, "to": edge.to_task_id, "type": edge.edge_type}
                for edge in self.edges()
            ],
        }
