"""Task graph tests: DAG validation, readiness, blocking, depth, persistence."""
from __future__ import annotations

import pytest

from creatoros.db.enums import ObjectiveState, TaskState
from creatoros.db.models import AgentObjective, AgentTask, TaskEdge
from creatoros.services.task_graph import (
    CycleDetected,
    DepthExceeded,
    GraphEdge,
    TaskGraph,
    maximum_depth,
    validate_dag,
)


def test_dag_topological_order():
    order = validate_dag(["a", "b", "c"], [GraphEdge("a", "b"), GraphEdge("b", "c")])
    assert order == ["a", "b", "c"]


def test_cycle_is_rejected():
    with pytest.raises(CycleDetected):
        validate_dag(["a", "b", "c"], [GraphEdge("a", "b"), GraphEdge("b", "c"), GraphEdge("c", "a")])


def test_self_edge_is_a_cycle():
    with pytest.raises(CycleDetected):
        validate_dag(["a"], [GraphEdge("a", "a")])


def test_unknown_edge_endpoint_rejected():
    with pytest.raises(ValueError):
        validate_dag(["a"], [GraphEdge("a", "ghost")])


def test_duplicate_edges_do_not_break_ordering():
    order = validate_dag(["a", "b"], [GraphEdge("a", "b"), GraphEdge("a", "b")])
    assert order == ["a", "b"]


def test_maximum_depth_of_a_chain():
    from creatoros.services.task_graph import GraphNode

    nodes = {
        "a": GraphNode(id="a", agent_id="x", team_id="t", state="succeeded", depth=0),
        "b": GraphNode(id="b", agent_id="x", team_id="t", state="pending", depth=1),
        "c": GraphNode(id="c", agent_id="x", team_id="t", state="pending", depth=2),
    }
    edges = [GraphEdge("a", "b"), GraphEdge("b", "c")]
    assert maximum_depth(nodes, edges) == 2


@pytest.fixture()
def objective(session):
    from creatoros.services.registry import AgentRegistry

    AgentRegistry(session).ensure_seeded()
    row = AgentObjective(objective="Test objective for the graph", state=ObjectiveState.RUNNING, limits_json={})
    session.add(row)
    session.flush()
    return row


def _task(session, objective, key, state=TaskState.PENDING, depth=0, priority=5, agent="hook_writer_agent"):
    from creatoros.agents.catalog import agent_specs

    spec = next(item for item in agent_specs() if item["agent_id"] == agent)
    task = AgentTask(
        agent_id=agent,
        team_id=spec["department"],
        objective_id=objective.id,
        depth=depth,
        priority=priority,
        state=state,
        title=key,
        task_type="test",
        input_json={},
    )
    session.add(task)
    session.flush()
    task.root_task_id = task.root_task_id or task.id
    return task


def test_ready_set_respects_dependencies_and_priority(session, objective):
    first = _task(session, objective, "first", priority=1)
    second = _task(session, objective, "second", priority=9, state=TaskState.SUCCEEDED)
    third = _task(session, objective, "third", priority=5)
    session.add(TaskEdge(from_task_id=first.id, to_task_id=third.id, edge_type="depends"))
    session.add(TaskEdge(from_task_id=second.id, to_task_id=third.id, edge_type="depends"))
    session.flush()

    graph = TaskGraph(session, objective.id)
    assert graph.ready_task_ids() == [first.id]

    first.state = TaskState.SUCCEEDED
    session.flush()
    assert graph.ready_task_ids() == [third.id]


def test_failed_dependency_blocks_downstream(session, objective):
    upstream = _task(session, objective, "up", state=TaskState.FAILED)
    downstream = _task(session, objective, "down")
    session.add(TaskEdge(from_task_id=upstream.id, to_task_id=downstream.id, edge_type="depends"))
    session.flush()
    graph = TaskGraph(session, objective.id)
    assert graph.ready_task_ids() == []
    assert graph.blocked_task_ids() == [downstream.id]


def test_progress_counts_terminal_states(session, objective):
    _task(session, objective, "a", state=TaskState.SUCCEEDED)
    _task(session, objective, "b", state=TaskState.FAILED)
    _task(session, objective, "c", state=TaskState.PENDING)
    progress = TaskGraph(session, objective.id).progress()
    assert progress["task_count"] == 3
    assert progress["terminal"] == 2
    assert progress["remaining"] == 1
    assert progress["states"]["pending"] == 1


def test_graph_validation_reports_structure(session, objective):
    parent = _task(session, objective, "parent")
    child = _task(session, objective, "child", depth=1)
    session.add(TaskEdge(from_task_id=parent.id, to_task_id=child.id, edge_type="depends"))
    session.flush()
    summary = TaskGraph(session, objective.id).validate(max_depth=3)
    assert summary["task_count"] == 2
    assert summary["edge_count"] == 1
    assert summary["max_depth"] == 1


def test_graph_validation_enforces_depth_budget(session, objective):
    a = _task(session, objective, "a", depth=0)
    b = _task(session, objective, "b", depth=1)
    c = _task(session, objective, "c", depth=2)
    d = _task(session, objective, "d", depth=3)
    for left, right in ((a, b), (b, c), (c, d)):
        session.add(TaskEdge(from_task_id=left.id, to_task_id=right.id, edge_type="depends"))
    session.flush()
    with pytest.raises(DepthExceeded):
        TaskGraph(session, objective.id).validate(max_depth=2)


def test_graph_serialisation_shape(session, objective):
    parent = _task(session, objective, "parent")
    child = _task(session, objective, "child", depth=1)
    session.add(TaskEdge(from_task_id=parent.id, to_task_id=child.id, edge_type="depends"))
    session.flush()
    payload = TaskGraph(session, objective.id).as_dict()
    assert payload["objective_id"] == objective.id
    assert len(payload["nodes"]) == 2
    assert payload["edges"][0]["type"] == "depends"
    assert child.id in [node["task_id"] for node in payload["nodes"]]
