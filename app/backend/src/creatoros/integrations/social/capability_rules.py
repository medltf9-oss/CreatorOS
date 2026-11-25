"""Capability rules: pure functions from stored facts to normalized states.

No model, no network, no database. Given an account type, the scopes the provider actually
granted, the app mode and the token state, this decides what the account can do - and records
why. Same input always produces the same verdict, which is what makes it testable and what makes
"why is this disabled?" answerable.
"""
from __future__ import annotations

from ...db.enums import (
    AccountType,
    CapabilityState,
    OAuthRoute,
    Platform,
    TokenState,
)
from .base import CapabilityVerdict
from .config import CapabilityDefinition, capabilities_for

# Tokens that cannot be used at all right now.
_DEAD_TOKEN_STATES: dict[TokenState, CapabilityState] = {
    TokenState.EXPIRED: CapabilityState.EXPIRED,
    TokenState.REVOKED: CapabilityState.REVOKED,
    TokenState.REAUTHORIZATION_REQUIRED: CapabilityState.REVOKED,
    TokenState.CREDENTIAL_UNAVAILABLE: CapabilityState.UNAVAILABLE,
    TokenState.REFRESH_FAILED: CapabilityState.UNAVAILABLE,
}


def _state_verb(state: CapabilityState) -> str:
    return {
        CapabilityState.AVAILABLE: "available",
        CapabilityState.NEEDS_USER_CONSENT: "not consented",
        CapabilityState.REQUIRES_DEVELOPER_APPROVAL: "waiting for developer-app approval",
        CapabilityState.UNSUPPORTED: "not supported for this account",
        CapabilityState.UNAVAILABLE: "unavailable right now",
        CapabilityState.EXPIRED: "blocked by an expired token",
        CapabilityState.REVOKED: "blocked by a revoked token",
        CapabilityState.MANUAL_ONLY: "manual only",
        CapabilityState.UNKNOWN: "unknown",
    }.get(state, state.value)


def evaluate(
    definition: CapabilityDefinition,
    *,
    route: OAuthRoute | None,
    account_type: AccountType,
    granted_scopes: list[str],
    token_state: TokenState,
    app_mode: str = "development",
    app_approved: bool | None = None,
    extra: dict | None = None,
    page_tasks: tuple[str, ...] | None = None,
) -> CapabilityVerdict:
    """One capability, one deterministic verdict with an explainable reason."""
    granted = list(dict.fromkeys(granted_scopes))
    required = list(definition.scopes_for(route))
    missing = [scope for scope in required if scope not in granted]
    required_tasks = list(definition.required_page_tasks)
    held_tasks = list(page_tasks or ())
    missing_tasks = [task for task in required_tasks if task not in held_tasks] if required_tasks else []
    evidence: dict = {
        "app_mode": app_mode,
        "route": route.value if route else None,
        "account_type": account_type.value,
        "token_state": token_state.value,
        "granted_scopes": granted,
        "evaluated_from": "stored account facts",
    }
    if required_tasks:
        # A Page task is a role, not a permission: it is only knowable once the provider told us.
        evidence["required_page_tasks"] = required_tasks
        evidence["page_tasks"] = held_tasks if page_tasks is not None else "unknown"
    if extra:
        evidence.update(extra)

    def verdict(state: CapabilityState, reason: str) -> CapabilityVerdict:
        return CapabilityVerdict(
            capability=definition.key,
            state=state.value,
            reason=reason,
            required=required,
            granted=granted,
            missing=missing,
            docs_reference=definition.docs_url,
            evidence=evidence,
        )

    if route is not None and route not in definition.routes:
        return verdict(
            CapabilityState.UNSUPPORTED,
            f"{definition.label} is only reachable through "
            f"{', '.join(r.value for r in definition.routes)}; this account uses {route.value}",
        )

    if definition.manual_only:
        return verdict(
            CapabilityState.MANUAL_ONLY,
            f"{definition.label} is always done by a human; nothing here is automated",
        )

    if account_type not in definition.account_types:
        return verdict(
            CapabilityState.UNSUPPORTED,
            f"{definition.label} needs a {', '.join(a.value for a in definition.account_types)} account, "
            f"this one is {account_type.value}",
        )

    dead = _DEAD_TOKEN_STATES.get(token_state)
    if dead is not None:
        return verdict(dead, f"{definition.label} is {_state_verb(dead)}: the credential is {token_state.value}")

    if not missing:
        if required_tasks and page_tasks is None:
            return verdict(
                CapabilityState.NEEDS_USER_CONSENT,
                f"{definition.label} needs the Page {', '.join(required_tasks)} task and the Page role "
                f"has not been verified yet",
            )
        if missing_tasks:
            return verdict(
                CapabilityState.NEEDS_USER_CONSENT,
                f"{definition.label} needs the Page {', '.join(missing_tasks)} task; the OAuth permission "
                f"is granted, but a Page task is a role the user must hold, not a scope",
            )
        return verdict(CapabilityState.AVAILABLE, f"{definition.label} is available: every required scope is granted")

    if app_approved is False:
        return verdict(
            CapabilityState.REQUIRES_DEVELOPER_APPROVAL,
            f"{definition.label} needs {', '.join(missing)}; the developer app does not have that permission approved",
        )
    if app_mode != "live":
        return verdict(
            CapabilityState.REQUIRES_DEVELOPER_APPROVAL,
            f"{definition.label} needs {', '.join(missing)}; the developer app is still in "
            f"{app_mode} mode, so this permission is only granted to app testers",
        )
    return verdict(
        CapabilityState.NEEDS_USER_CONSENT,
        f"{definition.label} needs {', '.join(missing)}, which the account did not grant",
    )


def evaluate_platform(
    platform: Platform,
    route: OAuthRoute | None,
    *,
    account_type: AccountType,
    granted_scopes: list[str],
    token_state: TokenState,
    app_mode: str = "development",
    app_approved: bool | None = None,
    extra: dict | None = None,
    page_tasks: tuple[str, ...] | None = None,
) -> list[CapabilityVerdict]:
    definitions = capabilities_for(platform, route)
    return [
        evaluate(
            definition,
            route=route,
            account_type=account_type,
            granted_scopes=granted_scopes,
            token_state=token_state,
            app_mode=app_mode,
            app_approved=app_approved,
            extra=extra,
            page_tasks=page_tasks,
        )
        for definition in definitions
    ]


def overall_state(verdicts: list[CapabilityVerdict]) -> CapabilityState:
    """Collapse a set of verdicts into one headline state for an account."""
    if not verdicts:
        return CapabilityState.UNKNOWN
    states = {CapabilityState(verdict.state) for verdict in verdicts}
    for candidate in (
        CapabilityState.REVOKED,
        CapabilityState.EXPIRED,
        CapabilityState.UNAVAILABLE,
        CapabilityState.REQUIRES_DEVELOPER_APPROVAL,
        CapabilityState.NEEDS_USER_CONSENT,
        CapabilityState.UNSUPPORTED,
    ):
        if candidate in states:
            return candidate
    if states == {CapabilityState.MANUAL_ONLY}:
        return CapabilityState.MANUAL_ONLY
    if CapabilityState.AVAILABLE in states:
        return CapabilityState.AVAILABLE
    return CapabilityState.UNKNOWN
