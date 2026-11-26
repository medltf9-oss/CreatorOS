"""Activation status: one command that says exactly what is missing, and what to do next.

    python scripts/activation_status.py            # human report
    python scripts/activation_status.py --json     # machine-readable
    python scripts/activation_status.py --probe    # also make the read-only network probes

Reads the rows the application itself reads - `provider_app_config`, `secret_ref`,
`social_account`, `account_capability`, `app_setting['publish.delivery']`, `analytics_*` - plus the
static route specifications the OAuth engine uses. It stores nothing, changes nothing and prints no
secret. Every gap names the next action, because "not configured" on its own is not actionable.

Exit codes:
    0  coherent: nothing is misconfigured. A fresh installation with no provider apps is *ready to be
       activated*, not broken, and says so.
    1  something is configured but wrong (a redirect URI a provider will reject, a required scope
       missing from the registration, a stored secret the vault cannot read) - or `--require-ready`
       was passed and something is still missing.
    2  the report itself could not be built.

That distinction is the point: "not activated yet" and "misconfigured" are different states, and only
the second one is a defect.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any

OK = "ok"
READY = "ready"  # nothing configured yet, nothing wrong
MISSING = "missing"
INVALID = "invalid"
UNKNOWN = "unknown"

SEVERITY = {OK: 0, READY: 0, MISSING: 1, UNKNOWN: 1, INVALID: 2}


@dataclass(slots=True)
class Check:
    """One fact about the activation state, with the action that resolves it."""

    key: str
    status: str
    detail: str
    action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "status": self.status, "detail": self.detail, "action": self.action}


@dataclass(slots=True)
class RouteReport:
    platform: str
    route: str
    label: str
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        worst = max((SEVERITY[check.status] for check in self.checks), default=0)
        if worst == 2:
            return INVALID
        if any(check.status == MISSING for check in self.checks):
            return MISSING
        if any(check.status == UNKNOWN for check in self.checks):
            return UNKNOWN
        return OK if any(check.status == OK for check in self.checks) else READY

    @property
    def next_action(self) -> str:
        """The first check that needs a person, in the order the checks were written."""
        for check in self.checks:
            if check.status == INVALID and check.action:
                return check.action
        for check in self.checks:
            if check.status == MISSING and check.action:
                return check.action
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "route": self.route,
            "label": self.label,
            "status": self.status,
            "next_action": self.next_action,
            "checks": [check.as_dict() for check in self.checks],
        }


def _account_rows(session, platform) -> list:
    import sqlalchemy as sa

    from creatoros.db.models import SocialAccount

    return list(session.scalars(sa.select(SocialAccount).where(SocialAccount.platform == platform)))


def _secret_readable(service, ref_id: str | None) -> Check:
    """A stored secret that the vault cannot read is worse than no secret: it fails at connect time."""
    if not ref_id:
        return Check("client_secret", MISSING, "no app secret has been stored",
                     "Social accounts -> App settings -> Client secret (it goes straight to the DPAPI vault)")
    ref = service._secret_ref(ref_id) if hasattr(service, "_secret_ref") else None
    if ref is None:
        return Check("client_secret", INVALID, "the linked secret record no longer exists",
                     "re-enter the client secret in Social accounts -> App settings")
    return Check("client_secret", OK, f"stored in the vault as {ref.id} (purpose {ref.purpose})")


async def _secret_verdict(service, ref_id: str | None) -> Check:
    """Ask the vault to read the record, which is the only way to know it is still decryptable."""
    if not ref_id:
        return Check("client_secret", MISSING, "no app secret has been stored",
                     "Social accounts -> App settings -> Client secret (it goes straight to the DPAPI vault)")
    try:
        ref = service._secret_ref(ref_id)
        if ref is None:
            return Check("client_secret", INVALID, "the linked secret record no longer exists",
                         "re-enter the client secret in Social accounts -> App settings")
        verdict = await service.secrets.verify(ref)
    except Exception as exc:  # noqa: BLE001 - a vault that cannot answer is a finding, not a crash
        return Check("client_secret", UNKNOWN, f"the vault did not answer: {type(exc).__name__}",
                     "start the desktop app: the vault lives in the Electron main process")
    if verdict.get("ok"):
        return Check("client_secret", OK, "the vault decrypted the app secret")
    return Check(
        "client_secret",
        INVALID,
        f"the vault cannot read the app secret ({verdict.get('code')})",
        "re-enter the client secret; a profile or user change invalidates DPAPI ciphertext",
    )


def _route_checks(session, vault, spec, *, probe: bool) -> RouteReport:
    from creatoros.services.social.apps import ProviderAppService, validate_redirect_uri
    from creatoros.services.social.tokens import TokenService, compute_status

    report = RouteReport(platform=spec.platform.value, route=spec.route.value, label=spec.label)
    service = ProviderAppService(session, vault)
    config = service.get(spec.platform, spec.route)

    if config is None:
        report.checks.append(
            Check(
                "provider_app",
                MISSING,
                "this login path has no registered developer app",
                f"create the app at {spec.docs_url} then register it in Social accounts -> App settings",
            )
        )
        # Still report what the registration will need, so the person knows what to copy.
        report.checks.append(
            Check("scopes", READY, "will request: " + ", ".join(spec.scopes), "")
        )
        report.checks.append(
            Check(
                "redirect_uri",
                READY,
                "must be http://127.0.0.1:<port><callback path>, the exact string the provider stores",
                "",
            )
        )
        if probe:
            report.checks.append(
                Check("provider_probe", UNKNOWN, "not run: no registered app and no connected account",
                      "register the app and connect an account first, then re-run with --probe")
            )
        return report

    report.checks.append(Check("provider_app", OK, f"registered, mode {config.app_mode}, version {config.api_version or 'default'}"))

    try:
        validate_redirect_uri(config.redirect_uri, spec.route)
        report.checks.append(Check("redirect_uri", OK, config.redirect_uri))
    except Exception as error:  # noqa: BLE001 - SocialError carries the human message
        report.checks.append(
            Check("redirect_uri", INVALID, getattr(error, "message", str(error)),
                  "Social accounts -> App settings -> Redirect URI")
        )

    registered = [str(scope) for scope in (config.scopes_json or [])]
    missing_required = [scope for scope in spec.scopes if scope not in registered]
    missing_extra = [scope for scope in spec.extra_scopes if scope not in registered]
    if missing_required:
        report.checks.append(
            Check(
                "scopes",
                INVALID,
                "missing from the registration: " + ", ".join(missing_required) + " - the login will be refused",
                "add these scopes to the app registration (both here and in the provider console)",
            )
        )
    elif missing_extra:
        report.checks.append(
            Check(
                "scopes",
                OK,
                "required scopes registered; optional not requested: " + ", ".join(missing_extra),
                f"add them only if you need that feature - some need separate review ({spec.docs_url})",
            )
        )
    else:
        report.checks.append(Check("scopes", OK, "every scope this path requests is registered"))

    try:
        report.checks.append(asyncio.run(_secret_verdict(service, config.client_secret_ref_id)))
    except RuntimeError:
        # Already inside an event loop (tests): fall back to the non-decrypting check.
        report.checks.append(_secret_readable(service, config.client_secret_ref_id))

    accounts = _account_rows(session, spec.platform)
    connected = [a for a in accounts if str(a.connection_status.value) == "connected"]
    if not accounts:
        report.checks.append(
            Check("account", MISSING, "no account of this platform has been connected",
                  "Social accounts -> Connect account (this is the OAuth step)")
        )
    elif not connected:
        report.checks.append(
            Check("account", MISSING, f"{len(accounts)} account(s) known, none connected",
                  "Social accounts -> Connected accounts: reconnect, then read the reason shown there")
        )
    else:
        account = connected[0]
        status = compute_status(account)
        report.checks.append(
            Check("account", OK, f"{account.display_name or account.id}: {status.state}, {len(account.scopes_json or [])} scope(s) stored")
        )
        report.checks.append(_token_check(session, vault, account, TokenService))
        report.checks.append(_capability_check(account))

    if probe and config is not None and connected:
        report.checks.append(asyncio.run(_probe_check(session, vault, connected[0], spec)))
    elif probe:
        report.checks.append(Check("provider_probe", UNKNOWN, "not run: no connected account",
                                   "connect the account first, then re-run with --probe"))

    return report


def _token_check(session, vault, account, TokenService) -> Check:
    from creatoros.services.social.tokens import compute_status

    status = compute_status(account)
    state = str(status.state)
    if state in {"valid", "expiring_soon"}:
        detail = f"token {state}"
        if status.expires_at is not None:
            detail += f", expires {status.expires_at.date().isoformat()}"
        return Check("token", OK, detail)
    if state == "expired":
        return Check("token", MISSING, "the access token has expired",
                     "Social accounts -> Account details -> refresh (a provider that offers refresh does it itself)")
    return Check("token", MISSING, f"token {state}", "Social accounts -> Connected accounts: reconnect")


def _capability_check(account) -> Check:
    from creatoros.db.enums import CapabilityState

    capabilities = account.capabilities_json or {}
    if not capabilities:
        return Check("capabilities", UNKNOWN, "no capability verdict recorded yet",
                     "open the account once (or POST /social/accounts/{id}/capabilities/refresh)")
    available = [key for key, value in capabilities.items() if value == CapabilityState.AVAILABLE.value]
    blocked = {key: value for key, value in capabilities.items() if value != CapabilityState.AVAILABLE.value}
    if blocked:
        sample = ", ".join(f"{key}={value}" for key, value in list(blocked.items())[:4])
        return Check("capabilities", OK, f"{len(available)} available, {len(blocked)} limited: {sample}")
    return Check("capabilities", OK, f"{len(available)} available, none limited")


async def _probe_check(session, vault, account, spec) -> Check:
    """A read-only provider call: does this token still do what the scope list implies?"""
    from creatoros.integrations.social.registry import AdapterRegistry

    from creatoros.services.social.apps import ProviderAppService
    from creatoros.services.social.tokens import TokenService

    try:
        _config, credentials = await ProviderAppService(session, vault).resolve_credentials(spec.platform, spec.route)
        token = await TokenService(session, vault).access_token(account)
        adapter = AdapterRegistry().get(spec.platform, spec.route)
        try:
            report = await adapter.diagnostics(credentials, token)
        finally:
            await adapter.aclose()
    except Exception as error:  # noqa: BLE001 - report the provider's answer, never swallow it
        return Check("provider_probe", INVALID, f"the probe failed: {getattr(error, 'message', str(error))}",
                     "read GET /social/accounts/{id}/diagnostics in the app for the classified reason")
    checks = report.get("checks") or []
    failed = [check for check in checks if check.get("ok") is False]
    if failed:
        return Check(
            "provider_probe",
            INVALID,
            "; ".join(f"{check.get('name')}: {check.get('detail')}" for check in failed[:3]),
            "fix the first failing check; the others usually follow",
        )
    return Check("provider_probe", OK, f"{len(checks)} provider check(s) passed")


def _delivery_report(session, vault) -> dict[str, Any]:
    from creatoros.services.publishing.delivery import DeliveryService

    service = DeliveryService(session, vault=vault)
    config = service.load_config()
    if config is None:
        return {
            "status": MISSING,
            "detail": "no S3-compatible delivery provider is configured",
            "action": "Publishing -> Media delivery: endpoint, bucket, region, prefix, then the keys (they go to the vault)",
            "missing": service.missing(),
        }
    missing = service.missing()
    if missing:
        return {
            "status": INVALID,
            "detail": "configured but incomplete: missing " + ", ".join(missing),
            "action": "Publishing -> Media delivery",
        }
    return {
        "status": OK,
        "detail": f"{config.provider} bucket {config.bucket} in {config.region}, URLs live {config.url_ttl_s}s",
        "action": "",
        "credentials": "vault" if service.secret_ref() else "none",
    }


def _analytics_report(session) -> dict[str, Any]:
    import sqlalchemy as sa

    from creatoros.db.models import AnalyticsCollectionRun, AnalyticsSnapshot, AnalyticsSyncState

    runs = session.scalar(sa.select(sa.func.count()).select_from(AnalyticsCollectionRun)) or 0
    snapshots = session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot)) or 0
    states = session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSyncState)) or 0
    if not states:
        return {
            "status": READY,
            "detail": "no analytics sync state yet: nothing has been scheduled",
            "action": "connect an account; the analytics lane adopts it on the next sweeper tick",
            "runs": runs,
            "observations": snapshots,
        }
    return {
        "status": OK if snapshots else MISSING,
        "detail": f"{states} sync state(s), {runs} run(s), {snapshots} observation(s)",
        "action": "" if snapshots else "Analytics -> Overview -> Refresh, or wait for the daily collection",
        "runs": runs,
        "observations": snapshots,
    }


def build_report(*, probe: bool = False) -> dict[str, Any]:
    from creatoros.api.routes.social import get_vault
    from creatoros.db.session import get_session_factory
    from creatoros.integrations.social import config as social_config

    session = get_session_factory()()
    try:
        vault = get_vault()
        routes = []
        for spec in social_config.ROUTE_SPECS.values():
            routes.append(_route_checks(session, vault, spec, probe=probe).as_dict())

        delivery = _delivery_report(session, vault)
        analytics = _analytics_report(session)
        return {
            "routes": routes,
            "delivery": delivery,
            "analytics": analytics,
            "platform_totals": _platform_totals(routes),
        }
    finally:
        session.close()


def _platform_totals(routes: list[dict[str, Any]]) -> dict[str, str]:
    totals: dict[str, str] = {}
    for route in routes:
        current = totals.get(route["platform"])
        if current is None or SEVERITY[route["status"]] > SEVERITY[current]:
            totals[route["platform"]] = route["status"]
    return totals


def worst_status(report: dict[str, Any]) -> str:
    statuses = [route["status"] for route in report["routes"]]
    statuses.append(report["delivery"]["status"])
    worst = max((SEVERITY[status] for status in statuses), default=0)
    return INVALID if worst == 2 else MISSING if worst == 1 else OK


def exit_code(report: dict[str, Any], *, require_ready: bool = False) -> int:
    """0 when coherent, 1 when misconfigured (or, with `--require-ready`, when anything is missing)."""
    status = worst_status(report)
    if status == INVALID:
        return 1
    if require_ready and status == MISSING:
        return 1
    return 0


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for route in report["routes"]:
        lines.append(f"{route['label']} [{route['platform']}/{route['route']}]  ->  {route['status'].upper()}")
        for check in route["checks"]:
            mark = {"ok": "ok  ", "ready": "....", "missing": "MISS", "invalid": "BAD ", "unknown": "?   "}[check["status"]]
            lines.append(f"   [{mark}] {check['key']:<14} {check['detail']}")
        if route["next_action"]:
            lines.append(f"   next: {route['next_action']}")
        lines.append("")
    delivery = report["delivery"]
    lines.append(f"Media delivery  ->  {delivery['status'].upper()}")
    lines.append(f"   {delivery['detail']}")
    if delivery.get("action"):
        lines.append(f"   next: {delivery['action']}")
    analytics = report["analytics"]
    lines.append("")
    lines.append(f"Analytics  ->  {analytics['status'].upper()}")
    lines.append(f"   {analytics['detail']}")
    if analytics.get("action"):
        lines.append(f"   next: {analytics['action']}")
    lines.append("")
    lines.append("Overall: " + worst_status(report).upper())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report what CreatorOS still needs for live operation")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--probe", action="store_true", help="also call the provider read-only diagnostics")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="treat 'still missing' as a failure too (use this before a live test, not on a fresh install)",
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(probe=args.probe)
    except Exception as error:  # noqa: BLE001 - the report failing is its own answer
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 1 if exit_code(report, require_ready=args.require_ready) else 0


if __name__ == "__main__":
    raise SystemExit(main())
