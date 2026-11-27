"""First-run completion and prerequisite verification state.

Both live in `app_setting`, so an upgrade keeps them, a repair keeps them and an uninstall that
keeps user data keeps them. The wizard is skippable by design: opening the application must never
depend on a model download, so "completed" records a decision, not a successful AI setup.

Verification records are fingerprints, not permissions. A record says what one hash run observed
(path, size, modified time, SHA-256 and publisher); the status route compares the recorded
size and modified time with the file and never re-hashes, so a record can go stale without
disappearing. A stale record is reported as unverified instead of being silently trusted.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ...db.models import AppSetting

SETTING_KEY = "setup.first_run"
SETTING_SCOPE = "app"
VERIFICATION_KEY = "setup.verification"
VERIFICATION_SCOPE = "app"
VERIFICATION_LIMIT = 64
CONSENT_KEY = "setup.consent"
CONSENT_SCOPE = "app"


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
    override_required_checks: bool = False,
) -> dict[str, Any]:
    """Record that the user finished (or deliberately skipped) the first-run wizard.

    Idempotent on purpose: a second call keeps the first `completed_at` and the first
    `completed_version`, so an upgrade cannot rewrite the recorded decision. `version` is always the
    completed version, never the currently running one; callers compare it with the app version to
    report `version_changed`. Notes are kept when the new request omits them and replaced when it
    supplies them. `override_required_checks` is sticky: once the user overrode a failing required
    check, that fact stays recorded.
    """
    existing = read_state(session)
    completed_before = bool(existing and existing.get("completed"))
    if completed_before and existing is not None:
        completed_at = str(existing.get("completed_at") or datetime.now(UTC).isoformat())
        # Rows written before completed_version existed stored the completed version in "version".
        completed_version = str(existing.get("completed_version") or existing.get("version") or version)
        value: dict[str, Any] = {
            "completed": True,
            "completed_at": completed_at,
            "version": completed_version,
            "completed_version": completed_version,
            "skipped_ai": bool(skipped_ai),
            "actor": str(existing.get("actor") or actor),
        }
        if existing.get("notes") is not None:
            value["notes"] = existing["notes"]
        if notes is not None:
            value["notes"] = notes
        if existing.get("override_required_checks") or override_required_checks:
            value["override_required_checks"] = True
    else:
        value = {
            "completed": True,
            "completed_at": datetime.now(UTC).isoformat(),
            "version": version,
            "completed_version": version,
            "skipped_ai": bool(skipped_ai),
            "actor": actor,
        }
        if notes is not None:
            value["notes"] = notes
        if override_required_checks:
            value["override_required_checks"] = True

    row = session.get(AppSetting, SETTING_KEY)
    if row is None:
        row = AppSetting(key=SETTING_KEY, value_json=value, scope=SETTING_SCOPE)
        session.add(row)
    else:
        row.value_json = value
    session.flush()
    return value


def read_verifications(session: Session) -> dict[str, dict[str, Any]]:
    """Return the persisted verification record per component id. Never raises on bad JSON."""
    row = session.get(AppSetting, VERIFICATION_KEY)
    if row is None or not row.value_json:
        return {}
    payload = row.value_json.get("verifications") if isinstance(row.value_json, dict) else None
    if not isinstance(payload, dict):
        return {}
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def record_verification(session: Session, component_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Persist one verification attempt, replacing the previous record for that component."""
    records = read_verifications(session)
    records[component_id] = dict(record)
    if len(records) > VERIFICATION_LIMIT:
        # Keep the component just written plus the newest records by verification time.
        newest = sorted(
            (key for key in records if key != component_id),
            key=lambda key: str(records[key].get("verified_at") or ""),
            reverse=True,
        )[: VERIFICATION_LIMIT - 1]
        records = {key: records[key] for key in [component_id, *newest]}
    value = {"schema": "creatoros.setup.verifications.v1", "verifications": records}
    row = session.get(AppSetting, VERIFICATION_KEY)
    if row is None:
        row = AppSetting(key=VERIFICATION_KEY, value_json=value, scope=VERIFICATION_SCOPE)
        session.add(row)
    else:
        row.value_json = value
    session.flush()
    return record


def read_consents(session: Session) -> dict[str, dict[str, Any]]:
    """Return the recorded consent decision per component, if any."""
    row = session.get(AppSetting, CONSENT_KEY)
    if row is None or not row.value_json:
        return {}
    payload = row.value_json.get("consents") if isinstance(row.value_json, dict) else None
    if not isinstance(payload, dict):
        return {}
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def record_consent(
    session: Session,
    component_id: str,
    *,
    accepted: bool,
    version: str,
    sha256: str,
) -> dict[str, Any]:
    """Record an explicit yes/no. A refusal is evidence too and is never overwritten by a pass."""
    consents = read_consents(session)
    record = {
        "accepted": bool(accepted),
        "recorded_at": datetime.now(UTC).isoformat(),
        "version": str(version),
        "sha256": str(sha256),
    }
    consents[component_id] = record
    row = session.get(AppSetting, CONSENT_KEY)
    value = {"schema": "creatoros.setup.consents.v1", "consents": consents}
    if row is None:
        row = AppSetting(key=CONSENT_KEY, value_json=value, scope=CONSENT_SCOPE)
        session.add(row)
    else:
        row.value_json = value
    session.flush()
    return record
