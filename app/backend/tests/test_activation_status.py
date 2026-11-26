"""The activation reporter is only useful if it distinguishes 'not yet' from 'wrong'.

These tests drive `scripts/activation_status.py` against the same database and the same in-memory vault
double the social tests use. They assert the three states the mission cares about: a fresh install is
reported as ready-to-activate (not broken), a misconfigured app is reported as invalid with the exact
reason, and a secret the vault cannot read is never reported as present.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "activation_status.py"


@pytest.fixture()
def reporter():
    spec = importlib.util.spec_from_file_location("activation_status", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolve their module through sys.modules, so it has to be registered first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _register_app(
    session,
    vault,
    *,
    scopes=None,
    redirect_uri="http://127.0.0.1:47821/oauth/callback/facebook_login",
    secret=True,
    seal=False,
):
    """Register a Facebook app the way `PUT /social/provider-apps/...` does: secret recorded, then sealed."""
    import asyncio
    import hashlib

    from creatoros.core.ids import new_ulid
    from creatoros.db.enums import OAuthRoute, Platform, SecretStatus
    from creatoros.db.models import ProviderAppConfig, SecretRef

    ref_id = None
    if secret:
        ref_id = new_ulid()
        session.add(
            SecretRef(
                id=ref_id,
                provider=Platform.FACEBOOK.value,
                key_alias=f"provider_app:{Platform.FACEBOOK.value}"[:160],
                fingerprint=hashlib.sha256(b"app-secret").hexdigest()[:64],
                cipher="dpapi",
                dpapi_scope="user",
                purpose="provider_app_secret",
                schema_version=1,
                status=SecretStatus.ACTIVE,
                byte_size=10,
            )
        )
        session.flush()
        if seal:
            asyncio.run(
                vault.put(secret_id=ref_id, value="app-secret", purpose="provider_app_secret")
            )
    session.add(
        ProviderAppConfig(
            provider=Platform.FACEBOOK,
            route=OAuthRoute.FACEBOOK_LOGIN,
            client_id="1234567890",
            client_secret_ref_id=ref_id,
            redirect_uri=redirect_uri,
            scopes_json=scopes
            if scopes is not None
            else ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
            api_version="v23.0",
            app_mode="development",
        )
    )
    session.commit()


def _route(report, platform="facebook"):
    return next(route for route in report["routes"] if route["platform"] == platform)


def _check(route, key):
    return next(check for check in route["checks"] if check["key"] == key)


def test_fresh_install_is_ready_to_activate_not_broken(reporter, session, vault):
    report = reporter.build_report()

    facebook = _route(report)
    assert facebook["status"] == "missing", "no app registered is a gap, and it is named as one"
    assert "developers.facebook.com" in facebook["next_action"]
    assert reporter.worst_status(report) == "missing"
    assert reporter.exit_code(report) == 0, "a fresh install is coherent, not failing"
    assert reporter.exit_code(report, require_ready=True) == 1

    # The delivery bucket and analytics are reported separately, with their own next action.
    assert report["delivery"]["status"] == "missing"
    assert "Media delivery" in report["delivery"]["action"]
    assert report["analytics"]["status"] == "ready"
    assert report["analytics"]["observations"] == 0


def test_the_report_states_the_scopes_a_registration_will_need(reporter, session, vault):
    route = _route(reporter.build_report())
    scopes = _check(route, "scopes")
    assert scopes["status"] == "ready"
    assert "pages_manage_posts" in scopes["detail"]


def test_a_registered_app_with_a_loopback_redirect_is_accepted(reporter, session, vault):
    _register_app(session, vault)
    route = _route(reporter.build_report())
    assert _check(route, "provider_app")["status"] == "ok"
    assert _check(route, "redirect_uri")["status"] == "ok"
    assert _check(route, "scopes")["status"] == "ok"


@pytest.mark.parametrize(
    ("redirect_uri", "expected_fragment"),
    [
        ("https://example.com/callback", "must use http on the loopback interface"),
        ("http://localhost:47821/oauth/callback/facebook_login", "use 127.0.0.1 rather than localhost"),
        ("http://127.0.0.1/callback", "explicit loopback port"),
    ],
)
def test_a_redirect_a_provider_will_reject_is_reported_invalid(reporter, session, vault, redirect_uri, expected_fragment):
    _register_app(session, vault, redirect_uri=redirect_uri)
    route = _route(reporter.build_report())
    check = _check(route, "redirect_uri")
    assert check["status"] == "invalid"
    assert expected_fragment in check["detail"]
    assert route["status"] == "invalid"
    assert check["action"] == "Social accounts -> App settings -> Redirect URI"


def test_a_registration_missing_a_required_scope_is_invalid_and_names_it(reporter, session, vault):
    _register_app(session, vault, scopes=["pages_show_list"])
    route = _route(reporter.build_report())
    check = _check(route, "scopes")
    assert check["status"] == "invalid"
    assert "pages_read_engagement" in check["detail"]
    assert "pages_manage_posts" in check["detail"]
    assert reporter.worst_status(reporter.build_report()) == "invalid"
    assert reporter.exit_code(reporter.build_report()) == 1


def test_optional_scopes_are_reported_without_failing_the_route(reporter, session, vault):
    _register_app(session, vault, scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"], seal=True)
    route = _route(reporter.build_report())
    check = _check(route, "scopes")
    assert check["status"] == "ok"
    assert "read_insights" in check["detail"], "the optional analytics scope is stated, not silently dropped"
    assert route["status"] == "missing", "still missing an account, but not invalid"


def test_an_app_without_a_secret_is_missing_one_and_says_where_it_goes(reporter, session, vault):
    _register_app(session, vault, secret=False)
    route = _route(reporter.build_report())
    check = _check(route, "client_secret")
    assert check["status"] == "missing"
    assert "DPAPI vault" in check["action"]


def test_a_secret_the_vault_cannot_decrypt_is_invalid_not_present(reporter, session, vault):
    _register_app(session, vault)
    route = _route(reporter.build_report())
    check = _check(route, "client_secret")
    assert check["status"] == "invalid", "a recorded reference the vault cannot open is not a stored secret"
    assert "cannot read" in check["detail"]


def test_a_sealed_secret_is_reported_as_readable(reporter, session, vault):
    _register_app(session, vault, seal=True)
    route = _route(reporter.build_report())
    assert _check(route, "client_secret")["status"] == "ok"


def test_a_connected_account_reports_token_and_capabilities(reporter, session, vault):
    from creatoros.db.enums import (
        AccountType,
        CapabilityState,
        ConnectionStatus,
        OAuthRoute,
        Platform,
        TokenState,
    )
    from creatoros.db.models import ProviderAppConfig, SocialAccount

    _register_app(session, vault)
    session.add(
        SocialAccount(
            platform=Platform.FACEBOOK,
            route=OAuthRoute.FACEBOOK_LOGIN,
            account_type=AccountType.FACEBOOK_PAGE,
            provider_account_id="page-1",
            display_name="Page One",
            connection_status=ConnectionStatus.CONNECTED,
            token_state=TokenState.VALID,
            token_ref_id=session.query(ProviderAppConfig).one().client_secret_ref_id,
            scopes_json=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
            capabilities_json={
                "publish_video": CapabilityState.AVAILABLE.value,
                "read_insights": CapabilityState.NEEDS_APP_REVIEW.value,
            },
        )
    )
    session.commit()

    route = _route(reporter.build_report())
    assert _check(route, "account")["status"] == "ok"
    assert _check(route, "token")["status"] == "ok"
    capabilities = _check(route, "capabilities")
    assert capabilities["status"] == "ok"
    assert "1 available" in capabilities["detail"]
    assert "needs_app_review" in capabilities["detail"], "a limited capability is shown, not hidden"


def test_probe_without_an_account_says_so_instead_of_passing(reporter, session, vault):
    route = _route(reporter.build_report(probe=True))
    check = _check(route, "provider_probe")
    assert check["status"] == "unknown"
    assert "not run" in check["detail"]


def test_json_output_is_valid_and_secret_free(reporter, session, vault, capsys):
    _register_app(session, vault)
    assert reporter.main(["--json"]) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["routes"] and payload["delivery"] and payload["analytics"]
    text = json.dumps(payload)
    assert "app-secret" not in text
    assert "client_secret" not in text.replace('"client_secret"', ""), "no secret value is ever printed"


def test_render_prints_one_line_per_check_and_the_next_action(reporter, session, vault):
    rendered = reporter.render(reporter.build_report())
    assert "Facebook Login (Pages)" in rendered
    assert "Overall:" in rendered
    assert "next:" in rendered
    for line in rendered.splitlines():
        assert "\x00" not in line
