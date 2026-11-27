"""agent workforce schema (Phase 4)

Adds departments, objectives and approval requests, and extends the agent tables
with the fields the orchestration engine needs.

The three pre-existing agent tables carry CHECK constraints generated from the
Phase 2 enums. SQLite cannot widen a CHECK in place, so each table is rebuilt:
rename, create with the new constraint, copy the rows that existed, drop the old
table. At the time of writing all three tables are empty (the agent runtime ships
in this phase), but the copy keeps the migration honest for any database that has
rows.

Revision ID: 3d820ccb3d8e
Revises: 5ccf6ed4af2b
Create Date: 2026-11-22 03:48:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa

import creatoros.db.base  # registers UtcDateTime
from alembic import op

revision = "3d820ccb3d8e"
down_revision = "5ccf6ed4af2b"
branch_labels = None
depends_on = None

TASK_STATES = (
    "draft",
    "pending",
    "planned",
    "ready",
    "assigned",
    "awaiting_approval",
    "queued",
    "running",
    "waiting",
    "review",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
    "needs_human_review",
)
OLD_TASK_STATES = (
    "draft",
    "planned",
    "awaiting_approval",
    "queued",
    "running",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
    "needs_human_review",
)
MEMORY_SCOPES = ("task", "agent", "department", "project", "global", "long_term")
OLD_MEMORY_SCOPES = ("task", "project", "long_term")
OBJECTIVE_STATES = (
    "pending",
    "planning",
    "running",
    "review",
    "compiling",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "blocked",
)
APPROVAL_STATES = ("pending", "approved", "rejected", "expired", "cancelled")

UTCDateTime = creatoros.db.base.UtcDateTime

AGENT_DEFINITION_COLUMNS = (
    "agent_id", "name", "team_id", "mission", "description", "system_prompt", "prompt_key",
    "input_schema_json", "output_schema_json", "allowed_tools_json", "model_profile", "risk_level",
    "approval_requirement", "timeout_s", "max_tokens", "temperature", "max_retries",
    "task_types_json", "fallback_profiles_json", "restricted_tools_json", "hierarchy_level",
    "can_delegate", "config_version", "retry_policy_json", "quality_checks_json", "memory_scope",
    "catalog_version", "enabled", "created_at", "updated_at",
)
AGENT_DEFINITION_P3_COLUMNS = (
    "agent_id", "name", "team_id", "mission", "prompt_key", "input_schema_json",
    "output_schema_json", "allowed_tools_json", "model_profile", "risk_level",
    "approval_requirement", "timeout_s", "retry_policy_json", "quality_checks_json",
    "memory_scope", "catalog_version", "enabled", "created_at", "updated_at",
)
AGENT_TASK_COLUMNS = (
    "id", "agent_id", "team_id", "objective_id", "parent_task_id", "root_task_id", "depth",
    "task_type", "config_version", "title", "summary", "confidence", "model_tag", "tokens_used",
    "duration_ms", "error_code", "input_json", "output_json", "state", "priority", "retries",
    "evidence_refs_json", "error", "deadline_at", "cancel_requested", "started_at", "finished_at",
    "created_at", "updated_at",
)
AGENT_TASK_P3_COLUMNS = (
    "id", "agent_id", "team_id", "parent_task_id", "root_task_id", "depth", "title",
    "input_json", "output_json", "state", "priority", "retries", "evidence_refs_json", "error",
    "deadline_at", "cancel_requested", "started_at", "finished_at", "created_at", "updated_at",
)
AGENT_MEMORY_COLUMNS = (
    "id", "scope", "owner_id", "project_id", "source_task_id", "objective_id", "title", "content",
    "embedding_ref", "sensitivity", "approved_at", "expires_at", "created_at", "updated_at",
)
AGENT_MEMORY_P3_COLUMNS = (
    "id", "scope", "owner_id", "content", "embedding_ref", "sensitivity", "approved_at",
    "expires_at", "created_at", "updated_at",
)


def _recover_partial_backup(connection) -> None:
    tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in ("agent_definition", "agent_task", "agent_memory"):
        backup = f"{name}_p4"
        if backup not in tables:
            continue
        if name not in tables:
            op.rename_table(backup, name)
        else:
            op.execute(f'DROP TABLE "{backup}"')


def _recover_partial(connection) -> None:
    """SQLite DDL is not transactional, so a crash mid-migration can leave `*_p3`."""
    tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in ("agent_definition", "agent_task", "agent_memory"):
        backup = f"{name}_p3"
        if backup not in tables:
            continue
        if name not in tables:
            op.rename_table(backup, name)
        else:
            op.execute(f'DROP TABLE "{backup}"')


def _copy(source: str, target: str, columns: tuple[str, ...]) -> None:
    cols = ", ".join(f'"{name}"' for name in columns)
    op.execute(f'INSERT INTO "{target}" ({cols}) SELECT {cols} FROM "{source}"')


def _create_department() -> None:
    op.create_table(
        "agent_department",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("mission", sa.Text(), nullable=False),
        sa.Column("leader_agent_id", sa.String(length=48), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_department"),
    )


def _create_objective() -> None:
    op.create_table(
        "agent_objective",
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*OBJECTIVE_STATES, name="objective_state", native_enum=False, create_constraint=True,
                    validate_strings=True),
            nullable=False,
        ),
        sa.Column("tenant_scope", sa.String(length=40), nullable=False),
        sa.Column("root_task_id", sa.String(length=26), nullable=True),
        sa.Column("queue_job_id", sa.String(length=26), nullable=True),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("final_report_json", sa.JSON(), nullable=True),
        sa.Column("limits_json", sa.JSON(), nullable=False),
        sa.Column("calls_used", sa.Integer(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("departments_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_objective"),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{value}'" for value in OBJECTIVE_STATES) + ")",
            name="ck_agent_objective_objective_state",
        ),
    )


def _create_approval() -> None:
    op.create_table(
        "approval_request",
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*APPROVAL_STATES, name="approval_state", native_enum=False, create_constraint=True,
                    validate_strings=True),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(length=48), nullable=True),
        sa.Column("task_id", sa.String(length=26), nullable=True),
        sa.Column("objective_id", sa.String(length=26), nullable=True),
        sa.Column("target_type", sa.String(length=60), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("risks_json", sa.JSON(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=120), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.Column("decided_at", UTCDateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=120), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("consumed_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_approval_request"),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{value}'" for value in APPROVAL_STATES) + ")",
            name="ck_approval_request_approval_state",
        ),
    )
    op.create_index("ix_approval_request_objective_id", "approval_request", ["objective_id"])


def _create_agent_definition() -> None:
    op.create_table(
        "agent_definition",
        sa.Column("agent_id", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("team_id", sa.String(length=40), nullable=False),
        sa.Column("mission", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("prompt_key", sa.String(length=120), nullable=False),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("output_schema_json", sa.JSON(), nullable=False),
        sa.Column("allowed_tools_json", sa.JSON(), nullable=False),
        sa.Column("model_profile", sa.String(length=60), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("approval_requirement", sa.String(length=20), nullable=False),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("task_types_json", sa.JSON(), nullable=False),
        sa.Column("fallback_profiles_json", sa.JSON(), nullable=False),
        sa.Column("restricted_tools_json", sa.JSON(), nullable=False),
        sa.Column("hierarchy_level", sa.Integer(), nullable=False),
        sa.Column("can_delegate", sa.Boolean(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("retry_policy_json", sa.JSON(), nullable=False),
        sa.Column("quality_checks_json", sa.JSON(), nullable=False),
        sa.Column("memory_scope", sa.String(length=20), nullable=False),
        sa.Column("catalog_version", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", name="pk_agent_definition"),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')", name="ck_agent_definition_agent_risk"
        ),
        sa.CheckConstraint(
            "approval_requirement IN ('none', 'on_write', 'always_human')",
            name="ck_agent_definition_agent_approval",
        ),
        sa.CheckConstraint(
            "memory_scope IN (" + ", ".join(f"'{value}'" for value in MEMORY_SCOPES) + ")",
            name="ck_agent_definition_memory_scope",
        ),
    )

def _create_agent_task() -> None:
    op.create_table(
        "agent_task",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("agent_id", sa.String(length=48), nullable=False),
        sa.Column("team_id", sa.String(length=40), nullable=False),
        sa.Column("objective_id", sa.String(length=26), nullable=True),
        sa.Column("parent_task_id", sa.String(length=26), nullable=True),
        sa.Column("root_task_id", sa.String(length=26), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=60), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("model_tag", sa.String(length=120), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("deadline_at", UTCDateTime(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_task"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definition.agent_id"], name="fk_agent_task_agent_id_agent_definition"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["agent_task.id"], name="fk_agent_task_parent_task_id_agent_task",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{value}'" for value in TASK_STATES) + ")",
            name="ck_agent_task_task_state",
        ),
    )

def _create_agent_memory() -> None:
    op.create_table(
        "agent_memory",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=26), nullable=True),
        sa.Column("source_task_id", sa.String(length=26), nullable=True),
        sa.Column("objective_id", sa.String(length=26), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding_ref", sa.String(length=1024), nullable=True),
        sa.Column("sensitivity", sa.String(length=40), nullable=False),
        sa.Column("approved_at", UTCDateTime(), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_memory"),
        sa.CheckConstraint(
            "scope IN (" + ", ".join(f"'{value}'" for value in MEMORY_SCOPES) + ")",
            name="ck_agent_memory_agent_memory_scope",
        ),
    )

def upgrade() -> None:
    bind = op.get_bind()
    # Standard SQLite table-rebuild recipe: without these pragmas a rename rewrites
    # every foreign key that pointed at the table, and dropping a rebuilt table can
    # fail because a child still references its backup name.
    bind.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    _recover_partial(bind)

    # --- agent_definition: widen ids, add the orchestration columns ----------
    existing_definitions = bind.exec_driver_sql("SELECT COUNT(*) FROM agent_definition").scalar() or 0
    op.rename_table("agent_definition", "agent_definition_p3")
    _create_agent_definition()
    if existing_definitions:
        op.execute(
            """
            INSERT INTO agent_definition (
                agent_id, name, team_id, mission, description, system_prompt, prompt_key,
                input_schema_json, output_schema_json, allowed_tools_json, model_profile,
                risk_level, approval_requirement, timeout_s, max_tokens, temperature, max_retries,
                task_types_json, fallback_profiles_json, restricted_tools_json, hierarchy_level,
                can_delegate, config_version, retry_policy_json, quality_checks_json, memory_scope,
                catalog_version, enabled, created_at, updated_at
            )
            SELECT agent_id, name, team_id, mission, NULL,
                   (SELECT body FROM prompt_template WHERE prompt_template.key = agent_definition_p3.prompt_key
                    ORDER BY version DESC LIMIT 1),
                   prompt_key, input_schema_json, output_schema_json, allowed_tools_json,
                   model_profile, risk_level, approval_requirement, timeout_s, 512, 0.4, 2,
                   '[]', '[]', '[]', 2, 0, 1, retry_policy_json, quality_checks_json, memory_scope,
                   catalog_version, enabled, created_at, updated_at
            FROM agent_definition_p3
            """
        )
    # Rows that predate the catalogue defaults get sane values before the seeder
    # rewrites them on the next boot.
    op.execute(
        """
        UPDATE agent_definition SET
            max_tokens = 512,
            temperature = 0.4,
            max_retries = 2,
            task_types_json = '[]',
            fallback_profiles_json = '[]',
            restricted_tools_json = '[]',
            hierarchy_level = 2,
            can_delegate = 0,
            config_version = 1
        """
    )
    # --- agent_task: new columns and the wider state machine -----------------
    existing_tasks = bind.exec_driver_sql("SELECT COUNT(*) FROM agent_task").scalar() or 0
    op.rename_table("agent_task", "agent_task_p3")
    _create_agent_task()
    if existing_tasks:
        op.execute(
            """
            INSERT INTO agent_task (
                id, agent_id, team_id, depth, config_version, title, input_json, output_json, state,
                priority, retries, evidence_refs_json, error, deadline_at, cancel_requested,
                started_at, finished_at, created_at, updated_at
            )
            SELECT
                id, agent_id, team_id, depth, 1, title, input_json, output_json, state,
                priority, retries, evidence_refs_json, error, deadline_at, cancel_requested,
                started_at, finished_at, created_at, updated_at
            FROM agent_task_p3
            """
        )
    op.drop_table("agent_task_p3")
    op.create_index("ix_agent_task_state", "agent_task", ["state", "priority"])
    op.create_index("ix_agent_task_root", "agent_task", ["root_task_id"])
    op.create_index("ix_agent_task_objective_id", "agent_task", ["objective_id"])

    # --- agent_memory: attribution columns and the wider scope enum ----------
    existing_memory = bind.exec_driver_sql("SELECT COUNT(*) FROM agent_memory").scalar() or 0
    op.rename_table("agent_memory", "agent_memory_p3")
    _create_agent_memory()
    if existing_memory:
        op.execute(
            """
            INSERT INTO agent_memory (
                id, scope, owner_id, content, embedding_ref, sensitivity, approved_at, expires_at,
                created_at, updated_at
            )
            SELECT id, scope, owner_id, content, embedding_ref, sensitivity, approved_at, expires_at,
                   created_at, updated_at
            FROM agent_memory_p3
            """
        )
    op.drop_table("agent_memory_p3")
    op.create_index("ix_agent_memory_owner_id", "agent_memory", ["owner_id"])
    op.create_index("ix_agent_memory_project_id", "agent_memory", ["project_id"])
    op.create_index("ix_agent_memory_objective_id", "agent_memory", ["objective_id"])

    # The parent is dropped last: renaming it rewrote the child tables' FK
    # references to "agent_definition_p3", so it must outlive their rebuild.
    op.drop_table("agent_definition_p3")
    op.create_index("ix_agent_definition_team_id", "agent_definition", ["team_id"])

    _create_department()
    _create_objective()
    _create_approval()

    bind.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    _recover_partial_backup(bind)
    op.drop_index("ix_approval_request_objective_id", table_name="approval_request")
    op.drop_table("approval_request")
    op.drop_table("agent_objective")
    op.drop_table("agent_department")

    # --- back to the Phase 2/3 shapes ---------------------------------------
    op.rename_table("agent_memory", "agent_memory_p4")
    op.create_table(
        "agent_memory",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column(
            "scope",
            sa.Enum(*OLD_MEMORY_SCOPES, name="agent_memory_scope", native_enum=False, create_constraint=True,
                    validate_strings=True),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding_ref", sa.String(length=1024), nullable=True),
        sa.Column("sensitivity", sa.String(length=40), nullable=False),
        sa.Column("approved_at", UTCDateTime(), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_memory"),
        sa.CheckConstraint(
            "scope IN (" + ", ".join(f"'{value}'" for value in OLD_MEMORY_SCOPES) + ")",
            name="ck_agent_memory_agent_memory_scope",
        ),
    )
    op.execute(
        """
        INSERT INTO agent_memory (id, scope, owner_id, content, embedding_ref, sensitivity,
                                  approved_at, expires_at, created_at, updated_at)
        SELECT id, CASE WHEN scope IN ('task', 'project') THEN scope ELSE 'long_term' END,
               owner_id, content, embedding_ref, sensitivity, approved_at, expires_at,
               created_at, updated_at
        FROM agent_memory_p4
        """
    )
    op.drop_table("agent_memory_p4")
    op.create_index("ix_agent_memory_owner_id", "agent_memory", ["owner_id"])

    op.rename_table("agent_task", "agent_task_p4")
    op.create_table(
        "agent_task",
        sa.Column("agent_id", sa.String(length=48), nullable=False),
        sa.Column("team_id", sa.String(length=40), nullable=False),
        sa.Column("parent_task_id", sa.String(length=26), nullable=True),
        sa.Column("root_task_id", sa.String(length=26), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("deadline_at", UTCDateTime(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_task"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definition.agent_id"], name="fk_agent_task_agent_id_agent_definition"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["agent_task.id"], name="fk_agent_task_parent_task_id_agent_task",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{value}'" for value in OLD_TASK_STATES) + ")",
            name="ck_agent_task_task_state",
        ),
    )
    op.execute(
        """
        INSERT INTO agent_task (agent_id, team_id, parent_task_id, root_task_id, depth, title,
                                input_json, output_json, state, priority, retries,
                                evidence_refs_json, error, deadline_at, cancel_requested,
                                started_at, finished_at, id, created_at, updated_at)
        SELECT agent_id, team_id, parent_task_id, root_task_id, depth, title,
               input_json, output_json,
               CASE WHEN state IN ('pending', 'ready', 'assigned') THEN 'planned'
                    WHEN state IN ('waiting', 'review') THEN 'running'
                    ELSE state END,
               priority, retries, evidence_refs_json, error, deadline_at, cancel_requested,
               started_at, finished_at, id, created_at, updated_at
        FROM agent_task_p4
        """
    )
    op.drop_table("agent_task_p4")
    op.create_index("ix_agent_task_state", "agent_task", ["state", "priority"])
    op.create_index("ix_agent_task_root", "agent_task", ["root_task_id"])

    op.rename_table("agent_definition", "agent_definition_p4")
    op.create_table(
        "agent_definition",
        sa.Column("agent_id", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("team_id", sa.String(length=8), nullable=False),
        sa.Column("mission", sa.Text(), nullable=False),
        sa.Column("prompt_key", sa.String(length=120), nullable=False),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("output_schema_json", sa.JSON(), nullable=False),
        sa.Column("allowed_tools_json", sa.JSON(), nullable=False),
        sa.Column("model_profile", sa.String(length=60), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("approval_requirement", sa.String(length=20), nullable=False),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column("retry_policy_json", sa.JSON(), nullable=False),
        sa.Column("quality_checks_json", sa.JSON(), nullable=False),
        sa.Column("memory_scope", sa.String(length=20), nullable=False),
        sa.Column("catalog_version", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", name="pk_agent_definition"),
        sa.CheckConstraint("risk_level IN ('low', 'medium', 'high')", name="ck_agent_definition_agent_risk"),
        sa.CheckConstraint(
            "approval_requirement IN ('none', 'on_write', 'always_human')",
            name="ck_agent_definition_agent_approval",
        ),
        sa.CheckConstraint(
            "memory_scope IN ('task', 'project', 'long_term')", name="ck_agent_definition_memory_scope"
        ),
    )
    op.execute(
        """
        INSERT INTO agent_definition (agent_id, name, team_id, mission, prompt_key, input_schema_json,
                                      output_schema_json, allowed_tools_json, model_profile, risk_level,
                                      approval_requirement, timeout_s, retry_policy_json,
                                      quality_checks_json, memory_scope, catalog_version, enabled,
                                      created_at, updated_at)
        SELECT substr(agent_id, 1, 8), name, substr(team_id, 1, 8), mission, prompt_key,
               input_schema_json, output_schema_json, allowed_tools_json, model_profile, risk_level,
               approval_requirement, timeout_s, retry_policy_json, quality_checks_json,
               CASE WHEN memory_scope IN ('task', 'project') THEN memory_scope ELSE 'long_term' END,
               catalog_version, enabled, created_at, updated_at
        FROM agent_definition_p4
        """
    )
    op.drop_table("agent_definition_p4")
    op.create_index("ix_agent_definition_team_id", "agent_definition", ["team_id"])
    bind.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")
