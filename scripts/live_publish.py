"""Opt-in live publication harness.

This script exists so a person with real provider credentials can verify one publication end to end.
It is **not** part of the test suite and it never runs by itself: it refuses unless every one of these
is true, and it says which one is missing:

1. ``CREATOROS_LIVE_PUBLISH=1`` is set - an explicit, per-shell opt-in;
2. ``--variant <id>`` names a destination that already exists, is approved, and whose platform
   ``/version`` reports as ``available``;
3. ``--confirm <text>`` matches the exact phrase the script prints, including the caption hash, so a
   copy-pasted command from a log cannot fire by accident;
4. the destination is not already published.

What it does: asks the API to queue the destination (the same route the GUI uses) and then polls the
destination until it reaches a terminal state, printing the attempt ledger as it goes. It never
touches the provider directly and never bypasses approval.

Usage:
    set CREATOROS_LIVE_PUBLISH=1
    set CREATOROS_API=http://127.0.0.1:5000
    set CREATOROS_SESSION_TOKEN=<the token the shell minted>
    python scripts/live_publish.py --variant 01M8... --confirm "PUBLISH 01M8... a1b2c3d4"
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

CONFIRM_PREFIX = "PUBLISH"
OPT_IN_ENV = "CREATOROS_LIVE_PUBLISH"
TERMINAL = {"published", "failed", "cancelled", "blocked", "requires_user_action", "unknown_remote_state"}


def fail(message: str) -> int:
    print(f"refused: {message}", file=sys.stderr)
    return 2


def api_base() -> str:
    base = os.environ.get("CREATOROS_API", "").strip().rstrip("/")
    if not base:
        raise SystemExit("CREATOROS_API is not set (for example http://127.0.0.1:5000)")
    return base


def token() -> str:
    value = os.environ.get("CREATOROS_SESSION_TOKEN", "").strip()
    if not value:
        raise SystemExit("CREATOROS_SESSION_TOKEN is not set; the desktop shell prints it at boot")
    return value


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{api_base()}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode() or "{}"
            return json.loads(body)
    except urllib.error.HTTPError as error:
        detail = error.read().decode() or "{}"
        raise SystemExit(f"HTTP {error.code} on {method} {path}: {detail}") from error


def destination(variant_id: str) -> dict:
    return call("GET", f"/publishing/destinations/{variant_id}")


def main(argv: list[str]) -> int:
    if os.environ.get(OPT_IN_ENV) != "1":
        return fail(f"{OPT_IN_ENV}=1 is not set; this script publishes to a real account")
    if "--variant" not in argv:
        return fail("--variant <destination id> is required")
    variant_id = argv[argv.index("--variant") + 1]
    confirm_expected = None
    if "--confirm" in argv:
        confirm_expected = argv[argv.index("--confirm") + 1]

    detail = destination(variant_id)
    job = detail["job"]
    platform = job["platform"]
    state = job["state"]

    capabilities = call("GET", "/version")["capabilities"]
    platform_state = capabilities.get(f"publish_{platform}")
    if platform_state != "available":
        return fail(f"publishing to {platform} is {platform_state}, not available")
    if state == "published":
        return fail("this destination is already published; republishing is a new intent")
    if state not in {"approved", "scheduled", "ready", "requires_user_action", "failed"}:
        return fail(f"a destination in {state} cannot be published right now")

    caption = job["caption"] or ""
    phrase = f"{CONFIRM_PREFIX} {variant_id} {job['approval']['payload_hash'] or 'unapproved'}"
    print("about to publish:")
    print(f"  destination : {variant_id}")
    print(f"  platform    : {platform} / {job['mode']}")
    print(f"  account     : {job['account_name']}")
    print(f"  caption     : {caption[:80]!r}{'…' if len(caption) > 80 else ''}")
    print(f"  approval    : {'approved' if job['approval']['approved'] else 'not approved'}")
    print(f"  hash        : {job['approval']['payload_hash']}")
    print("this post becomes public on the provider.")

    if confirm_expected != phrase:
        print()
        print("to continue, re-run with:")
        print(f'  --confirm "{phrase}"')
        return fail("the confirmation phrase did not match")

    outcome = call("POST", f"/publishing/destinations/{variant_id}/publish-now", {})
    print(f"queued: {outcome}")
    if not outcome.get("queued"):
        return fail(f"the API did not queue it: {outcome.get('reason')} ({outcome.get('detail')})")

    deadline = time.time() + 900
    last_state = None
    while time.time() < deadline:
        current = destination(variant_id)["job"]
        if current["state"] != last_state:
            print(f"  state -> {current['state']} ({current['state_reason']})")
            last_state = current["state"]
        if current["state"] in TERMINAL:
            break
        time.sleep(5)

    final = destination(variant_id)["job"]
    print()
    print(f"final state : {final['state']} ({final['state_reason']})")
    print(f"permalink   : {final['provider_permalink']}")
    print(f"attempts    : {len(final['attempts_log'])}")
    for attempt in final["attempts_log"]:
        print(f"  #{attempt['attempt_no']} {attempt['outcome']} {attempt['error_category'] or ''}")
    if final["state"] == "published":
        print("RESULT: published and confirmed by the provider")
        return 0
    if final["state"] == "unknown_remote_state":
        print("RESULT: unknown remote state - run reconciliation before retrying")
        return 1
    print("RESULT: not published")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
