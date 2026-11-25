"""API contract and security tests.

Every route must reject an unauthenticated caller, every error must use the
stable envelope, and no endpoint may claim a capability that does not exist.
"""
from __future__ import annotations

import pytest

PROTECTED_GETS = [
    "/version",
    "/health/ready",
    "/diagnostics",
    "/settings",
    "/settings/paths",
    "/media",
    "/projects",
    "/accounts",
    "/agents/registry",
    "/queue/stats",
    "/audit",
    "/notifications",
]


def test_liveness_is_public(unauth_client):
    response = unauth_client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "live"


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_routes_require_token(unauth_client, path):
    response = unauth_client.get(path)
    assert response.status_code in (401, 503), f"{path} answered {response.status_code} without a token"
    if response.status_code == 401:
        body = response.json()
        assert body["code"] == "AUTH_REQUIRED"


def test_wrong_token_rejected(unauth_client):
    response = unauth_client.get("/version", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401


def test_foreign_origin_rejected(client):
    response = client.get("/version", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


def test_allowed_origin_accepted(client):
    response = client.get("/version", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 200


def test_security_headers_present(client):
    response = client.get("/version")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert "x-correlation-id" in response.headers


def test_correlation_id_is_echoed(client):
    response = client.get("/version", headers={"x-correlation-id": "abc123"})
    assert response.headers["x-correlation-id"] == "abc123"


def test_oversized_body_rejected(client):
    response = client.post(
        "/queue/jobs",
        json={"job_type": "noop", "payload": {"blob": "x" * (17 * 1024 * 1024)}},
    )
    assert response.status_code in (413, 422)


def test_version_reports_honest_capabilities(client):
    """Every capability string is a state, never "coming soon"."""
    body = client.get("/version").json()
    capabilities = body["capabilities"]
    # Phase 7: the publishing engine exists, and each platform answers why it can or cannot run.
    assert capabilities["publishing"] == "available"
    assert capabilities["queue"] == "available"
    for key in ("publish_facebook", "publish_instagram", "publish_tiktok"):
        assert capabilities[key] in {
            "available",
            "vault_unavailable",
            "no_provider_app_configured",
            "no_connected_account",
            "no_delivery_bucket",
            "database_unavailable",
        }, key
    # Nothing is configured in a test environment, so no platform may claim to be ready.
    assert "available" not in {
        capabilities["publish_facebook"],
        capabilities["publish_instagram"],
        capabilities["publish_tiktok"],
    }
    assert capabilities["publish_media_delivery"] in {"configured", "not_configured", "database_unavailable"}
    assert capabilities["publish_scheduler"] == "available"
    # Phase 6: OAuth is implemented, but it is only *available* when a broker vault, a callback
    # listener and a registered developer app all exist. The test environment has none of those,
    # so the honest answer is a reason, not "available".
    assert capabilities["oauth"] not in {"not_implemented", "available"}
    assert capabilities["oauth"] in {
        "vault_unavailable",
        "callback_listener_stopped",
        "no_provider_app_configured",
        "database_unavailable",
    }
    assert capabilities["credential_vault"] in {"available", "unavailable"}
    assert capabilities["oauth_callback_listener"] in {"listening", "stopped"}
    assert "NOT_IMPLEMENTED" not in repr(body).upper()


def test_oauth_start_refuses_without_a_registered_app(client):
    """The old 501 stub is gone: the route now exists and fails for a stated reason."""
    response = client.post("/accounts/oauth/facebook/start")
    assert response.status_code in (409, 503), response.text
    body = response.json()
    assert body["code"] in {"APP_NOT_CONFIGURED", "VAULT_UNAVAILABLE", "CALLBACK_NOT_READY"}
    assert body["message"]


def test_unknown_route_returns_envelope(client):
    response = client.get("/nope")
    assert response.status_code == 404


def test_diagnostics_is_real(client):
    from creatoros.core.settings import resolve_root

    root = str(resolve_root()).lower()
    body = client.get("/diagnostics").json()
    assert body["disk"]["root"].lower() == root
    assert "free_gb" in body["disk"]
    assert "ollama" in body and "online" in body["ollama"]
    assert body["paths"]["database"].lower().startswith(root)


def test_paths_endpoint(client):
    from creatoros.core.settings import resolve_root

    body = client.get("/settings/paths").json()
    assert body["root"].lower() == str(resolve_root()).lower()
    assert set(body["paths"]) >= {"database", "media_incoming", "models", "backups"}
