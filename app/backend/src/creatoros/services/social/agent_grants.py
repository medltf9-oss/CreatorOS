"""Grant the read-only social tools to agents that already exist in a shipped database.

Same discipline as the media grants: the catalogue is a seed, and an existing `agent_definition`
row may have been edited by a human, so Phase 6 adds tools through an explicit, idempotent,
audited grant keyed on an app setting. Nothing is ever removed.

The two tools are read-only:

* `social.accounts.list` - which accounts exist, their connection and token state, and what they
  can do;
* `social.capabilities.read` - the stored verdicts, required and missing scopes and reasons.

No agent gets a tool that can read a credential, refresh a token or publish anything. Phase 7 adds
publishing tools with their own approval requirements; those do not exist yet.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...db.models import AgentDefinition, AgentDepartment, AppSetting
from ..backup import AuditService

log = get_logger(__name__)

GRANT_KEY = "social.tool_grants_v1"

SOCIAL_TOOL_GRANTS: dict[str, tuple[str, ...]] = {
    # Who needs to know what the accounts can do: the people who plan, adapt, publish, measure
    # quality and decide strategy.
    "strategic_intelligence": ("social.accounts.list", "social.capabilities.read"),
    "platform_adaptation": ("social.accounts.list", "social.capabilities.read"),
    "publishing_operations": ("social.accounts.list", "social.capabilities.read"),
    "quality_governance": ("social.accounts.list", "social.capabilities.read"),
    "analytics_growth": ("social.accounts.list", "social.capabilities.read"),
}


def apply_social_tool_grants(session: Session) -> dict[str, object]:
    """Add the social tools to existing department agents exactly once."""
    from ..toolbox import TOOLS_BY_ID

    existing = session.get(AppSetting, GRANT_KEY)
    if existing is not None and (existing.value_json or {}).get("applied"):
        return {
            "applied": False,
            "reason": "already applied",
            "agents_updated": (existing.value_json or {}).get("agents_updated", 0),
            "key": GRANT_KEY,
        }

    unknown = {
        tool for tools in SOCIAL_TOOL_GRANTS.values() for tool in tools if tool not in TOOLS_BY_ID
    }
    if unknown:
        raise ValueError(f"social tool grants reference unknown tools: {sorted(unknown)}")

    departments = {row.id for row in session.scalars(sa.select(AgentDepartment))}
    updated: list[dict[str, object]] = []
    for department, tools in SOCIAL_TOOL_GRANTS.items():
        if departments and department not in departments:
            continue
        agents = session.scalars(
            sa.select(AgentDefinition).where(AgentDefinition.team_id == department)
        ).all()
        for agent in agents:
            current = list(agent.allowed_tools_json or [])
            additions = [tool for tool in tools if tool not in current]
            if not additions:
                continue
            agent.allowed_tools_json = [*current, *additions]
            agent.config_version = (agent.config_version or 1) + 1
            updated.append({"agent_id": agent.agent_id, "added": additions})

    payload = {
        "applied": True,
        "version": 1,
        "agents_updated": len(updated),
        "agents": updated,
        "departments": sorted(SOCIAL_TOOL_GRANTS),
        "note": "Additive only, read-only tools: no credential access and no publishing.",
    }
    if existing is None:
        session.add(AppSetting(key=GRANT_KEY, value_json=payload, scope="app"))
    else:
        existing.value_json = payload
    if updated:
        AuditService(session).record(
            "social.tool_grants",
            resource_type="agent_definition",
            resource_id="social.tool_grants_v1",
            actor="system",
            metadata={"agents_updated": len(updated), "agents": [item["agent_id"] for item in updated]},
        )
    session.commit()
    log.info("social tool grants applied: %s agent definition(s) updated", len(updated))
    return payload


def social_tools_by_department(session: Session) -> dict[str, list[str]]:
    """Which agents hold a social tool, derived from real rows."""
    rows = session.scalars(sa.select(AgentDefinition)).all()
    result: dict[str, list[str]] = {}
    for agent in rows:
        held = [tool for tool in (agent.allowed_tools_json or []) if str(tool).startswith("social.")]
        if held:
            result.setdefault(agent.team_id, []).append(agent.agent_id)
    return {department: sorted(set(agents)) for department, agents in sorted(result.items())}


__all__ = ["GRANT_KEY", "SOCIAL_TOOL_GRANTS", "apply_social_tool_grants", "social_tools_by_department"]
