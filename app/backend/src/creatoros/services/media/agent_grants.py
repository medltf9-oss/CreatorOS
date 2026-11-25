"""Grant the new media tools to the agents that already exist in a shipped database.

The catalogue is a seed, not a source of truth: an existing database has human-edited
`agent_definition` rows and must never be overwritten wholesale. Phase 5 therefore adds
the media tools through an explicit, idempotent, one-time grant keyed on an app setting,
so the upgrade is visible, auditable and cannot run twice.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...db.models import AgentDefinition, AgentDepartment, AppSetting
from ..backup import AuditService

log = get_logger(__name__)

GRANT_KEY = "media.tool_grants_v1"

# Department → tools the department gains in Phase 5. Only these are added; nothing is
# removed, so an operator who granted extra tools keeps them.
MEDIA_TOOL_GRANTS: dict[str, tuple[str, ...]] = {
    "video_production": (
        "media.derivatives.list",
        "media.scene.scan",
        "media.transcode.recommend",
        "media.quality.report",
    ),
    "design_visual": ("media.derivatives.list", "media.quality.report"),
    "platform_adaptation": ("media.quality.report",),
    "quality_governance": ("media.derivatives.list", "media.scene.scan", "media.quality.report"),
}


def apply_media_tool_grants(session: Session) -> dict[str, object]:
    """Add the media tools to existing department agents exactly once."""
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
        tool
        for tools in MEDIA_TOOL_GRANTS.values()
        for tool in tools
        if tool not in TOOLS_BY_ID
    }
    if unknown:
        raise ValueError(f"media tool grants reference unknown tools: {sorted(unknown)}")

    departments = {row.id for row in session.scalars(sa.select(AgentDepartment))}
    updated: list[dict[str, object]] = []
    for department, tools in MEDIA_TOOL_GRANTS.items():
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
        "departments": sorted(MEDIA_TOOL_GRANTS),
        "note": "Additive only: existing grants are preserved, nothing is revoked.",
    }
    if existing is None:
        session.add(AppSetting(key=GRANT_KEY, value_json=payload, scope="app"))
    else:
        existing.value_json = payload
    if updated:
        AuditService(session).record(
            "media.tool_grants",
            resource_type="agent_definition",
            resource_id="media.tool_grants_v1",
            actor="system",
            metadata={"agents_updated": len(updated), "agents": [item["agent_id"] for item in updated]},
        )
    session.commit()
    log.info("media tool grants applied: %s agent definition(s) updated", len(updated))
    return payload


def media_tools_by_department(session: Session) -> dict[str, list[str]]:
    """Which agents currently hold a media tool, derived from real rows."""
    rows = session.scalars(sa.select(AgentDefinition)).all()
    result: dict[str, list[str]] = {}
    for agent in rows:
        held = [tool for tool in (agent.allowed_tools_json or []) if str(tool).startswith("media.")]
        if held:
            result.setdefault(agent.team_id, []).append(agent.agent_id)
    return {department: sorted(set(agents)) for department, agents in sorted(result.items())}


__all__ = ["GRANT_KEY", "MEDIA_TOOL_GRANTS", "apply_media_tool_grants", "media_tools_by_department"]
