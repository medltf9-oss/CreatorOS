"""Facebook Pages adapter tests.

Everything is driven through `MockTransport`, so the request shape (endpoint, parameters,
appsecret_proof, long-lived exchange) is asserted exactly as the Graph API documents it.
"""
from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

import pytest
from social_fixtures import facebook_account_graph, run

from creatoros.db.enums import AccountType, OAuthRoute, Platform, TokenState
from creatoros.integrations.social import facebook as facebook_module
from creatoros.integrations.social.base import ProviderCredentials
from creatoros.integrations.social.errors import SocialError
from creatoros.integrations.social.facebook import FacebookAdapter


def credentials(**overrides) -> ProviderCredentials:
    base = {
        "provider": Platform.FACEBOOK,
        "route": OAuthRoute.FACEBOOK_LOGIN,
        "client_id": "app-123",
        "client_secret": "app-secret",
        "redirect_uri": "http://127.0.0.1:47821/oauth/callback/facebook_login",
        "api_version": "v23.0",
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        "app_mode": "development",
    }
    base.update(overrides)
    return ProviderCredentials(**base)


def adapter_for(mock) -> FacebookAdapter:
    return FacebookAdapter(transport=mock.transport("facebook"))


def test_authorization_url_carries_state_redirect_and_scopes():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    url = adapter.authorization_url(credentials(), state="s" * 43)
    assert url.startswith("https://www.facebook.com/v23.0/dialog/oauth?")
    assert "state=" + "s" * 43 in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A47821%2Foauth%2Fcallback%2Ffacebook_login" in url
    assert "pages_manage_posts" in url
    assert "response_type=code" in url


def test_code_exchange_returns_a_long_lived_token():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    bundle = run(adapter.exchange_code(credentials(), code="auth-code"))
    assert bundle.access_token == "EAAB-user-token"
    assert bundle.meta["flow"] == "fb_exchange_token"
    assert bundle.meta["long_lived"] is True
    exchange = mock.call(r"/oauth/access_token", "GET")
    assert exchange.params["code"] == "auth-code"
    assert exchange.params["client_secret"] == "app-secret"


def test_short_lived_token_survives_a_failed_extension():
    mock = facebook_account_graph()
    mock.routes = [route for route in mock.routes if "oauth/access_token" not in route[0]]
    mock.json(r"grant_type=fb_exchange_token", 400, {"error": {"code": 100, "message": "invalid token"}})
    mock.json(r"/oauth/access_token", 200, {"access_token": "short-token", "expires_in": 7200})
    adapter = adapter_for(mock)
    bundle = run(adapter.exchange_code(credentials(), code="auth-code"))
    assert bundle.access_token == "short-token"
    assert bundle.meta["long_lived"] is False
    # The failure is recorded, not hidden.
    assert bundle.meta["long_lived_error"] == "PROVIDER_ERROR"


def test_missing_app_secret_is_explicit():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    with pytest.raises(SocialError) as error:
        run(adapter.exchange_code(credentials(client_secret=None), code="x"))
    assert error.value.code == "APP_NOT_CONFIGURED"


def test_debug_token_reads_scopes_and_expiry():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    validation = run(adapter.validate_token(credentials(), "EAAB-user-token"))
    assert validation.valid is True
    assert validation.scopes == ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]
    assert validation.expires_at == datetime.fromtimestamp(1900000000, tz=UTC)
    assert validation.app_id == "app-123"


def test_discovery_sends_appsecret_proof_and_maps_page_tokens():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    accounts = run(adapter.discover_accounts(credentials(), "EAAB-user-token"))
    assert len(accounts) == 1
    page = accounts[0]
    assert page.provider_account_id == "1234567890"
    assert page.account_type == AccountType.FACEBOOK_PAGE
    assert page.access_token == "EAAB-page-token"
    # Page tokens from a long-lived user token do not expire.
    assert page.expires_at is None
    assert page.tasks == ["CREATE_CONTENT", "MANAGE"]
    assert page.meta["can_post"] is True
    call = mock.call(r"/me/accounts")
    assert call.params["appsecret_proof"]
    assert call.params["fields"].startswith("id,name,category,tasks,access_token")


def test_discovery_reports_a_page_without_content_task():
    mock = facebook_account_graph(tasks=["MANAGE"])
    adapter = adapter_for(mock)
    accounts = run(adapter.discover_accounts(credentials(), "token"))
    assert accounts[0].meta["can_post"] is False


def test_profile_reads_page_facts():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    profile = run(adapter.fetch_profile(credentials(), "EAAB-page-token", "1234567890"))
    assert profile.display_name == "Test Page"
    assert profile.username == "testpage"
    assert profile.followers == 1234
    assert profile.account_type == AccountType.FACEBOOK_PAGE


def test_revocation_reports_success_and_already_invalid():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    result = run(adapter.revoke(credentials(), "token"))
    assert result.revoked is True

    mock.routes = [route for route in mock.routes if "permissions" not in route[0]]
    mock.json(r"/me/permissions", 400, {"error": {"code": 190, "message": "expired"}})
    second = run(adapter.revoke(credentials(), "token"))
    assert second.revoked is False
    assert second.already_revoked is True


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        (190, "TOKEN_EXPIRED"),
        (102, "TOKEN_EXPIRED"),
        (4, "RATE_LIMITED"),
        (17, "RATE_LIMITED"),
        (10, "SCOPE_NOT_GRANTED"),
        (200, "SCOPE_NOT_GRANTED"),
        (100, "PROVIDER_ERROR"),
    ],
)
def test_error_classification_maps_graph_codes(provider_code, expected):
    mock = facebook_account_graph()
    mock.routes = []
    mock.json(r"/me/accounts", 400, {"error": {"code": provider_code, "message": "nope"}})
    adapter = adapter_for(mock)
    with pytest.raises(SocialError) as error:
        run(adapter.discover_accounts(credentials(), "token"))
    assert error.value.code == expected


def test_http_429_is_rate_limited_even_without_a_body():
    mock = facebook_account_graph()
    mock.routes = []
    mock.on(r"/me/accounts", lambda request: __import__("httpx").Response(429, headers={"retry-after": "30"}, json={}))
    adapter = adapter_for(mock)
    with pytest.raises(SocialError) as error:
        run(adapter.discover_accounts(credentials(), "token"))
    assert error.value.code == "RATE_LIMITED"
    assert error.value.retry_after_s == 30.0
    assert error.value.transient is True


def test_diagnostics_report_what_it_could_and_could_not_check():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    report = run(adapter.diagnostics(credentials(), "EAAB-user-token"))
    assert report["provider"] == "facebook"
    assert report["personal_profiles_supported"] is False
    assert report["token"]["valid"] is True
    assert any(check["name"] == "debug_token" and check["ok"] for check in report["checks"])


def test_capabilities_do_not_claim_publishing_without_the_scope():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    verdicts = adapter.evaluate_capabilities(
        account_type=AccountType.FACEBOOK_PAGE,
        granted_scopes=["pages_read_engagement"],
        token_state=TokenState.VALID.value,
        extra={"app_mode": "live"},
    )
    by_key = {verdict.capability: verdict.state for verdict in verdicts}
    assert by_key["facebook.page.read_profile"] == "available"
    assert by_key["facebook.page.publish_video"] == "needs_user_consent"


def test_personal_profile_is_not_a_supported_account_type():
    mock = facebook_account_graph()
    adapter = adapter_for(mock)
    verdicts = adapter.evaluate_capabilities(
        account_type=AccountType.PERSONAL_UNSUPPORTED,
        granted_scopes=["pages_manage_posts"],
        token_state=TokenState.VALID.value,
    )
    assert all(verdict.state == "unsupported" for verdict in verdicts)


def test_the_adapter_contains_no_publish_or_upload_endpoint():
    """A static guarantee: Phase 6 can read, introspect and revoke - never publish."""
    source = inspect.getsource(facebook_module)
    tree = ast.parse(source)
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    forbidden = ("/feed", "/photos", "/videos", "/comments", "/messages", "subscribed_apps")
    offenders = [text for text in strings if any(token in text for token in forbidden)]
    assert offenders == [], f"publish-capable endpoint found in the Facebook adapter: {offenders}"
