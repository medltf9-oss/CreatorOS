"""First-run completion state.

It lives in `app_setting`, so an upgrade keeps it, a repair keeps it and an uninstall that keeps user
data keeps it. The wizard is skippable by design: opening the application must never depend on a
model download, so "completed" records a decision, not a successful AI setup.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ...db.models import AppSetting

SETTING_KEY = "setup.first_run"
SETTING_SCOPE = "app"


def read_state(session: Session) -> dict[str, Any] | None:
    """Return the recorded state, or `None` when the wizard has never been finished."""
    row = session.get(AppSetting, SETTING_KEY)
    if row is None or not row.value_json:
        return None
    return dict(row.value_json)


def mark_complete(
    session: Session,
    *,
    version: str,
    skipped_ai: bool,
    actor: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record that the user finished (or deliberately skipped) the first-run wizard."""
    completed_at = datetime.now(UTC).isoformat()
    value: dict[str, Any] = {
        "completed": True,
        "completed_at": completed_at,
        "version": version,
        "skipped_ai": bool(skipped_ai),
        "actor": actor,
    }
    if notes:
        value["notes"] = notes
    row = session.get(AppSetting, SETTING_KEY)
    if row is None:
        row = AppSetting(key=SETTING_KEY, value_json=value, scope=SETTING_SCOPE)
        session.add(row)
    else:
        row.value_json = value
    session.flush()
    return value
