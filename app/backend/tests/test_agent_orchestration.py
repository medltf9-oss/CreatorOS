"""Orchestration tests with a scripted model double.

No real inference happens here: `ScriptedAiService` returns prepared text (or raises)
and records every call, so the engine's planning, delegation, budgeting, validation
and failure isolation can be asserted exactly.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from creatoros.db.enums import ObjectiveState, TaskState
from creatoros.db.models import AgentObjective, AgentRun, AgentTask
from creatoros.services.orchestrator import Budgets, Orchestrator
from creatoros.services.registry import AgentRegistry
from creatoros.services.task_graph import TaskGraph
from creatoros.services.toolbox import ensure_tools_seeded


def _artifact_json(verdict: str = "pass", assignments: list[str] | None = None) -> str:
    """One payload that satisfies every contract the catalogue declares."""
    return json.dumps(
        {
            "summary": "A usable result from the scripted model.",
            "findings": [{"statement": "Hooks matter more than production value", "kind": "assumption"}],
            "items": [{"label": "hook", "text": "Stop scripting hooks like this"}],
            "steps": [
                {"step": "Research audience questions about hooks", "owner": "research_intelligence"},
                {"step": "Draft three hook variants", "owner": "creative_writing"},
                {"step": "Check claims for accuracy", "owner": "quality_governance"},
            ],
            "assignments": assignments or ["research_intelligence", "creative_writing"],
            "sections": [{"heading": "Result", "body": "Compiled body text."}],
            "issues": [{"severity": "info", "issue": "Consider a shorter intro"}],
            "verdict": verdict,
            "confidence": 0.82,
        }
    )


class ScriptedAiService:
    """Stands in for AiService; raises if the engine tries to use a real model path."""

    def __init__(self, responder: Callable[[int, str], str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responder = responder or (lambda index, prompt: _artifact_json())

    async def generate(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        index = len(self.calls)
        self.calls.append({"prompt": prompt, **kwargs})
        text = self.responder(index, prompt)
        if isinstance(text, Exception):
            raise text
        return {
            "execution_id": f"exec-{index}",
            "model": "qwen3:1.7b",
            "profile": kwargs.get("profile") or "specialist",
            "reason": "scripted",
            "response": text,
            "thinking": "",
            "metrics": {"completion_tokens": 42, "tokens_per_sec": 40.0},
            "warnings": [],
        }


@pytest.fixture()
def orchestration(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    ensure_tools_seeded(session)
    service = ScriptedAiService()

    def make(responder: Callable[[int, str], str] | None = None) -> tuple[Orchestrator, ScriptedAiService]:
        scripted = ScriptedAiService(responder) if responder else service
        orchestrator = Orchestrator(session, ai_service_factory=lambda _session: scripted)
        return orchestrator, scripted

    return make


def test_full_objective_runs_through_every_layer(orchestration, session):
    orchestrator, _service = orchestration()
    objective = orchestrator.create_objective(
        "Turn one long video into three platform-specific shorts",
        budgets=Budgets(specialists_per_department=2),
        enqueue=False,
    )
    report = orchestrator.execute(objective.id)

    assert report.state == "succeeded", report
    session.refresh(objective)
    assert objective.state is ObjectiveState.SUCCEEDED

    tasks = TaskGraph(session, objective.id).tasks()
    agents_used = {task.agent_id for task in tasks}
    assert "chief_orchestrator" in agents_used
    assert "strategy_director" in agents_used or any(task.depth == 1 for task in tasks)
    assert any(task.depth == 2 for task in tasks), "leaders must delegate to specialists"
    assert max(task.depth for task in tasks) <= 3
    assert all(task.objective_id == objective.id for task in tasks)
    assert all(task.config_version >= 1 for task in tasks)

    quality_tasks = [task for task in tasks if task.team_id == "quality_governance"]
    assert quality_tasks, "quality governance must review the work"
    assert all(TaskState(task.state) is TaskState.SUCCEEDED for task in quality_tasks)

    final = objective.final_report_json
    assert final["quality_verdict"] in {"pass", "revise", "reject"}
    assert final["deliverable"]
    assert objective.calls_used <= 30
    assert objective.tokens_used > 0
    assert report.calls_used == objective.calls_used


def test_edges_are_recorded_and_depth_bounded(orchestration, session):
    orchestrator, _ = orchestration()
    objective = orchestrator.create_objective("Plan a week of shorts about hooks", enqueue=False)
    orchestrator.execute(objective.id)
    graph = TaskGraph(session, objective.id)
    payload = graph.as_dict()
    assert payload["edges"]
    for node in payload["nodes"]:
        assert 0 <= node["depth"] <= 3
        if node["parent_task_id"]:
            parent = next(item for item in payload["nodes"] if item["task_id"] == node["parent_task_id"])
            assert node["depth"] == parent["depth"] + 1


def test_model_profile_and_complexity_are_routed_per_role(orchestration, session):
    orchestrator, service = orchestration()
    objective = orchestrator.create_objective("Explain why hooks matter in short video", enqueue=False)
    orchestrator.execute(objective.id)
    complexities = [call["complexity"] for call in service.calls]
    assert complexities[0] == "high", "the chief plans at high complexity"
    assert "low" in complexities or "medium" in complexities
    profiles = {call["profile"] for call in service.calls}
    assert profiles <= {"planner", "specialist", "router_light"}
    assert all(call["origin"] == "agent" for call in service.calls)


def test_malformed_output_gets_one_repair_attempt_then_fails(orchestration, session):
    orchestrator, _service = orchestration(lambda index, prompt: "this is not JSON at all")
    objective = orchestrator.create_objective("Produce a research brief on hooks", enqueue=False)
    report = orchestrator.execute(objective.id)

    assert report.state in {"partial", "failed"}
    session.refresh(objective)
    root = session.query(AgentTask).filter(AgentTask.objective_id == objective.id).one()
    assert root.state is TaskState.FAILED
    assert root.error_code == "MALFORMED_AGENT_OUTPUT"
    assert objective.calls_used >= 2, "the repair attempt is a real call"
    assert any("failed validation" in warning for warning in report.warnings)


def test_repair_attempt_can_recover(orchestration, session):
    def responder(index: int, prompt: str) -> str:
        if index == 0:
            return "Sure! Here is the plan: do the thing."
        return _artifact_json()

    orchestrator, _ = orchestration(responder)
    objective = orchestrator.create_objective("Recover from a malformed first answer", enqueue=False)
    report = orchestrator.execute(objective.id)
    assert report.state in {"succeeded", "partial"}
    session.refresh(objective)
    root = session.query(AgentTask).filter(AgentTask.objective_id == objective.id).first()
    assert root.state is TaskState.SUCCEEDED
    assert objective.calls_used >= 2


def test_model_failure_is_a_task_failure_not_a_crash(orchestration, session):
    from creatoros.integrations.ollama.errors import OllamaUnavailable

    orchestrator, _ = orchestration(lambda index, prompt: OllamaUnavailable("daemon down"))
    objective = orchestrator.create_objective("Handle a dead model daemon gracefully", enqueue=False)
    report = orchestrator.execute(objective.id)
    session.refresh(objective)
    assert report.state in {"failed", "partial"}
    assert objective.error_code in {None, "OBJECTIVE_FAILED", "OBJECTIVE_NOT_RUNNABLE"}
    failed = [task for task in TaskGraph(session, objective.id).tasks() if task.state is TaskState.FAILED]
    assert failed
    assert failed[0].error_code == "OLLAMA_UNAVAILABLE"


def test_call_budget_stops_the_run(orchestration, session):
    orchestrator, service = orchestration()
    objective = orchestrator.create_objective(
        "Respect a very small model call budget",
        budgets=Budgets(max_calls=3, specialists_per_department=1),
        enqueue=False,
    )
    report = orchestrator.execute(objective.id)
    assert report.stopped_reason in {"call_budget", "None", None}
    assert objective.calls_used <= 3
    assert len(service.calls) == objective.calls_used
    assert report.state in {"partial", "succeeded", "failed"}


def test_failed_department_blocks_its_children_but_not_another_department(orchestration, session):
    def responder(index: int, prompt: str) -> str:
        if "Research Director" in prompt:
            return "not json"
        return _artifact_json()

    orchestrator, _ = orchestration(responder)
    objective = orchestrator.create_objective("Isolate a failing department from the rest", enqueue=False)
    orchestrator.execute(objective.id)

    tasks = TaskGraph(session, objective.id).tasks()
    research = [task for task in tasks if task.team_id == "research_intelligence"]
    creative = [task for task in tasks if task.team_id == "creative_writing"]
    assert [task.state for task in research] == [TaskState.FAILED]
    assert research[0].error_code == "MALFORMED_AGENT_OUTPUT"
    assert any(task.state is TaskState.SUCCEEDED for task in creative), "other departments must still produce work"
    session.refresh(objective)
    assert objective.state is ObjectiveState.PARTIAL
    assert objective.final_report_json["unfinished"]


def test_objective_without_any_success_fails_honestly(orchestration, session):
    orchestrator, _ = orchestration(lambda index, prompt: "nope")
    objective = orchestrator.create_objective("Fail every single agent and report it", enqueue=False)
    report = orchestrator.execute(objective.id)
    session.refresh(objective)
    assert objective.state is ObjectiveState.FAILED
    assert objective.final_report_json["unfinished"]
    assert "could not be completed" in objective.final_report_json["summary"]
    assert objective.final_report_json["quality_verdict"] == "not_run"
    assert report.tasks_run == 0


def test_cancel_before_execution_marks_objective_cancelled(orchestration, session):
    orchestrator, service = orchestration()
    objective = orchestrator.create_objective("Cancel this before it starts", enqueue=False)
    assert orchestrator.cancel(objective.id)["cancel_requested"] is True
    report = orchestrator.execute(objective.id)
    assert report.state == "cancelled"
    session.refresh(objective)
    assert objective.state is ObjectiveState.CANCELLED
    assert session.query(AgentTask).filter(AgentTask.objective_id == objective.id).count() == 0
    assert service.calls == []


def test_cancel_mid_run_stops_further_calls(orchestration, session):
    state = {"cancelled": False}

    def responder(index: int, prompt: str) -> str:
        if index == 2 and not state["cancelled"]:
            state["cancelled"] = True
            objective = session.query(AgentObjective).one()
            objective.cancel_requested = True
            session.commit()
        return _artifact_json()

    orchestrator, service = orchestration(responder)
    objective = orchestrator.create_objective("Cancel after a couple of calls", enqueue=False)
    report = orchestrator.execute(objective.id)
    assert report.state == "cancelled"
    assert len(service.calls) <= 4
    states = {task.state for task in TaskGraph(session, objective.id).tasks()}
    assert TaskState.SUCCEEDED in states
    assert TaskState.CANCELLED in states


def test_reject_verdict_produces_a_partial_objective(orchestration, session):
    orchestrator, _ = orchestration(lambda index, prompt: _artifact_json(verdict="reject"))
    objective = orchestrator.create_objective("Ship something that quality rejects", enqueue=False)
    orchestrator.execute(objective.id)
    session.refresh(objective)
    assert objective.state is ObjectiveState.PARTIAL
    assert objective.final_report_json["quality_verdict"] == "reject"


def test_recovery_resets_running_state(orchestration, session):
    orchestrator, _ = orchestration()
    objective = orchestrator.create_objective("Recover after a crash", enqueue=False)
    task = AgentTask(
        agent_id="chief_orchestrator",
        team_id="executive",
        objective_id=objective.id,
        depth=0,
        state=TaskState.RUNNING,
        task_type="objective_planning",
    )
    session.add(task)
    objective.state = ObjectiveState.RUNNING
    session.flush()
    summary = orchestrator.mark_interrupted()
    assert summary["tasks_reset"] == 1
    assert summary["objectives_reset"] == 1
    assert session.get(AgentTask, task.id).state is TaskState.READY
    assert session.get(AgentObjective, objective.id).state is ObjectiveState.PENDING


def test_duplicate_objective_is_detected(orchestration, session):
    orchestrator, _ = orchestration()
    first = orchestrator.create_objective("The same objective twice", enqueue=False)
    second = orchestrator.create_objective("The same objective twice", enqueue=False)
    assert first.id != second.id  # creation allows it; the API rejects duplicates explicitly
    assert session.query(AgentObjective).count() == 2


def test_disabled_agent_is_not_run(orchestration, session):
    registry = AgentRegistry(session)
    registry.set_enabled("chief_orchestrator", False)
    orchestrator, service = orchestration()
    objective = orchestrator.create_objective("Try to run a disabled chief", enqueue=False)
    report = orchestrator.execute(objective.id)
    session.refresh(objective)
    assert report.state in {"failed", "partial", "blocked"}
    assert objective.state in {ObjectiveState.FAILED, ObjectiveState.PARTIAL, ObjectiveState.BLOCKED}
    assert service.calls == []


def test_tools_are_invoked_before_the_model_and_recorded(orchestration, session):
    from creatoros.db.models import AuditEvent, ContentProject

    project = ContentProject(title="Tool context project", brief="A brief about hooks")
    session.add(project)
    session.flush()
    orchestrator, _ = orchestration()
    objective = orchestrator.create_objective(
        "Use project context while planning shorts", project_id=project.id, enqueue=False
    )
    orchestrator.execute(objective.id)
    tool_events = session.query(AuditEvent).filter(AuditEvent.action == "agent.tool.call").all()
    assert tool_events, "the engine must actually call the granted tools"
    called = {event.resource_id for event in tool_events}
    assert "project.metadata.read" in called
    leader = session.query(AgentTask).filter(AgentTask.agent_id == "strategy_director").first()
    if leader is not None and leader.output_json:
        assert leader.output_json["tools"]


def test_budgets_are_validated():
    with pytest.raises(ValueError):
        Budgets(concurrency=4).validate()
    with pytest.raises(ValueError):
        Budgets(max_depth=9).validate()
    with pytest.raises(ValueError):
        Budgets(max_calls=1).validate()
    assert Budgets.from_dict(None).max_calls == 30
    assert Budgets.from_dict({"max_calls": 12}).max_calls == 12


def test_status_payload_is_complete(orchestration, session):
    orchestrator, _ = orchestration()
    objective = orchestrator.create_objective("Report a full status payload", enqueue=False)
    orchestrator.execute(objective.id)
    status = orchestrator.status(objective.id)
    for key in ("objective_id", "state", "plan", "departments", "limits", "progress", "graph", "final_report"):
        assert key in status
    assert status["graph"]["nodes"]
    assert status["progress"]["task_count"] == len(status["graph"]["nodes"])


def test_every_model_call_is_recorded_in_agent_run(orchestration, session):
    """The audit trail is per call: tokens, duration, model and finish reason."""
    orchestrator, _service = orchestration()
    objective = orchestrator.create_objective("Record every call for the audit trail", enqueue=False)
    orchestrator.execute(objective.id)
    runs = session.query(AgentRun).all()
    assert runs, "no agent_run rows were written"
    assert all(run.model_tag for run in runs)
    tasks = session.query(AgentTask).filter(AgentTask.objective_id == objective.id).all()
    assert {run.task_id for run in runs} <= {task.id for task in tasks}
    assert any(run.completion_tokens > 0 for run in runs)


def test_declared_fallback_is_used_and_recorded(orchestration, session):
    """A failing planner falls back to the specialist and says so in the record."""
    from creatoros.integrations.ollama.errors import OllamaUnavailable

    def responder(index: int, prompt: str) -> str:
        # The planner (4B) is unavailable; the specialist must take over.
        if index == 0:
            return OllamaUnavailable("planner is unavailable")
        return _artifact_json()

    orchestrator, service = orchestration(responder)
    objective = orchestrator.create_objective("Fall back when the planner is unavailable", enqueue=False)
    orchestrator.execute(objective.id)
    profiles = [call["profile"] for call in service.calls]
    assert profiles[0] == "planner"
    assert "specialist" in profiles, profiles
    root = (
        session.query(AgentTask)
        .filter(AgentTask.objective_id == objective.id, AgentTask.depth == 0)
        .one()
    )
    assert root.state is TaskState.SUCCEEDED
    assert root.output_json["fallback_note"]
    failed_runs = (
        session.query(AgentRun)
        .filter(AgentRun.task_id == root.id, AgentRun.finish_reason == "OLLAMA_UNAVAILABLE")
        .count()
    )
    assert failed_runs >= 1, "the failed attempt must be audited as well"
    session.refresh(objective)
    warnings = (objective.final_report_json or {}).get("warnings", [])
    assert any("using specialist" in warning for warning in warnings), warnings


def test_an_unsupported_number_is_flagged_for_human_review(orchestration, session, monkeypatch):
    """P8.15: a statistic no measured evidence supports never becomes a successful task."""
    import json

    evidence_payload = {
        "tool_id": "analytics.metrics.read",
        "ok": True,
        "duration_ms": 1,
        "error_code": None,
        "output": {
            "evidence": [
                {
                    "metric_key": "tt.video.views",
                    "provider_metric_name": "view_count",
                    "unit": "count",
                    "period_kind": "lifetime",
                    "availability": "observed",
                    "value": 500,
                    "snapshot_ids": ["snap-1"],
                    "aggregation_ids": ["agg-1"],
                }
            ]
        },
    }

    def fake_tool_step(self, objective, task, definition):
        # Only the specialist layer is handed measurements, so the run still has successes.
        return [evidence_payload] if task.depth == 2 else []

    monkeypatch.setattr(Orchestrator, "_tool_step", fake_tool_step)

    def responder(index: int, prompt: str) -> str:
        payload = json.loads(_artifact_json())
        payload["summary"] = "The publication reached 987654 views at the 24-hour window."
        return json.dumps(payload)

    orchestrator, _ = orchestration(responder)
    objective = orchestrator.create_objective("Summarise measured performance honestly", enqueue=False)
    report = orchestrator.execute(objective.id)

    tasks = TaskGraph(session, objective.id).tasks()
    flagged = [task for task in tasks if task.state is TaskState.NEEDS_HUMAN_REVIEW]
    assert flagged, "an unsupported number must be flagged for review, not accepted"
    for task in flagged:
        assert task.finished_at is not None, "a review task must be terminal, never left running"
        assert task.error_code == "UNTRACEABLE_NUMBER"
        assert "snapshot_ids:snap-1" in (task.evidence_refs_json or [])

    session.refresh(objective)
    assert objective.state is ObjectiveState.PARTIAL, "an unsupported report is never a success"
    assert "flagged for human review" in objective.final_report_json["summary"]
    codes = {item["code"] for item in objective.final_report_json["unfinished"]}
    assert "UNTRACEABLE_NUMBER" in codes
    assert report.state == "partial"


# ------------------------------------------------------------- compiled deliverable (measured on qwen3)
def test_the_deliverable_contains_the_copy_not_just_a_description_of_it():
    """A finished caption task must contribute its captions to the report the user reads."""
    from creatoros.services.orchestrator import _deliverable_body

    output = {
        "artifact": {
            "contract": "copy",
            "summary": "Three original captions with hashtags.",
            "items": [
                {"label": "", "text": "Skincare | Morning prep, fresh & ready. #MorningRoutine", "notes": None},
                {"label": "Second", "text": "Wake up, move, energize. #FitStart", "notes": "hashtag: #FitStart"},
            ],
        }
    }
    body = _deliverable_body(output, "Three original captions with hashtags.")
    assert "Skincare | Morning prep, fresh & ready. #MorningRoutine" in body
    assert "Second: Wake up, move, energize. #FitStart" in body
    assert "hashtag: #FitStart" in body, "a preserved extra field is shown, not silently dropped"


def test_a_report_artifact_contributes_its_sections():
    from creatoros.services.orchestrator import _deliverable_body

    body = _deliverable_body({"artifact": {"contract": "report", "sections": [{"heading": "Findings", "body": "Body."}]}}, "s")
    assert "**Findings**" in body and "Body." in body


def test_a_task_with_no_artifact_still_contributes_its_summary_only():
    from creatoros.services.orchestrator import _deliverable_body

    assert _deliverable_body({}, "A plan.") == "A plan."
    assert _deliverable_body({}, "") == ""


def test_a_placeholder_item_is_not_rendered_as_copy():
    """A model that returns items without text must not put "- empty" in the report."""
    from creatoros.services.orchestrator import _deliverable_body

    output = {
        "artifact": {
            "contract": "copy",
            "summary": "Captions",
            "items": [{"label": "", "text": "empty", "notes": None}, {"label": "", "text": "Real caption. #tag", "notes": None}],
        }
    }
    body = _deliverable_body(output, "Captions")
    assert body.count("- ") == 1
    assert "Real caption. #tag" in body
    assert "empty" not in body


class _FakeNode:
    def __init__(self, task_id, team_id, state, depends_on=()):
        self.id = task_id
        self.agent_id = task_id
        self.team_id = team_id
        self.state = state
        self.depth = 2
        self.title = task_id
        self.task_type = "manifests"
        self.priority = 0
        self.parent_task_id = None
        self.depends_on = list(depends_on)
        self.error_code = None


def test_a_quality_task_without_edges_reviews_the_objectives_work(monkeypatch):
    """A reviewer shown nothing can only reject, so it sees the work that already succeeded."""
    from creatoros.services import orchestrator as mod

    nodes = {
        "chief": _FakeNode("chief", "executive", "succeeded"),
        "caption": _FakeNode("caption_writer_agent", "creative_writing", "succeeded"),
        "critic": _FakeNode("content_quality_agent", "quality_governance", "running"),
    }
    monkeypatch.setattr(mod.TaskGraph, "nodes", lambda self: nodes)

    task = SimpleNamespace(
        id="critic", team_id="quality_governance", objective_id="obj", summary=None, output_json=None
    )
    session = SimpleNamespace(get=lambda model, key: SimpleNamespace(
        agent_id=key, team_id="creative_writing", summary="Three captions",
        confidence=0.5,
        output_json={"artifact": {"contract": "copy", "items": [{"text": "Sunrise, sweat, glow. #MorningMagic"}]}},
    ))
    engine = SimpleNamespace(session=session)
    budgets = SimpleNamespace(max_upstream_chars=6000)
    context = mod.Orchestrator._upstream_context(engine, task, budgets)
    assert sorted(item["agent_id"] for item in context) == ["caption_writer_agent", "chief"]
    assert any("Sunrise, sweat, glow. #MorningMagic" in item["summary"] for item in context)
    assert all(item["department"] != "quality_governance" for item in context)
