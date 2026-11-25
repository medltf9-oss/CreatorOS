"""Agent workforce API: registry, orchestration, monitoring, memory, tools, approvals.

Every route is authenticated by the router-level token dependency. Mutations are
audited. Nothing here pretends: an objective that cannot run because inference is
unavailable returns 503 with `AI_UNAVAILABLE`, and a tool run that the agent was not
granted returns 403 with the exact reason.
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ...db.enums import ObjectiveState, QueueState, TaskState
from ...db.models import (
    AgentDefinition,
    AgentDepartment,
    AgentMemory,
    AgentObjective,
    AgentTask,
    ApprovalRequest,
    AuditEvent,
    PromptTemplate,
    QueueJob,
)
from ...services.agent_memory import AgentMemoryService, MemoryScopeError, SecretInMemory
from ...services.approvals import (
    ApprovalExpired,
    ApprovalHashMismatch,
    ApprovalNotFound,
    ApprovalService,
    ApprovalStateError,
)
from ...services.orchestrator import Budgets, Orchestrator, objective_signature
from ...services.queue import QueueService
from ...services.registry import AgentNotFound, AgentRegistry, InvalidAgentUpdate
from ...services.task_graph import CycleDetected, DepthExceeded, TaskGraph
from ...services.toolbox import ToolDenied, ToolError, ToolExecutor, ToolInvalidInput, ToolNotFound
from ..deps import Audit, DbSession

router = APIRouter(tags=["agents"])


# --------------------------------------------------------------------- models
class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    mission: str | None = None
    system_prompt: str | None = None
    model_profile: str | None = None
    fallback_profiles_json: list[str] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: int | None = None
    max_retries: int | None = None
    allowed_tools_json: list[str] | None = None
    restricted_tools_json: list[str] | None = None
    task_types_json: list[str] | None = None
    memory_scope: str | None = None
    enabled: bool | None = None


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ObjectiveCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=8, max_length=8000)
    project_id: str | None = Field(default=None, max_length=26)
    priority: int = Field(default=50, ge=1, le=100)
    limits: dict[str, Any] | None = None
    run_now: bool = Field(
        default=False,
        description="Execute synchronously in this request (used by tests and the CLI). "
        "The desktop app leaves this false and lets the durable worker run it.",
    )


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    owner_id: str | None = None
    content: str = Field(min_length=1, max_length=20000)
    title: str | None = Field(default=None, max_length=200)
    project_id: str | None = None
    objective_id: str | None = None
    task_id: str | None = None
    sensitivity: str = Field(default="normal", max_length=40)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = "project"
    owner_id: str | None = None
    project_id: str | None = None
    objective_id: str | None = None
    search: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class ToolRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=3, max_length=48)
    payload: dict[str, Any] = Field(default_factory=dict)
    objective_id: str | None = None


class ApprovalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=3, max_length=80)
    target_type: str = Field(min_length=2, max_length=60)
    target_id: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=3, max_length=1000)
    rationale: str | None = Field(default=None, max_length=2000)
    risks: list[str] = Field(default_factory=list, max_length=10)
    payload: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    objective_id: str | None = None
    ttl_hours: int = Field(default=72, ge=1, le=720)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str | None = Field(default=None, max_length=2000)


# ------------------------------------------------------------------ serialise
def _agent_out(definition: AgentDefinition) -> dict[str, Any]:
    return {
        "agent_id": definition.agent_id,
        "name": definition.name,
        "department": definition.team_id,
        "description": definition.description,
        "mission": definition.mission,
        "system_prompt": definition.system_prompt,
        "prompt_key": definition.prompt_key,
        "model_profile": definition.model_profile,
        "fallback_profiles": list(definition.fallback_profiles_json or []),
        "temperature": definition.temperature,
        "max_tokens": definition.max_tokens,
        "timeout_s": definition.timeout_s,
        "max_retries": definition.max_retries,
        "allowed_tools": list(definition.allowed_tools_json or []),
        "restricted_tools": list(definition.restricted_tools_json or []),
        "task_types": list(definition.task_types_json or []),
        "memory_scope": str(definition.memory_scope),
        "hierarchy_level": definition.hierarchy_level,
        "can_delegate": definition.can_delegate,
        "risk_level": str(definition.risk_level),
        "approval_requirement": str(definition.approval_requirement),
        "output_contract": (definition.output_schema_json or {}).get("contract"),
        "config_version": definition.config_version,
        "catalog_version": definition.catalog_version,
        "enabled": definition.enabled,
        "created_at": definition.created_at.isoformat() if definition.created_at else None,
        "updated_at": definition.updated_at.isoformat() if definition.updated_at else None,
    }


def _department_out(session: Any, department: AgentDepartment, registry: AgentRegistry) -> dict[str, Any]:
    members = registry.by_department(department.id)
    return {
        "id": department.id,
        "name": department.name,
        "mission": department.mission,
        "leader_agent_id": department.leader_agent_id,
        "display_order": department.display_order,
        "enabled": department.enabled,
        "catalog_version": department.catalog_version,
        "agent_count": len(members),
        "enabled_count": sum(1 for member in members if member.enabled),
    }


def _task_out(task: AgentTask) -> dict[str, Any]:
    graph_edges = None
    if task.objective_id:
        graph_edges = None
    return {
        "task_id": task.id,
        "objective_id": task.objective_id,
        "agent_id": task.agent_id,
        "department": task.team_id,
        "role": "chief"
        if task.agent_id == "chief_orchestrator"
        else ("leader" if task.depth == 1 else "specialist"),
        "parent_task_id": task.parent_task_id,
        "root_task_id": task.root_task_id,
        "depth": task.depth,
        "task_type": task.task_type,
        "title": task.title,
        "state": str(task.state),
        "priority": task.priority,
        "summary": task.summary,
        "confidence": task.confidence,
        "model_tag": task.model_tag,
        "tokens_used": task.tokens_used,
        "duration_ms": task.duration_ms,
        "config_version": task.config_version,
        "error_code": task.error_code,
        "error": task.error,
        "attempts": task.retries,
        "input": task.input_json,
        "output": task.output_json,
        "evidence_refs": list(task.evidence_refs_json or []),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "cancel_requested": task.cancel_requested,
        "edges": graph_edges,
    }


def _approval_out(row: ApprovalRequest) -> dict[str, Any]:
    return {
        "approval_request_id": row.id,
        "action": row.action,
        "state": str(row.state),
        "agent_id": row.agent_id,
        "task_id": row.task_id,
        "objective_id": row.objective_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "summary": row.summary,
        "rationale": row.rationale,
        "risks": list(row.risks_json or []),
        "payload_hash": row.payload_hash,
        "requested_by": row.requested_by,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decided_by": row.decided_by,
        "decision_note": row.decision_note,
        "consumed_at": row.consumed_at.isoformat() if row.consumed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "executed": False,
    }


def _memory_out(row: AgentMemory) -> dict[str, Any]:
    return {
        "memory_id": row.id,
        "scope": str(row.scope),
        "owner_id": row.owner_id,
        "project_id": row.project_id,
        "objective_id": row.objective_id,
        "source_task_id": row.source_task_id,
        "title": row.title,
        "content": row.content,
        "sensitivity": row.sensitivity,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _http(exc: Exception, *, not_found: int = status.HTTP_404_NOT_FOUND) -> HTTPException:
    code = getattr(exc, "code", "AGENT_ERROR")
    message = str(exc) or code
    if isinstance(exc, (AgentNotFound, ApprovalNotFound)):
        return HTTPException(not_found, f"{code}: {message}")
    if isinstance(exc, (SecretInMemory, MemoryScopeError, InvalidAgentUpdate, ToolInvalidInput)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{code}: {message}")
    if isinstance(exc, ToolDenied):
        return HTTPException(status.HTTP_403_FORBIDDEN, f"{code}: {message}")
    if isinstance(exc, ToolNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, f"{code}: {message}")
    if isinstance(exc, (ApprovalStateError, ApprovalExpired, ApprovalHashMismatch)):
        return HTTPException(status.HTTP_409_CONFLICT, f"{code}: {message}")
    if isinstance(exc, (CycleDetected, DepthExceeded)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{code}: {message}")
    if isinstance(exc, ToolError):
        return HTTPException(exc.status, f"{code}: {message}")
    if isinstance(exc, ValueError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{code}: {message}")
    if isinstance(exc, KeyError):
        return HTTPException(not_found, f"{code}: not found")
    return HTTPException(status.HTTP_400_BAD_REQUEST, f"{code}: {message}")


# ------------------------------------------------------------------- registry
@router.get("/agents/registry/summary")
def registry_summary(session: DbSession) -> dict[str, Any]:
    registry = AgentRegistry(session)
    counts = registry.counts()
    tools = ToolExecutor(session).catalog()
    queue_depth = session.scalar(
        sa.select(sa.func.count())
        .select_from(QueueJob)
        .where(QueueJob.state.in_([QueueState.QUEUED, QueueState.CLAIMED, QueueState.RUNNING]))
    )
    return {
        **counts,
        "tools_registered": sum(1 for tool in tools if tool["registered"]),
        "tools_enabled": sum(1 for tool in tools if tool["enabled"]),
        "queue_depth": int(queue_depth or 0),
        "delegation_limits": Budgets().as_dict(),
        "delegation_hard_limits": {
            "max_depth": 4,
            "max_calls": 60,
            "max_revisions": 3,
            "concurrency": 1,
            "wall_clock_s": 3600,
        },
    }


@router.get("/agents/departments")
def list_departments(session: DbSession) -> list[dict[str, Any]]:
    registry = AgentRegistry(session)
    return [_department_out(session, department, registry) for department in registry.departments()]


@router.get("/agents/departments/{department_id}")
def get_department(department_id: str, session: DbSession) -> dict[str, Any]:
    registry = AgentRegistry(session)
    department = registry.department(department_id)
    if department is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "DEPARTMENT_NOT_FOUND: unknown department")
    payload = _department_out(session, department, registry)
    payload["agents"] = [_agent_out(agent) for agent in registry.by_department(department_id)]
    return payload


@router.post("/agents/departments/{department_id}/state")
def set_department_state(department_id: str, body: EnabledBody, session: DbSession, audit: Audit) -> dict[str, Any]:
    registry = AgentRegistry(session)
    try:
        department = registry.set_department_enabled(department_id, body.enabled)
    except AgentNotFound as exc:
        raise _http(exc) from exc
    audit.record(
        "agent.department.state",
        resource_type="agent_department",
        resource_id=department_id,
        metadata={"enabled": body.enabled},
    )
    return _department_out(session, department, registry)


@router.get("/agents/registry")
def list_agents(
    session: DbSession,
    department: str | None = Query(default=None, max_length=40),
    enabled: bool | None = None,
    hierarchy_level: int | None = Query(default=None, ge=0, le=2),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=200, ge=1, le=200),
) -> list[dict[str, Any]]:
    registry = AgentRegistry(session)
    rows = registry.list_agents(
        department=department, enabled=enabled, hierarchy_level=hierarchy_level, search=search, limit=limit
    )
    return [_agent_out(row) for row in rows]


@router.get("/agents/registry/{agent_id}")
def get_agent(agent_id: str, session: DbSession) -> dict[str, Any]:
    registry = AgentRegistry(session)
    try:
        definition = registry.get(agent_id)
    except AgentNotFound as exc:
        raise _http(exc) from exc
    payload = _agent_out(definition)
    prompt_versions = session.scalars(
        sa.select(PromptTemplate.version)
        .where(PromptTemplate.key == definition.prompt_key)
        .order_by(PromptTemplate.version.desc())
        .limit(20)
    ).all()
    payload["prompt_versions"] = [int(version) for version in prompt_versions]
    payload["prompt_active"] = registry.prompt_for(definition)
    recent_tasks = list(
        session.scalars(
            sa.select(AgentTask)
            .where(AgentTask.agent_id == agent_id)
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            .limit(10)
        )
    )
    payload["recent_tasks"] = [
        {
            "task_id": task.id,
            "state": str(task.state),
            "title": task.title,
            "duration_ms": task.duration_ms,
            "tokens": task.tokens_used,
            "created_at": task.created_at.isoformat() if task.created_at else None,
        }
        for task in recent_tasks
    ]
    return payload


@router.patch("/agents/registry/{agent_id}")
def update_agent(agent_id: str, body: AgentUpdate, session: DbSession, audit: Audit) -> dict[str, Any]:
    registry = AgentRegistry(session)
    try:
        definition = registry.update(agent_id, body.model_dump(exclude_none=True))
    except (AgentNotFound, InvalidAgentUpdate) as exc:
        raise _http(exc) from exc
    audit.record(
        "agent.registry.update",
        resource_type="agent_definition",
        resource_id=agent_id,
        metadata={"config_version": definition.config_version, "changed": list(body.model_dump(exclude_none=True))},
    )
    return _agent_out(definition)


@router.post("/agents/registry/{agent_id}/state")
def set_agent_state(agent_id: str, body: EnabledBody, session: DbSession, audit: Audit) -> dict[str, Any]:
    registry = AgentRegistry(session)
    try:
        definition = registry.set_enabled(agent_id, body.enabled)
    except AgentNotFound as exc:
        raise _http(exc) from exc
    audit.record(
        "agent.registry.state",
        resource_type="agent_definition",
        resource_id=agent_id,
        metadata={"enabled": body.enabled},
    )
    return _agent_out(definition)


# --------------------------------------------------------------- orchestration
@router.post("/agents/objectives", status_code=status.HTTP_201_CREATED)
def create_objective(
    body: ObjectiveCreate,
    session: DbSession,
    audit: Audit,
    background: BackgroundTasks,
) -> dict[str, Any]:
    from ...services.ai_runtime import build_service
    from ...services.orchestrator import Budgets as _Budgets

    if body.run_now:
        # Preflight before creating anything: a synchronous run without a model runtime
        # is a 503, not a stored failed objective.
        try:
            build_service(session)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"AI_UNAVAILABLE: {exc}") from exc

    try:
        limits = _Budgets.from_dict(body.limits)
    except ValueError as exc:
        raise _http(exc) from exc
    orchestrator = Orchestrator(session)
    signature = objective_signature(body.objective, body.project_id, limits.as_dict())
    existing = session.scalars(
        sa.select(AgentObjective)
        .where(AgentObjective.state.in_([ObjectiveState.PENDING, ObjectiveState.PLANNING, ObjectiveState.RUNNING]))
        .order_by(AgentObjective.created_at.desc(), AgentObjective.id.desc())
    ).all()
    for candidate in existing:
        if objective_signature(
            candidate.objective,
            (candidate.limits_json or {}).get("project_id"),
            {key: value for key, value in (candidate.limits_json or {}).items() if key in limits.as_dict()},
        ) == signature:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"DUPLICATE_OBJECTIVE: an identical objective is already {candidate.state} ({candidate.id})",
            )
    try:
        objective = orchestrator.create_objective(
            body.objective,
            project_id=body.project_id,
            budgets=limits,
            priority=body.priority,
            # A synchronous run must not also be queued, or the worker races the request.
            enqueue=not body.run_now,
        )
    except ValueError as exc:
        raise _http(exc) from exc
    audit.record(
        "agent.objective.request",
        resource_type="agent_objective",
        resource_id=objective.id,
        metadata={"priority": body.priority, "project_id": body.project_id},
    )
    if body.run_now:
        orchestrator = Orchestrator(session)
        try:
            report = orchestrator.execute(objective.id)
        except RuntimeError as exc:
            # No gateway/governor: say so instead of pretending the run started.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"AI_UNAVAILABLE: {exc}") from exc
        session.refresh(objective)
        payload = orchestrator.status(objective.id)
        payload["run"] = report.as_dict()
        return payload
    return orchestrator.status(objective.id)


@router.get("/agents/objectives")
def list_objectives(
    session: DbSession,
    state: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    stmt = sa.select(AgentObjective).order_by(AgentObjective.created_at.desc(), AgentObjective.id.desc())
    if state:
        stmt = stmt.where(AgentObjective.state == ObjectiveState(state))
    rows = list(session.scalars(stmt.limit(limit)))
    return [
        {
            "objective_id": row.id,
            "objective": row.objective,
            "state": str(row.state),
            "departments": row.departments_json,
            "calls_used": row.calls_used,
            "tokens_used": row.tokens_used,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "error_code": row.error_code,
            "summary": (row.final_report_json or {}).get("summary"),
            "progress": TaskGraph(session, row.id).progress(),
        }
        for row in rows
    ]


@router.get("/agents/objectives/{objective_id}")
def get_objective(objective_id: str, session: DbSession) -> dict[str, Any]:
    orchestrator = Orchestrator(session)
    try:
        return orchestrator.status(objective_id)
    except KeyError as exc:
        raise _http(exc) from exc


@router.get("/agents/objectives/{objective_id}/graph")
def get_objective_graph(objective_id: str, session: DbSession) -> dict[str, Any]:
    if session.get(AgentObjective, objective_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBJECTIVE_NOT_FOUND: unknown objective")
    graph = TaskGraph(session, objective_id)
    payload = graph.as_dict()
    payload["progress"] = graph.progress()
    payload["tasks"] = [_task_out(task) for task in graph.tasks()]
    return payload


@router.post("/agents/objectives/{objective_id}/cancel")
def cancel_objective(objective_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    orchestrator = Orchestrator(session)
    try:
        result = orchestrator.cancel(objective_id)
    except KeyError as exc:
        raise _http(exc) from exc
    audit.record("agent.objective.cancel", resource_type="agent_objective", resource_id=objective_id)
    return result


@router.post("/agents/objectives/{objective_id}/resume")
def resume_objective(objective_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    objective = session.get(AgentObjective, objective_id)
    if objective is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OBJECTIVE_NOT_FOUND: unknown objective")
    if ObjectiveState(objective.state) not in (ObjectiveState.PENDING, ObjectiveState.BLOCKED):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"OBJECTIVE_NOT_RESUMABLE: state is {objective.state}; only pending or blocked objectives can resume",
        )
    objective.state = ObjectiveState.PENDING
    objective.cancel_requested = False
    result = QueueService(session).enqueue(
        "agents.objective",
        {"objective_id": objective.id},
        priority=40,
        max_attempts=2,
        idempotency_key=f"objective:{objective.id}:manual-resume",
    )
    objective.queue_job_id = result.job_id or objective.queue_job_id
    session.commit()
    audit.record("agent.objective.resume", resource_type="agent_objective", resource_id=objective_id)
    return {"objective_id": objective_id, "state": str(objective.state), "queued_job_id": result.job_id}


@router.get("/agents/tasks")
def list_tasks(
    session: DbSession,
    state: str | None = Query(default=None, max_length=20),
    objective_id: str | None = Query(default=None, max_length=26),
    agent_id: str | None = Query(default=None, max_length=48),
    limit: int = Query(default=100, ge=1, le=300),
) -> list[dict[str, Any]]:
    stmt = sa.select(AgentTask).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
    if state:
        stmt = stmt.where(AgentTask.state == TaskState(state))
    if objective_id:
        stmt = stmt.where(AgentTask.objective_id == objective_id)
    if agent_id:
        stmt = stmt.where(AgentTask.agent_id == agent_id)
    return [_task_out(task) for task in session.scalars(stmt.limit(limit))]


@router.get("/agents/tasks/{task_id}")
def get_task(task_id: str, session: DbSession) -> dict[str, Any]:
    task = session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "TASK_NOT_FOUND: unknown task")
    payload = _task_out(task)
    executions = []
    if task.output_json and task.output_json.get("execution_id"):
        executions.append(task.output_json["execution_id"])
    payload["execution_ids"] = executions
    if task.objective_id:
        graph = TaskGraph(session, task.objective_id)
        node = graph.nodes().get(task_id)
        payload["depends_on"] = node.depends_on if node else []
    return payload


@router.post("/agents/tasks/{task_id}/cancel")
def cancel_task(task_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    task = session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "TASK_NOT_FOUND: unknown task")
    if TaskState(task.state) not in (TaskState.PENDING, TaskState.READY, TaskState.ASSIGNED, TaskState.RUNNING, TaskState.WAITING, TaskState.REVIEW):
        raise HTTPException(status.HTTP_409_CONFLICT, f"TASK_TERMINAL: task is already {task.state}")
    task.cancel_requested = True
    if TaskState(task.state) in (TaskState.PENDING, TaskState.READY, TaskState.ASSIGNED, TaskState.WAITING):
        task.state = TaskState.CANCELLED
        task.error_code = "CANCELLED"
        task.error = "cancelled by user"
    session.commit()
    audit.record("agent.task.cancel", resource_type="agent_task", resource_id=task_id)
    return _task_out(task)


# ----------------------------------------------------------------- monitoring
@router.get("/agents/monitor")
def monitor(session: DbSession) -> dict[str, Any]:
    active = list(
        session.scalars(
            sa.select(AgentObjective)
            .where(
                AgentObjective.state.in_(
                    [ObjectiveState.PENDING, ObjectiveState.PLANNING, ObjectiveState.RUNNING, ObjectiveState.COMPILING]
                )
            )
            .order_by(AgentObjective.created_at.desc(), AgentObjective.id.desc())
            .limit(10)
        )
    )
    task_states = dict(
        session.execute(sa.select(AgentTask.state, sa.func.count()).group_by(AgentTask.state)).all()
    )
    queue_states = dict(
        session.execute(sa.select(QueueJob.state, sa.func.count()).group_by(QueueJob.state)).all()
    )
    active_payload = []
    for objective in active:
        graph = TaskGraph(session, objective.id)
        running_task = session.scalars(
            sa.select(AgentTask)
            .where(AgentTask.objective_id == objective.id, AgentTask.state == TaskState.RUNNING)
            .order_by(AgentTask.started_at.desc())
            .limit(1)
        ).first()
        active_payload.append(
            {
                "objective_id": objective.id,
                "objective": objective.objective,
                "state": str(objective.state),
                "calls_used": objective.calls_used,
                "tokens_used": objective.tokens_used,
                "started_at": objective.started_at.isoformat() if objective.started_at else None,
                "running_task": _task_out(running_task) if running_task is not None else None,
                "progress": graph.progress(),
            }
        )
    agent_rows = list(
        session.scalars(
            sa.select(AgentTask)
            .where(AgentTask.state.in_([TaskState.RUNNING, TaskState.REVIEW, TaskState.WAITING]))
            .order_by(AgentTask.started_at.desc())
            .limit(10)
        )
    )
    return {
        "active_objectives": active_payload,
        "active_tasks": [_task_out(task) for task in agent_rows],
        "task_states": {str(key): int(value) for key, value in task_states.items()},
        "queue_states": {str(key): int(value) for key, value in queue_states.items()},
        "generated_at": _now_iso(),
    }


@router.get("/agents/activity")
def activity(
    session: DbSession,
    action: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=300),
) -> list[dict[str, Any]]:
    stmt = (
        sa.select(AuditEvent)
        .where(AuditEvent.action.like("agent.%"))
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    )
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    rows = list(session.scalars(stmt.limit(limit)))
    return [
        {
            "event_id": row.id,
            "action": row.action,
            "actor": row.actor,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "outcome": row.outcome,
            "metadata": row.metadata_redacted_json,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.get("/agents/metrics/summary")
def metrics_summary(session: DbSession) -> dict[str, Any]:
    by_state = dict(session.execute(sa.select(AgentTask.state, sa.func.count()).group_by(AgentTask.state)).all())
    by_department = dict(
        session.execute(sa.select(AgentTask.team_id, sa.func.count()).group_by(AgentTask.team_id)).all()
    )
    totals = session.execute(
        sa.select(
            sa.func.coalesce(sa.func.sum(AgentTask.tokens_used), 0),
            sa.func.coalesce(sa.func.sum(AgentTask.duration_ms), 0),
            sa.func.coalesce(sa.func.avg(AgentTask.duration_ms), 0),
        )
    ).one()
    objectives = dict(
        session.execute(sa.select(AgentObjective.state, sa.func.count()).group_by(AgentObjective.state)).all()
    )
    return {
        "tasks_by_state": {str(key): int(value) for key, value in by_state.items()},
        "tasks_by_department": {str(key): int(value) for key, value in by_department.items()},
        "objectives_by_state": {str(key): int(value) for key, value in objectives.items()},
        "tokens_total": int(totals[0] or 0),
        "duration_ms_total": int(totals[1] or 0),
        "duration_ms_avg": round(float(totals[2] or 0), 1),
        "concurrency_limit": 1,
    }


# --------------------------------------------------------------------- memory
@router.get("/agents/memory")
def list_memory(
    session: DbSession,
    scope: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    service = AgentMemoryService(session)
    try:
        rows = service.list_recent(limit=limit, scope=scope)
    except MemoryScopeError as exc:
        raise _http(exc) from exc
    return [_memory_out(row) for row in rows]


@router.post("/agents/memory", status_code=status.HTTP_201_CREATED)
def write_memory(body: MemoryCreate, session: DbSession) -> dict[str, Any]:
    service = AgentMemoryService(session)
    try:
        result = service.write(
            scope=body.scope,
            owner_id=body.owner_id,
            content=body.content,
            title=body.title,
            project_id=body.project_id,
            objective_id=body.objective_id,
            task_id=body.task_id,
            sensitivity=body.sensitivity,
            ttl_days=body.ttl_days,
        )
    except (MemoryScopeError, SecretInMemory) as exc:
        raise _http(exc) from exc
    session.commit()
    return {
        "memory_id": result.memory_id,
        "scope": result.scope,
        "owner_id": result.owner_id,
        "expires_at": result.expires_at.isoformat() if result.expires_at else None,
    }


@router.post("/agents/memory/query")
def query_memory(body: MemoryQuery, session: DbSession) -> list[dict[str, Any]]:
    service = AgentMemoryService(session)
    try:
        rows = service.query(
            scope=body.scope,
            owner_id=body.owner_id,
            project_id=body.project_id,
            objective_id=body.objective_id,
            search=body.search,
            limit=body.limit,
        )
    except MemoryScopeError as exc:
        raise _http(exc) from exc
    return [_memory_out(row) for row in rows]


@router.get("/agents/memory/stats")
def memory_stats(session: DbSession) -> dict[str, Any]:
    return AgentMemoryService(session).stats()


@router.post("/agents/memory/retention")
def run_retention(session: DbSession, audit: Audit, dry_run: bool = False) -> dict[str, Any]:
    result = AgentMemoryService(session).enforce_retention(dry_run=dry_run)
    if not dry_run:
        audit.record("agent.memory.retention", resource_type="agent_memory", metadata=result)
    return result


# ---------------------------------------------------------------------- tools
@router.get("/agents/tools")
def list_tools(session: DbSession) -> list[dict[str, Any]]:
    return ToolExecutor(session).catalog()


@router.post("/agents/tools/{tool_id}/run")
def run_tool(tool_id: str, body: ToolRunBody, session: DbSession, audit: Audit) -> dict[str, Any]:
    executor = ToolExecutor(session)
    try:
        outcome = executor.execute(
            tool_id,
            agent_id=body.agent_id,
            payload=body.payload,
            objective_id=body.objective_id,
        )
    except (ToolNotFound, ToolDenied, ToolInvalidInput) as exc:
        raise _http(exc) from exc
    session.commit()
    audit.record(
        "agent.tool.manual_run",
        resource_type="tool",
        resource_id=tool_id,
        outcome="success" if outcome.ok else "failure",
        metadata={"agent_id": body.agent_id, "duration_ms": outcome.duration_ms},
    )
    return {
        "tool_id": tool_id,
        "ok": outcome.ok,
        "output": outcome.output,
        "duration_ms": outcome.duration_ms,
        "error_code": outcome.error_code,
        "error": outcome.error,
    }


# ------------------------------------------------------------------ approvals
@router.get("/agents/approvals")
def list_approvals(
    session: DbSession,
    state: str | None = Query(default=None, max_length=20),
    objective_id: str | None = Query(default=None, max_length=26),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    try:
        rows = ApprovalService(session).list(state=state, objective_id=objective_id, limit=limit)
    except ValueError as exc:
        raise _http(exc) from exc
    return [_approval_out(row) for row in rows]


@router.post("/agents/approvals", status_code=status.HTTP_201_CREATED)
def create_approval(body: ApprovalCreate, session: DbSession) -> dict[str, Any]:
    row = ApprovalService(session).create(
        action=body.action,
        target_type=body.target_type,
        target_id=body.target_id,
        summary=body.summary,
        payload=body.payload,
        agent_id=body.agent_id,
        objective_id=body.objective_id,
        rationale=body.rationale,
        risks=body.risks,
        ttl_hours=body.ttl_hours,
        requested_by="local-user",
    )
    session.commit()
    return _approval_out(row)


@router.post("/agents/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, body: ApprovalDecision, session: DbSession, audit: Audit) -> dict[str, Any]:
    service = ApprovalService(session)
    try:
        row = service.decide(approval_id, approve=body.approve, note=body.note)
    except (ApprovalNotFound, ApprovalStateError, ApprovalExpired) as exc:
        raise _http(exc) from exc
    audit.record(
        "approval.decide",
        resource_type="approval_request",
        resource_id=approval_id,
        outcome="success" if body.approve else "denied",
        metadata={"approved": body.approve},
    )
    return _approval_out(row)


@router.post("/agents/approvals/{approval_id}/cancel")
def cancel_approval(approval_id: str, session: DbSession, audit: Audit) -> dict[str, Any]:
    try:
        row = ApprovalService(session).cancel(approval_id)
    except (ApprovalNotFound, ApprovalStateError) as exc:
        raise _http(exc) from exc
    audit.record("approval.cancel", resource_type="approval_request", resource_id=approval_id)
    return _approval_out(row)


@router.post("/agents/approvals/expire")
def expire_approvals(session: DbSession) -> dict[str, Any]:
    expired = ApprovalService(session).expire_stale()
    return {"expired": expired}


# ---------------------------------------------------------------------- helpers

def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
