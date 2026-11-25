"""Memory tests: scope isolation, secret rejection, retention, attribution."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from creatoros.db.models import AgentMemory
from creatoros.services.agent_memory import (
    AgentMemoryService,
    MemoryScopeError,
    SecretInMemory,
    detect_secret,
)


@pytest.fixture()
def memory(session):
    return AgentMemoryService(session)


def test_write_and_read_back(memory):
    result = memory.write(
        scope="agent",
        owner_id="hook_writer_agent",
        content="Short hooks work better than long ones for this audience.",
        title="Hook lesson",
        project_id="proj1",
    )
    assert result.memory_id
    assert result.scope == "agent"
    rows = memory.query(scope="agent", owner_id="hook_writer_agent", project_id="proj1")
    assert [row.content for row in rows] == ["Short hooks work better than long ones for this audience."]


def test_project_isolation_blocks_cross_project_reads(memory):
    memory.write(scope="project", owner_id="p1", content="project one secret sauce", project_id="p1")
    memory.write(scope="project", owner_id="p2", content="project two plan", project_id="p2")
    only_p1 = memory.query(scope="project", project_id="p1")
    assert [row.project_id for row in only_p1] == ["p1"]


def test_global_memory_is_shared_across_projects(memory):
    memory.write(scope="global", owner_id="creatoros", content="the brand never uses exclamation marks")
    visible = memory.query(scope="project", project_id="p9")
    assert [row.scope.value for row in visible] == ["global"]
    assert visible[0].project_id is None


def test_narrower_scopes_are_not_visible_from_broader_queries(memory):
    memory.write(scope="agent", owner_id="other_agent", content="private agent note")
    memory.write(scope="department", owner_id="creative_writing", content="department note")
    department_rows = memory.query(scope="department", owner_id="hook_writer_agent")
    assert all(row.scope.value != "agent" for row in department_rows)
    assert [row.content for row in department_rows] == ["department note"]


def test_agent_memory_of_another_agent_is_hidden(memory):
    memory.write(scope="agent", owner_id="a1", content="a1 private note")
    memory.write(scope="agent", owner_id="a2", content="a2 private note")
    assert [row.content for row in memory.query(scope="agent", owner_id="a1")] == ["a1 private note"]


def test_task_memory_requires_an_owner_and_is_task_scoped(memory):
    with pytest.raises(MemoryScopeError):
        memory.query(scope="task")
    memory.write(scope="task", owner_id="task-1", task_id="task-1", content="task one detail")
    memory.write(scope="task", owner_id="task-2", task_id="task-2", content="task two detail")
    assert [row.content for row in memory.query(scope="task", owner_id="task-1")] == ["task one detail"]


def test_project_scope_requires_project_id(memory):
    with pytest.raises(MemoryScopeError):
        memory.write(scope="project", owner_id="p1", content="no project id given")


def test_unknown_scope_rejected(memory):
    with pytest.raises(MemoryScopeError):
        memory.write(scope="galaxy", owner_id="x", content="anything")
    with pytest.raises(MemoryScopeError):
        memory.query(scope="galaxy")


def test_secrets_are_rejected_not_redacted(memory, session):
    with pytest.raises(SecretInMemory):
        memory.write(scope="global", owner_id="creatoros", content="token EAAGabcdefghijklmnop please")
    assert session.query(AgentMemory).count() == 0


def test_secret_detector_covers_common_shapes():
    samples = {
        "EAAGabcdefghijklmnop": True,
        "sk_live_abcdefghijkl": True,
        "ghp_abcdefghijklmnopqrst": True,
        "AKIAABCDEFGHIJKLMNOP": True,
        "-----BEGIN RSA PRIVATE KEY-----": True,
        "Bearer abcdefghijklmnop": True,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij": True,
        "just a normal sentence about video hooks": False,
    }
    for sample, expected in samples.items():
        assert bool(detect_secret(sample)) is expected, sample


def test_title_is_scanned_too(memory, session):
    with pytest.raises(SecretInMemory):
        memory.write(scope="global", owner_id="creatoros", content="harmless body", title="ghp_abcdefghijklmnopqrst")
    assert session.query(AgentMemory).count() == 0


def test_rejection_is_audited(memory, session):
    from creatoros.db.models import AuditEvent

    with pytest.raises(SecretInMemory):
        memory.write(scope="global", owner_id="creatoros", content="EAAGabcdefghijklmnop")
    actions = [row.action for row in session.query(AuditEvent).all()]
    assert "agent.memory.rejected" in actions


def test_expired_rows_are_deleted_by_retention(memory, session):
    result = memory.write(scope="global", owner_id="creatoros", content="old note")
    row = session.get(AgentMemory, result.memory_id)
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    session.flush()
    summary = memory.enforce_retention()
    assert summary["expired_deleted"] == 1
    assert session.query(AgentMemory).count() == 0


def test_retention_dry_run_changes_nothing(memory, session):
    result = memory.write(scope="global", owner_id="creatoros", content="old note")
    row = session.get(AgentMemory, result.memory_id)
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    session.flush()
    summary = memory.enforce_retention(dry_run=True)
    assert summary["expired_deleted"] == 1
    assert session.query(AgentMemory).count() == 1


def test_scope_budget_evicts_oldest(memory, session):
    # Each row is capped at 20k chars, so a budget of 200k needs eleven rows to trip.
    identifiers = [
        memory.write(scope="global", owner_id="creatoros", content=chr(96 + index) * 20000).memory_id
        for index in range(1, 12)
    ]
    now = datetime.now(UTC)
    for offset, memory_id in enumerate(identifiers):
        session.get(AgentMemory, memory_id).created_at = now - timedelta(minutes=len(identifiers) - offset)
    session.flush()
    summary = memory.enforce_retention()
    assert summary["over_budget_removed"] >= 1
    assert session.get(AgentMemory, identifiers[0]) is None
    assert session.get(AgentMemory, identifiers[-1]) is not None


def test_expired_rows_are_hidden_from_queries(memory, session):
    result = memory.write(scope="global", owner_id="creatoros", content="stale note")
    session.get(AgentMemory, result.memory_id).expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.flush()
    assert memory.query(scope="global") == []
    assert len(memory.query(scope="global", include_expired=True)) == 1


def test_stats_report_lengths_and_scopes(memory):
    memory.write(scope="global", owner_id="creatoros", content="one")
    memory.write(scope="department", owner_id="creative_writing", content="two")
    stats = memory.stats()
    assert stats["total"] == 2
    assert stats["by_scope"]["global"] == 1
    assert stats["total_chars"] == 6
    assert "task" in stats["retention_days"]


def test_search_filters_by_text(memory):
    memory.write(scope="global", owner_id="creatoros", content="use a question hook")
    memory.write(scope="global", owner_id="creatoros", content="avoid generic intros")
    assert len(memory.query(scope="global", search="question")) == 1


def test_write_is_audited_with_attribution(memory, session):
    from creatoros.db.models import AuditEvent

    memory.write(scope="agent", owner_id="hook_writer_agent", content="note", agent_id="hook_writer_agent")
    event = session.query(AuditEvent).filter(AuditEvent.action == "agent.memory.write").one()
    assert event.actor == "hook_writer_agent"
    assert event.metadata_redacted_json["owner_id"] == "hook_writer_agent"
