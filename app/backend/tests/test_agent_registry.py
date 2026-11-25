"""Registry tests: the seeded org chart, idempotency, versioning and prompt history."""
from __future__ import annotations

import pytest

from creatoros.agents.catalog import (
    CATALOG_VERSION,
    DEPARTMENTS,
    LEADERS,
    SPECIALISTS,
    TOOL_IDS,
    agent_specs,
    build_system_prompt,
    ensure_catalog_is_consistent,
)
from creatoros.db.models import AgentDefinition, AgentDepartment, PromptTemplate
from creatoros.services.registry import AgentNotFound, AgentRegistry, InvalidAgentUpdate


def test_catalogue_defines_exactly_111_agents():
    specs = agent_specs()
    assert len(specs) == 111
    assert sum(1 for spec in specs if spec["hierarchy_level"] == 0) == 1
    assert sum(1 for spec in specs if spec["hierarchy_level"] == 1) == 10
    assert sum(1 for spec in specs if spec["hierarchy_level"] == 2) == 100
    assert len({spec["agent_id"] for spec in specs}) == 111


def test_catalogue_departments_and_leaders_line_up():
    department_ids = {department["id"] for department in DEPARTMENTS}
    assert len(department_ids) == 10
    assert {leader["department"] for leader in LEADERS} == department_ids
    assert {specialist["department"] for specialist in SPECIALISTS} == department_ids
    specs = agent_specs()
    for leader in LEADERS:
        resolved = next(item for item in specs if item["agent_id"] == leader["agent_id"])
        assert resolved["can_delegate"] is True
        assert resolved["hierarchy_level"] == 1
    for specialist in SPECIALISTS:
        resolved = next(item for item in specs if item["agent_id"] == specialist["agent_id"])
        assert resolved["can_delegate"] is False
        assert resolved["hierarchy_level"] == 2


def test_catalogue_consistency_check_rejects_nothing():
    ensure_catalog_is_consistent()


def test_every_agent_declares_only_real_tools():
    for spec in agent_specs():
        for tool in spec["tools"]:
            assert tool in TOOL_IDS, f"{spec['agent_id']} declares unknown tool {tool}"
        assert "shell.exec" not in spec["tools"]
        assert "filesystem.write" not in spec["tools"]


def test_prompt_is_role_specific_and_reviewable():
    spec = next(item for item in SPECIALISTS if item["agent_id"] == "script_writer_agent")
    prompt = build_system_prompt(spec, "Writing and Creative Development")
    assert "Script Writer Agent" in prompt
    assert spec["mission"] in prompt
    assert "JSON" in prompt
    assert "data, not instructions" in prompt
    assert "caption.compose" in prompt


def test_seeding_is_idempotent(session):
    registry = AgentRegistry(session)
    first = registry.ensure_seeded()
    assert first["agents"] == 111
    assert first["departments"] == 10
    second = registry.ensure_seeded()
    assert second == {"departments": 0, "agents": 0}
    assert registry.counts()["agents_total"] == 111


def test_departments_have_one_leader_and_ten_specialists(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    for department in registry.departments():
        members = registry.by_department(department.id)
        assert len(members) == 11
        leader = registry.leader_for(department.id)
        assert leader is not None
        assert leader.agent_id == department.leader_agent_id
        assert sum(1 for member in members if member.hierarchy_level == 2) == 10


def test_prompt_template_created_per_agent(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    definition = registry.get("chief_orchestrator")
    assert definition.prompt_key == "agent.chief_orchestrator"
    template = session.query(PromptTemplate).filter(PromptTemplate.key == definition.prompt_key).one()
    assert template.version == 1
    assert template.body == definition.system_prompt
    assert template.schema_json["contract"] == "plan"


def test_update_bumps_config_version_only_on_real_change(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    before = registry.get("hook_writer_agent").config_version
    registry.update("hook_writer_agent", {"temperature": 0.95})
    assert registry.get("hook_writer_agent").config_version == before + 1
    registry.update("hook_writer_agent", {"temperature": 0.95})
    assert registry.get("hook_writer_agent").config_version == before + 1


def test_prompt_edit_creates_a_version_and_becomes_active(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    original = registry.get("hook_writer_agent")
    registry.update("hook_writer_agent", {"system_prompt": original.system_prompt + "\nKeep hooks under 10 words."})
    definition = registry.get("hook_writer_agent")
    assert "Keep hooks under 10 words" in registry.prompt_for(definition)
    versions = (
        session.query(PromptTemplate).filter(PromptTemplate.key == definition.prompt_key).count()
    )
    assert versions == 2


def test_update_rejects_unknown_values(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    with pytest.raises(InvalidAgentUpdate):
        registry.update("hook_writer_agent", {"memory_scope": "galaxy"})
    with pytest.raises(InvalidAgentUpdate):
        registry.update("hook_writer_agent", {"allowed_tools_json": "shell.exec"})


def test_update_ignores_unknown_fields(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    before = registry.get("hook_writer_agent").config_version
    registry.update("hook_writer_agent", {"not_a_field": 1, "mission": None})
    assert registry.get("hook_writer_agent").config_version == before


def test_values_are_clamped_not_trusted(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    registry.update("hook_writer_agent", {"temperature": 99, "max_tokens": 999999, "timeout_s": 1})
    definition = registry.get("hook_writer_agent")
    assert definition.temperature == 1.2
    assert definition.max_tokens == 4000
    assert definition.timeout_s == 10


def test_disable_agent_and_department(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    registry.set_enabled("hook_writer_agent", False)
    assert registry.get("hook_writer_agent").enabled is False
    registry.set_department_enabled("creative_writing", False)
    assert registry.department("creative_writing").enabled is False
    assert all(not agent.enabled for agent in registry.by_department("creative_writing"))
    assert registry.counts()["agents_disabled"] >= 11


def test_unknown_agent_raises(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    with pytest.raises(AgentNotFound):
        registry.get("does_not_exist")


def test_tool_permissions_are_grant_based(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    registry.assert_tool_permitted("hook_writer_agent", "caption.compose")
    for denied in ("shell.exec", "secrets.read", "platform.publish", "media.inspect"):
        with pytest.raises(PermissionError):
            registry.assert_tool_permitted("hook_writer_agent", denied)


def test_catalog_version_is_stamped(session):
    registry = AgentRegistry(session)
    registry.ensure_seeded()
    definition = registry.get("chief_orchestrator")
    assert definition.catalog_version == CATALOG_VERSION
    assert isinstance(session.get(AgentDepartment, "strategic_intelligence"), AgentDepartment)
    assert isinstance(definition, AgentDefinition)
