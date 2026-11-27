"""Helpers shared by the migration tests.

The head revision moves every phase, so the migration tests ask Alembic for it instead of hard
coding a revision id that goes stale the moment a new migration lands.
"""
from __future__ import annotations

from pathlib import Path


def alembic_config(root: Path | None = None):
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    return config


def alembic_head() -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None, "the migrations directory has no head revision"
    return head


def revision_before(target: str) -> str:
    """The revision that `target` revises, i.e. the version a user would be upgrading from."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config()).get_revision(target)
    assert script is not None
    down = script.down_revision
    assert isinstance(down, str), f"{target} does not have a single parent revision"
    return down


def ensure_revision(config, target: str) -> None:
    """Put a database at ``target`` whatever revision it is on.

    Phases stack, so a test that used to call ``upgrade(previous)`` (a no-op while ``previous`` was
    head) breaks the moment a newer revision lands. This helper asks Alembic where it is and moves
    in the correct direction, so the migration tests stay phase-agnostic.
    """
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config)
    assert script.get_revision(target) is not None

    # The URL is resolved by migrations/env.py from the configured data root, never from
    # alembic.ini, so build the engine the same way the migration does.
    from creatoros.db.session import get_engine

    engine = get_engine()
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    if current == target:
        return
    if current is None:
        command.upgrade(config, target)
        return

    ancestry = {rev.revision for rev in script.iterate_revisions(current, "base")}
    if target in ancestry:
        command.downgrade(config, target)
    else:
        command.upgrade(config, target)
