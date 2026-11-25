"""Tool registry tests: authorization, schema validation, real effects, no escape."""
from __future__ import annotations

import json

import pytest

from creatoros.db.enums import ConnectionStatus, Platform, RightsStatus
from creatoros.db.models import (
    ApprovalRequest,
    ContentProject,
    PlatformVariant,
    PublishJob,
    SocialAccount,
    Transcript,
)
from creatoros.db.models import (
    ToolRegistry as ToolRegistryRow,
)
from creatoros.services.registry import AgentRegistry
from creatoros.services.toolbox import (
    TOOL_SPECS,
    ToolDenied,
    ToolExecutor,
    ToolInvalidInput,
    ToolNotFound,
    ensure_tools_seeded,
)


@pytest.fixture()
def tools(session):
    AgentRegistry(session).ensure_seeded()
    ensure_tools_seeded(session)
    return ToolExecutor(session)


@pytest.fixture()
def project(session, asset_factory):
    asset = asset_factory(rights_status=RightsStatus.OWNED)
    row = ContentProject(title="Hook test", brief="Explain why hooks matter", source_asset_id=asset.id)
    session.add(row)
    session.flush()
    session.add(
        PlatformVariant(
            project_id=row.id,
            platform=Platform.INSTAGRAM,
            caption="A caption that explains the hook",
            hashtags_json=["hooks", "video"],
            output_path="<DATA_ROOT>\\data\\media\\exports\\v1.mp4",
        )
    )
    session.flush()
    return row


def test_tool_surface_is_complete_and_seeded(tools, session):
    # 12 Phase 4 tools, 4 media tools, 2 social tools, 3 publishing tools and 5 analytics tools.
    assert len(TOOL_SPECS) == 26
    assert session.query(ToolRegistryRow).count() == 26
    catalog = tools.catalog()
    assert all(entry["registered"] and entry["enabled"] for entry in catalog)
    assert {entry["category"] for entry in catalog} == {"read", "compute", "write"}


def test_unknown_tool_is_refused(tools):
    with pytest.raises(ToolNotFound):
        tools.execute("shell.exec", agent_id="hook_writer_agent", payload={})


def test_agent_without_the_grant_is_denied(tools):
    with pytest.raises(ToolDenied):
        tools.execute("media.inspect", agent_id="hook_writer_agent", payload={"asset_id": "x"})


def test_disabled_tool_is_refused(tools, session):
    row = session.get(ToolRegistryRow, "internal.search")
    row.enabled = False
    session.flush()
    with pytest.raises(ToolDenied):
        tools.execute("internal.search", agent_id="hook_writer_agent", payload={"query": "hooks"})


def test_invalid_payload_is_rejected(tools, project):
    with pytest.raises(ToolInvalidInput):
        tools.execute("project.metadata.read", agent_id="research_synthesis_agent", payload={"project_id": ""})


def test_project_metadata_read(tools, project):
    outcome = tools.execute(
        "project.metadata.read", agent_id="research_synthesis_agent", payload={"project_id": project.id}
    )
    assert outcome.ok
    assert outcome.output["title"] == "Hook test"
    assert outcome.output["source_asset"]["rights_status"] == "owned"
    assert outcome.output["variant_count"] == 1


def test_missing_project_is_a_tool_error_not_a_crash(tools):
    outcome = tools.execute(
        "project.metadata.read", agent_id="research_synthesis_agent", payload={"project_id": "01AAAAAAAAAAAAAAAAAAAAAAAA"}
    )
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_INPUT"


def test_caption_compose_is_deterministic_and_checks_limits(tools):
    payload = {
        "platform": "instagram",
        "hook": "Stop scripting hooks like this",
        "body": "Here is what works instead.",
        "cta": "Save this.",
        "hashtags": ["hooks", "#video", "hooks"],
    }
    first = tools.execute("caption.compose", agent_id="hook_writer_agent", payload=payload)
    second = tools.execute("caption.compose", agent_id="hook_writer_agent", payload=payload)
    assert first.ok and second.ok
    assert first.output["caption"] == second.output["caption"]
    assert first.output["caption_hash"] == second.output["caption_hash"]
    assert first.output["hashtags"] == ["hooks", "video"]
    assert first.output["chars"] <= first.output["limit"]


def test_caption_compose_flags_overflow(tools):
    outcome = tools.execute(
        "caption.compose",
        agent_id="hook_writer_agent",
        payload={
            "platform": "tiktok",
            "hook": "h" * 400,
            "body": "b" * 1600,
            "cta": "c" * 200,
            "hashtags": [f"tag{index}" for index in range(20)],
        },
    )
    assert outcome.ok
    assert outcome.output["over_limit"] is True
    assert outcome.output["warnings"]


def test_readiness_check_fails_closed_without_account(tools, project):
    outcome = tools.execute(
        "content.readiness.check",
        agent_id="publishing_validation_agent",
        payload={"project_id": project.id, "platforms": ["instagram"]},
    )
    assert outcome.ok
    assert outcome.output["ready"] is False
    failed = {check["check"] for check in outcome.output["blocking_failures"]}
    assert "account_instagram" in failed


def test_publish_plan_generates_no_job_without_an_account(tools, project, session):
    outcome = tools.execute(
        "publish.plan.generate",
        agent_id="publishing_preparation_agent",
        payload={"project_id": project.id, "platforms": ["instagram"]},
    )
    assert outcome.ok
    assert outcome.output["created"] == []
    assert outcome.output["blocked"][0]["reason"] == "NO_CONNECTED_ACCOUNT"
    assert outcome.output["external_actions_taken"] is False
    assert session.query(PublishJob).count() == 0


def test_publish_plan_creates_a_draft_job_for_a_connected_account(tools, project, session):
    account = SocialAccount(
        platform=Platform.INSTAGRAM,
        provider_account_id="17841400000000000",
        display_name="Test IG",
        connection_status=ConnectionStatus.CONNECTED,
    )
    session.add(account)
    session.flush()
    outcome = tools.execute(
        "publish.plan.generate",
        agent_id="publishing_preparation_agent",
        payload={"project_id": project.id, "platforms": ["instagram"]},
    )
    assert outcome.ok
    assert len(outcome.output["created"]) == 1
    job = session.query(PublishJob).one()
    assert job.job_state.value == "draft"
    assert job.account_id == account.id
    again = tools.execute(
        "publish.plan.generate",
        agent_id="publishing_preparation_agent",
        payload={"project_id": project.id, "platforms": ["instagram"]},
    )
    assert again.output["created"] == []
    assert again.output["blocked"][0]["reason"] == "DUPLICATE_PLAN"
    assert session.query(PublishJob).count() == 1


def test_report_is_written_under_the_data_root(tools):
    outcome = tools.execute(
        "report.generate",
        agent_id="research_synthesis_agent",
        payload={"title": "Research brief", "sections": [{"heading": "Findings", "body": "Hooks matter."}]},
    )
    assert outcome.ok
    from pathlib import Path

    path = Path(outcome.output["path"])
    assert path.exists()
    assert "Research brief" in path.read_text(encoding="utf-8")
    assert len(outcome.output["sha256"]) == 64


def test_approval_request_is_hash_bound_and_not_executed(tools, session):
    outcome = tools.execute(
        "human.approval.request",
        agent_id="publishing_preparation_agent",
        payload={
            "action": "publish.post",
            "target_type": "platform_variant",
            "target_id": "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "summary": "Publish the instagram variant",
            "payload": {"caption": "hello"},
        },
    )
    assert outcome.ok
    row = session.query(ApprovalRequest).one()
    assert row.payload_hash == outcome.output["payload_hash"]
    assert row.state.value == "pending"
    assert outcome.output["executed"] is False


def test_duplicate_pending_approval_is_refused(tools):
    payload = {
        "action": "publish.post",
        "target_type": "platform_variant",
        "target_id": "01BBBBBBBBBBBBBBBBBBBBBBBB",
        "summary": "Publish variant",
        "payload": {"caption": "hello"},
    }
    first = tools.execute("human.approval.request", agent_id="publishing_preparation_agent", payload=payload)
    second = tools.execute("human.approval.request", agent_id="publishing_preparation_agent", payload=payload)
    assert first.ok
    assert second.ok is False
    assert second.error_code == "APPROVAL_ALREADY_PENDING"


def test_transcript_read_reports_a_missing_transcript(tools, asset_factory):
    asset = asset_factory()
    outcome = tools.execute("transcript.read", agent_id="subtitle_generation_agent", payload={"asset_id": asset.id})
    assert outcome.ok is False
    assert outcome.error_code == "NO_TRANSCRIPT"


def test_transcript_read_returns_stored_text(tools, session, asset_factory):
    asset = asset_factory()
    session.add(Transcript(asset_id=asset.id, language="en", text="the hook is the promise", segments_json=[{"s": 0}]))
    session.flush()
    outcome = tools.execute("transcript.read", agent_id="subtitle_generation_agent", payload={"asset_id": asset.id})
    assert outcome.ok
    assert outcome.output["text"] == "the hook is the promise"
    assert outcome.output["segment_count"] == 1


def test_media_inspect_never_reads_outside_the_root(tools, session, asset_factory):
    asset = asset_factory(source_path="C:\\Windows\\System32\\drivers\\etc\\hosts")
    outcome = tools.execute("media.inspect", agent_id="video_inspection_agent", payload={"asset_id": asset.id})
    assert outcome.ok
    assert outcome.output["probe"]["ran"] is False
    assert "outside the CreatorOS root" in outcome.output["probe"]["reason"]


def test_media_inspect_reports_missing_file(tools, asset_factory):
    asset = asset_factory(source_path="<DATA_ROOT>\\data\\media\\incoming\\nope.mp4")
    outcome = tools.execute("media.inspect", agent_id="video_inspection_agent", payload={"asset_id": asset.id})
    assert outcome.ok
    assert outcome.output["probe"]["ran"] is False


def test_internal_search_finds_projects_and_memory(tools, project):
    outcome = tools.execute(
        "internal.search", agent_id="topic_discovery_agent", payload={"query": "hooks", "project_id": project.id}
    )
    assert outcome.ok
    kinds = {hit["kind"] for hit in outcome.output["results"]}
    assert "content_project" in kinds


def test_brand_rules_without_a_brand_is_honest(tools):
    outcome = tools.execute("brand.rules.read", agent_id="brand_strategy_agent", payload={})
    assert outcome.ok
    assert outcome.output["brand"] is None
    assert outcome.output["warnings"]


def test_tool_calls_are_audited(tools, project, session):
    from creatoros.db.models import AuditEvent

    tools.execute("project.metadata.read", agent_id="research_synthesis_agent", payload={"project_id": project.id})
    events = session.query(AuditEvent).filter(AuditEvent.action == "agent.tool.call").all()
    assert len(events) == 1
    assert events[0].resource_id == "project.metadata.read"
    assert events[0].metadata_redacted_json["agent_id"] == "research_synthesis_agent"


def test_tool_input_schemas_are_json_serialisable(tools):
    for entry in tools.catalog():
        json.dumps(entry["input_schema"])


def test_executor_can_run_without_grants_for_internal_use(session):
    AgentRegistry(session).ensure_seeded()
    ensure_tools_seeded(session)
    executor = ToolExecutor(session, enforce_grant=False)
    outcome = executor.execute("internal.search", agent_id="not_an_agent", payload={"query": "anything"})
    assert outcome.ok
