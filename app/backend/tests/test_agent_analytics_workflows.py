"""P8.16: the W1-W3 analytics workflows.

The workflows exist so a growth review runs the *same reviewed chain* every time instead of
whatever a model chose to delegate that day. These tests assert the chain, that it runs with a
scripted model, that a failure turns the objective `partial` with a reason, and that no workflow
task can reach a publisher.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from creatoros.db.enums import ConnectionStatus, ObjectiveState, Platform, TaskState
from creatoros.db.models import AgentDefinition, AgentTask, PromptTemplate, SocialAccount
from creatoros.services.analytics import agent_grants, workflows
from creatoros.services.orchestrator import Budgets, Orchestrator
from creatoros.services.registry import AgentRegistry
from creatoros.services.task_graph import TaskGraph
from creatoros.services.toolbox import ensure_tools_seeded


def _artifact_json() -> str:
    """One payload that satisfies every output contract the catalogue declares."""
    return json.dumps(
        {
            "summary": "A usable result from the scripted model.",
            "findings": [{"statement": "A measured association, not a cause", "kind": "assumption"}],
            "items": [{"label": "note", "text": "Keep it short"}],
            "steps": [{"step": "Do the next thing", "owner": "analytics_growth"}],
            "assignments": ["analytics_growth"],
            "sections": [{"heading": "Result", "body": "Compiled body text."}],
            "issues": [{"severity": "info", "issue": "Nothing blocking"}],
            "verdict": "pass",
            "confidence": 0.8,
        }
    )


class ScriptedAiService:
    """Stands in for AiService; the workflows never need a real model."""

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
def seeded(session):
    """A workforce with the analytics grants and prompt seeds, and one connected account."""
    AgentRegistry(session).ensure_seeded()
    ensure_tools_seeded(session)
    agent_grants.apply_analytics_tool_grants(session)
    workflows.ensure_analytics_prompts(session)
    from creatoros.services.analytics import registry as metric_registry

    metric_registry.ensure_metric_catalog(session)
    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="wf-account",
        display_name="Workflow account",
        account_type="tiktok_creator",
        scopes_json=["user.info.basic"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2",
        meta_json={},
    )
    session.add(account)
    session.commit()
    return account


def _orchestrator(session, responder: Callable[[int, str], str] | None = None):
    service = ScriptedAiService(responder)
    return Orchestrator(session, ai_service_factory=lambda _session: service), service


def _tasks(session, objective) -> list[AgentTask]:
    return TaskGraph(session, objective.id).tasks()


def _by_agent(tasks) -> dict[str, AgentTask]:
    return {task.agent_id: task for task in tasks}


def _edges(session, objective, tasks) -> set[tuple[str, str]]:
    agent_by_id = {task.id: task.agent_id for task in tasks}
    return {
        (agent_by_id.get(edge.from_task_id), agent_by_id.get(edge.to_task_id))
        for edge in TaskGraph(session, objective.id).edges()
    }


# ------------------------------------------------------------------ shapes


@pytest.mark.parametrize("key", sorted(workflows.WORKFLOWS))
def test_each_workflow_builds_its_documented_chain(seeded, session, key):
    workflow = workflows.WORKFLOWS[key]
    objective = workflows.create_workflow(session, key=key, account_id=seeded.id)
    tasks = _tasks(session, objective)

    agent_ids = {task.agent_id for task in tasks}
    assert set(workflow.agent_ids()) <= agent_ids, "every documented agent must have a task"
    assert len(tasks) == len(workflow.stages), "the builder must not invent extra tasks"

    assert objective.plan_json["workflow"] == key
    assert objective.plan_json["deterministic"] is True
    assert objective.plan_json["stages"] == [stage.key for stage in workflow.stages]

    expected_edges = {
        (workflow.stages[next(i for i, s in enumerate(workflow.stages) if s.key == dep)].agent_id, stage.agent_id)
        for stage in workflow.stages
        for dep in stage.depends_on
    }
    assert expected_edges <= _edges(session, objective, tasks)

    # No stage may use a task type the engine auto-expands into model-chosen work.
    assert all(task.task_type not in ("department_plan", "revision") for task in tasks)


def test_w1_is_the_documented_eight_analysis_chain(seeded, session):
    objective = workflows.create_workflow(session, key="weekly_growth_review", account_id=seeded.id)
    tasks = _by_agent(_tasks(session, objective))
    assert tasks["analytics_director"].task_type == workflows.ANALYTICS_TASK_TYPE
    for agent_id in (
        "performance_analysis_agent",
        "views_analysis_agent",
        "engagement_analysis_agent",
        "retention_analysis_agent",
        "audience_growth_agent",
        "hashtag_performance_agent",
        "campaign_analysis_agent",
        "content_comparison_agent",
    ):
        assert agent_id in tasks
    # The report depends on every analysis; the strategy and compile stages close the chain.
    assert tasks["growth_reporting_agent"].depth == 2
    assert tasks["strategy_director"].depth == 1
    assert tasks["chief_orchestrator"].depth == 0


def test_an_unknown_workflow_and_a_missing_account_are_refused(seeded, session):
    with pytest.raises(ValueError):
        workflows.create_workflow(session, key="does_not_exist", account_id=seeded.id)
    with pytest.raises(ValueError):
        workflows.create_workflow(session, key="weekly_growth_review", account_id="")


# ------------------------------------------------------------------ running


def test_w1_runs_and_every_documented_task_satisfies_its_contract(seeded, session):
    orchestrator, service = _orchestrator(session)
    objective = workflows.create_workflow(
        session, key="weekly_growth_review", account_id=seeded.id, period="2026-11-16..2026-11-22"
    )
    report = orchestrator.execute(objective.id)

    assert report.state == "succeeded", report
    session.refresh(objective)
    assert objective.state is ObjectiveState.SUCCEEDED

    documented = set(workflows.WORKFLOWS["weekly_growth_review"].agent_ids())
    tasks = [task for task in _tasks(session, objective) if task.agent_id in documented]
    assert len(tasks) == len(workflows.WORKFLOWS["weekly_growth_review"].stages)
    for task in tasks:
        assert task.state is TaskState.SUCCEEDED, f"{task.agent_id}: {task.error_code}"
        artifact = (task.output_json or {}).get("artifact")
        assert artifact, task.agent_id
        definition = session.get(AgentDefinition, task.agent_id)
        required = set((definition.output_schema_json or {}).get("required") or [])
        assert required <= set(artifact), f"{task.agent_id} did not satisfy {required}"
    # The engine still adds its own independent quality review over the finished work.
    assert any(task.team_id == "quality_governance" for task in _tasks(session, objective))
    assert service.calls, "the workflow must actually call the model"


def test_a_failing_stage_turns_the_objective_partial_with_reasons(seeded, session):
    def responder(index: int, prompt: str) -> str:
        if "Retention Analysis Agent" in prompt:
            return "this is not JSON"
        return _artifact_json()

    orchestrator, _ = _orchestrator(session, responder)
    objective = workflows.create_workflow(session, key="weekly_growth_review", account_id=seeded.id)
    report = orchestrator.execute(objective.id)

    assert report.state == "partial", report
    session.refresh(objective)
    assert objective.state is ObjectiveState.PARTIAL

    tasks = _by_agent(_tasks(session, objective))
    assert tasks["retention_analysis_agent"].state is TaskState.FAILED
    assert tasks["retention_analysis_agent"].error_code == "MALFORMED_AGENT_OUTPUT"
    # The stages downstream of the failure are blocked, not silently skipped.
    assert tasks["growth_reporting_agent"].state is TaskState.BLOCKED
    assert tasks["growth_reporting_agent"].error_code == "DEPENDENCY_FAILED"

    unfinished = objective.final_report_json["unfinished"]
    codes = {item["code"] for item in unfinished}
    assert {"MALFORMED_AGENT_OUTPUT", "DEPENDENCY_FAILED"} <= codes
    assert "partial" in objective.final_report_json["summary"].lower()


# ------------------------------------------------------------------ safety


def test_no_workflow_task_can_publish(seeded, session):
    for key in workflows.WORKFLOWS:
        objective = workflows.create_workflow(session, key=key, account_id=seeded.id)
        for task in _tasks(session, objective):
            definition = session.get(AgentDefinition, task.agent_id)
            tools = [str(tool) for tool in (definition.allowed_tools_json or [])]
            assert not [tool for tool in tools if tool.startswith(("publishing.", "platform.publish"))], (
                f"{key}: {task.agent_id} holds a publishing tool"
            )


def test_the_workflow_module_imports_no_publishing_path():
    from pathlib import Path

    import creatoros

    root = Path(creatoros.__file__).parent / "services" / "analytics"
    text = (root / "workflows.py").read_text(encoding="utf-8")
    for forbidden in (
        "from ..publishing",
        "from ...services.publishing",
        "publishing.executor",
        "publishing.publisher",
        "publishing.scheduler",
    ):
        assert forbidden not in text, forbidden


def test_the_brief_carries_the_measurement_rules(seeded, session):
    objective = workflows.create_workflow(session, key="weekly_growth_review", account_id=seeded.id)
    for task in _tasks(session, objective):
        step = str((task.input_json or {}).get("step") or "")
        assert "never describe it as zero" in step
        assert "Correlation is not causation" in step


# ------------------------------------------------------------------ prompt seeds


def test_analytics_prompts_are_seeded_once_and_never_overwritten(seeded, session):
    first = workflows.ensure_analytics_prompts(session)
    assert first["created"] == []
    keys = session.scalars(
        sa.select(PromptTemplate.key).where(PromptTemplate.key.like("analytics.%"))
    ).all()
    assert workflows.MEASUREMENT_RULES_KEY in keys
    assert f"{workflows.WORKFLOW_PROMPT_PREFIX}weekly_growth_review" in keys

    # A human edit must survive a re-seed.
    row = session.get(PromptTemplate, ("analytics.workflow.experiment_design", 1))
    row.body = "Edited by a human"
    session.flush()
    workflows.ensure_analytics_prompts(session)
    session.refresh(row)
    assert row.body == "Edited by a human"


def test_seeded_but_unseeded_database_still_gets_the_rules(session):
    # `measurement_rules` must fall back to the constant when nothing is seeded.
    assert workflows.measurement_rules(session) == workflows.MEASUREMENT_RULES


def test_budgets_are_honoured_when_building(seeded, session):
    orchestrator, _ = _orchestrator(session)
    objective = workflows.create_workflow(
        session,
        key="experiment_design",
        account_id=seeded.id,
        budgets=Budgets(max_calls=12),
        enqueue=False,
    )
    assert objective.calls_used == 0
    report = orchestrator.execute(objective.id)
    assert report.state in {"succeeded", "partial"}
    assert objective.calls_used <= 12
