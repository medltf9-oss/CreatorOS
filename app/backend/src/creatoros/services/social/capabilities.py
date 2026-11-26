"""Capability engine: evaluate an account, store the current state and the evidence history.

Two tables, two jobs:

* `account_capability` - one row per (account, capability) holding the *current* state plus the
  reason, required and granted scopes, API version and evidence reference. This is what Phase 7
  reads to decide whether a publish path exists.
* `capability_snapshot` - an append-only record of each evaluation, so "why did it say available
  last Tuesday?" has an answer.

The state vocabulary is canonical (`CAPABILITY_CANONICAL_STATES`); legacy strings are normalized
on the way in, so the UI can never show two names for one condition.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.base import utcnow
from ...db.enums import (
    CANONICAL_CAPABILITY_STATES,
    CapabilityState,
    ConnectionStatus,
    normalize_capability_state,
)
from ...db.models.platform import AccountCapability, CapabilitySnapshot, SocialAccount
from ...integrations.social.base import CapabilityVerdict
from ...integrations.social.capability_rules import overall_state
from ...integrations.social.config import capabilities_for
from ..backup import AuditService

# How a normalized state reads as an account-level connection status.
CONNECTION_STATUS_FOR: dict[CapabilityState, ConnectionStatus] = {
    CapabilityState.AVAILABLE: ConnectionStatus.CONNECTED,
    CapabilityState.NEEDS_USER_CONSENT: ConnectionStatus.MISSING_PERMISSION,
    CapabilityState.REQUIRES_DEVELOPER_APPROVAL: ConnectionStatus.NEEDS_APP_REVIEW,
    CapabilityState.UNSUPPORTED: ConnectionStatus.UNSUPPORTED_ACCOUNT_TYPE,
    CapabilityState.EXPIRED: ConnectionStatus.TOKEN_EXPIRED,
    CapabilityState.REVOKED: ConnectionStatus.TOKEN_EXPIRED,
    CapabilityState.UNAVAILABLE: ConnectionStatus.UNKNOWN,
    CapabilityState.MANUAL_ONLY: ConnectionStatus.CONNECTED,
    CapabilityState.UNKNOWN: ConnectionStatus.UNKNOWN,
}


def stored_page_tasks(account: SocialAccount) -> tuple[str, ...] | None:
    """The Page role list the provider last told us, from stored facts only.

    ``None`` means "the provider never reported a task list" (an account connected before P8.20, or
    a discovery that returned none) - it is deliberately not the same as "reported an empty list",
    because a Facebook Page grant (`ANALYZE`) must be *verified*, never assumed. Analytics consent
    (P8.20) and `refresh_capabilities` both read this so the two never disagree.
    """
    meta = account.meta_json or {}
    if "page_tasks" not in meta:
        return None
    return tuple(str(task) for task in (meta.get("page_tasks") or []))


@dataclass(slots=True)
class CapabilityReport:
    account_id: str
    overall: CapabilityState
    capabilities: list[CapabilityVerdict]
    snapshot_id: str
    checked_at: str

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "overall_state": self.overall.value,
            "checked_at": self.checked_at,
            "snapshot_id": self.snapshot_id,
            "capabilities": [
                {
                    "capability": verdict.capability,
                    "state": verdict.state,
                    "reason": verdict.reason,
                    "required_scopes": verdict.required,
                    "granted_scopes": verdict.granted,
                    "missing_scopes": verdict.missing,
                    "docs_reference": verdict.docs_reference,
                    "evidence": verdict.evidence,
                }
                for verdict in self.capabilities
            ],
        }


class CapabilityService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)

    # -- write -------------------------------------------------------------
    def record(
        self,
        account: SocialAccount,
        verdicts: list[CapabilityVerdict],
        *,
        trigger: str,
        api_version: str | None,
        evidence_ref: str | None = None,
    ) -> CapabilityReport:
        now = utcnow()
        existing = {
            row.capability: row
            for row in self.session.execute(
                sa.select(AccountCapability).where(AccountCapability.account_id == account.id)
            ).scalars()
        }
        for verdict in verdicts:
            state = normalize_capability_state(verdict.state)
            if state not in CANONICAL_CAPABILITY_STATES:  # pragma: no cover - defensive
                state = CapabilityState.UNKNOWN
            row = existing.get(verdict.capability)
            if row is None:
                row = AccountCapability(
                    account_id=account.id,
                    capability=verdict.capability,
                    state=state,
                    verified_at=now,
                )
                self.session.add(row)
            row.state = state
            row.reason = verdict.reason[:2000]
            row.required_json = list(verdict.required)
            row.granted_json = list(verdict.granted)
            row.missing_json = list(verdict.missing)
            row.verified_at = now
            row.api_version = api_version
            row.evidence_ref = evidence_ref
            row.docs_reference = verdict.docs_reference[:512]
        self.session.flush()

        # Only headline capabilities decide the account's headline state; analytics-style
        # capabilities need permissions this flow never requests and are listed separately.
        headline_keys = {definition.key for definition in capabilities_for(account.platform) if definition.headline}
        overall = overall_state([verdict for verdict in verdicts if verdict.capability in headline_keys])
        # `connection_status` answers "is this credential connected and working", not "is every
        # capability unlocked". An account with a valid token and one working capability is
        # connected; a permission or app-review gap is reported on the capabilities themselves.
        any_available = any(normalize_capability_state(v.state) == CapabilityState.AVAILABLE for v in verdicts)
        connection = CONNECTION_STATUS_FOR.get(overall, ConnectionStatus.UNKNOWN)
        if any_available and connection in {
            ConnectionStatus.MISSING_PERMISSION,
            ConnectionStatus.NEEDS_APP_REVIEW,
            ConnectionStatus.UNSUPPORTED_ACCOUNT_TYPE,
        }:
            connection = ConnectionStatus.CONNECTED
        snapshot = CapabilitySnapshot(
            account_id=account.id,
            checked_at=now,
            docs_version=api_version,
            trigger=trigger,
            api_version=api_version,
            overall_state=overall,
            granted_json=list(account.scopes_json or []),
            evidence_ref=evidence_ref,
            checks_json={
                verdict.capability: {
                    "state": normalize_capability_state(verdict.state).value,
                    "reason": verdict.reason,
                    "required": verdict.required,
                    "missing": verdict.missing,
                    "evidence": verdict.evidence,
                }
                for verdict in verdicts
            },
        )
        self.session.add(snapshot)
        account.capabilities_json = {
            verdict.capability: normalize_capability_state(verdict.state).value for verdict in verdicts
        }
        account.connection_status = connection
        account.last_verified_at = now
        self.session.flush()
        self.audit.record(
            "account.capabilities_evaluated",
            resource_type="social_account",
            resource_id=account.id,
            metadata={
                "trigger": trigger,
                "overall": overall.value,
                "states": {verdict.capability: verdict.state for verdict in verdicts},
            },
        )
        return CapabilityReport(
            account_id=account.id,
            overall=overall,
            capabilities=verdicts,
            snapshot_id=snapshot.id,
            checked_at=now.isoformat(),
        )

    # -- read --------------------------------------------------------------
    def current(self, account_id: str) -> list[AccountCapability]:
        return list(
            self.session.execute(
                sa.select(AccountCapability)
                .where(AccountCapability.account_id == account_id)
                .order_by(AccountCapability.capability)
            ).scalars()
        )

    def snapshots(self, account_id: str, *, limit: int = 20) -> list[CapabilitySnapshot]:
        return list(
            self.session.execute(
                sa.select(CapabilitySnapshot)
                .where(CapabilitySnapshot.account_id == account_id)
                .order_by(CapabilitySnapshot.checked_at.desc())
                .limit(limit)
            ).scalars()
        )

    def matrix(self) -> dict:
        """Every account's capabilities, for the Permissions page."""
        accounts = list(self.session.execute(sa.select(SocialAccount)).scalars())
        rows = list(self.session.execute(sa.select(AccountCapability)).scalars())
        by_account: dict[str, list[AccountCapability]] = {}
        for row in rows:
            by_account.setdefault(row.account_id, []).append(row)
        capabilities = sorted({row.capability for row in rows})
        states: dict[str, int] = {}
        for row in rows:
            key = row.state.value if hasattr(row.state, "value") else str(row.state)
            states[key] = states.get(key, 0) + 1
        return {
            "capabilities": capabilities,
            "accounts": [
                {
                    "id": account.id,
                    "platform": account.platform.value,
                    "label": account.internal_label or account.display_name,
                    "route": account.route.value if account.route else None,
                    "states": {
                        row.capability: row.state.value if hasattr(row.state, "value") else str(row.state)
                        for row in by_account.get(account.id, [])
                    },
                }
                for account in accounts
            ],
            "totals": states,
        }
