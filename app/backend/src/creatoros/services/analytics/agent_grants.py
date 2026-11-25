"""Grants for the five analytics tools, following the media/social/publishing precedent.

The catalogue is a seed; an `agent_definition` row may have been edited by a human, so Phase 8 adds
the analytics tools through an explicit, idempotent, audited grant. Nothing is ever removed, and no
grant here can publish: `analytics.recommendation.propose` writes a draft recommendation only.

Grant shape (plan §11.1):

* `analytics_growth` — all five, including the propose write.
* `strategic_intelligence` — the four reads; it interprets, it does not author drafts.
* `research_intelligence` — `analytics.recommendations.read` only, plus its existing report tool.
* `publishing_operations`, `platform_adaptation` — `analytics.publications.read` only.
* `quality_governance` — `analytics.metrics.read` only, for availability-aware verdicts.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...db.models import AgentDefinition, AgentDepartment, AppSetting
from ..backup import AuditService

log = get_logger(__name__)

GRANT_KEY = "analytics.tool_grants_v1"

READ_TOOLS = (
    "analytics.metrics.read",
    "analytics.publications.read",
    "analytics.comparison.read",
    "analytics.recommendations.read",
)

ANALYTICS_TOOL_GRANTS: dict[str, tuple[str, ...]] = {
    "analytics_growth": (*READ_TOOLS, "analytics.recommendation.propose"),
    "strategic_intelligence": READ_TOOLS,
    "research_intelligence": ("analytics.recommendations.read",),
    "publishing_operations": ("analytics.publications.read",),
    "platform_adaptation": ("analytics.publications.read",),
    "quality_governance": ("analytics.metrics.read",),
}


def apply_analytics_tool_grants(session: Session) -> dict[str, object]:
    """Add the analytics tools to existing department agents exactly once."""
    from ..toolbox import TOOLS_BY_ID

    existing = session.get(AppSetting, GRANT_KEY)
    if existing is not None and (existing.value_json or {}).get("applied"):
        return {
            "applied": False,
            "reason": "already applied",
            "agents_updated": (existing.value_json or {}).get("agents_updated", 0),
            "key": GRANT_KEY,
        }

    unknown = {tool for tools in ANALYTICS_TOOL_GRANTS.values() for tool in tools if tool not in TOOLS_BY_ID}
    if unknown:
        raise ValueError(f"analytics tool grants reference unknown tools: {sorted(unknown)}")

    departments = {row.id for row in session.scalars(sa.select(AgentDepartment))}
    updated: list[dict[str, object]] = []
    for department, tools in ANALYTICS_TOOL_GRANTS.items():
        if departments and department not in departments:
            continue
        agents = session.scalars(sa.select(AgentDefinition).where(AgentDefinition.team_id == department)).all()
        for agent in agents:
            current = list(agent.allowed_tools_json or [])
            additions = [tool for tool in tools if tool not in current]
            if not additions:
                continue
            agent.allowed_tools_json = [*current, *additions]
            agent.config_version = (agent.config_version or 1) + 1
            updated.append({"agent_id": agent.agent_id, "added": additions})

    payload: dict[str, object] = {
        "applied": True,
        "version": 1,
        "agents_updated": len(updated),
        "agents": updated,
        "departments": sorted(ANALYTICS_TOOL_GRANTS),
        "note": (
            "Additive only. No granted tool can approve, schedule a run or publish; "
            "analytics.recommendation.propose writes a draft recommendation."
        ),
    }
    if existing is None:
        session.add(AppSetting(key=GRANT_KEY, value_json=payload, scope="app"))
    else:
        existing.value_json = payload
    if updated:
        AuditService(session).record(
            "analytics.tool_grants",
            resource_type="agent_definition",
            resource_id=GRANT_KEY,
            actor="system",
            metadata={"agents_updated": len(updated), "agents": [item["agent_id"] for item in updated]},
        )
    session.flush()
    log.info("analytics tool grants applied: %d agent definition(s) updated", len(updated))
    return payload


def analytics_tools_by_department(session: Session) -> dict[str, list[str]]:
    """Which agents hold an analytics tool, derived from real rows rather than the catalogue."""
    result: dict[str, list[str]] = {}
    for agent in session.scalars(sa.select(AgentDefinition)).all():
        held = [tool for tool in (agent.allowed_tools_json or []) if str(tool).startswith("analytics.")]
        if held:
            result.setdefault(agent.team_id, []).append(agent.agent_id)
    return {department: sorted(set(agents)) for department, agents in sorted(result.items())}


def publishing_tools_held_by_analytics_roles(session: Session) -> list[str]:
    """Read tools only. Used by a test to prove analytics roles cannot publish."""
    offenders: list[str] = []
    for agent in session.scalars(sa.select(AgentDefinition)).all():
        if agent.team_id not in ANALYTICS_TOOL_GRANTS:
            continue
        for tool in agent.allowed_tools_json or []:
            if str(tool).startswith(("publishing.", "platform.publish")):
                offenders.append(f"{agent.agent_id}: {tool}")
    return sorted(set(offenders))


__all__ = [
    "ANALYTICS_TOOL_GRANTS",
    "GRANT_KEY",
    "READ_TOOLS",
    "analytics_tools_by_department",
    "apply_analytics_tool_grants",
    "publishing_tools_held_by_analytics_roles",
]
