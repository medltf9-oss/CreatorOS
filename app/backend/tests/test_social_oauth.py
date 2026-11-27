"""OAuth engine tests: state, PKCE, expiry, replay, redirect binding and the callback listener.

Every test drives the real engine against a `MockTransport` provider: the state machine, the
database rows and the vault calls are all real, only the provider's HTTP answers are fixtures.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading

import pytest
from social_fixtures import (
    facebook_account_graph,
    instagram_login_graph,
    make_app_config,
    run,
    tiktok_graph,
)

from creatoros.db.enums import OAuthRoute, Platform
from creatoros.db.models.platform import OAuthSession
from creatoros.integrations.social.base import AdapterRegistry
from creatoros.integrations.social.errors import SocialError
from creatoros.integrations.social.oauth import (
    challenge_for,
    new_verifier,
)
from creatoros.services.social.callback_server import CallbackListener, set_listener
from creatoros.services.social.oauth_engine import OAuthEngine


@pytest.fixture(autouse=True)
def _callback_listener_registry():
    """A started-listener stand-in for every test in this module.

    `OAuthEngine.start` refuses to begin a flow when no listener is running (an authorization
    that cannot be received is a dead end), so the fake is installed before each test and removed
    afterwards. Tests that assert the refusal remove it themselves.
    """
    set_listener(_fake_listener(47821))
    yield
    set_listener(None)


def registry_for(mock):
    return AdapterRegistry(transport_factory=lambda provider: mock.transport(provider))


def build_engine(session, vault, mock, *, port: int = 47821) -> OAuthEngine:
    return OAuthEngine(session, vault, callback_port=port, adapter_factory=lambda: registry_for(mock))


def start_pending(session, vault, mock, platform=Platform.FACEBOOK, route=OAuthRoute.FACEBOOK_LOGIN, **kwargs):
    config = run(make_app_config(session, vault, platform=platform, route=route, **kwargs))
    session.commit()
    engine = build_engine(session, vault, mock)
    payload = run(engine.start(platform, route))
    session.commit()
    return config, engine, payload


def callback_params(payload: dict, state: str, code: str = "auth-code-1", **extra) -> dict:
    params = {"code": code, "state": state}
    params.update(extra)
    return params


def state_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["state"][0]


# ------------------------------------------------------------------ primitives


def test_state_is_long_random_and_hashed_at_rest(session, vault):
    mock = facebook_account_graph()
    _config, _engine, payload = start_pending(session, vault, mock)
    row = session.query(OAuthSession).one()
    state = state_from_url(payload["authorization_url"])
    assert len(state) >= 43
    assert row.state_hash == hashlib.sha256(state.encode()).hexdigest()
    # The plaintext state is not in the row anywhere.
    assert state not in repr(row.__dict__)


def test_pkce_challenge_is_s256_of_the_verifier():
    verifier = new_verifier()
    import base64

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge_for(verifier) == expected
    assert challenge_for(verifier) != challenge_for(new_verifier())


def test_start_refuses_without_a_registered_app(session, vault):
    engine = build_engine(session, vault, facebook_account_graph())
    with pytest.raises(SocialError) as error:
        run(engine.start(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN))
    assert error.value.code == "APP_NOT_CONFIGURED"


def test_start_refuses_without_a_vault(session):
    from creatoros.services.social.vault import UnavailableVault

    engine = build_engine(session, UnavailableVault("no broker"), facebook_account_graph())
    with pytest.raises(SocialError) as error:
        run(engine.start(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN))
    assert error.value.code == "APP_NOT_CONFIGURED"


def test_start_refuses_a_redirect_that_does_not_match_the_listener(session, vault):
    config = run(
        make_app_config(
            session,
            vault,
            redirect_uri="http://127.0.0.1:47821/oauth/callback/facebook_login",
        )
    )
    session.commit()
    engine = build_engine(session, vault, facebook_account_graph(), port=47822)
    set_listener(_fake_listener(47822))
    try:
        with pytest.raises(SocialError) as error:
            run(engine.start(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN))
    finally:
        set_listener(None)
    assert error.value.code == "REDIRECT_MISMATCH"
    assert error.value.detail["registered"] == config.redirect_uri


def test_start_refuses_when_the_listener_is_not_running(session, vault):
    run(make_app_config(session, vault))
    session.commit()
    engine = build_engine(session, vault, facebook_account_graph())
    set_listener(None)
    with pytest.raises(SocialError) as error:
        run(engine.start(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN))
    assert error.value.code == "CALLBACK_NOT_READY"


def test_scopes_outside_the_route_surface_are_refused(session, vault):
    run(make_app_config(session, vault))
    session.commit()
    engine = build_engine(session, vault, facebook_account_graph())
    set_listener(_fake_listener(47821))
    try:
        with pytest.raises(SocialError) as error:
            run(engine.start(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, scopes=["pages_manage_posts", "ads_management"]))
    finally:
        set_listener(None)
    assert error.value.code == "SCOPE_UNKNOWN"


# ------------------------------------------------------------------ happy paths


def test_facebook_page_connect_creates_an_account_with_capabilities(session, vault):
    mock = facebook_account_graph()
    _config, engine, payload = start_pending(session, vault, mock)
    set_listener(_fake_listener(47821))
    try:
        outcome = run(engine.handle_callback("facebook_login", callback_params(payload, state_from_url(payload["authorization_url"]))))
    finally:
        set_listener(None)
    assert outcome["ok"] is True, outcome
    assert outcome["code"] == "CONNECTED"

    from creatoros.db.models.platform import AccountCapability, SocialAccount

    account = session.query(SocialAccount).one()
    assert account.provider_account_id == "1234567890"
    assert account.account_type.value == "facebook_page"
    assert account.route == OAuthRoute.FACEBOOK_LOGIN
    assert account.connection_status.value == "connected"
    assert "pages_manage_posts" in account.scopes_json
    capabilities = {row.capability: row.state.value for row in session.query(AccountCapability).all()}
    assert capabilities["facebook.page.publish_video"] == "available"
    # The page token is in the vault, never in the row.
    assert account.token_ref_id in vault.records or account.token_ref_id
    row_state = session.query(OAuthSession).one()
    assert row_state.status == "succeeded"
    assert row_state.consumed_at is not None


def test_callback_is_single_use(session, vault):
    mock = facebook_account_graph()
    _config, engine, payload = start_pending(session, vault, mock)
    state = state_from_url(payload["authorization_url"])
    set_listener(_fake_listener(47821))
    try:
        first = run(engine.handle_callback("facebook_login", callback_params(payload, state)))
        replay = run(engine.handle_callback("facebook_login", callback_params(payload, state)))
    finally:
        set_listener(None)
    assert first["ok"] is True
    assert replay["ok"] is False
    assert replay["code"] == "STATE_CONSUMED"


def test_unknown_state_is_rejected(session, vault):
    mock = facebook_account_graph()
    _config, engine, _payload = start_pending(session, vault, mock)
    set_listener(_fake_listener(47821))
    try:
        outcome = run(engine.handle_callback("facebook_login", {"code": "x", "state": "z" * 60}))
    finally:
        set_listener(None)
    assert outcome["code"] == "STATE_INVALID"


def test_missing_state_is_rejected(session, vault):
    mock = facebook_account_graph()
    _config, engine, _payload = start_pending(session, vault, mock)
    outcome = run(engine.handle_callback("facebook_login", {"code": "x"}))
    assert outcome["code"] == "STATE_INVALID"


def test_expired_state_is_rejected(session, vault):
    mock = facebook_account_graph()
    _config, engine, payload = start_pending(session, vault, mock)
    row = session.query(OAuthSession).one()
    from datetime import timedelta

    from creatoros.db.base import utcnow

    row.expires_at = utcnow() - timedelta(minutes=1)
    session.commit()
    set_listener(_fake_listener(47821))
    try:
        outcome = run(engine.handle_callback("facebook_login", callback_params(payload, state_from_url(payload["authorization_url"]))))
    finally:
        set_listener(None)
    assert outcome["code"] == "STATE_EXPIRED"
    session.refresh(row)
    assert row.status == "expired"


def test_state_bound_to_another_route_is_rejected(session, vault):
    mock = facebook_account_graph()
    _config, engine, payload = start_pending(session, vault, mock)
    outcome = run(engine.handle_callback("tiktok_login", callback_params(payload, state_from_url(payload["authorization_url"]))))
    assert outcome["code"] == "STATE_ROUTE_MISMATCH"


def test_user_denial_is_reported_not_swallowed(session, vault):
    mock = facebook_account_graph()
    _config, engine, payload = start_pending(session, vault, mock)
    state = state_from_url(payload["authorization_url"])
    set_listener(_fake_listener(47821))
    try:
        outcome = run(
            engine.handle_callback(
                "facebook_login",
                {"state": state, "error": "access_denied", "error_description": "User denied the request"},
            )
        )
    finally:
        set_listener(None)
    assert outcome["ok"] is False
    assert outcome["code"] == "AUTHORIZATION_DENIED"
    row = session.query(OAuthSession).one()
    assert row.status == "failed"
    assert session.query(__import__("creatoros.db.models", fromlist=["SocialAccount"]).SocialAccount).count() == 0


def test_no_supported_account_is_explained(session, vault):
    mock = facebook_account_graph()
    # Replace the pages answer rather than adding a second one: the mock returns the first match.
    mock.routes = [route for route in mock.routes if "me/accounts" not in route[0]]
    mock.json(r"/me/accounts", 200, {"data": []})
    _config, engine, payload = start_pending(session, vault, mock)
    set_listener(_fake_listener(47821))
    try:
        outcome = run(engine.handle_callback("facebook_login", callback_params(payload, state_from_url(payload["authorization_url"]))))
    finally:
        set_listener(None)
    assert outcome["ok"] is False
    assert outcome["code"] == "NO_SUPPORTED_ACCOUNT"
    assert "Pages only" in outcome["message"] or "does not administer" in outcome["message"]


def test_instagram_login_path_connects_a_professional_account(session, vault):
    mock = instagram_login_graph()
    _config, engine, payload = start_pending(
        session, vault, mock, platform=Platform.INSTAGRAM, route=OAuthRoute.INSTAGRAM_LOGIN
    )
    set_listener(_fake_listener(47821))
    try:
        outcome = run(
            engine.handle_callback(
                "instagram_login", callback_params(payload, state_from_url(payload["authorization_url"]))
            )
        )
    finally:
        set_listener(None)
    assert outcome["ok"] is True, outcome
    from creatoros.db.models.platform import AccountCapability, SocialAccount

    account = session.query(SocialAccount).one()
    assert account.platform == Platform.INSTAGRAM
    assert account.route == OAuthRoute.INSTAGRAM_LOGIN
    capabilities = {row.capability: row.state.value for row in session.query(AccountCapability).all()}
    assert capabilities["instagram.publish_reels"] in {"requires_developer_approval", "available"}


def test_instagram_personal_account_is_unsupported(session, vault):
    mock = instagram_login_graph(account_type="PERSONAL")
    _config, engine, payload = start_pending(
        session, vault, mock, platform=Platform.INSTAGRAM, route=OAuthRoute.INSTAGRAM_LOGIN
    )
    set_listener(_fake_listener(47821))
    try:
        outcome = run(
            engine.handle_callback(
                "instagram_login", callback_params(payload, state_from_url(payload["authorization_url"]))
            )
        )
    finally:
        set_listener(None)
    assert outcome["ok"] is False
    assert outcome["code"] == "NO_SUPPORTED_ACCOUNT"
    assert "professional" in outcome["message"]


def test_tiktok_login_requires_pkce_and_stores_a_rotating_refresh_token(session, vault):
    mock = tiktok_graph()
    run(
        make_app_config(
            session,
            vault,
            platform=Platform.TIKTOK,
            route=OAuthRoute.TIKTOK_LOGIN,
            client_id="client-key",
            redirect_uri="http://127.0.0.1:47821/oauth/callback/tiktok_login",
            scopes=["user.info.basic", "video.list"],
            api_version="v2",
        )
    )
    session.commit()
    engine = build_engine(session, vault, mock)
    payload = run(engine.start(Platform.TIKTOK, OAuthRoute.TIKTOK_LOGIN))
    session.commit()
    assert "code_challenge=" in payload["authorization_url"]
    assert payload["pkce"] == "S256"

    refs = session.query(__import__("creatoros.db.models", fromlist=["SecretRef"]).SecretRef).all()
    verifiers = [ref for ref in refs if ref.purpose == "oauth_pkce_verifier"]
    assert len(verifiers) == 1
    verifier_row = verifiers[0]

    set_listener(_fake_listener(47821))
    try:
        outcome = run(
            engine.handle_callback("tiktok_login", callback_params(payload, state_from_url(payload["authorization_url"])))
        )
    finally:
        set_listener(None)
    assert outcome["ok"] is True, outcome
    exchange = mock.call(r"oauth/token/", "POST")
    assert exchange.data["code_verifier"] == vault.records[verifier_row.id]["value"]
    assert exchange.data["grant_type"] == "authorization_code"

    from creatoros.db.models.platform import SocialAccount

    account = session.query(SocialAccount).one()
    assert account.platform == Platform.TIKTOK
    assert account.refresh_ref_id is not None
    assert vault.records[account.refresh_ref_id]["value"] == "tt-refresh-1"


def test_tiktok_without_code_challenge_is_refused_by_the_adapter():
    mock = tiktok_graph()
    from creatoros.integrations.social.base import ProviderCredentials
    from creatoros.integrations.social.tiktok import TikTokAdapter

    adapter = TikTokAdapter(transport=mock.transport("tiktok"))
    credentials = ProviderCredentials(
        provider=Platform.TIKTOK,
        route=OAuthRoute.TIKTOK_LOGIN,
        client_id="client-key",
        client_secret="secret",
        redirect_uri="http://127.0.0.1:47821/oauth/callback/tiktok_login",
        api_version="v2",
        scopes=["user.info.basic"],
    )
    with pytest.raises(SocialError) as error:
        adapter.authorization_url(credentials, state="s" * 50, code_challenge=None)
    assert error.value.code == "PKCE_REQUIRED"


# ------------------------------------------------------------------ transactions


def test_transaction_reports_progress_and_never_returns_the_state(session, vault):
    mock = facebook_account_graph()
    _config, engine, payload = start_pending(session, vault, mock)
    state = state_from_url(payload["authorization_url"])
    transaction = engine.transaction(payload["session_id"])
    assert transaction["status"] == "pending"
    assert state not in repr(transaction)
    set_listener(_fake_listener(47821))
    try:
        run(engine.handle_callback("facebook_login", callback_params(payload, state)))
    finally:
        set_listener(None)
    after = engine.transaction(payload["session_id"])
    assert after["status"] == "succeeded"
    assert after["accounts"]
    assert after["accounts"][0]["platform"] == "facebook"


def test_cancel_marks_a_pending_attempt(session, vault):
    mock = facebook_account_graph()
    _config, engine, payload = start_pending(session, vault, mock)
    cancelled = engine.cancel(payload["session_id"])
    assert cancelled["status"] == "cancelled"
    again = engine.cancel(payload["session_id"])
    assert again["status"] == "cancelled"


# ------------------------------------------------------------------ listener


class _FakeListener:
    def __init__(self, port: int) -> None:
        self.port = port
        self.running = True
        self.requests = 0

    def status(self) -> dict:
        return {"running": True, "port": self.port, "url_base": f"http://127.0.0.1:{self.port}/oauth/callback/"}


def _fake_listener(port: int) -> _FakeListener:
    return _FakeListener(port)


def _listener_engine(session, vault):
    """A real listener + real engine, wired the way main.py wires them."""
    loop = asyncio.new_event_loop()
    mock = facebook_account_graph()

    async def handler(route: str, params: dict) -> dict:
        engine = build_engine(session, vault, mock)
        return await engine.handle_callback(route, params)

    listener = CallbackListener(port=0, handler=handler, loop=loop, logger=lambda _line: None)
    return listener, loop, mock


def test_callback_listener_binds_loopback_and_answers_only_its_path(session, vault):
    import threading
    import urllib.error
    import urllib.request

    loop = asyncio.new_event_loop()
    seen: list[dict] = []

    async def handler(route: str, params: dict) -> dict:
        seen.append({"route": route, "params": params})
        return {"ok": True, "code": "CONNECTED", "message": "done"}

    listener = CallbackListener(port=0, handler=handler, loop=loop, logger=lambda _line: None)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    port = listener.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/oauth/callback/facebook_login?code=abc&state=xyz") as response:
            body = response.read().decode()
        assert response.status == 200
        assert "Connected" in body
        assert "abc" not in body  # the code is never echoed into the page
        assert seen and seen[0]["route"] == "facebook_login"
        assert seen[0]["params"]["code"] == "abc"

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        listener.stop()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def test_callback_listener_reports_a_handler_failure_as_a_page_not_a_crash(session, vault):
    import urllib.error
    import urllib.request

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    async def handler(_route: str, _params: dict) -> dict:
        raise SocialError("AUTHORIZATION_DENIED", "the user declined")

    listener = CallbackListener(port=0, handler=handler, loop=loop, logger=lambda _line: None)
    port = listener.start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/oauth/callback/facebook_login?code=abc&state=xyz")
            raise AssertionError("expected a 400 page")
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            assert error.code == 400
            assert "AUTHORIZATION_DENIED" in body
            assert "user declined" in body
    finally:
        listener.stop()
        loop.call_soon_threadsafe(loop.stop)


def test_listener_rejects_post(session, vault):
    import urllib.error
    import urllib.request

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    async def handler(_route: str, _params: dict) -> dict:
        return {"ok": True, "code": "CONNECTED", "message": "done"}

    listener = CallbackListener(port=0, handler=handler, loop=loop, logger=lambda _line: None)
    port = listener.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/oauth/callback/facebook_login", data=b"x", method="POST")
        try:
            urllib.request.urlopen(request)
            raise AssertionError("expected 405")
        except urllib.error.HTTPError as error:
            assert error.code == 405
    finally:
        listener.stop()
        loop.call_soon_threadsafe(loop.stop)
