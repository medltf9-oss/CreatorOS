"""Agent registry: seeding, lookup, versioned configuration.

The catalogue in `agents/catalog.py` is the *seed*; the database is the truth. Seeding
is insert-only, so a human edit to a prompt, temperature or tool list is never
overwritten by a later boot. Editing through the API bumps `config_version`, and a
task stores the version it started with, so an execution record always explains which
configuration produced it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..agents.catalog import (
    ANALYTICS_DEPARTMENT,
    CATALOG_VERSION,
    DEPARTMENTS,
    OUTPUT_CONTRACTS,
    agent_specs,
    build_system_prompt,
    ensure_catalog_is_consistent,
)
from ..db.enums import ApprovalRequirement, MemoryScope, RiskLevel
from ..db.models import AgentDefinition, AgentDepartment, PromptTemplate

EDITABLE_FIELDS = (
    "name",
    "description",
    "mission",
    "system_prompt",
    "model_profile",
    "fallback_profiles_json",
    "temperature",
    "max_tokens",
    "timeout_s",
    "max_retries",
    "allowed_tools_json",
    "restricted_tools_json",
    "task_types_json",
    "memory_scope",
    "enabled",
)

RISK_BY_DEPARTMENT = {
    "publishing_operations": RiskLevel.MEDIUM,
    "quality_governance": RiskLevel.MEDIUM,
    "video_production": RiskLevel.MEDIUM,
}
APPROVAL_BY_DEPARTMENT = {
    "publishing_operations": ApprovalRequirement.ON_WRITE,
}


# Declared fallback chains per profile. A planner that cannot run must degrade to the
# general specialist, not to the tiny routing model, or quality collapses silently.
DEFAULT_FALLBACKS: dict[str, list[str]] = {
    "planner": ["specialist"],
    "specialist": ["router_light"],
    "router_light": [],
    "coding": ["specialist"],
    "vision": ["planner", "specialist"],
    "embedding": [],
}


class AgentNotFound(KeyError):
    code = "AGENT_NOT_FOUND"


class InvalidAgentUpdate(ValueError):
    code = "INVALID_AGENT_CONFIG"


def _memory_scope(value: str) -> MemoryScope:
    try:
        return MemoryScope(value)
    except ValueError:
        return MemoryScope.TASK


def _prompt_schema(spec: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """The stored prompt's machine-readable header.

    Analytics agents carry the Phase 8 flag the plan records for them; every other agent gets the
    plain contract header it has always had.
    """
    schema: dict[str, Any] = {"contract": spec["output_contract"], "fields": contract["fields"]}
    if spec.get("department") == ANALYTICS_DEPARTMENT:
        schema["correlation_is_not_causation"] = True
    return schema


class AgentRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ seed
    def ensure_seeded(self) -> dict[str, int]:
        """Idempotent: insert missing departments/agents, never duplicate or overwrite."""
        ensure_catalog_is_consistent()
        created_departments = 0
        created_agents = 0

        existing_departments = {row.id for row in self.session.scalars(sa.select(AgentDepartment))}
        for spec in DEPARTMENTS:
            if spec["id"] in existing_departments:
                continue
            self.session.add(
                AgentDepartment(
                    id=spec["id"],
                    name=spec["name"],
                    mission=spec["mission"],
                    leader_agent_id=spec["leader"],
                    display_order=spec["order"],
                    enabled=True,
                    catalog_version=1,
                )
            )
            created_departments += 1

        department_names = {spec["id"]: spec["name"] for spec in DEPARTMENTS}
        department_names["executive"] = "Executive"

        existing_agents = {row.agent_id for row in self.session.scalars(sa.select(AgentDefinition))}
        for spec in agent_specs():
            if spec["agent_id"] in existing_agents:
                continue
            contract = OUTPUT_CONTRACTS[spec["output_contract"]]
            prompt = build_system_prompt(spec, department_names[spec["department"]])
            self.session.add(
                AgentDefinition(
                    agent_id=spec["agent_id"],
                    name=spec["name"],
                    team_id=spec["department"],
                    mission=spec["mission"],
                    description=spec["description"],
                    system_prompt=prompt,
                    prompt_key=f"agent.{spec['agent_id']}",
                    input_schema_json={
                        "type": "object",
                        "properties": {
                            "objective": {"type": "string"},
                            "brief": {"type": "string"},
                            "upstream": {"type": "array"},
                        },
                        "required": ["objective"],
                    },
                    output_schema_json={
                        "contract": spec["output_contract"],
                        "fields": contract["fields"],
                        "required": contract["required"],
                    },
                    allowed_tools_json=list(spec["tools"]),
                    model_profile=spec["profile"],
                    fallback_profiles_json=list(DEFAULT_FALLBACKS.get(spec["profile"], [])),
                    restricted_tools_json=["shell.exec", "filesystem.write", "http.fetch", "secrets.read"],
                    risk_level=RISK_BY_DEPARTMENT.get(spec["department"], RiskLevel.LOW),
                    approval_requirement=APPROVAL_BY_DEPARTMENT.get(
                        spec["department"], ApprovalRequirement.NONE
                    ),
                    timeout_s=180 if spec["hierarchy_level"] == 0 else 120,
                    max_tokens=1200 if spec["hierarchy_level"] < 2 else 700,
                    temperature=spec["temperature"],
                    max_retries=2,
                    retry_policy_json={"max_retries": 2, "backoff_seconds": 1.0},
                    task_types_json=list(spec["task_types"]),
                    quality_checks_json=[],
                    hierarchy_level=spec["hierarchy_level"],
                    can_delegate=spec["can_delegate"],
                    config_version=1,
                    memory_scope=_memory_scope(spec["memory_scope"]),
                    catalog_version=CATALOG_VERSION,
                    enabled=True,
                )
            )
            self.session.add(
                PromptTemplate(
                    key=f"agent.{spec['agent_id']}",
                    version=1,
                    body=prompt,
                    schema_json=_prompt_schema(spec, contract),
                    active=True,
                )
            )
            created_agents += 1

        if created_agents == 0:
            # One-time correction of the original placeholder chain, which pointed every
            # non-light profile straight at router_light. Only the exact old default is
            # replaced, so a human edit is never overwritten.
            for definition in self.session.scalars(sa.select(AgentDefinition)):
                desired = list(DEFAULT_FALLBACKS.get(definition.model_profile, []))
                if (definition.fallback_profiles_json or []) == ["router_light"] and desired != ["router_light"]:
                    definition.fallback_profiles_json = desired
                    definition.config_version += 1

        self.session.commit()
        return {"departments": created_departments, "agents": created_agents}

    # --------------------------------------------------------------- queries
    def list_agents(
        self,
        *,
        department: str | None = None,
        enabled: bool | None = None,
        hierarchy_level: int | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> list[AgentDefinition]:
        stmt = sa.select(AgentDefinition)
        if department:
            stmt = stmt.where(AgentDefinition.team_id == department)
        if enabled is not None:
            stmt = stmt.where(AgentDefinition.enabled.is_(enabled))
        if hierarchy_level is not None:
            stmt = stmt.where(AgentDefinition.hierarchy_level == hierarchy_level)
        if search:
            needle = f"%{search.strip()}%"
            stmt = stmt.where(
                sa.or_(
                    AgentDefinition.name.ilike(needle),
                    AgentDefinition.agent_id.ilike(needle),
                    AgentDefinition.mission.ilike(needle),
                )
            )
        stmt = stmt.order_by(AgentDefinition.hierarchy_level, AgentDefinition.team_id, AgentDefinition.agent_id)
        return list(self.session.scalars(stmt.limit(max(1, limit))))

    def get(self, agent_id: str) -> AgentDefinition:
        definition = self.session.get(AgentDefinition, agent_id)
        if definition is None:
            raise AgentNotFound(agent_id)
        return definition

    def by_department(self, department: str) -> list[AgentDefinition]:
        return self.list_agents(department=department)

    def leader_for(self, department: str) -> AgentDefinition | None:
        department_row = self.session.get(AgentDepartment, department)
        if department_row is None or not department_row.leader_agent_id:
            return None
        return self.session.get(AgentDefinition, department_row.leader_agent_id)

    def departments(self, *, enabled_only: bool = False) -> list[AgentDepartment]:
        stmt = sa.select(AgentDepartment).order_by(AgentDepartment.display_order, AgentDepartment.id)
        if enabled_only:
            stmt = stmt.where(AgentDepartment.enabled.is_(True))
        return list(self.session.scalars(stmt))

    def department(self, department_id: str) -> AgentDepartment | None:
        return self.session.get(AgentDepartment, department_id)

    def counts(self) -> dict[str, Any]:
        total = self.session.scalar(sa.select(sa.func.count()).select_from(AgentDefinition)) or 0
        enabled = (
            self.session.scalar(
                sa.select(sa.func.count()).select_from(AgentDefinition).where(AgentDefinition.enabled.is_(True))
            )
            or 0
        )
        departments = self.session.scalar(sa.select(sa.func.count()).select_from(AgentDepartment)) or 0
        by_level = dict(
            self.session.execute(
                sa.select(AgentDefinition.hierarchy_level, sa.func.count()).group_by(AgentDefinition.hierarchy_level)
            ).all()
        )
        return {
            "agents_total": int(total),
            "agents_enabled": int(enabled),
            "agents_disabled": int(total) - int(enabled),
            "departments": int(departments),
            "chiefs": int(by_level.get(0, 0)),
            "leaders": int(by_level.get(1, 0)),
            "specialists": int(by_level.get(2, 0)),
        }

    # --------------------------------------------------------------- updates
    def update(self, agent_id: str, changes: dict[str, Any], *, actor: str = "local-user") -> AgentDefinition:
        definition = self.get(agent_id)
        applied: dict[str, Any] = {}
        for field, value in changes.items():
            if field not in EDITABLE_FIELDS or value is None:
                continue
            if field == "temperature":
                value = max(0.0, min(1.2, float(value)))
            elif field == "max_tokens":
                value = max(64, min(4000, int(value)))
            elif field == "timeout_s":
                value = max(10, min(1800, int(value)))
            elif field == "max_retries":
                value = max(0, min(5, int(value)))
            elif field == "memory_scope":
                if value not in {scope.value for scope in MemoryScope}:
                    raise InvalidAgentUpdate(f"unknown memory scope {value!r}")
                value = MemoryScope(value)
            elif field in {"allowed_tools_json", "restricted_tools_json", "task_types_json", "fallback_profiles_json"}:
                if not isinstance(value, list):
                    raise InvalidAgentUpdate(f"{field} must be a list")
                value = [str(item) for item in value]
            if getattr(definition, field) == value:
                continue
            setattr(definition, field, value)
            applied[field] = value
        if not applied:
            return definition

        definition.config_version += 1
        if "system_prompt" in applied:
            self.session.add(
                PromptTemplate(
                    key=definition.prompt_key,
                    version=definition.config_version,
                    body=str(applied["system_prompt"]),
                    schema_json=definition.output_schema_json or {},
                    active=True,
                )
            )
        self.session.commit()
        return definition

    def set_enabled(self, agent_id: str, enabled: bool) -> AgentDefinition:
        definition = self.get(agent_id)
        if definition.enabled == enabled:
            return definition
        definition.enabled = enabled
        definition.config_version += 1
        self.session.commit()
        return definition

    def set_department_enabled(self, department_id: str, enabled: bool) -> AgentDepartment:
        department = self.department(department_id)
        if department is None:
            raise AgentNotFound(department_id)
        department.enabled = enabled
        department.catalog_version += 1
        for agent in self.by_department(department_id):
            agent.enabled = enabled
        self.session.commit()
        return department

    # -------------------------------------------------------------- helpers
    def tool_permissions(self, agent_id: str) -> tuple[list[str], list[str]]:
        definition = self.get(agent_id)
        allowed = [str(tool) for tool in (definition.allowed_tools_json or [])]
        restricted = [str(tool) for tool in (definition.restricted_tools_json or [])]
        return allowed, restricted

    def assert_tool_permitted(self, agent_id: str, tool_id: str) -> None:
        allowed, restricted = self.tool_permissions(agent_id)
        if tool_id in restricted:
            raise PermissionError(f"{agent_id} is explicitly forbidden from using {tool_id}")
        if tool_id not in allowed:
            raise PermissionError(f"{agent_id} is not granted {tool_id}")

    def prompt_for(self, definition: AgentDefinition) -> str:
        """Latest stored prompt wins; the catalogue text is only the seed."""
        versions: Iterable[PromptTemplate] = self.session.scalars(
            sa.select(PromptTemplate)
            .where(PromptTemplate.key == definition.prompt_key, PromptTemplate.active.is_(True))
            .order_by(PromptTemplate.version.desc())
            .limit(1)
        )
        prompt = next(iter(versions), None)
        if prompt is not None and prompt.body:
            return prompt.body
        return definition.system_prompt or ""
