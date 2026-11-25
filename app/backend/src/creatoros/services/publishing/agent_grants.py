"""Grants for the publishing tools to the agents that plan and publish.

Same discipline as the media and social grants: the catalogue is a seed and an existing
`agent_definition` row may have been edited by a human, so Phase 7 adds tools through an explicit,
idempotent, audited grant keyed on an app setting. Nothing is ever removed.

What is granted, and what is deliberately not:

* `publishing.queue.read` and `publishing.destination.read` - read-only views of the queue and of one
  destination's ledger. They cannot approve, retry or publish.
* `publishing.draft.create` - creates a draft destination (a write, gated by the existing on-write
  approval). It cannot request an approval, decide one, schedule a run or enqueue anything.

There is no agent tool that reaches `PublishExecutor`. Publishing is a human action in this project
(D4), and the absence is enforced by a test rather than by a comment.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...db.models import AgentDefinition, AgentDepartment, AppSetting
from ..backup import AuditService

log = get_logger(__name__)

GRANT_KEY = "publishing.tool_grants_v1"

PUBLISHING_TOOL_GRANTS: dict[str, tuple[str, ...]] = {
    # Read the queue: everyone who plans, adapts, publishes, audits or measures.
    "strategic_intelligence": ("publishing.queue.read", "publishing.destination.read"),
    "platform_adaptation": ("publishing.queue.read", "publishing.destination.read"),
    "quality_governance": ("publishing.queue.read", "publishing.destination.read"),
    "analytics_growth": ("publishing.queue.read", "publishing.destination.read"),
    # Operate the queue: this one also drafts, which is a write and needs the on-write approval.
    "publishing_operations": (
        "publishing.queue.read",
        "publishing.destination.read",
        "publishing.draft.create",
    ),
}


def apply_publishing_tool_grants(session: Session) -> dict[str, object]:
    """Add the publishing tools to existing department agents exactly once."""
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
        tool for tools in PUBLISHING_TOOL_GRANTS.values() for tool in tools if tool not in TOOLS_BY_ID
    }
    if unknown:
        raise ValueError(f"publishing tool grants reference unknown tools: {sorted(unknown)}")

    departments = {row.id for row in session.scalars(sa.select(AgentDepartment))}
    updated: list[dict[str, object]] = []
    for department, tools in PUBLISHING_TOOL_GRANTS.items():
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

    payload: dict[str, object] = {
        "applied": True,
        "version": 1,
        "agents_updated": len(updated),
        "agents": updated,
        "departments": sorted(PUBLISHING_TOOL_GRANTS),
        "note": "Additive only. No granted tool can approve, schedule a run or publish.",
    }
    if existing is None:
        session.add(AppSetting(key=GRANT_KEY, value_json=payload, scope="app"))
    else:
        existing.value_json = payload
    if updated:
        AuditService(session).record(
            "publishing.tool_grants",
            resource_type="agent_definition",
            resource_id=GRANT_KEY,
            actor="system",
            metadata={"agents_updated": len(updated), "agents": [item["agent_id"] for item in updated]},
        )
    session.flush()
    log.info("publishing tool grants applied: %d agent definition(s) updated", len(updated))
    return payload


def publishing_tools_by_department(session: Session) -> dict[str, list[str]]:
    """Which agents hold a publishing tool, derived from real rows."""
    rows = session.scalars(sa.select(AgentDefinition)).all()
    result: dict[str, list[str]] = {}
    for agent in rows:
        held = [tool for tool in (agent.allowed_tools_json or []) if str(tool).startswith("publishing.")]
        if held:
            result.setdefault(agent.team_id, []).append(agent.agent_id)
    return {department: sorted(set(agents)) for department, agents in sorted(result.items())}


__all__ = [
    "GRANT_KEY",
    "PUBLISHING_TOOL_GRANTS",
    "apply_publishing_tool_grants",
    "publishing_tools_by_department",
]
