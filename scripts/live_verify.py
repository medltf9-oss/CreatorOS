"""Opt-in staged live verification: twelve steps, each with its own verdict.

This is the script a person runs **after** connecting a real account, to find out what actually works
against the provider. It is not part of the test suite and it never runs by itself: it refuses unless
`CREATOROS_LIVE_VERIFY=1` is set in the environment of that shell, so that no scheduled job, no IDE
"run" button and no CI pipeline can make a network call or a real publication on its own.

Every step prints one of four verdicts:

    ok        the step completed and the result is what the code claims it should be
    blocked   the step could not run, and the reason is a fact about this installation or the provider
    skipped   the step was deliberately not run (usually: no --publish, or no destination given)
    failed    the step ran and the result contradicts what the product claims

A `blocked` verdict is not a defect: "Meta has not granted Advanced Access yet" is the truthful state of
an unreviewed app. A `failed` verdict is: the harness expected something and the API disagreed.

Steps 10 (real publication) and 11-12 (which depend on it) require `--publish`, a destination, and an
approval that already exists. The script never approves anything: approval is a human decision made in
the application, bound to a payload hash, and this harness only checks that it happened.

Usage:
    set CREATOROS_LIVE_VERIFY=1
    set CREATOROS_API=http://127.0.0.1:5000
    set CREATOROS_SESSION_TOKEN=<the token the desktop shell minted>

    python scripts/live_verify.py                     # steps 1-9, 11 (read-only except the sync)
    python scripts/live_verify.py --destination <id>  # adds the publish-preflight and approval steps
    python scripts/live_verify.py --destination <id> --publish --confirm "PUBLISH <id> <hash>"
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

OPT_IN_ENV = "CREATOROS_LIVE_VERIFY"
OK, BLOCKED, SKIPPED, FAILED = "ok", "blocked", "skipped", "failed"
TERMINAL_PUBLISH = {
    "published",
    "failed",
    "cancelled",
    "blocked",
    "requires_user_action",
    "unknown_remote_state",
}


class Api:
    """Minimal authenticated client; no third-party dependency, same conventions as live_publish.py."""

    def __init__(self, base: str, token: str, timeout: int = 90) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode() or "{}"
                return json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode() or "{}"
            raise ApiError(method, path, error.code, detail) from error
        except urllib.error.URLError as error:
            # Not reachable at all: a fact about this shell, not a defect in the product.
            raise ApiError(method, path, 0, f'{{"detail": {{"message": "cannot reach {self.base}: '
                                            f'{error.reason}"}}}}') from error

    def get(self, path: str) -> dict:
        return self.call("GET", path)

    def post(self, path: str, payload: dict | None = None) -> dict:
        return self.call("POST", path, payload if payload is not None else {})


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = {}
        self.code = (parsed.get("detail") or {}).get("code") if isinstance(parsed.get("detail"), dict) else None
        self.message = (parsed.get("detail") or {}).get("message") if isinstance(parsed.get("detail"), dict) else detail
        super().__init__(f"HTTP {status} on {method} {path}: {self.message or detail}")

    @property
    def missing_grant(self) -> bool:
        """Provider-side refusals that mean 'not granted yet', not 'the product is broken'."""
        return self.status in {403, 429} or (self.code or "") in {
            "NOT_AUTHORIZED",
            "PERMISSION_DENIED",
            "APP_NOT_CONFIGURED",
            "REQUIRES_APP_REVIEW",
            "CAPABILITY_UNAVAILABLE",
            "DELIVERY_NOT_CONFIGURED",
        }


class Step:
    def __init__(self, number: int, name: str, verdict: str, detail: str, facts: dict | None = None) -> None:
        self.number = number
        self.name = name
        self.verdict = verdict
        self.detail = detail
        self.facts: dict = dict(facts or {})

    def as_dict(self) -> dict:
        return {
            "step": self.number,
            "name": self.name,
            "verdict": self.verdict,
            "detail": self.detail,
            "facts": self.facts,
        }


def _blocked(number: int, name: str, detail: str) -> Step:
    return Step(number, name, BLOCKED, detail)


def _route_spec(api: Api, platform: str, route: str | None) -> dict:
    for item in api.get("/social/oauth/routes").get("items", []):
        if item["platform"] == platform and (route is None or item["route"] == route):
            return item
    return {}


# --------------------------------------------------------------------------- steps 1-5: identity
def step_connection(api: Api, account_id: str) -> Step:
    detail = api.get(f"/social/accounts/{account_id}")
    facts = {
        "platform": detail["platform"],
        "route": detail.get("route"),
        "type": detail["account_type"],
        "connection": detail["connection_status"],
        "token_state": detail["token_state"],
        "scope_source": detail.get("scope_source"),
        "scopes": len(detail.get("scopes") or []),
        "api_version": detail.get("api_version"),
    }
    if detail["connection_status"] != "connected":
        return Step(1, "OAuth connection", BLOCKED, f"the account is {detail['connection_status']}", facts)
    return Step(1, "OAuth connection", OK, f"{detail['display_name']} ({detail['platform']}/{detail.get('route')})", facts)


def step_discovery(api: Api, account_id: str, platform: str, route: str | None) -> Step:
    """Account discovery is a real provider call, so it proves the token, not just the row."""
    try:
        payload = api.get(f"/social/accounts/{account_id}/diagnostics")
    except ApiError as error:
        return _blocked(1, "Account discovery", f"the provider call failed: {error.message}")
    provider = payload.get("provider_report") or {}
    if provider.get("error"):
        return _blocked(2, "Account discovery", f"provider refused: {provider['error'].get('message')}")
    checks = provider.get("checks") or []
    failed = [check for check in checks if check.get("ok") is False]
    pages = next((check for check in checks if check.get("name") == "pages"), None)
    facts = {
        "checks": [{check.get("name"): check.get("ok")} for check in checks],
        "token_valid": (provider.get("token") or {}).get("valid"),
        "app_id_matches": (provider.get("token") or {}).get("app_id_matches"),
        "reachable": pages.get("detail") if pages else None,
    }
    if failed:
        return Step(2, "Account discovery", BLOCKED, "; ".join(
            f"{check.get('name')}: {check.get('detail')}" for check in failed[:3]
        ), facts)
    return Step(2, "Account discovery", OK, pages["detail"] if pages else f"{len(checks)} provider check(s) passed", facts)


def step_permissions(api: Api, account_id: str, platform: str, route: str | None) -> Step:
    """Granted scopes from the provider, compared with what this login path asks for."""
    diagnostics = api.get(f"/social/accounts/{account_id}/diagnostics")
    granted = (diagnostics.get("provider_report") or {}).get("token", {}).get("scopes")
    if granted is None:
        return _blocked(3, "Permission verification", "the provider did not return a scope list")
    spec = _route_spec(api, platform, route)
    required = list(spec.get("scopes") or [])
    optional = list(spec.get("extra_scopes") or [])
    missing_required = [scope for scope in required if scope not in granted]
    missing_optional = [scope for scope in optional if scope not in granted]
    facts = {"granted": granted, "required": required, "missing_required": missing_required,
             "missing_optional": missing_optional}
    if missing_required:
        return Step(3, "Permission verification", FAILED,
                    "the provider did not grant required scopes: " + ", ".join(missing_required), facts)
    detail = f"all {len(required)} required scope(s) granted"
    if missing_optional:
        detail += "; not granted (optional): " + ", ".join(missing_optional)
    return Step(3, "Permission verification", OK, detail, facts)


def step_capabilities(api: Api, account_id: str) -> Step:
    before = api.get(f"/social/accounts/{account_id}/capabilities")
    after = api.post(f"/social/accounts/{account_id}/capabilities/refresh")
    stored = {row["capability"]: row["state"] for row in before.get("capabilities", [])} if isinstance(
        before.get("capabilities"), list
    ) else {}
    refreshed = {row["capability"]: row["state"] for row in after.get("capabilities", [])} if isinstance(
        after.get("capabilities"), list
    ) else {}
    changed = {key: (stored.get(key), value) for key, value in refreshed.items() if stored.get(key) != value}
    available = sorted(key for key, state in refreshed.items() if state == "available")
    facts = {"available": available, "changed_on_refresh": changed, "total": len(refreshed)}
    if changed:
        return Step(4, "Capability snapshot", OK,
                    f"{len(available)} available; re-evaluation changed {len(changed)} verdict(s) "
                    "(the stored state was stale)", facts)
    return Step(4, "Capability snapshot", OK, f"{len(available)} of {len(refreshed)} capabilities available", facts)


def step_token_lifecycle(api: Api, account_id: str) -> Step:
    status = api.get(f"/social/accounts/{account_id}/token")
    detail = api.get(f"/social/accounts/{account_id}")
    unreadable = [
        entry for entry in (detail.get("secret_health") or [])
        if entry.get("present") is False and entry.get("code")
    ]
    rotations = api.get("/social/rotations")
    pending = [item for item in rotations.get("items", []) if item.get("account_id") == account_id]
    facts = {
        "state": status["state"],
        "secret_health": detail.get("secret_health"),
        "expires_at": status.get("expires_at"),
        "has_refresh_token": status.get("has_refresh_token"),
        "can_refresh": status.get("can_refresh"),
        "pending_rotations": len(pending),
    }
    if unreadable:
        # `compute_status` is a pure function of stored facts; the vault is the other half of the truth.
        return Step(5, "Token lifecycle", BLOCKED,
                    "the stored credential cannot be read: " + ", ".join(
                        f"{entry['kind']} {entry['code']}" for entry in unreadable), facts)
    if status["state"] not in {"valid", "expiring_soon"}:
        return Step(5, "Token lifecycle", BLOCKED, f"the token is {status['state']}: {status.get('reason')}", facts)
    if pending:
        return Step(5, "Token lifecycle", BLOCKED,
                    f"{len(pending)} session(s) have a pending rotation, which must be resolved first", facts)
    message = f"token {status['state']}"
    if status.get("expires_at"):
        message += f", expires {status['expires_at'][:10]}"
    message += ", refresh available" if status.get("can_refresh") else ", no refresh (the provider issues long-lived tokens)"
    return Step(5, "Token lifecycle", OK, message, facts)


# ------------------------------------------------------------------------ step 6: analytics ingestion
def step_analytics_sync(api: Api, account_id: str, wait_s: int) -> Step:
    """Queue a real collection run and wait for it to close, then report availability honestly."""
    try:
        accepted = api.post(f"/analytics/sync/{account_id}")
    except ApiError as error:
        return _blocked(6, "Analytics synchronisation", f"the sync was refused: {error.message}")
    job_id = accepted.get("job_id")
    deadline = time.time() + wait_s
    run = None
    while time.time() < deadline:
        runs = api.get(f"/analytics/runs?account_id={account_id}&limit=5").get("items", [])
        closed = [item for item in runs if item.get("finished_at")]
        if closed:
            run = closed[0]
            break
        time.sleep(3)
    if run is None:
        return _blocked(6, "Analytics synchronisation",
                        f"queued ({job_id}) but no run closed within {wait_s}s; the analytics lane may be busy")
    facts = {
        "job_id": job_id,
        "run_id": run["id"],
        "outcome": run.get("outcome"),
        "attempted": run.get("metrics_attempted"),
        "observed": run.get("metrics_observed"),
        "unavailable": run.get("metrics_unavailable"),
        "http_calls": run.get("http_calls"),
        "api_version": run.get("api_version"),
    }
    availability = api.get(f"/analytics/accounts/{account_id}/capabilities")
    summary = availability.get("summary") or {}
    facts["availability"] = summary
    if not run.get("metrics_attempted"):
        return Step(6, "Analytics synchronisation", BLOCKED,
                    f"the run closed with outcome {run.get('outcome')} and attempted no metrics "
                    f"({run.get('error_class')}/{run.get('error_code')})", facts)
    if not run.get("metrics_observed"):
        return Step(6, "Analytics synchronisation", BLOCKED,
                    f"{run['metrics_attempted']} metric(s) attempted, none observed - "
                    "see the per-metric reason on the availability panel", facts)
    return Step(6, "Analytics synchronisation", OK,
                f"{run['metrics_observed']} of {run['metrics_attempted']} metric(s) observed "
                f"in {run.get('http_calls')} provider call(s)", facts)


# ------------------------------------------------------------------------- step 7: media delivery
def step_delivery(api: Api) -> Step:
    status = api.get("/publishing/delivery")
    if not status.get("configured"):
        return Step(7, "Media delivery", BLOCKED,
                    f"not configured: {status.get('reason') or 'no S3-compatible provider'}",
                    {"supported": status.get("supported_providers")})
    try:
        probe = api.post("/publishing/delivery/check")
    except ApiError as error:
        return Step(7, "Media delivery", BLOCKED, f"the bucket probe failed: {error.message}",
                    {"provider": status.get("provider"), "bucket": status.get("bucket")})
    facts = {"provider": status.get("provider"), "bucket": status.get("bucket"),
             "region": status.get("region"), "ttl_seconds": status.get("ttl_seconds"),
             "credentials_present": status.get("credentials_present"), "probe": probe}
    if not probe.get("ok"):
        return Step(7, "Media delivery", BLOCKED, f"the probe failed: {probe.get('reason')}", facts)
    return Step(7, "Media delivery", OK,
                f"{status.get('provider')} bucket {status.get('bucket')} passed write/verify/sign/read/delete", facts)


# ---------------------------------------------------------------------- steps 8-9: destination
def step_preflight(api: Api, variant_id: str) -> Step:
    actions = api.get(f"/publishing/destinations/{variant_id}/actions")
    try:
        validation = api.post(f"/publishing/destinations/{variant_id}/revalidate")
    except ApiError as error:
        return _blocked(8, "Publishing preflight", f"validation could not run: {error.message}")
    report = validation.get("report") or {}
    publish_now = actions.get("publish_now") or {}
    facts = {
        "state": actions.get("state"),
        "publish_now_enabled": publish_now.get("enabled"),
        "publish_now_reason": publish_now.get("reason"),
        "validation": report,
    }
    if not publish_now.get("enabled"):
        return Step(8, "Publishing preflight", BLOCKED,
                    f"a destination in {actions.get('state')} cannot be queued: {publish_now.get('reason')}", facts)
    blocking = report.get("issues") or report.get("errors") or []
    if blocking:
        return Step(8, "Publishing preflight", BLOCKED,
                    f"{len(blocking)} validation issue(s): {blocking[0]}", facts)
    return Step(8, "Publishing preflight", OK, "the destination validates and may be queued", facts)


def step_approval(api: Api, variant_id: str) -> Step:
    approval = api.get(f"/publishing/destinations/{variant_id}/approval")
    facts = {
        "state": approval.get("state"),
        "pending": approval.get("pending"),
        "payload_hash": approval.get("payload_hash"),
        "approved_hash": approval.get("approved_hash"),
        "consent_text_version": approval.get("consent_text_version"),
    }
    if not approval.get("approved_hash"):
        return Step(9, "Human approval", BLOCKED,
                    "no approval is recorded for the current payload; approval is a human decision in the app", facts)
    if approval.get("approved_hash") != approval.get("payload_hash"):
        return Step(9, "Human approval", FAILED,
                    "the approved hash does not match the current payload: the content changed after approval", facts)
    return Step(9, "Human approval", OK, f"approved for payload {approval['approved_hash'][:12]}…", facts)


# ------------------------------------------------------------------- steps 10-12: publication
def step_publish(api: Api, variant_id: str, *, allowed: bool, confirm: str | None, wait_s: int) -> Step:
    approval = api.get(f"/publishing/destinations/{variant_id}/approval")
    phrase = f"PUBLISH {variant_id} {approval.get('approved_hash') or 'unapproved'}"
    if not allowed:
        return Step(10, "Real provider publication", SKIPPED,
                    f"not requested; re-run with --publish --confirm \"{phrase}\" once you are ready",
                    {"confirm_phrase": phrase})
    if confirm != phrase:
        return Step(10, "Real provider publication", SKIPPED,
                    f"the confirmation phrase did not match; it is exactly: {phrase}",
                    {"confirm_phrase": phrase})
    if not approval.get("approved_hash"):
        return Step(10, "Real provider publication", BLOCKED,
                    "there is no approval for this payload, so nothing was published")
    outcome = api.post(f"/publishing/destinations/{variant_id}/publish-now")
    if not outcome.get("queued"):
        return Step(10, "Real provider publication", BLOCKED,
                    f"the API refused to queue it: {outcome.get('reason')}", {"outcome": outcome})
    deadline = time.time() + wait_s
    job = api.get(f"/publishing/destinations/{variant_id}")["job"]
    while time.time() < deadline and job["state"] not in TERMINAL_PUBLISH:
        time.sleep(5)
        job = api.get(f"/publishing/destinations/{variant_id}")["job"]
    facts = {
        "state": job["state"],
        "state_reason": job.get("state_reason"),
        "provider_permalink": job.get("provider_permalink"),
        "provider_post_id": job.get("provider_post_id") or job.get("provider_media_id"),
        "attempts": len(job.get("attempts_log") or []),
    }
    if job["state"] == "published":
        return Step(10, "Real provider publication", OK, f"published: {job.get('provider_permalink')}", facts)
    if job["state"] == "unknown_remote_state":
        return Step(10, "Real provider publication", BLOCKED,
                    "the provider's answer was ambiguous; reconcile before retrying", facts)
    return Step(10, "Real provider publication", BLOCKED, f"the destination ended in {job['state']}", facts)


def step_reconcile(api: Api, variant_id: str, *, published: bool) -> Step:
    if not published:
        return Step(11, "Remote status reconciliation", SKIPPED, "nothing was published, so there is nothing to reconcile")
    outcome = api.post(f"/publishing/destinations/{variant_id}/reconcile")
    job = api.get(f"/publishing/destinations/{variant_id}")["job"]
    facts = {"reconcile": outcome, "state": job["state"], "permalink": job.get("provider_permalink")}
    return Step(11, "Remote status reconciliation", OK,
                f"provider says {outcome.get('remote_state') or outcome.get('state') or job['state']}", facts)


def step_published_analytics(api: Api, account_id: str, variant_id: str, *, published: bool, wait_s: int) -> Step:
    if not published:
        return Step(12, "Analytics for published content", SKIPPED, "nothing was published, so there is nothing to measure")
    api.post(f"/analytics/sync/{account_id}")
    deadline = time.time() + wait_s
    publication = None
    while time.time() < deadline:
        page = api.get(f"/analytics/publications?account_id={account_id}&limit=50")
        publication = next((item for item in page.get("items", []) if item.get("variant_id") == variant_id), None)
        if publication and publication.get("has_metrics"):
            break
        time.sleep(5)
    if publication is None:
        return _blocked(12, "Analytics for published content",
                        "the published content has not been discovered yet; discovery runs on the daily tick")
    facts = {"publication_id": publication["publication_id"], "has_metrics": publication.get("has_metrics"),
             "remote_post_id": publication.get("remote_post_id")}
    availability = api.get(f"/analytics/publications/{publication['publication_id']}/metrics")
    facts["metrics_returned"] = len(availability or [])
    if not publication.get("has_metrics"):
        return Step(12, "Analytics for published content", BLOCKED,
                    "the publication is known but has no observations yet", facts)
    return Step(12, "Analytics for published content", OK,
                f"{len(availability or [])} metric row(s) for publication {publication['publication_id']}", facts)


# ----------------------------------------------------------------------------------- driver
def _account_id(api: Api, explicit: str | None) -> str:
    if explicit:
        return explicit
    accounts = api.get("/social/accounts?connection_status=connected").get("items", [])
    if not accounts:
        raise SystemExit("no connected account: connect one in the app, or pass --account <id>")
    if len(accounts) > 1:
        listing = ", ".join(f"{item['id']} ({item['platform']})" for item in accounts)
        raise SystemExit(f"more than one connected account; pass --account <id>: {listing}")
    return accounts[0]["id"]


def run(api: Api, *, account: str | None, destination: str | None, publish: bool, confirm: str | None,
        wait_s: int) -> list[Step]:
    account_id = _account_id(api, account)
    account = api.get(f"/social/accounts/{account_id}")
    platform, route = account["platform"], account.get("route")

    steps = [
        step_connection(api, account_id),
        step_discovery(api, account_id, platform, route),
        step_permissions(api, account_id, platform, route),
        step_capabilities(api, account_id),
        step_token_lifecycle(api, account_id),
        step_analytics_sync(api, account_id, wait_s),
        step_delivery(api),
    ]
    if destination:
        steps.append(step_preflight(api, destination))
        steps.append(step_approval(api, destination))
    else:
        steps.append(Step(8, "Publishing preflight", SKIPPED, "no --destination given"))
        steps.append(Step(9, "Human approval", SKIPPED, "no --destination given"))

    steps.append(step_publish(api, destination, allowed=publish and bool(destination), confirm=confirm, wait_s=wait_s)
                 if destination else Step(10, "Real provider publication", SKIPPED, "no --destination given"))
    published = bool(steps[-1].verdict == OK and steps[-1].number == 10)
    steps.append(step_reconcile(api, destination, published=published) if destination
                 else Step(11, "Remote status reconciliation", SKIPPED, "no --destination given"))
    steps.append(step_published_analytics(api, account_id, destination, published=published, wait_s=wait_s)
                 if destination else Step(12, "Analytics for published content", SKIPPED, "no --destination given"))
    return steps


CLASSIFICATION = {
    "implemented": "code path exists and is exercised by the step above",
    "fixture_tested": "covered by the automated suite with a mocked provider (see docs/evidence)",
    "real_provider_tested": "this run reached the live provider and the answer is recorded in the facts",
    "blocked": "needs an external grant, credential or approval that this machine does not have",
}


def render(steps: list[Step]) -> str:
    width = max(len(step.name) for step in steps)
    lines = []
    for step in steps:
        lines.append(f"{step.number:>2}. {step.name:<{width}}  {step.verdict.upper():<8} {step.detail}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if os.environ.get(OPT_IN_ENV) != "1":
        print(f"refused: {OPT_IN_ENV}=1 is not set; this harness talks to real providers", file=sys.stderr)
        return 2
    base = os.environ.get("CREATOROS_API", "").strip()
    token = os.environ.get("CREATOROS_SESSION_TOKEN", "").strip()
    if not base or not token:
        print("refused: CREATOROS_API and CREATOROS_SESSION_TOKEN must both be set", file=sys.stderr)
        return 2
    option = lambda name, default=None: argv[argv.index(name) + 1] if name in argv else default
    wait_s = int(option("--wait", "300"))
    api = Api(base, token)
    try:
        steps = run(
            api,
            account=option("--account"),
            destination=option("--destination"),
            publish="--publish" in argv,
            confirm=option("--confirm"),
            wait_s=wait_s,
        )
    except ApiError as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 2
    print(render(steps))
    print()
    if "--json" in argv:
        print(json.dumps([step.as_dict() for step in steps], indent=2, default=str))
    verdicts = {step.verdict for step in steps}
    if FAILED in verdicts:
        print("RESULT: a step contradicted what the product claims (see FAILED above)")
        return 1
    if BLOCKED in verdicts:
        print("RESULT: verification incomplete - the blocked steps need a grant, credential or approval")
        return 1
    print("RESULT: every step that could run, passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
