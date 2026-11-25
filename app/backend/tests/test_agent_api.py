"""Agent workforce API tests: registry, orchestration, monitoring, memory, tools, approvals."""
from __future__ import annotations

import json

import pytest

from creatoros.db.models import ContentProject


@pytest.fixture()
def scripted(monkeypatch):
    """Install a scripted model double as the application's AI service factory."""
    from creatoros.services.ai_runtime import set_service_factory
    from tests.test_agent_orchestration import ScriptedAiService, _artifact_json

    service = ScriptedAiService(lambda index, prompt: _artifact_json())
    set_service_factory(lambda _session: service)
    yield service
    set_service_factory(None)


@pytest.fixture()
def project(session):
    row = ContentProject(title="API hook project", brief="Explain hooks in 30 seconds")
    session.add(row)
    session.commit()
    return row


# ------------------------------------------------------------------- registry
def test_registry_summary_reports_the_real_workforce(client):
    response = client.get("/agents/registry/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agents_total"] == 111
    assert body["chiefs"] == 1
    assert body["leaders"] == 10
    assert body["specialists"] == 100
    assert body["departments"] == 10
    assert body["tools_registered"] == 26
    assert body["delegation_limits"]["concurrency"] == 1
    assert body["delegation_hard_limits"]["max_depth"] == 4


def test_list_departments_and_one_department(client):
    response = client.get("/agents/departments")
    assert response.status_code == 200
    departments = response.json()
    assert len(departments) == 10
    assert {item["agent_count"] for item in departments} == {11}

    detail = client.get("/agents/departments/creative_writing")
    assert detail.status_code == 200
    body = detail.json()
    assert body["leader_agent_id"] == "creative_director"
    assert len(body["agents"]) == 11


def test_unknown_department_is_404(client):
    response = client.get("/agents/departments/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "DEPARTMENT_NOT_FOUND"


def test_list_and_filter_agents(client):
    everyone = client.get("/agents/registry")
    assert everyone.status_code == 200
    assert len(everyone.json()) == 111

    leaders = client.get("/agents/registry", params={"hierarchy_level": 1})
    assert len(leaders.json()) == 10

    creative = client.get("/agents/registry", params={"department": "creative_writing"})
    assert {item["department"] for item in creative.json()} == {"creative_writing"}

    searched = client.get("/agents/registry", params={"search": "hook"}).json()
    assert "hook_writer_agent" in {item["agent_id"] for item in searched}


def test_agent_detail_includes_prompt_history_and_recent_tasks(client):
    response = client.get("/agents/registry/hook_writer_agent")
    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == "hook_writer_agent"
    assert body["prompt_versions"] == [1]
    assert body["output_contract"] == "copy"
    assert "caption.compose" in body["allowed_tools"]
    assert body["recent_tasks"] == []


def test_patch_agent_bumps_version_and_rejects_bad_values(client):
    response = client.patch("/agents/registry/hook_writer_agent", json={"temperature": 0.85})
    assert response.status_code == 200
    assert response.json()["config_version"] == 2
    assert response.json()["temperature"] == pytest.approx(0.85)

    bad = client.patch("/agents/registry/hook_writer_agent", json={"memory_scope": "galaxy"})
    assert bad.status_code == 422
    assert bad.json()["code"] == "INVALID_MEMORY_SCOPE" or bad.json()["code"] == "INVALID_AGENT_CONFIG"


def test_disable_and_enable_agent(client):
    off = client.post("/agents/registry/hook_writer_agent/state", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    on = client.post("/agents/registry/hook_writer_agent/state", json={"enabled": True})
    assert on.json()["enabled"] is True


def test_disable_department_toggles_its_agents(client):
    response = client.post("/agents/departments/creative_writing/state", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled_count"] == 0
    listed = client.get("/agents/registry", params={"department": "creative_writing"}).json()
    assert all(item["enabled"] is False for item in listed)


# -------------------------------------------------------------- orchestration
def test_create_objective_queues_it_without_running(client, scripted, session):
    response = client.post("/agents/objectives", json={"objective": "Plan three shorts about hooks"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "pending"
    assert body["objective"] == "Plan three shorts about hooks"
    assert body["progress"]["task_count"] == 0
    assert scripted.calls == []

    from creatoros.db.models import QueueJob

    jobs = session.query(QueueJob).all()
    assert any(job.job_type == "agents.objective" for job in jobs)


def test_duplicate_objective_is_rejected(client, scripted):
    first = client.post("/agents/objectives", json={"objective": "An objective that will be requested twice"})
    assert first.status_code == 201
    second = client.post("/agents/objectives", json={"objective": "An objective that will be requested twice"})
    assert second.status_code == 409
    assert second.json()["code"] == "DUPLICATE_OBJECTIVE"


def test_short_objective_is_rejected(client):
    response = client.post("/agents/objectives", json={"objective": "short"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_invalid_budget_is_rejected(client):
    response = client.post(
        "/agents/objectives",
        json={"objective": "An objective with an illegal concurrency", "limits": {"concurrency": 8}},
    )
    assert response.status_code == 422


def test_run_now_executes_and_returns_the_graph(client, scripted, project):
    response = client.post(
        "/agents/objectives",
        json={
            "objective": "Turn one clip into three platform cuts",
            "project_id": project.id,
            "run_now": True,
            "limits": {"specialists_per_department": 2},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] in {"succeeded", "partial"}
    assert body["graph"]["nodes"]
    assert body["final_report"]["deliverable"]
    assert body["run"]["calls_used"] == body["calls_used"]
    assert scripted.calls


def test_run_without_inference_is_503(client, project):
    response = client.post(
        "/agents/objectives",
        json={"objective": "Run with no local inference available", "run_now": True},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "AI_UNAVAILABLE"


def test_objective_listing_and_detail(client, scripted, project):
    created = client.post(
        "/agents/objectives",
        json={"objective": "List and inspect this objective", "run_now": True, "limits": {"specialists_per_department": 1}},
    ).json()
    objective_id = created["objective_id"]

    listed = client.get("/agents/objectives")
    assert listed.status_code == 200
    assert [item["objective_id"] for item in listed.json()] == [objective_id]

    detail = client.get(f"/agents/objectives/{objective_id}")
    assert detail.status_code == 200
    assert detail.json()["plan"]["departments"]

    graph = client.get(f"/agents/objectives/{objective_id}/graph")
    assert graph.status_code == 200
    assert graph.json()["edges"]
    assert len(graph.json()["tasks"]) == len(graph.json()["nodes"])


def test_unknown_objective_is_404(client):
    assert client.get("/agents/objectives/01ZZZZZZZZZZZZZZZZZZZZZZZZ").status_code == 404
    assert client.get("/agents/objectives/01ZZZZZZZZZZZZZZZZZZZZZZZZ/graph").status_code == 404


def test_cancel_and_resume_objective(client, scripted):
    created = client.post("/agents/objectives", json={"objective": "Cancel this objective please"}).json()
    cancelled = client.post(f"/agents/objectives/{created['objective_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True
    resumed = client.post(f"/agents/objectives/{created['objective_id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["queued_job_id"]
    already = client.post(f"/agents/objectives/{created['objective_id']}/resume")
    assert already.status_code == 200


def test_task_listing_and_cancel(client, scripted):
    created = client.post(
        "/agents/objectives",
        json={"objective": "Produce tasks to list", "run_now": True, "limits": {"specialists_per_department": 1}},
    ).json()
    tasks = client.get("/agents/tasks", params={"objective_id": created["objective_id"]})
    assert tasks.status_code == 200
    assert len(tasks.json()) == len(created["graph"]["nodes"])

    pending = [task for task in tasks.json() if task["state"] == "pending"]
    if pending:
        response = client.post(f"/agents/tasks/{pending[0]['task_id']}/cancel")
        assert response.status_code in (200, 409)


def test_monitoring_activity_and_metrics(client, scripted):
    client.post(
        "/agents/objectives",
        json={"objective": "Generate monitoring data", "run_now": True, "limits": {"specialists_per_department": 1}},
    )
    monitor = client.get("/agents/monitor")
    assert monitor.status_code == 200
    body = monitor.json()
    assert body.get("task_states")
    assert body["queue_states"] is not None

    activity = client.get("/agents/activity")
    assert activity.status_code == 200
    assert activity.json()
    assert all(item["action"].startswith("agent.") for item in activity.json())

    metrics = client.get("/agents/metrics/summary")
    assert metrics.status_code == 200
    assert metrics.json()["tokens_total"] > 0
    assert metrics.json()["tasks_by_state"]


# --------------------------------------------------------------------- memory
def test_memory_write_query_and_stats(client):
    created = client.post(
        "/agents/memory",
        json={"scope": "agent", "owner_id": "hook_writer_agent", "content": "Short hooks beat long ones"},
    )
    assert created.status_code == 201, created.text

    queried = client.post(
        "/agents/memory/query", json={"scope": "agent", "owner_id": "hook_writer_agent"}
    )
    assert queried.status_code == 200
    assert [row["content"] for row in queried.json()] == ["Short hooks beat long ones"]

    stats = client.get("/agents/memory/stats")
    assert stats.json()["total"] == 1

    listed = client.get("/agents/memory", params={"scope": "agent"})
    assert len(listed.json()) == 1


def test_memory_rejects_secrets_and_bad_scopes(client):
    secret = client.post(
        "/agents/memory",
        json={"scope": "global", "owner_id": "creatoros", "content": "token EAAGabcdefghijklmnopq"},
    )
    assert secret.status_code == 422
    assert secret.json()["code"] == "SECRET_IN_MEMORY"

    bad = client.post("/agents/memory", json={"scope": "galaxy", "owner_id": "x", "content": "hi"})
    assert bad.status_code == 422


def test_memory_retention_endpoint(client):
    client.post("/agents/memory", json={"scope": "global", "owner_id": "creatoros", "content": "keep me"})
    dry = client.post("/agents/memory/retention", params={"dry_run": True})
    assert dry.status_code == 200
    assert dry.json()["dry_run"] is True


# ---------------------------------------------------------------------- tools
def test_tool_catalog_lists_every_declared_tool(client):
    response = client.get("/agents/tools")
    assert response.status_code == 200
    tools = response.json()
    # 12 Phase 4 tools, 4 media tools, 2 social tools, 3 publishing tools and 5 analytics tools.
    assert len(tools) == 26
    assert {tool["tool_id"] for tool in tools} >= {
        "media.inspect",
        "media.derivatives.list",
        "media.scene.scan",
        "media.transcode.recommend",
        "media.quality.report",
        "report.generate",
        "human.approval.request",
    }
    assert all(tool["timeout_s"] > 0 for tool in tools)


def test_manual_tool_run_respects_grants(client, project):
    allowed = client.post(
        "/agents/tools/project.metadata.read/run",
        json={"agent_id": "research_synthesis_agent", "payload": {"project_id": project.id}},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["ok"] is True

    denied = client.post(
        "/agents/tools/content.readiness.check/run",
        json={"agent_id": "hook_writer_agent", "payload": {"project_id": project.id}},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "TOOL_DENIED"

    unknown = client.post(
        "/agents/tools/shell.exec/run", json={"agent_id": "hook_writer_agent", "payload": {}}
    )
    assert unknown.status_code == 404


def test_manual_tool_run_validates_payload(client):
    response = client.post(
        "/agents/tools/caption.compose/run",
        json={"agent_id": "hook_writer_agent", "payload": {"platform": "myspace", "hook": "hi"}},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "TOOL_INVALID_INPUT"


# ------------------------------------------------------------------ approvals
def test_approval_lifecycle(client):
    created = client.post(
        "/agents/approvals",
        json={
            "action": "publish.post",
            "target_type": "platform_variant",
            "target_id": "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "summary": "Publish the instagram variant",
            "payload": {"caption": "hello"},
            "risks": ["caption is final"],
        },
    )
    assert created.status_code == 201, created.text
    approval_id = created.json()["approval_request_id"]
    assert created.json()["state"] == "pending"
    assert created.json()["decided_by"] is None

    decided = client.post(f"/agents/approvals/{approval_id}/decide", json={"approve": True, "note": "ok"})
    assert decided.status_code == 200
    assert decided.json()["state"] == "approved"
    assert decided.json()["consumed_at"] is None

    again = client.post(f"/agents/approvals/{approval_id}/decide", json={"approve": False})
    assert again.status_code == 409

    listed = client.get("/agents/approvals", params={"state": "approved"})
    assert len(listed.json()) == 1


def test_approval_rejection_and_cancel(client):
    first = client.post(
        "/agents/approvals",
        json={
            "action": "publish.post",
            "target_type": "platform_variant",
            "target_id": "01BBBBBBBBBBBBBBBBBBBBBBBB",
            "summary": "Reject me",
            "payload": {"a": 1},
        },
    ).json()
    rejected = client.post(f"/agents/approvals/{first['approval_request_id']}/decide", json={"approve": False})
    assert rejected.json()["state"] == "rejected"

    second = client.post(
        "/agents/approvals",
        json={
            "action": "publish.post",
            "target_type": "platform_variant",
            "target_id": "01CCCCCCCCCCCCCCCCCCCCCCCC",
            "summary": "Cancel me",
            "payload": {"b": 2},
        },
    ).json()
    cancelled = client.post(f"/agents/approvals/{second['approval_request_id']}/cancel")
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["executed"] is False


def test_approval_expiry_sweep_endpoint(client):
    assert client.post("/agents/approvals/expire").json()["expired"] == 0


# ------------------------------------------------------------------- security
def test_agent_routes_require_the_token(unauth_client):
    for path in (
        "/agents/registry/summary",
        "/agents/departments",
        "/agents/registry",
        "/agents/objectives",
        "/agents/tasks",
        "/agents/monitor",
        "/agents/memory/stats",
        "/agents/tools",
        "/agents/approvals",
    ):
        response = unauth_client.get(path)
        assert response.status_code == 401, path
        assert response.json()["code"] == "AUTH_REQUIRED"


def test_version_reports_phase_4_capabilities(client):
    body = client.get("/version").json()
    assert body["api"] == "phase-8.24"
    assert body["version"].startswith("0.8.")
    capabilities = body["capabilities"]
    assert capabilities["agents_catalog"] == "available"
    assert capabilities["agent_task_graph"] == "available"
    assert capabilities["human_approvals"] == "intent_only"
    assert capabilities["agent_orchestration"] in {"available", "requires_local_inference"}


def test_agent_definition_serialisation_is_json_safe(client):
    payload = client.get("/agents/registry/chief_orchestrator").json()
    json.dumps(payload)
    assert payload["can_delegate"] is True
    assert payload["hierarchy_level"] == 0


def test_unknown_fields_are_rejected_not_ignored(client):
    """A client must not believe it set a budget the server ignored."""
    response = client.post("/agents/objectives", json={"objective": "Reject unknown fields please", "max_calls": 5})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_objective_limits_are_clamped_to_hard_caps(client):
    too_many = client.post(
        "/agents/objectives",
        json={"objective": "This asks for more calls than allowed", "limits": {"max_calls": 500}},
    )
    assert too_many.status_code == 422
    ok = client.post(
        "/agents/objectives",
        json={"objective": "This asks for fewer calls", "limits": {"max_calls": 4, "max_departments": 1}},
    )
    assert ok.status_code in (200, 201)
    limits = ok.json()["limits"]
    assert limits["max_calls"] == 4
    assert limits["max_departments"] == 1
    assert limits["concurrency"] == 1
