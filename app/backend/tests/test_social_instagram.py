"""Instagram adapter tests: both login paths, kept separate on purpose.

The important assertions are about *not* merging: an account connected through Instagram Login and
the same human's account reached through a Facebook Page produce two rows with different ids,
different scopes and different capability sets.
"""
from __future__ import annotations

import ast
import inspect

import pytest
from social_fixtures import facebook_account_graph, instagram_login_graph, run

from creatoros.db.enums import AccountType, OAuthRoute, Platform, TokenState
from creatoros.integrations.social import instagram as instagram_module
from creatoros.integrations.social.base import ProviderCredentials
from creatoros.integrations.social.errors import SocialError
from creatoros.integrations.social.instagram import InstagramAdapter


def ig_credentials(**overrides) -> ProviderCredentials:
    base = {
        "provider": Platform.INSTAGRAM,
        "route": OAuthRoute.INSTAGRAM_LOGIN,
        "client_id": "ig-app-1",
        "client_secret": "ig-secret",
        "redirect_uri": "http://127.0.0.1:47821/oauth/callback/instagram_login",
        "api_version": "v23.0",
        "scopes": ["instagram_business_basic", "instagram_business_content_publish"],
        "app_mode": "development",
    }
    base.update(overrides)
    return ProviderCredentials(**base)


def fb_credentials(**overrides) -> ProviderCredentials:
    base = {
        "provider": Platform.INSTAGRAM,
        "route": OAuthRoute.FACEBOOK_LOGIN,
        "client_id": "app-123",
        "client_secret": "app-secret",
        "redirect_uri": "http://127.0.0.1:47821/oauth/callback/facebook_login",
        "api_version": "v23.0",
        "scopes": ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"],
        "app_mode": "development",
    }
    base.update(overrides)
    return ProviderCredentials(**base)


def login_adapter(mock) -> InstagramAdapter:
    adapter = InstagramAdapter(transport=mock.transport("instagram"))
    adapter.route = OAuthRoute.INSTAGRAM_LOGIN
    return adapter


def facebook_path_adapter(mock) -> InstagramAdapter:
    adapter = InstagramAdapter(transport=mock.transport("instagram"))
    adapter.route = OAuthRoute.FACEBOOK_LOGIN
    return adapter


# ---------------------------------------------------------------- Instagram Login


def test_instagram_login_authorization_url_uses_the_instagram_host():
    mock = instagram_login_graph()
    url = login_adapter(mock).authorization_url(ig_credentials(), state="s" * 43)
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert "instagram_business_basic" in url
    assert "response_type=code" in url


def test_instagram_login_exchange_extends_the_token():
    mock = instagram_login_graph()
    bundle = run(login_adapter(mock).exchange_code(ig_credentials(), code="code-1"))
    assert bundle.access_token == "IG-long-token"
    assert bundle.meta["long_lived"] is True
    assert bundle.provider_account_id == "17841400000000000"
    assert bundle.scopes == ["instagram_business_basic", "instagram_business_content_publish"]
    assert bundle.expires_at is not None
    exchange = mock.call(r"oauth/access_token", "POST")
    assert exchange.data["grant_type"] == "authorization_code"
    assert exchange.data["code"] == "code-1"


def test_instagram_login_without_permissions_falls_back_to_consent_scope_record():
    mock = instagram_login_graph()
    mock.routes = [
        route for route in mock.routes if not ("instagram" in route[0] and "oauth/access_token" in route[0])
    ]
    mock.json(r"api\.instagram\.com/oauth/access_token", 200, {"access_token": "IG-short", "user_id": "1784"})
    bundle = run(login_adapter(mock).exchange_code(ig_credentials(), code="code-1"))
    assert bundle.meta["permissions_returned"] is False
    assert bundle.meta["scope_source"] == "consent_request"


def test_instagram_login_discovery_maps_professional_types():
    for raw, expected in (
        ("BUSINESS", AccountType.INSTAGRAM_BUSINESS),
        ("MEDIA_CREATOR", AccountType.INSTAGRAM_CREATOR),
        ("PERSONAL", AccountType.PERSONAL_UNSUPPORTED),
    ):
        mock = instagram_login_graph(account_type=raw)
        accounts = run(login_adapter(mock).discover_accounts(ig_credentials(), "IG-long-token"))
        assert len(accounts) == 1
        assert accounts[0].account_type == expected


def test_instagram_login_validation_is_a_real_profile_read():
    mock = instagram_login_graph()
    validation = run(login_adapter(mock).validate_token(ig_credentials(), "IG-long-token"))
    assert validation.valid is True
    assert validation.raw_flags["validation"] == "profile read"
    assert validation.raw_flags["scope_source"] == "consent_request"


def test_instagram_login_refresh_uses_the_token_not_a_refresh_token():
    mock = instagram_login_graph()
    bundle = run(login_adapter(mock).refresh(ig_credentials(), "IG-long-token"))
    assert bundle.access_token == "IG-refreshed-token"
    assert bundle.refresh_token is None
    assert bundle.meta["rotation"] == "none"


def test_instagram_login_revocation_is_declared_unsupported_not_faked():
    mock = instagram_login_graph()
    result = run(login_adapter(mock).revoke(ig_credentials(), "IG-long-token"))
    assert result.revoked is False
    assert "no token revocation endpoint" in result.detail


def test_instagram_login_capabilities_reflect_scope_and_app_mode():
    mock = instagram_login_graph()
    adapter = login_adapter(mock)
    granted = ["instagram_business_basic", "instagram_business_content_publish"]
    verdicts = adapter.evaluate_capabilities(
        account_type=AccountType.INSTAGRAM_BUSINESS,
        granted_scopes=granted,
        token_state=TokenState.VALID.value,
        extra={"app_mode": "development"},
    )
    by_key = {verdict.capability: verdict.state for verdict in verdicts}
    assert by_key["instagram.read_profile"] == "available"
    assert by_key["instagram.publish_reels"] == "available"
    # Insights were not granted at all.
    assert by_key["instagram.read_insights"] == "requires_developer_approval"


def test_instagram_without_publish_scope_needs_consent():
    mock = instagram_login_graph()
    verdicts = login_adapter(mock).evaluate_capabilities(
        account_type=AccountType.INSTAGRAM_BUSINESS,
        granted_scopes=["instagram_business_basic"],
        token_state=TokenState.VALID.value,
        extra={"app_mode": "live"},
    )
    by_key = {verdict.capability: verdict.state for verdict in verdicts}
    assert by_key["instagram.publish_reels"] == "needs_user_consent"
    assert any("instagram_business_content_publish" in verdict.missing for verdict in verdicts)


# ---------------------------------------------------------------- Facebook Login path


def test_facebook_login_path_discovers_only_accounts_with_a_linked_instagram_account():
    mock = facebook_account_graph(ig_id="17841400000000001")
    accounts = run(facebook_path_adapter(mock).discover_accounts(fb_credentials(), "EAAB-user-token"))
    assert len(accounts) == 1
    ig = accounts[0]
    assert ig.provider_account_id == "17841400000000001"
    assert ig.parent_page_id == "1234567890"
    assert ig.meta["discovered_via"] == "facebook_login"
    assert ig.access_token == "EAAB-page-token"


def test_facebook_login_path_returns_nothing_when_no_page_has_an_instagram_account():
    mock = facebook_account_graph(ig_id=None)
    assert run(facebook_path_adapter(mock).discover_accounts(fb_credentials(), "token")) == []


def test_facebook_login_path_profile_reads_the_facebook_graph():
    mock = facebook_account_graph(ig_id="17841400000000001")
    profile = run(facebook_path_adapter(mock).fetch_profile(fb_credentials(), "EAAB-page-token", "17841400000000001"))
    assert profile.username == "test.page.ig"
    assert profile.followers == 555
    assert profile.meta["route"] == "facebook_login"


def test_facebook_login_path_capabilities_use_the_facebook_scope_vocabulary():
    mock = facebook_account_graph()
    verdicts = facebook_path_adapter(mock).evaluate_capabilities(
        account_type=AccountType.INSTAGRAM_BUSINESS,
        granted_scopes=["instagram_basic", "instagram_content_publish"],
        token_state=TokenState.VALID.value,
        extra={"app_mode": "live"},
    )
    by_key = {verdict.capability: verdict.state for verdict in verdicts}
    assert by_key["instagram.publish_reels"] == "available"
    # P8.20: read_insights is reachable on this path through instagram_manage_insights. It is not
    # granted here, so it is needs_user_consent (the user can fix it), not unsupported.
    assert by_key["instagram.read_insights"] == "needs_user_consent"


def test_facebook_login_path_insights_available_with_the_facebook_vocabulary():
    mock = facebook_account_graph()
    verdicts = facebook_path_adapter(mock).evaluate_capabilities(
        account_type=AccountType.INSTAGRAM_BUSINESS,
        granted_scopes=["instagram_basic", "instagram_manage_insights", "pages_read_engagement"],
        token_state=TokenState.VALID.value,
        extra={"app_mode": "live"},
    )
    by_key = {verdict.capability: verdict.state for verdict in verdicts}
    assert by_key["instagram.read_insights"] == "available"


def test_two_paths_produce_two_distinct_identities_for_one_human():
    """The same username over two paths must stay two accounts (ADR-11)."""
    login = login_adapter(instagram_login_graph(user_id="17841400000000000"))
    via_facebook = facebook_path_adapter(facebook_account_graph(ig_id="17841400000000001"))
    first = run(login.discover_accounts(ig_credentials(), "IG-long-token"))[0]
    second = run(via_facebook.discover_accounts(fb_credentials(), "EAAB-user-token"))[0]
    assert first.provider_account_id != second.provider_account_id
    assert first.meta["discovered_via"] != second.meta["discovered_via"]


def test_the_adapter_contains_no_publish_endpoint():
    source = inspect.getsource(instagram_module)
    tree = ast.parse(source)
    strings = {
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = [text for text in strings if any(token in text for token in ("/media_publish", "/media?", "container"))]
    assert offenders == [], f"publish-capable call found in the Instagram adapter: {offenders}"


def test_personal_account_capabilities_are_unsupported():
    mock = instagram_login_graph(account_type="PERSONAL")
    verdicts = login_adapter(mock).evaluate_capabilities(
        account_type=AccountType.PERSONAL_UNSUPPORTED,
        granted_scopes=["instagram_business_basic"],
        token_state=TokenState.VALID.value,
    )
    assert all(verdict.state == "unsupported" for verdict in verdicts)


def test_missing_secret_is_reported():
    mock = instagram_login_graph()
    with pytest.raises(SocialError) as error:
        run(login_adapter(mock).exchange_code(ig_credentials(client_secret=None), code="x"))
    assert error.value.code == "APP_NOT_CONFIGURED"
