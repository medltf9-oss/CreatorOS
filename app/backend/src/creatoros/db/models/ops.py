"""Operational tables: agents, task graph, runs, memory, prompts, queue, audit,
notifications, automations, model registry and diagnostics.

Phase 8 moved ``analytics_snapshot`` into ``db/models/analytics.py``: the observation ledger now has
its own module and a schema that can express "not observed".
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, IdMixin, TimestampMixin, UtcDateTime, enum_column
from ..enums import (
    ApprovalRequirement,
    ApprovalState,
    MemoryScope,
    ObjectiveState,
    QueueState,
    RiskLevel,
    Severity,
    TaskState,
    ToolRisk,
)


class AgentDepartment(Base, TimestampMixin):
    """A department of the workforce. Seeded, editable, never hardcoded at runtime."""

    __tablename__ = "agent_department"

    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    mission: Mapped[str] = mapped_column(sa.Text, nullable=False)
    leader_agent_id: Mapped[str | None] = mapped_column(sa.String(48))
    display_order: Mapped[int] = mapped_column(sa.Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    catalog_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class AgentDefinition(Base, TimestampMixin):
    __tablename__ = "agent_definition"

    agent_id: Mapped[str] = mapped_column(sa.String(48), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    # `team_id` holds the department key (kept for compatibility with Phase 2 tables).
    team_id: Mapped[str] = mapped_column(sa.String(40), nullable=False, index=True)
    mission: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    system_prompt: Mapped[str | None] = mapped_column(sa.Text)
    prompt_key: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    input_schema_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    output_schema_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    allowed_tools_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    model_profile: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel, "agent_risk"), default=RiskLevel.LOW, nullable=False
    )
    approval_requirement: Mapped[ApprovalRequirement] = mapped_column(
        enum_column(ApprovalRequirement, "agent_approval"), default=ApprovalRequirement.NONE, nullable=False
    )
    timeout_s: Mapped[int] = mapped_column(sa.Integer, default=90, nullable=False)
    max_tokens: Mapped[int] = mapped_column(sa.Integer, default=512, nullable=False)
    temperature: Mapped[float] = mapped_column(sa.Float, default=0.4, nullable=False)
    max_retries: Mapped[int] = mapped_column(sa.Integer, default=2, nullable=False)
    task_types_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    fallback_profiles_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    restricted_tools_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    hierarchy_level: Mapped[int] = mapped_column(sa.Integer, default=2, nullable=False)
    can_delegate: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    config_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    retry_policy_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    quality_checks_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    memory_scope: Mapped[MemoryScope] = mapped_column(
        enum_column(MemoryScope, "memory_scope"), default=MemoryScope.TASK, nullable=False
    )
    catalog_version: Mapped[str] = mapped_column(sa.String(32), default="1", nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)


class AgentTask(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_task"
    __table_args__ = (
        sa.Index("ix_agent_task_state", "state", "priority"),
        sa.Index("ix_agent_task_root", "root_task_id"),
    )

    agent_id: Mapped[str] = mapped_column(sa.ForeignKey("agent_definition.agent_id"), nullable=False)
    team_id: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    objective_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(sa.ForeignKey("agent_task.id", ondelete="CASCADE"))
    root_task_id: Mapped[str | None] = mapped_column(sa.String(26))
    depth: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    task_type: Mapped[str | None] = mapped_column(sa.String(60))
    config_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    title: Mapped[str | None] = mapped_column(sa.String(300))
    summary: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    model_tag: Mapped[str | None] = mapped_column(sa.String(120))
    tokens_used: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error_code: Mapped[str | None] = mapped_column(sa.String(60))
    input_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    output_json: Mapped[dict | None] = mapped_column(sa.JSON)
    state: Mapped[TaskState] = mapped_column(
        enum_column(TaskState, "task_state"), default=TaskState.DRAFT, nullable=False
    )
    priority: Mapped[int] = mapped_column(sa.Integer, default=5, nullable=False)
    retries: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    evidence_refs_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(sa.Text)
    deadline_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class TaskEdge(Base, TimestampMixin):
    __tablename__ = "task_edge"

    from_task_id: Mapped[str] = mapped_column(
        sa.ForeignKey("agent_task.id", ondelete="CASCADE"), primary_key=True
    )
    to_task_id: Mapped[str] = mapped_column(sa.ForeignKey("agent_task.id", ondelete="CASCADE"), primary_key=True)
    edge_type: Mapped[str] = mapped_column(sa.String(24), default="depends", primary_key=True)


class AgentRun(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_run"

    task_id: Mapped[str] = mapped_column(sa.ForeignKey("agent_task.id", ondelete="CASCADE"), nullable=False, index=True)
    model_tag: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    tool_calls_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    raw_output_ref: Mapped[str | None] = mapped_column(sa.String(1024))
    finish_reason: Mapped[str | None] = mapped_column(sa.String(60))

    task: Mapped[AgentTask] = relationship()


class AgentMemory(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_memory"

    scope: Mapped[MemoryScope] = mapped_column(
        enum_column(MemoryScope, "agent_memory_scope"), default=MemoryScope.TASK, nullable=False
    )
    owner_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    source_task_id: Mapped[str | None] = mapped_column(sa.String(26))
    objective_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    title: Mapped[str | None] = mapped_column(sa.String(200))
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    embedding_ref: Mapped[str | None] = mapped_column(sa.String(1024))
    sensitivity: Mapped[str] = mapped_column(sa.String(40), default="normal", nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AgentObjective(Base, IdMixin, TimestampMixin):
    """One user objective owned by the chief orchestrator."""

    __tablename__ = "agent_objective"

    objective: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[ObjectiveState] = mapped_column(
        enum_column(ObjectiveState, "objective_state"), default=ObjectiveState.PENDING, nullable=False
    )
    tenant_scope: Mapped[str] = mapped_column(sa.String(40), default="local", nullable=False)
    root_task_id: Mapped[str | None] = mapped_column(sa.String(26))
    queue_job_id: Mapped[str | None] = mapped_column(sa.String(26))
    plan_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    final_report_json: Mapped[dict | None] = mapped_column(sa.JSON)
    limits_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    calls_used: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    tokens_used: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    departments_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    error_code: Mapped[str | None] = mapped_column(sa.String(60))
    error_detail: Mapped[str | None] = mapped_column(sa.Text)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class ApprovalRequest(Base, IdMixin, TimestampMixin):
    """Hash-bound, single-use human consent for a consequential action."""

    __tablename__ = "approval_request"

    action: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    state: Mapped[ApprovalState] = mapped_column(
        enum_column(ApprovalState, "approval_state"), default=ApprovalState.PENDING, nullable=False
    )
    agent_id: Mapped[str | None] = mapped_column(sa.String(48))
    task_id: Mapped[str | None] = mapped_column(sa.String(26))
    objective_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    target_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    target_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    risks_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    payload_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    payload_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(sa.String(120), default="agent", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decided_by: Mapped[str | None] = mapped_column(sa.String(120))
    decision_note: Mapped[str | None] = mapped_column(sa.Text)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class PromptTemplate(Base, TimestampMixin):
    __tablename__ = "prompt_template"

    key: Mapped[str] = mapped_column(sa.String(120), primary_key=True)
    version: Mapped[int] = mapped_column(sa.Integer, primary_key=True, default=1)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    schema_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)


class ToolRegistry(Base, TimestampMixin):
    """Declared tool surface. Agents may only call tools listed here (INV-4)."""

    __tablename__ = "tool_registry"

    name: Mapped[str] = mapped_column(sa.String(120), primary_key=True)
    risk: Mapped[ToolRisk] = mapped_column(enum_column(ToolRisk, "tool_risk"), nullable=False)
    approval_requirement: Mapped[ApprovalRequirement] = mapped_column(
        enum_column(ApprovalRequirement, "tool_approval"), default=ApprovalRequirement.NONE, nullable=False
    )
    schema_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    allowed_roles_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)


class QueueJob(Base, IdMixin, TimestampMixin):
    __tablename__ = "queue_job"
    __table_args__ = (
        sa.Index("ix_queue_job_claimable", "queue", "state", "run_after", "priority"),
        sa.Index("ix_queue_job_dedupe", "job_type", "idempotency_key"),
    )

    queue: Mapped[str] = mapped_column(sa.String(60), default="default", nullable=False)
    job_type: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    payload_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(sa.Integer, default=50, nullable=False)
    state: Mapped[QueueState] = mapped_column(
        enum_column(QueueState, "queue_state"), default=QueueState.QUEUED, nullable=False
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, default=3, nullable=False)
    run_after: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(64))
    lease_owner: Mapped[str | None] = mapped_column(sa.String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    result_json: Mapped[dict | None] = mapped_column(sa.JSON)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AuditEvent(Base, IdMixin, TimestampMixin):
    __tablename__ = "audit_event"
    __table_args__ = (sa.Index("ix_audit_event_created", "created_at"),)

    actor: Mapped[str] = mapped_column(sa.String(120), default="local-user", nullable=False)
    action: Mapped[str] = mapped_column(sa.String(120), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    outcome: Mapped[str] = mapped_column(sa.String(40), default="success", nullable=False)
    metadata_redacted_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)


class Notification(Base, IdMixin, TimestampMixin):
    __tablename__ = "notification"

    severity: Mapped[Severity] = mapped_column(
        enum_column(Severity, "notification_severity"), default=Severity.INFO, nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(sa.Text)
    route: Mapped[str | None] = mapped_column(sa.String(200))
    dedupe_key: Mapped[str | None] = mapped_column(sa.String(120), index=True)
    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AutomationRule(Base, IdMixin, TimestampMixin):
    __tablename__ = "automation_rule"

    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    trigger_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    conditions_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    actions_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    approval_required: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AutomationRun(Base, IdMixin, TimestampMixin):
    __tablename__ = "automation_run"

    rule_id: Mapped[str] = mapped_column(
        sa.ForeignKey("automation_rule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(sa.String(40), default="pending", nullable=False)
    dry_run: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    result_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(sa.Text)


class ModelProfile(Base, TimestampMixin):
    __tablename__ = "model_profile"

    name: Mapped[str] = mapped_column(sa.String(60), primary_key=True)
    ollama_tag: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    context_len: Mapped[int] = mapped_column(sa.Integer, default=4096, nullable=False)
    num_gpu: Mapped[int | None] = mapped_column(sa.Integer)
    temperature: Mapped[float] = mapped_column(sa.Float, default=0.7, nullable=False)
    roles_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    fallback_profile: Mapped[str | None] = mapped_column(sa.String(60))
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    disk_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)


class ModelBenchmark(Base, IdMixin, TimestampMixin):
    __tablename__ = "model_benchmark"

    profile_name: Mapped[str] = mapped_column(sa.String(60), nullable=False, index=True)
    tokens_per_sec: Mapped[float] = mapped_column(sa.Float, nullable=False)
    ttft_ms: Mapped[int | None] = mapped_column(sa.Integer)
    vram_peak_mb: Mapped[int | None] = mapped_column(sa.Integer)
    ram_peak_mb: Mapped[int | None] = mapped_column(sa.Integer)
    context_len: Mapped[int | None] = mapped_column(sa.Integer)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    # "reference" rows are the ADR-09 readings seeded at first boot; "measured"
    # rows come from a real run on this machine. A measured row always wins.
    source: Mapped[str] = mapped_column(sa.String(16), default="measured", nullable=False)


class AiExecution(Base, IdMixin, TimestampMixin):
    """One real inference call: interactive or queued (Phase 3).

    Rows are written *before* the model is called and updated on completion, so a
    crash leaves a `running` row that the governor/UI can surface as interrupted
    rather than silently losing the execution.
    """

    __tablename__ = "ai_execution"
    __table_args__ = (
        sa.Index("ix_ai_execution_state", "state", "created_at"),
        sa.Index("ix_ai_execution_model", "model_tag", "created_at"),
    )

    kind: Mapped[str] = mapped_column(sa.String(24), default="generate", nullable=False)
    model_tag: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    profile_name: Mapped[str | None] = mapped_column(sa.String(60))
    state: Mapped[str] = mapped_column(sa.String(24), default="running", nullable=False)
    origin: Mapped[str] = mapped_column(sa.String(24), default="interactive", nullable=False)
    prompt_text: Mapped[str | None] = mapped_column(sa.Text)
    prompt_hash: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    response_text: Mapped[str | None] = mapped_column(sa.Text)
    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    ttft_ms: Mapped[int | None] = mapped_column(sa.Integer)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    tokens_per_sec: Mapped[float | None] = mapped_column(sa.Float)
    context_len: Mapped[int | None] = mapped_column(sa.Integer)
    options_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    usage_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(sa.String(60))
    error_detail: Mapped[str | None] = mapped_column(sa.Text)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    queue_job_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    task_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64))
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class DiagnosticsRun(Base, IdMixin, TimestampMixin):
    __tablename__ = "diagnostics_run"

    results_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    severity_summary: Mapped[str | None] = mapped_column(sa.String(120))
