"""repair agent_run / task_edge foreign keys after the workforce rebuild

The first pass of the Phase 4 schema rebuild renamed ``agent_task`` to a scratch
table before the child tables were rebuilt, so on databases migrated with that
build ``agent_run`` and ``task_edge`` kept foreign keys pointing at the scratch
name ``agent_task_p3``. Fresh databases are correct, which is why the test suite
never saw it; a real database failed with ``no such table: main.agent_task_p3``.

This migration rebuilds any child table whose SQL still references the scratch
name so its foreign keys point at ``agent_task``, preserving all rows.

Revision ID: b7f1c02d4e61
Revises: 3d820ccb3d8e
Create Date: 2026-11-22 05:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7f1c02d4e61"
down_revision = "3d820ccb3d8e"
branch_labels = None
depends_on = None

SCRATCH = "agent_task_p3"

# Exact definitions of the only two tables that referenced the scratch name.
DEFINITIONS: dict[str, str] = {
    "agent_run": """
        CREATE TABLE {table} (
            task_id VARCHAR(26) NOT NULL,
            model_tag VARCHAR(120) NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            tool_calls_json JSON NOT NULL,
            raw_output_ref VARCHAR(1024),
            finish_reason VARCHAR(60),
            id VARCHAR(26) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT pk_agent_run PRIMARY KEY (id),
            CONSTRAINT fk_agent_run_task_id_agent_task
                FOREIGN KEY(task_id) REFERENCES agent_task (id) ON DELETE CASCADE
        )
    """,
    "task_edge": """
        CREATE TABLE {table} (
            from_task_id VARCHAR(26) NOT NULL,
            to_task_id VARCHAR(26) NOT NULL,
            edge_type VARCHAR(24) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT pk_task_edge PRIMARY KEY (from_task_id, to_task_id, edge_type),
            CONSTRAINT fk_task_edge_from_task_id_agent_task
                FOREIGN KEY(from_task_id) REFERENCES agent_task (id) ON DELETE CASCADE,
            CONSTRAINT fk_task_edge_to_task_id_agent_task
                FOREIGN KEY(to_task_id) REFERENCES agent_task (id) ON DELETE CASCADE
        )
    """,
}

COLUMNS: dict[str, list[str]] = {
    "agent_run": [
        "task_id",
        "model_tag",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
        "tool_calls_json",
        "raw_output_ref",
        "finish_reason",
        "id",
        "created_at",
        "updated_at",
    ],
    "task_edge": ["from_task_id", "to_task_id", "edge_type", "created_at", "updated_at"],
}

INDEXES: dict[str, list[str]] = {
    "agent_run": ["CREATE INDEX ix_agent_run_task_id ON agent_run (task_id)"],
    "task_edge": [],
}


def _tables_referencing_scratch(bind: sa.Connection) -> list[str]:
    rows = bind.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE :needle"),
        {"needle": f'%"{SCRATCH}"%'},
    ).scalars()
    return [name for name in rows if name in DEFINITIONS]


def _rebuild(bind: sa.Connection, table: str) -> None:
    scratch_table = f"{table}_repair"
    bind.execute(sa.text(f'DROP TABLE IF EXISTS "{scratch_table}"'))
    bind.execute(sa.text(DEFINITIONS[table].format(table=f'"{scratch_table}"')))
    columns = ", ".join(f'"{name}"' for name in COLUMNS[table])
    bind.execute(sa.text(f'INSERT INTO "{scratch_table}" ({columns}) SELECT {columns} FROM "{table}"'))
    bind.execute(sa.text(f'DROP TABLE "{table}"'))
    bind.execute(sa.text(f'ALTER TABLE "{scratch_table}" RENAME TO "{table}"'))
    for statement in INDEXES[table]:
        bind.execute(sa.text(statement))


def _repair() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("PRAGMA foreign_keys=OFF"))
    bind.execute(sa.text("PRAGMA legacy_alter_table=ON"))
    for table in _tables_referencing_scratch(bind):
        _rebuild(bind, table)
    violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    bind.execute(sa.text("PRAGMA legacy_alter_table=OFF"))
    bind.execute(sa.text("PRAGMA foreign_keys=ON"))
    if violations:
        raise RuntimeError(f"foreign key violations after repair: {violations[:5]}")


def upgrade() -> None:
    _repair()


def downgrade() -> None:
    """Nothing to undo.

    The pre-repair state was broken (foreign keys pointing at a table that no
    longer exists), so re-creating it would only reintroduce data-loss bugs.
    The repaired schema is the schema the models describe.
    """
