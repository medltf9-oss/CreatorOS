"""Publishing agent-tool tests: read the queue, draft a destination, never publish.

The interesting assertions here are the negative ones. An agent may see the queue and may create a
draft; it may not approve, schedule, retry, cancel or publish, and it may not reach the executor.
Those absences are checked against the tool table and the module graph, not against a comment.
"""
from __future__ import annotations

import inspect
import json

import sqlalchemy as sa
from publishing_fixtures import make_account, make_asset

from creatoros.db.enums import PublishState
from creatoros.db.models import AgentDefinition, AppSetting, PublishJob
from creatoros.services import toolbox
from creatoros.services.publishing.agent_grants import (
    GRANT_KEY,
    PUBLISHING_TOOL_GRANTS,
    apply_publishing_tool_grants,
    publishing_tools_by_department,
)
from creatoros.services.toolbox import TOOLS_BY_ID, ToolExecutor


def execute(session, tool_id, payload, *, agent="publishing_director"):
    from creatoros.services.toolbox import ensure_tools_seeded

    ensure_tools_seeded(session)
    return ToolExecutor(session, enforce_grant=False).execute(tool_id, agent_id=agent, payload=payload)


def test_the_publishing_tools_are_registered_with_the_right_risk():
    for tool_id in ("publishing.queue.read", "publishing.destination.read"):
        spec = TOOLS_BY_ID[tool_id]
        assert spec.category == "read"
        assert spec.approval.value == "none"

    write = TOOLS_BY_ID["publishing.draft.create"]
    assert write.category == "write"
    assert write.approval.value != "none", "creating a draft is still a write"


def test_no_agent_tool_can_publish_and_the_toolbox_cannot_reach_the_executor():
    assert "publish.destination" not in TOOLS_BY_ID
    assert "publish.post" not in TOOLS_BY_ID
    for tool_id in TOOLS_BY_ID:
        lowered = tool_id.lower()
        assert not lowered.endswith(".publish"), tool_id
        assert not lowered.endswith(".approve"), tool_id

    source = inspect.getsource(toolbox)
    assert "PublishExecutor" not in source
    assert "PublishActions" not in source
    assert ".enqueue(" not in source
    assert "publish_now" not in source


def test_the_queue_tool_reports_states_and_never_media_or_credentials(session):
    account = make_account(session, platform="facebook")
    asset = make_asset(session)
    outcome = execute(session, "publishing.draft.create", {
        "title": "Morning routine",
        "platform": "facebook",
        "account_id": account.id,
        "caption": "A quiet start",
        "asset_id": asset.id,
    })
    assert outcome.ok is True, outcome.error

    queue = execute(session, "publishing.queue.read", {})
    assert queue.ok is True, queue.error
    assert queue.output["count"] == 1
    row = queue.output["items"][0]
    assert row["state"] == "draft"
    assert row["needs_human"] is False
    assert queue.output["late_policy"]["policy"] == "ask"
    assert "Only a human can approve or publish" in queue.output["note"]

    serialised = json.dumps(queue.output)
    assert "source_path" not in serialised
    assert "token" not in serialised.lower()


def test_the_queue_tool_can_filter_by_state_and_refuses_an_unknown_one(session):
    account = make_account(session, platform="facebook")
    asset = make_asset(session)
    created = execute(
        session,
        "publishing.draft.create",
        {"title": "Second", "platform": "facebook", "account_id": account.id, "asset_id": asset.id},
    )
    job = session.get(PublishJob, created.output["job_id"])
    job.job_state = PublishState.REQUIRES_USER_ACTION
    job.state_reason = "approval_revoked"
    session.flush()

    blocked = execute(session, "publishing.queue.read", {"state": "requires_user_action"})
    assert blocked.ok is True
    assert blocked.output["count"] == 1
    assert blocked.output["items"][0]["needs_human"] is True

    draft = execute(session, "publishing.queue.read", {"state": "draft"})
    assert draft.output["count"] == 0

    bad = execute(session, "publishing.queue.read", {"state": "half-published"})
    assert bad.ok is False
    assert "not a publish state" in str(bad.error)


def test_the_destination_tool_returns_the_ledger_but_not_a_secret(session):
    account = make_account(session, platform="facebook")
    asset = make_asset(session)
    created = execute(
        session,
        "publishing.draft.create",
        {"title": "Ledger", "platform": "facebook", "account_id": account.id, "asset_id": asset.id},
    )
    outcome = execute(session, "publishing.destination.read", {"job_id": created.output["job_id"]})
    assert outcome.ok is True, outcome.error
    assert outcome.output["state"] == "draft"
    assert "attempts_log" in outcome.output
    assert "deliveries" in outcome.output
    assert "reconciliations" in outcome.output
    assert "approval_record" in outcome.output
    serialised = json.dumps(outcome.output)
    assert "access_token" not in serialised
    assert "upload_url" not in serialised


def test_the_destination_tool_rejects_an_unknown_id(session):
    outcome = execute(session, "publishing.destination.read", {"job_id": "01M8MISSING0000000000000"})
    assert outcome.ok is False
    assert "does not exist" in str(outcome.error)


def test_a_drafted_destination_is_a_draft_and_says_what_happens_next(session):
    account = make_account(session, platform="facebook")
    asset = make_asset(session)
    outcome = execute(
        session,
        "publishing.draft.create",
        {
            "title": "Agent draft",
            "platform": "facebook",
            "account_id": account.id,
            "caption": "Written by an agent",
            "asset_id": asset.id,
        },
    )
    assert outcome.ok is True, outcome.error
    assert outcome.output["state"] == "draft"
    assert "approval" in outcome.output["next_step"]

    job = session.get(PublishJob, outcome.output["job_id"])
    assert job.job_state is PublishState.DRAFT
    assert job.approval_request_id is None, "an agent cannot even request an approval"
    assert job.scheduled_at_utc is None
    assert job.field_provenance_json["caption"].startswith("agent:")


def test_a_draft_for_an_unknown_account_is_a_validation_error(session):
    asset = make_asset(session)
    outcome = execute(
        session,
        "publishing.draft.create",
        {"title": "Nope", "platform": "facebook", "account_id": "01M8MISSING0000000000000", "asset_id": asset.id},
    )
    assert outcome.ok is False
    assert "ACCOUNT" in str(outcome.error).upper() or "not found" in str(outcome.error).lower()


def test_grants_are_additive_idempotent_and_audited(session):
    from creatoros.services.registry import AgentRegistry

    AgentRegistry(session).ensure_seeded()
    session.flush()

    first = apply_publishing_tool_grants(session)
    assert first["applied"] is True
    assert first["agents_updated"] > 0

    second = apply_publishing_tool_grants(session)
    assert second["applied"] is False
    assert second["reason"] == "already applied"

    holders = publishing_tools_by_department(session)
    assert "publishing_operations" in holders
    for department in PUBLISHING_TOOL_GRANTS:
        assert holders.get(department), f"{department} should hold at least one publishing tool"

    row = session.get(AppSetting, GRANT_KEY)
    assert row is not None and row.value_json["applied"] is True

    # Nobody outside the granted departments gained a publishing tool.
    granted = set(PUBLISHING_TOOL_GRANTS)
    for agent in session.scalars(sa.select(AgentDefinition)).all():
        held = [tool for tool in (agent.allowed_tools_json or []) if str(tool).startswith("publishing.")]
        if held:
            assert agent.team_id in granted, f"{agent.agent_id} in {agent.team_id} holds {held}"


def test_a_grant_only_adds_tools_the_catalogue_knows(session):
    for tools in PUBLISHING_TOOL_GRANTS.values():
        for tool_id in tools:
            assert tool_id in TOOLS_BY_ID, tool_id


def test_the_grant_does_not_touch_an_agents_other_tools(session):
    from creatoros.services.registry import AgentRegistry

    AgentRegistry(session).ensure_seeded()
    session.flush()
    agent = session.scalars(
        sa.select(AgentDefinition).where(AgentDefinition.team_id == "publishing_operations")
    ).first()
    before = list(agent.allowed_tools_json or [])
    apply_publishing_tool_grants(session)
    session.refresh(agent)
    after = list(agent.allowed_tools_json or [])
    assert after[: len(before)] == before, "existing tools keep their order and are never removed"
    assert set(after) - set(before) == set(PUBLISHING_TOOL_GRANTS["publishing_operations"])
