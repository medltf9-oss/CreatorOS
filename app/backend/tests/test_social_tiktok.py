"""TikTok adapter tests: Login Kit, PKCE, and refresh tokens that rotate.

The rotation is the interesting part. TikTok invalidates the old refresh token as soon as it
answers, so the adapter must always report the new one and the token service must store it before
anything else can fail.
"""
from __future__ import annotations

import ast
import inspect

import pytest
from social_fixtures import run, tiktok_graph

from creatoros.db.enums import AccountType, OAuthRoute, Platform, TokenState
from creatoros.integrations.social import tiktok as tiktok_module
from creatoros.integrations.social.base import ProviderCredentials
from creatoros.integrations.social.errors import SocialError
from creatoros.integrations.social.tiktok import TikTokAdapter, pkce_challenge

VERIFIER = "verifier-" + "v" * 60


def credentials(**overrides) -> ProviderCredentials:
    base = {
        "provider": Platform.TIKTOK,
        "route": OAuthRoute.TIKTOK_LOGIN,
        "client_id": "client-key",
        "client_secret": "client-secret",
        "redirect_uri": "http://127.0.0.1:47821/oauth/callback/tiktok_login",
        "api_version": "v2",
        "scopes": ["user.info.basic", "video.list"],
        "app_mode": "development",
    }
    base.update(overrides)
    return ProviderCredentials(**base)


def adapter_for(mock) -> TikTokAdapter:
    return TikTokAdapter(transport=mock.transport("tiktok"))


def test_authorization_url_uses_client_key_and_pkce_s256():
    mock = tiktok_graph()
    url = adapter_for(mock).authorization_url(credentials(), state="s" * 43, code_challenge=pkce_challenge(VERIFIER))
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert "client_key=client-key" in url
    assert "code_challenge_method=S256" in url
    assert "code_challenge=" + pkce_challenge(VERIFIER) in url
    assert "user.info.basic" in url


def test_authorization_without_pkce_is_refused():
    with pytest.raises(SocialError) as error:
        adapter_for(tiktok_graph()).authorization_url(credentials(), state="s" * 43)
    assert error.value.code == "PKCE_REQUIRED"


def test_code_exchange_sends_the_verifier_and_reads_the_rotating_refresh_token():
    mock = tiktok_graph()
    bundle = run(adapter_for(mock).exchange_code(credentials(), code="code-1", code_verifier=VERIFIER))
    assert bundle.access_token == "tt-access-1"
    assert bundle.refresh_token == "tt-refresh-1"
    assert bundle.refresh_expires_at is not None
    assert bundle.provider_account_id == "tt-open-id-1"
    assert bundle.meta["rotation"] == "refresh_token_rotates"
    call = mock.call(r"oauth/token/", "POST")
    assert call.data["grant_type"] == "authorization_code"
    assert call.data["code_verifier"] == VERIFIER
    assert call.data["client_key"] == "client-key"


def test_exchange_without_a_verifier_is_refused():
    with pytest.raises(SocialError) as error:
        run(adapter_for(tiktok_graph()).exchange_code(credentials(), code="code-1", code_verifier=None))
    assert error.value.code == "PKCE_REQUIRED"


def test_refresh_reports_the_new_refresh_token():
    mock = tiktok_graph()
    mock.routes = [route for route in mock.routes if "oauth/token" not in route[0]]
    mock.json(r"open\.tiktokapis\.com/v2/oauth/token/", 200, {
        "access_token": "tt-access-2",
        "expires_in": 86400,
        "open_id": "tt-open-id-1",
        "refresh_expires_in": 31536000,
        "refresh_token": "tt-refresh-2",
        "scope": "user.info.basic,video.list",
        "token_type": "Bearer",
    })
    bundle = run(adapter_for(mock).refresh(credentials(), "tt-refresh-1"))
    assert bundle.access_token == "tt-access-2"
    assert bundle.refresh_token == "tt-refresh-2"
    assert bundle.meta["rotation"] == "rotated"
    call = mock.call(r"oauth/token/", "POST")
    assert call.data["grant_type"] == "refresh_token"
    assert call.data["refresh_token"] == "tt-refresh-1"


def test_refresh_failure_is_reauthorization_not_a_retry_loop():
    mock = tiktok_graph()
    mock.routes = []
    mock.json(r"oauth/token/", 400, {"error": {"code": "invalid_grant", "message": "refresh token expired"}})
    with pytest.raises(SocialError) as error:
        run(adapter_for(mock).refresh(credentials(), "dead-refresh"))
    assert error.value.code == "REAUTHORIZATION_REQUIRED"
    assert error.value.user_action_required is True


def test_error_codes_are_mapped():
    mock = tiktok_graph()
    mock.routes = []
    mock.json(r"user/info/", 401, {"error": {"code": "access_token_invalid", "message": "bad token"}})
    with pytest.raises(SocialError) as error:
        run(adapter_for(mock).discover_accounts(credentials(), "token"))
    assert error.value.code == "TOKEN_EXPIRED"


def test_user_info_returns_one_creator():
    mock = tiktok_graph()
    accounts = run(adapter_for(mock).discover_accounts(credentials(), "tt-access-1"))
    assert len(accounts) == 1
    creator = accounts[0]
    assert creator.account_type == AccountType.TIKTOK_CREATOR
    assert creator.display_name == "Test TikTok"
    assert creator.meta["follower_count"] == 999
    call = mock.call(r"user/info/")
    assert call.headers["authorization"] == "Bearer tt-access-1"


def test_revocation_posts_to_the_revoke_endpoint():
    mock = tiktok_graph()
    result = run(adapter_for(mock).revoke(credentials(), "tt-access-1"))
    assert result.revoked is True
    call = mock.call(r"oauth/revoke/", "POST")
    assert call.data["token"] == "tt-access-1"


def test_creator_info_is_only_probed_when_video_publish_was_granted():
    mock = tiktok_graph()
    without = run(adapter_for(mock).diagnostics(credentials(), "tt-access-1"))
    assert any(check["name"] == "creator_info" and check["ok"] is False for check in without["checks"])
    assert "creator_info" not in without

    mock_with_publish = tiktok_graph()
    with_publish = run(
        adapter_for(mock_with_publish).diagnostics(
            credentials(scopes=["user.info.basic", "video.list", "video.publish"]), "tt-access-1"
        )
    )
    assert with_publish["creator_info"]["max_video_post_duration_sec"] == 600


def test_capabilities_mark_direct_post_as_needing_consent_or_review():
    mock = tiktok_graph()
    adapter = adapter_for(mock)
    without_publish = adapter.evaluate_capabilities(
        account_type=AccountType.TIKTOK_CREATOR,
        granted_scopes=["user.info.basic", "video.list"],
        token_state=TokenState.VALID.value,
        extra={"app_mode": "development"},
    )
    by_key = {verdict.capability: verdict.state for verdict in without_publish}
    assert by_key["tiktok.read_profile"] == "available"
    assert by_key["tiktok.list_videos"] == "available"
    assert by_key["tiktok.direct_post"] == "requires_developer_approval"
    assert by_key["manual.export_package"] == "manual_only"

    with_publish = adapter.evaluate_capabilities(
        account_type=AccountType.TIKTOK_CREATOR,
        granted_scopes=["user.info.basic", "video.list", "video.upload", "video.publish"],
        token_state=TokenState.VALID.value,
        extra={"app_mode": "live"},
    )
    available = {verdict.capability for verdict in with_publish if verdict.state == "available"}
    assert "tiktok.direct_post" in available


def test_an_expired_token_makes_every_capability_unusable():
    verdicts = adapter_for(tiktok_graph()).evaluate_capabilities(
        account_type=AccountType.TIKTOK_CREATOR,
        granted_scopes=["user.info.basic", "video.list", "video.upload"],
        token_state=TokenState.EXPIRED.value,
    )
    assert all(verdict.state in {"expired", "manual_only"} for verdict in verdicts)


def test_the_adapter_publishes_nothing():
    """Only one POST exists (creator_info, a read-only metadata query), and no publish path."""
    from creatoros.integrations.social import config as social_config

    strings: set[str] = set()
    for module in (tiktok_module, social_config):
        tree = ast.parse(inspect.getsource(module))
        strings |= {
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
    offenders = [
        text
        for text in strings
        if any(token in text for token in ("/post/publish/video", "/post/publish/content", "/post/publish/status"))
    ]
    assert offenders == []
    # The creator-info probe is the only publish namespace reference, and it is read-only.
    assert any("/post/publish/creator_info/query/" in text for text in strings)
