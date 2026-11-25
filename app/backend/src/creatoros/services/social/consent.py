"""Analytics consent: the precise permission difference, and the upgrade it starts (P8.20).

One question is answered here, in one place: *for this account, which analytics permissions are
missing, why, and who has to act?* The answer is computed from stored facts and the capability
catalogue - never from a client-supplied scope list, and never by re-requesting the whole connect
scope surface.

Two scope vocabularies meet and must not be merged:

* the **login path** decides which namespace is used (Instagram Login speaks ``instagram_business_*``;
  Instagram through Facebook Login speaks ``instagram_*`` and ``pages_*``);
* the **capability** decides which permission is needed (only the read-insight scope, never a
  publishing scope).

``required_scopes`` is the union of the path's analytics base scopes and the capability's insight
scope. ``requested_scopes`` is that union; the callback unions it again with what the account
already held, so a re-consent can add a permission but can never silently drop one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import (
    AnalyticsObjectType,
    ConsentStatus,
    OAuthRoute,
    Platform,
)
from ...db.models.platform import SocialAccount
from ...integrations.social.config import (
    analytics_capability,
    route_spec,
)
from .apps import ProviderAppService
from .capabilities import stored_page_tasks

#: The analytics *base* scopes a re-consent must keep for each login path: the scopes insights are
#: read with, without which the insight scope alone is useless. Deliberately excludes publishing
#: scopes - a permission upgrade is not a re-connect. Derived from the documented per-path scope
#: sets (see ``integrations/analytics/catalog.py`` for the metric-level form).
CONSENT_BASE_SCOPES: dict[tuple[Platform, OAuthRoute], tuple[str, ...]] = {
    (Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN): ("pages_read_engagement",),
    (Platform.INSTAGRAM, OAuthRoute.INSTAGRAM_LOGIN): ("instagram_business_basic",),
    (Platform.INSTAGRAM, OAuthRoute.FACEBOOK_LOGIN): (
        "instagram_basic",
        "pages_show_list",
        "pages_read_engagement",
    ),
    (Platform.TIKTOK, OAuthRoute.TIKTOK_LOGIN): ("user.info.basic",),
}

#: One sentence per *blocking condition*, so the UI never has to invent wording.
BLOCKER_TEXT: dict[str, str] = {
    "user_consent": "the account has not granted the insight permission yet",
    "developer_approval": "the permission is granted by the user but the developer app has not been approved for it",
    "page_task": "the OAuth permission is granted but the user does not hold the Page task it requires",
    "unsupported": "this account type or login path cannot reach the insight permission",
}

#: Only these object types carry analytics that the consent decision is about.
_CONSENT_OBJECT_TYPES = (
    AnalyticsObjectType.ACCOUNT,
    AnalyticsObjectType.POST,
    AnalyticsObjectType.VIDEO,
    AnalyticsObjectType.REEL,
)


@dataclass(slots=True)
class ConsentPlan:
    """Everything the UI and the API need to explain a permission gap and act on it."""

    account_id: str
    platform: Platform
    route: OAuthRoute | None
    account_type: str
    provider_account_id: str
    capability_key: str | None
    capability_label: str | None
    capability_state: str
    status: ConsentStatus
    required_scopes: list[str] = field(default_factory=list)
    granted_scopes: list[str] = field(default_factory=list)
    missing_scopes: list[str] = field(default_factory=list)
    requested_scopes: list[str] = field(default_factory=list)
    scope_reasons: dict[str, str] = field(default_factory=dict)
    page_tasks_required: list[str] = field(default_factory=list)
    page_tasks_held: list[str] = field(default_factory=list)
    page_tasks_missing: list[str] = field(default_factory=list)
    page_tasks_known: bool = False
    app_mode: str | None = None
    developer_approval_required: bool = False
    can_start: bool = False
    blocked_by: list[str] = field(default_factory=list)
    blockers_text: list[str] = field(default_factory=list)
    metrics_blocked: list[dict] = field(default_factory=list)
    docs_url: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "platform": self.platform.value,
            "route": self.route.value if self.route else None,
            "account_type": self.account_type,
            "provider_account_id": self.provider_account_id,
            "capability_key": self.capability_key,
            "capability_label": self.capability_label,
            "capability_state": self.capability_state,
            "status": self.status.value,
            "required_scopes": self.required_scopes,
            "granted_scopes": self.granted_scopes,
            "missing_scopes": self.missing_scopes,
            "requested_scopes": self.requested_scopes,
            "scope_reasons": self.scope_reasons,
            "page_tasks_required": self.page_tasks_required,
            "page_tasks_held": self.page_tasks_held,
            "page_tasks_missing": self.page_tasks_missing,
            "page_tasks_known": self.page_tasks_known,
            "app_mode": self.app_mode,
            "developer_approval_required": self.developer_approval_required,
            "can_start": self.can_start,
            "blocked_by": self.blocked_by,
            "blockers_text": self.blockers_text,
            "metrics_blocked": self.metrics_blocked,
            "docs_url": self.docs_url,
            "detail": self.detail,
        }


class ConsentService:
    """Plan a permission upgrade for one account, and start it through the existing OAuth engine."""

    def __init__(self, session: Session, vault) -> None:
        self.session = session
        self.vault = vault
        self.apps = ProviderAppService(session, vault)

    # -- planning ----------------------------------------------------------
    def plan(self, account: SocialAccount) -> ConsentPlan:
        platform = account.platform
        route = account.route
        capability = analytics_capability(platform)
        granted = list(dict.fromkeys(str(scope) for scope in (account.scopes_json or [])))

        plan = ConsentPlan(
            account_id=account.id,
            platform=platform,
            route=route,
            account_type=account.account_type.value,
            provider_account_id=account.provider_account_id,
            capability_key=capability.key if capability else None,
            capability_label=capability.label if capability else None,
            capability_state=(account.capabilities_json or {}).get(capability.key, "unknown")
            if capability
            else "unknown",
            status=ConsentStatus.UNSUPPORTED,
            granted_scopes=granted,
            docs_url=capability.docs_url if capability else None,
        )

        if capability is None or route is None or route not in capability.routes:
            plan.blocked_by = ["unsupported"]
            plan.blockers_text = [BLOCKER_TEXT["unsupported"]]
            plan.detail = (
                "no analytics insight permission exists for this account's platform and login path"
            )
            return plan
        if account.account_type not in capability.account_types:
            plan.blocked_by = ["unsupported"]
            plan.blockers_text = [BLOCKER_TEXT["unsupported"]]
            plan.detail = (
                f"{capability.label} needs a "
                f"{', '.join(item.value for item in capability.account_types)} account; this one is "
                f"{account.account_type.value}"
            )
            return plan

        base = list(CONSENT_BASE_SCOPES.get((platform, route), ()))
        insight = list(capability.scopes_for(route))
        required = list(dict.fromkeys([*base, *insight]))
        missing = [scope for scope in required if scope not in granted]

        plan.required_scopes = required
        plan.missing_scopes = missing
        # Ask only for what is required and missing; the callback unions it with what is already held.
        plan.requested_scopes = list(required)
        for scope in required:
            if scope in insight:
                plan.scope_reasons[scope] = f"required to read analytics: {capability.label}"
            else:
                plan.scope_reasons[scope] = "kept so the existing analytics access is not lost"

        config = self.apps.get(platform, route)
        plan.app_mode = config.app_mode if config else None

        required_tasks = list(capability.required_page_tasks)
        held = stored_page_tasks(account)
        plan.page_tasks_required = required_tasks
        plan.page_tasks_known = held is not None
        plan.page_tasks_held = list(held or ())
        plan.page_tasks_missing = [task for task in required_tasks if held is None or task not in held]

        plan.metrics_blocked = self._blocked_metrics(account, granted, required)

        if not missing:
            if plan.page_tasks_missing:
                plan.status = ConsentStatus.PAGE_TASK_REQUIRED
                plan.blocked_by = ["page_task"]
                plan.blockers_text = [BLOCKER_TEXT["page_task"]]
                plan.detail = (
                    "the insight permission is granted, but the user must also hold the Page task "
                    "that reads it; a Page task is assigned in Facebook, not granted here"
                )
            else:
                plan.status = ConsentStatus.CONSENT_NOT_NEEDED
                plan.detail = "every permission analytics needs is already granted"
            return plan

        developer = plan.app_mode not in {None, "live"}
        plan.developer_approval_required = developer
        if developer:
            plan.status = ConsentStatus.DEVELOPER_APPROVAL_REQUIRED
            plan.blocked_by = ["developer_approval"]
            plan.blockers_text = [BLOCKER_TEXT["developer_approval"]]
            plan.detail = (
                f"the developer app is in {plan.app_mode} mode, so this permission is only granted "
                "to app testers; user consent alone cannot make it available"
            )
        else:
            plan.status = ConsentStatus.CONSENT_REQUIRED
            plan.blocked_by = ["user_consent"]
            plan.blockers_text = [BLOCKER_TEXT["user_consent"]]
            plan.detail = f"analytics needs {', '.join(missing)}, which the account did not grant"
        # The user can still try; if the app is in development mode the attempt is the honest way to
        # find out whether this user is a tester, and the result reports the truth either way.
        plan.can_start = True
        if plan.page_tasks_missing and "page_task" not in plan.blocked_by:
            plan.blocked_by.append("page_task")
            plan.blockers_text.append(BLOCKER_TEXT["page_task"])
        return plan

    def _blocked_metrics(
        self, account: SocialAccount, granted: list[str], requested: list[str]
    ) -> list[dict]:
        """Which stored metrics would still be ``not_authorized`` after the upgrade, and why.

        Best effort and purely informational: it reads the metric registry, and if the registry has
        not been seeded it returns nothing rather than inventing a gap.
        """
        from ..analytics import registry

        scopes = set(granted) | set(requested)
        seen: dict[str, dict] = {}
        for object_type in _CONSENT_OBJECT_TYPES:
            try:
                planned = registry.plan(
                    self.session,
                    platform=account.platform,
                    object_type=object_type,
                    granted_scopes=scopes,
                    api_version=account.api_version,
                )
            except Exception:  # noqa: BLE001 - informational only, never blocks the plan
                return []
            for item in planned:
                if item.reason != registry.REASON_NOT_AUTHORIZED:
                    continue
                missing = [
                    scope
                    for scope in item.detail.split(":", 1)[-1].strip().split(", ")
                    if scope
                ]
                seen.setdefault(
                    item.metric_key,
                    {
                        "metric_key": item.metric_key,
                        "object_type": object_type.value,
                        "missing_scopes": missing,
                        "detail": item.detail,
                    },
                )
        return sorted(seen.values(), key=lambda row: row["metric_key"])

    # -- acting ------------------------------------------------------------
    async def start(self, account: SocialAccount, *, context: dict | None = None) -> dict:
        """Begin the re-consent transaction, bound to this account.

        Refuses when the account is not one that can ever read insights, and refuses to start a
        flow that would request no additional permission (there is nothing to consent to).
        """
        from .oauth_engine import OAuthEngine

        plan = self.plan(account)
        if not plan.can_start:
            return {
                "status": plan.status.value,
                "account_id": account.id,
                "plan": plan.to_dict(),
                "authorization_url": None,
                "session_id": None,
                "message": plan.detail,
            }
        engine = OAuthEngine(self.session, self.vault)
        started = await engine.start(
            account.platform,
            account.route,
            scopes=plan.requested_scopes,
            return_route=f"/accounts/{account.id}",
            account_id=account.id,
            purpose="analytics_consent",
            context={
                "missing_scopes": plan.missing_scopes,
                "required_scopes": plan.required_scopes,
                "capability_key": plan.capability_key,
                "page_tasks_required": plan.page_tasks_required,
                "status_before": plan.status.value,
            },
        )
        return {
            "status": ConsentStatus.AUTHORIZATION_STARTED.value,
            "account_id": account.id,
            "plan": plan.to_dict(),
            "session_id": started["session_id"],
            "authorization_url": started["authorization_url"],
            "redirect_uri": started["redirect_uri"],
            "expires_at": started["expires_at"],
            "scopes_requested": started["scopes_requested"],
            "pkce": started["pkce"],
            "app_mode": started["app_mode"],
            "poll_url": started["poll_url"],
            "message": (
                "Open the authorization page and approve the analytics permission. The account stays "
                "connected whether you approve or decline."
            ),
        }


def analytics_scope_surface(platform: Platform, route: OAuthRoute) -> set[str]:
    """The scopes a consent request for this path is allowed to name.

    A guard so the requested set can never contain a scope the route does not document: the same
    check the OAuth engine applies, exposed here for the catalogue test.
    """
    spec = route_spec(platform, route)
    return set(spec.scopes) | set(spec.extra_scopes)


def account_consent_scope_set(platform: Platform, route: OAuthRoute) -> list[str]:
    """The exact scopes a consent request for this path would ask for (ordered, de-duplicated)."""
    capability = analytics_capability(platform)
    if capability is None or route not in capability.routes:
        return []
    base = list(CONSENT_BASE_SCOPES.get((platform, route), ()))
    return list(dict.fromkeys([*base, *capability.scopes_for(route)]))


def accounts_needing_consent(session: Session, *, limit: int = 200) -> list[dict]:
    """Every connected account whose analytics capability is blocked by something a user can act on.

    Read-only and deterministic; used by the Social Accounts page to show an actionable notice
    without guessing from the platform name.
    """
    service = ConsentService(session, vault=None)
    rows = list(
        session.execute(
            sa.select(SocialAccount)
            .where(SocialAccount.disconnected_at.is_(None))
            .order_by(SocialAccount.created_at.desc())
            .limit(limit)
        ).scalars()
    )
    out: list[dict] = []
    for account in rows:
        plan = service.plan(account)
        if plan.status in {ConsentStatus.CONSENT_NOT_NEEDED, ConsentStatus.UNSUPPORTED}:
            continue
        out.append(
            {
                "account_id": account.id,
                "platform": account.platform.value,
                "label": account.internal_label or account.display_name,
                "status": plan.status.value,
                "capability_key": plan.capability_key,
                "missing_scopes": plan.missing_scopes,
                "can_start": plan.can_start,
                "detail": plan.detail,
            }
        )
    return out
