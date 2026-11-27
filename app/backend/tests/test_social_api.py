"""Social API tests: the authenticated surface, its envelopes, and what it refuses to return.

The OAuth round trip is exercised end to end here: start over HTTP, deliver the callback through the
engine (the loopback listener is covered separately), then read the account through the API.
"""
from __future__ import annotations

import pytest
from social_fixtures import (
    assert_no_secret_in,
    facebook_account_graph,
    make_app_config,
    run,
)

from creatoros.db.enums import OAuthRoute, Platform, TokenState
from creatoros.integrations.social.base import AdapterRegistry
from creatoros.services.social.oauth_engine import OAuthEngine

SECRET = "super-secret-app-secret"


@pytest.fixture()
def social_app(session, vault, monkeypatch):
    """A registered Facebook app whose secret is in the vault double."""
    config = run(
        make_app_config(
            session,
            vault,
            platform=Platform.FACEBOOK,
            route=OAuthRoute.FACEBOOK_LOGIN,
            client_secret=SECRET,
            app_mode="development",
        )
    )
    session.commit()
    return config


def test_social_routes_require_the_session_token(unauth_client):
    for path in (
        "/social/overview",
        "/social/accounts",
        "/social/oauth/routes",
        "/social/vault/health",
        "/social/provider-apps",
        "/social/capabilities/matrix",
        "/social/rotations",
        "/social/callback",
        "/social/oauth/transactions",
    ):
        response = unauth_client.get(path)
        assert response.status_code == 401, path
        assert response.json()["code"] == "AUTH_REQUIRED"


def test_overview_reports_vault_listener_and_phase7_readiness(client, social_app):
    body = client.get("/social/overview").json()
    assert body["accounts"]["total"] == 0
    assert body["vault"]["available"] is True
    assert body["vault"]["protection"]
    assert len(body["providers"]) == 4
    assert body["phase7"]["accounts_connected"] == 0
    assert body["phase7"]["blockers"]


def test_vault_health_accepts_the_fields_the_real_desktop_vault_reports(client, vault):
    """The desktop vault adds `directory`; a strict schema must allow it without a 500."""

    async def health_with_directory() -> dict:
        return {
            "available": True,
            "backend": "dpapi",
            "schema_version": 1,
            "alg": "dpapi-safeStorage",
            "directory": r"<DATA_ROOT>\config\secrets",
            "entries": 0,
            "corrupt": 0,
            "protection": "Windows DPAPI, current user.",
        }

    vault.health = health_with_directory  # type: ignore[method-assign]
    response = client.get("/social/vault/health")
    assert response.status_code == 200, response.text
    assert response.json()["directory"].endswith("secrets")


def test_provider_app_registration_never_returns_the_secret(client, session, vault):
    response = client.put(
        "/social/provider-apps/facebook/facebook_login",
        json={
            "client_id": "app-123",
            "client_secret": SECRET,
            "app_mode": "development",
            "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_client_secret"] is True
    assert body["client_id_suffix"] == "-123"
    assert_no_secret_in(body, [SECRET])


def test_provider_app_rejects_a_non_loopback_redirect(client, social_app):
    response = client.put(
        "/social/provider-apps/facebook/facebook_login",
        json={"client_id": "app-123", "redirect_uri": "https://evil.example/callback", "app_mode": "development"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "REDIRECT_INVALID"


def test_provider_app_rejects_unknown_scopes(client, social_app):
    response = client.put(
        "/social/provider-apps/facebook/facebook_login",
        json={"client_id": "app-123", "scopes": ["ads_management"], "app_mode": "development"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "SCOPE_UNKNOWN"


def test_provider_app_verification_states_what_it_cannot_check(client, social_app):
    body = client.post("/social/provider-apps/facebook/facebook_login/verify").json()
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["redirect_uri"]["ok"] is True
    assert checks["scopes"]["ok"] is True
    assert checks["client_secret"]["ok"] is True
    # The provider reachability check is explicitly not claimed.
    assert checks["provider_reachability"]["ok"] is None
    assert "not tested" in checks["provider_reachability"]["detail"]


def test_oauth_start_returns_a_url_and_no_secret(client, social_app, oauth_listener):
    response = client.post("/social/oauth/start", json={"platform": "facebook", "route": "facebook_login"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization_url"].startswith("https://www.facebook.com/")
    assert body["session_id"]
    assert body["redirect_uri"] == social_app.redirect_uri
    assert_no_secret_in(body, [SECRET])

    transaction = client.get(f"/social/oauth/transactions/{body['session_id']}").json()
    assert transaction["status"] == "pending"
    # The state is in the URL the browser will hit; it is never echoed back by the API.
    assert "state" not in transaction


def test_oauth_start_refuses_an_unregistered_platform(client):
    response = client.post("/social/oauth/start", json={"platform": "tiktok", "route": "tiktok_login"})
    assert response.status_code == 409
    assert response.json()["code"] == "APP_NOT_CONFIGURED"


def test_oauth_start_validates_the_body(client, social_app):
    response = client.post("/social/oauth/start", json={"platform": "myspace"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    response = client.post("/social/oauth/start", json={"platform": "facebook", "surprise": 1})
    assert response.status_code == 422


def test_full_connect_flow_over_the_api(client, session, vault, social_app, oauth_listener):
    started = client.post("/social/oauth/start", json={"platform": "facebook"}).json()
    state = _state_from(started["authorization_url"])

    mock = facebook_account_graph()
    engine = OAuthEngine(session, vault, adapter_factory=lambda: AdapterRegistry(
        transport_factory=lambda provider: mock.transport(provider)
    ))
    outcome = run(engine.handle_callback("facebook_login", {"code": "auth-code", "state": state}))
    session.commit()
    assert outcome["ok"] is True, outcome

    accounts = client.get("/social/accounts").json()
    assert accounts["total"] == 1
    account = accounts["items"][0]
    assert account["platform"] == "facebook"
    assert account["connection_status"] == "connected"
    assert account["capabilities"]["facebook.page.publish_video"] == "available"
    assert_no_secret_in(accounts, [SECRET, "EAAB-page-token", "EAAB-user-token"])

    detail = client.get(f"/social/accounts/{account['id']}").json()
    assert detail["secret_health"]
    assert all("value" not in entry for entry in detail["secret_health"])
    assert detail["token"]["state"] == "valid"

    capabilities = client.get(f"/social/accounts/{account['id']}/capabilities").json()
    assert capabilities["overall_state"] == "available"
    assert capabilities["snapshots"][0]["trigger"] == "oauth_connect"

    token = client.get(f"/social/accounts/{account['id']}/token").json()
    assert token["state"] == "valid"
    assert token["has_refresh_token"] is False

    transaction = client.get(f"/social/oauth/transactions/{started['session_id']}").json()
    assert transaction["status"] == "succeeded"
    assert transaction["accounts"][0]["id"] == account["id"]


def test_capability_refresh_is_offline_and_idempotent(client, session, vault, social_app, oauth_listener):
    started = client.post("/social/oauth/start", json={"platform": "facebook"}).json()
    mock = facebook_account_graph()
    engine = OAuthEngine(session, vault, adapter_factory=lambda: AdapterRegistry(
        transport_factory=lambda provider: mock.transport(provider)
    ))
    run(engine.handle_callback("facebook_login", {"code": "c", "state": _state_from(started["authorization_url"])}))
    session.commit()
    account_id = client.get("/social/accounts").json()["items"][0]["id"]

    first = client.post(f"/social/accounts/{account_id}/capabilities/refresh").json()
    second = client.post(f"/social/accounts/{account_id}/capabilities/refresh").json()
    assert {item["capability"]: item["state"] for item in first["capabilities"]} == {
        item["capability"]: item["state"] for item in second["capabilities"]
    }
    assert len(second["snapshots"]) >= 2


def test_label_update_and_validation(client, session, vault, social_app, oauth_listener):
    started = client.post("/social/oauth/start", json={"platform": "facebook"}).json()
    mock = facebook_account_graph()
    engine = OAuthEngine(session, vault, adapter_factory=lambda: AdapterRegistry(
        transport_factory=lambda provider: mock.transport(provider)
    ))
    run(engine.handle_callback("facebook_login", {"code": "c", "state": _state_from(started["authorization_url"])}))
    session.commit()
    account_id = client.get("/social/accounts").json()["items"][0]["id"]

    updated = client.patch(f"/social/accounts/{account_id}", json={"label": "Client A", "group": "Retainer"})
    assert updated.status_code == 200
    assert updated.json()["label"] == "Client A"
    assert updated.json()["group"] == "Retainer"

    too_long = client.patch(f"/social/accounts/{account_id}", json={"label": "x" * 201})
    assert too_long.status_code == 422


def test_disconnect_then_delete_requires_a_disconnect_first(client, session, vault, social_app, oauth_listener):
    started = client.post("/social/oauth/start", json={"platform": "facebook"}).json()
    mock = facebook_account_graph()
    engine = OAuthEngine(session, vault, adapter_factory=lambda: AdapterRegistry(
        transport_factory=lambda provider: mock.transport(provider)
    ))
    run(engine.handle_callback("facebook_login", {"code": "c", "state": _state_from(started["authorization_url"])}))
    session.commit()
    account_id = client.get("/social/accounts").json()["items"][0]["id"]

    blocked = client.delete(f"/social/accounts/{account_id}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "ACCOUNT_STILL_CONNECTED"

    disconnected = client.post(f"/social/accounts/{account_id}/disconnect", json={"revoke_at_provider": False})
    assert disconnected.status_code == 200
    assert disconnected.json()["connection_status"] == "disconnected"

    deleted = client.delete(f"/social/accounts/{account_id}")
    assert deleted.status_code == 204
    assert client.get("/social/accounts").json()["total"] == 0


def test_vault_secret_reservation_requires_a_vault(client):
    from creatoros.api.routes.social import set_vault
    from creatoros.services.social.vault import UnavailableVault

    set_vault(UnavailableVault("test"))
    try:
        response = client.post("/social/vault/secrets/reserve")
    finally:
        set_vault(None)
    assert response.status_code == 503
    assert response.json()["code"] == "VAULT_UNAVAILABLE"


def test_vault_secret_reservation_returns_an_id_only(client, vault):
    body = client.post("/social/vault/secrets/reserve").json()
    assert len(body["secret_id"]) == 26
    assert body["purpose"] == "provider_app_secret"
    assert set(body) == {"secret_id", "purpose", "max_bytes", "purpose_options"}
    assert "value" not in body and "secret" not in body


def test_deleting_a_secret_in_use_is_refused(client, session, vault, social_app, oauth_listener):
    started = client.post("/social/oauth/start", json={"platform": "facebook"}).json()
    mock = facebook_account_graph()
    engine = OAuthEngine(session, vault, adapter_factory=lambda: AdapterRegistry(
        transport_factory=lambda provider: mock.transport(provider)
    ))
    run(engine.handle_callback("facebook_login", {"code": "c", "state": _state_from(started["authorization_url"])}))
    session.commit()
    from creatoros.db.models.platform import SocialAccount

    account = session.query(SocialAccount).one()
    response = client.delete(f"/social/vault/secrets/{account.token_ref_id}")
    assert response.status_code == 409
    assert response.json()["code"] == "SECRET_IN_USE"


def test_rotations_endpoint_lists_and_resolves(client, session, vault, social_app):
    from datetime import timedelta

    from creatoros.db.base import utcnow
    from creatoros.db.enums import AccountType, ConnectionStatus, RotationState
    from creatoros.db.models.platform import CredentialRotation, SocialAccount

    account = SocialAccount(
        platform=Platform.FACEBOOK,
        provider_account_id="p1",
        display_name="Page",
        account_type=AccountType.FACEBOOK_PAGE,
        route=OAuthRoute.FACEBOOK_LOGIN,
        connection_status=ConnectionStatus.CONNECTED,
        token_state=TokenState.VALID,
        token_ref_id=None,
    )
    session.add(account)
    session.flush()
    session.add(
        CredentialRotation(
            account_id=account.id,
            provider=Platform.FACEBOOK,
            state=RotationState.IN_PROGRESS,
            lock_token="x",
            started_at=utcnow() - timedelta(minutes=1),
        )
    )
    session.commit()

    listing = client.get("/social/rotations").json()
    assert listing["pending"] == 1
    assert listing["items"][0]["state"] == "in_progress"

    resolved = client.post(f"/social/rotations/{account.id}/resolve").json()
    assert resolved["resolved"]


def test_capability_matrix_exposes_the_catalogue(client):
    body = client.get("/social/capabilities/matrix").json()
    keys = {entry["key"] for entry in body["catalogue"]}
    assert "instagram.publish_reels" in keys
    assert "tiktok.direct_post" in keys
    assert "manual.export_package" in keys
    assert body["accounts"] == []


def test_oauth_routes_endpoint_reports_configuration_state(client, social_app):
    body = client.get("/social/oauth/routes").json()
    facebook = next(item for item in body["items"] if item["platform"] == "facebook")
    assert facebook["configured"] is True
    assert facebook["pkce_required"] is False
    tiktok = next(item for item in body["items"] if item["platform"] == "tiktok")
    assert tiktok["configured"] is False
    assert tiktok["pkce_required"] is True


def test_diagnostics_for_an_account_without_a_credential(client, session, vault, social_app):
    from creatoros.db.enums import AccountType, ConnectionStatus
    from creatoros.db.models.platform import SocialAccount

    account = SocialAccount(
        platform=Platform.FACEBOOK,
        provider_account_id="p9",
        display_name="Page 9",
        account_type=AccountType.FACEBOOK_PAGE,
        route=OAuthRoute.FACEBOOK_LOGIN,
        connection_status=ConnectionStatus.UNKNOWN,
    )
    session.add(account)
    session.commit()
    body = client.get(f"/social/accounts/{account.id}/diagnostics").json()
    assert body["local"]["token_state"]["state"] == "credential_unavailable"
    assert body["provider_report"]["checks"][0]["ok"] is False


def test_unknown_account_is_a_clean_404(client):
    for path in (
        "/social/accounts/nope",
        "/social/accounts/nope/capabilities",
        "/social/accounts/nope/token",
        "/social/accounts/nope/diagnostics",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["code"] == "NOT_FOUND"


def test_instagram_and_tiktok_routes_are_exposed(client, session, vault):
    run(
        make_app_config(
            session,
            vault,
            platform=Platform.INSTAGRAM,
            route=OAuthRoute.INSTAGRAM_LOGIN,
            client_id="ig-app",
            redirect_uri="http://127.0.0.1:47821/oauth/callback/instagram_login",
        )
    )
    session.commit()
    body = client.get("/social/oauth/routes").json()
    instagram = next(item for item in body["items"] if item["route"] == "instagram_login")
    assert instagram["configured"] is True
    assert "instagram_business_basic" in instagram["scopes"]


def test_callback_status_endpoint_reports_a_stopped_listener(client):
    body = client.get("/social/callback").json()
    assert body["running"] is False
    assert "hint" in body


def _state_from(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["state"][0]
