"""Account discovery: turn a token's visible accounts into account rows, without merging.

Discovery is where it is easiest to lie. A Facebook user token can see Pages; an Instagram
professional account may be reachable through two different login paths; a TikTok login exposes
exactly one creator. The rules here:

* one row per (platform, provider account id) - the provider's own identity, never a username;
* a second login path that resolves to a *different* provider id creates a second row and records
  the observation in `meta.observations`, it never overwrites the first;
* an account type the platform does not support is discovered and reported, not dropped;
* a discovered account without a usable token is reported with `token_present=False` so the caller
  can refuse to claim it is connected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...db.enums import AccountType, ConnectionStatus, OAuthRoute, Platform
from ...integrations.social.base import DiscoveredAccount


@dataclass(slots=True)
class DiscoveryPlan:
    claimed: list[DiscoveredAccount] = field(default_factory=list)
    unsupported: list[DiscoveredAccount] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    @property
    def usable(self) -> list[DiscoveredAccount]:
        return [account for account in self.claimed if account.access_token]

    def summary(self) -> dict:
        return {
            "discovered": len(self.claimed) + len(self.unsupported),
            "claimable": len(self.usable),
            "unsupported": [account.provider_account_id for account in self.unsupported],
            "duplicates": self.duplicates,
            "observations": self.observations,
        }


SUPPORTED_TYPES: dict[Platform, set[AccountType]] = {
    Platform.FACEBOOK: {AccountType.FACEBOOK_PAGE},
    Platform.INSTAGRAM: {AccountType.INSTAGRAM_BUSINESS, AccountType.INSTAGRAM_CREATOR},
    Platform.TIKTOK: {AccountType.TIKTOK_CREATOR},
}


def plan_discovery(
    platform: Platform,
    route: OAuthRoute,
    discovered: list[DiscoveredAccount],
    *,
    existing: dict[str, str] | None = None,
) -> DiscoveryPlan:
    """Split discovery results into claimable accounts and things to report.

    `existing` maps provider account id to the route it was already connected through, which is
    how "this Instagram account is connected twice, through two login paths" is detected.
    """
    plan = DiscoveryPlan()
    existing = existing or {}
    supported = SUPPORTED_TYPES[platform]
    seen: set[str] = set()
    usernames: dict[str, str] = {}

    for account in discovered:
        if not account.provider_account_id:
            plan.observations.append("the provider returned an account without an id; it was ignored")
            continue
        if account.provider_account_id in seen:
            plan.observations.append(
                f"{account.provider_account_id} appeared twice in one discovery; the duplicate was ignored"
            )
            continue
        seen.add(account.provider_account_id)

        if account.account_type not in supported:
            plan.unsupported.append(account)
            continue

        previous_route = existing.get(account.provider_account_id)
        if previous_route and previous_route != route.value:
            plan.duplicates.append(
                {
                    "provider_account_id": account.provider_account_id,
                    "already_connected_via": previous_route,
                    "new_route": route.value,
                }
            )

        if account.username:
            other = usernames.get(account.username.lower())
            if other and other != account.provider_account_id:
                plan.observations.append(
                    f"username {account.username!r} maps to two provider ids ({other}, "
                    f"{account.provider_account_id}); both are kept as separate accounts"
                )
            usernames[account.username.lower()] = account.provider_account_id

        plan.claimed.append(account)

        if not account.access_token:
            plan.observations.append(
                f"{account.provider_account_id} was discovered but came back without a token; "
                "it cannot be claimed"
            )
        if account.tasks and "CREATE_CONTENT" in account.tasks:
            plan.observations.append(f"{account.provider_account_id} grants CREATE_CONTENT")
        if account.tasks and "CREATE_CONTENT" not in account.tasks and "MANAGE" not in account.tasks:
            plan.observations.append(
                f"{account.provider_account_id} does not list a content task; posting will be reported as unavailable"
            )
    return plan


def connection_status_for(account: DiscoveredAccount, *, scopes_granted: list[str], required: list[str]) -> ConnectionStatus:
    if not account.access_token:
        return ConnectionStatus.DISCONNECTED
    missing = [scope for scope in required if scope not in scopes_granted]
    if missing:
        return ConnectionStatus.MISSING_PERMISSION
    return ConnectionStatus.CONNECTED
