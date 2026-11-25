"""P8.20: the analytics consent path - the permission difference, the upgrade, and its safety.

Every test drives the real services against a `MockTransport` provider: the planner, the OAuth
engine, the vault calls, the capability rows and the audit records are all real; only the provider's
HTTP answers are fixtures. Nothing here claims a live account exists.
"""
from __future__ import annotations

import pytest
from social_fixtures import (
    InMemoryVault,
    assert_no_secret_in,
    facebook_account_graph,
    instagram_login_graph,
    make_app_config,
    run,
    tiktok_graph,
)

from creatoros.db.enums import (
    AccountType,
    CapabilityState,
    ConnectionStatus,
    ConsentStatus,
    OAuthRoute,
    Platform,
)
from creatoros.db.models import AuditEvent
from creatoros.db.models.platform import OAuthSession, SecretRef, SocialAccount
from creatoros.integrations.social.base import AdapterRegistry
from creatoros.integrations.social.errors import SocialError
from creatoros.services.social.callback_server import set_listener
from creatoros.services.social.capabilities import CapabilityService
from creatoros.services.social.consent import (
    CONSENT_BASE_SCOPES,
    ConsentService,
    account_consent_scope_set,
    analytics_scope_surface,
)
from creatoros.services.social.oauth_engine import OAuthEngine
from creatoros.services.social.secrets import SecretStore
from creatoros.services.social.vault import VaultError

CALLBACK_PORT = 47821


# ------------------------------------------------------------------ fixtures / helpers


class _FakeListener:
    def __init__(self, port: int = CALLBACK_PORT) -> None:
        self.port = port
        self.running = True

    def status(self) -> dict:
        return {"running": True, "port": self.port, "url_base": f"http://127.0.0.1:{self.port}/oauth/callback/"}


@pytest.fixture(autouse=True)
def _listener():
    set_listener(_FakeListener())
    yield
    set_listener(None)


def registry_for(mock) -> AdapterRegistry:
    return AdapterRegistry(transport_factory=lambda provider: mock.transport(provider))


def engine_for(session, vault, mock) -> OAuthEngine:
    return OAuthEngine(session, vault, callback_port=CALLBACK_PORT, adapter_factory=lambda: registry_for(mock))


def state_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["state"][0]


def make_account(
    session,
    *,
    platform: Platform,
    route: OAuthRoute,
    scopes: list[str],
    provider_account_id: str,
    account_type: AccountType,
    meta: dict | None = None,
    api_version: str = "v23.0",
) -> SocialAccount:
    account = SocialAccount(
        platform=platform,
        route=route,
        provider_account_id=provider_account_id,
        display_name="Existing account",
        username="existing",
        account_type=account_type,
        scopes_json=list(scopes),
        connection_status=ConnectionStatus.CONNECTED,
        api_version=api_version,
        meta_json=meta if meta is not None else {},
    )
    session.add(account)
    session.flush()
    return account


def seed_credential(session, vault, account, *, token: str = "old-access-token") -> str:
    """Give the account a real stored credential so "preserved" assertions are meaningful."""
    stored = run(
        SecretStore(session, vault).create(
            value=token,
            purpose="access_token",
            provider=account.platform.value,
            alias=f"account:{account.platform.value}:{account.provider_account_id}",
        )
    )
    account.token_ref_id = stored.ref.id
    session.flush()
    return stored.ref.id


def latest_session(session, account) -> OAuthSession:
    row = (
        session.query(OAuthSession)
        .filter(OAuthSession.account_id == account.id)
        .order_by(OAuthSession.created_at.desc())
        .first()
    )
    assert row is not None, "no consent session was started"
    return row


def start_consent(session, vault, account: SocialAccount) -> dict:
    return run(ConsentService(session, vault).start(account))


def callback(session, vault, mock, *, route: OAuthRoute, params: dict) -> dict:
    return run(engine_for(session, vault, mock).handle_callback(route.value, params))


# ------------------------------------------------------------------ planning


def test_facebook_plan_requests_only_the_missing_insight_scope(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.CONSENT_REQUIRED
    assert plan.missing_scopes == ["read_insights"]
    assert plan.requested_scopes == ["pages_read_engagement", "read_insights"]
    # No publishing scope is ever requested in a permission upgrade.
    assert "pages_manage_posts" not in plan.requested_scopes
    assert plan.can_start is True
    assert plan.scope_reasons["read_insights"].startswith("required to read analytics")


def test_instagram_login_plan_uses_the_business_vocabulary(session, vault):
    run(make_app_config(session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.INSTAGRAM_LOGIN, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.INSTAGRAM_LOGIN,
        provider_account_id="17841400000000000",
        account_type=AccountType.INSTAGRAM_BUSINESS,
        scopes=["instagram_business_basic", "instagram_business_content_publish"],
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.CONSENT_REQUIRED
    assert plan.missing_scopes == ["instagram_business_manage_insights"]
    assert "instagram_manage_insights" not in plan.requested_scopes


def test_instagram_facebook_login_plan_uses_the_facebook_vocabulary(session, vault):
    run(make_app_config(session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.FACEBOOK_LOGIN, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="17841400000000001",
        account_type=AccountType.INSTAGRAM_BUSINESS,
        scopes=["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"],
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.CONSENT_REQUIRED
    assert plan.missing_scopes == ["instagram_manage_insights"]
    assert plan.requested_scopes == [
        "instagram_basic",
        "pages_show_list",
        "pages_read_engagement",
        "instagram_manage_insights",
    ]
    assert "instagram_business_manage_insights" not in plan.requested_scopes


def test_already_granted_scope_is_a_no_op(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_read_engagement", "read_insights"],
        meta={"page_tasks": ["ANALYZE", "CREATE_CONTENT"]},
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.CONSENT_NOT_NEEDED
    assert plan.missing_scopes == []
    # Nothing to consent to: starting is refused and no session is created.
    result = start_consent(session, vault, account)
    assert result["status"] == ConsentStatus.CONSENT_NOT_NEEDED.value
    assert result["authorization_url"] is None
    assert session.query(OAuthSession).count() == 0


def test_tiktok_stats_scope_present_is_a_no_op(session, vault):
    run(make_app_config(session, vault, platform=Platform.TIKTOK, route=OAuthRoute.TIKTOK_LOGIN, api_version="v2"))
    account = make_account(
        session,
        platform=Platform.TIKTOK,
        route=OAuthRoute.TIKTOK_LOGIN,
        provider_account_id="tt-open-id-1",
        account_type=AccountType.TIKTOK_CREATOR,
        scopes=["user.info.basic", "user.info.stats", "video.list"],
        api_version="v2",
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.CONSENT_NOT_NEEDED
    assert plan.missing_scopes == []
    # No invented TikTok insight permission: the required set is exactly the documented scopes.
    assert plan.requested_scopes == ["user.info.basic", "user.info.stats"]


def test_tiktok_without_stats_requests_only_user_info_stats(session, vault):
    run(
        make_app_config(
            session, vault, platform=Platform.TIKTOK, route=OAuthRoute.TIKTOK_LOGIN, api_version="v2", app_mode="live"
        )
    )
    account = make_account(
        session,
        platform=Platform.TIKTOK,
        route=OAuthRoute.TIKTOK_LOGIN,
        provider_account_id="tt-open-id-1",
        account_type=AccountType.TIKTOK_CREATOR,
        scopes=["user.info.basic", "video.list"],
        api_version="v2",
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.CONSENT_REQUIRED
    assert plan.missing_scopes == ["user.info.stats"]


def test_developer_app_in_development_reports_developer_approval_and_explains_it(session, vault):
    run(make_app_config(session, vault, app_mode="development"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.DEVELOPER_APPROVAL_REQUIRED
    assert plan.developer_approval_required is True
    assert "developer app" in plan.detail
    # The user can still attempt it (an app tester may hold the permission); the result is honest.
    assert plan.can_start is True


def test_page_analyze_task_missing_blocks_even_with_the_scope(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_read_engagement", "read_insights"],
        meta={"page_tasks": ["CREATE_CONTENT"]},
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.PAGE_TASK_REQUIRED
    assert plan.page_tasks_required == ["ANALYZE"]
    assert plan.page_tasks_missing == ["ANALYZE"]
    assert plan.can_start is False


def test_page_task_never_reported_is_not_assumed_present(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_read_engagement", "read_insights"],
        meta={},
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.page_tasks_known is False
    assert plan.status is ConsentStatus.PAGE_TASK_REQUIRED


def test_unsupported_account_type_is_reported_not_hidden(session, vault):
    run(make_app_config(session, vault, platform=Platform.TIKTOK, route=OAuthRoute.TIKTOK_LOGIN, api_version="v2"))
    account = make_account(
        session,
        platform=Platform.TIKTOK,
        route=OAuthRoute.TIKTOK_LOGIN,
        provider_account_id="tt-open-id-1",
        account_type=AccountType.PERSONAL_UNSUPPORTED,
        scopes=["user.info.basic"],
        api_version="v2",
    )
    session.commit()
    plan = ConsentService(session, vault).plan(account)
    assert plan.status is ConsentStatus.UNSUPPORTED
    assert plan.can_start is False


def test_consent_scope_sets_are_within_each_route_surface():
    """A guard: the requested scopes can never name something the route does not document."""
    for (platform, route) in CONSENT_BASE_SCOPES:
        allowed = analytics_scope_surface(platform, route)
        requested = account_consent_scope_set(platform, route)
        assert requested, f"{platform.value}/{route.value} has no consent scope set"
        assert set(requested) <= allowed, f"{platform.value}/{route.value} asks for an unknown scope"


# ------------------------------------------------------------------ upgrade, happy path


def test_facebook_upgrade_adopts_the_scope_and_preserves_identity(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        meta={"page_tasks": ["ANALYZE", "CREATE_CONTENT"]},
    )
    session.commit()
    account_id = account.id
    old_ref = seed_credential(session, vault, account)
    session.commit()

    started = start_consent(session, vault, account)
    assert started["status"] == ConsentStatus.AUTHORIZATION_STARTED.value
    assert started["scopes_requested"] == ["pages_read_engagement", "read_insights"]

    mock = facebook_account_graph(
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts", "read_insights"],
        tasks=["ANALYZE", "CREATE_CONTENT"],
    )
    outcome = callback(
        session, vault, mock,
        route=OAuthRoute.FACEBOOK_LOGIN,
        params={"code": "c", "state": state_from_url(started["authorization_url"])},
    )
    assert outcome["ok"] is True
    assert outcome["code"] == "CAPABILITY_AVAILABLE"
    session.commit()

    assert account.id == account_id  # same row, never a new account
    assert "read_insights" in account.scopes_json
    assert "pages_manage_posts" in account.scopes_json  # existing grants preserved
    assert account.token_ref_id != old_ref  # credential replaced
    assert session.get(SecretRef, old_ref).status.value == "revoked"  # old revoked only after new
    capability = {row.capability: row.state for row in CapabilityService(session).current(account.id)}
    assert capability["facebook.page.read_insights"] is CapabilityState.AVAILABLE
    row = latest_session(session, account)
    assert row.status == "succeeded"
    assert row.result_json["upgrade"]["outcome"] == "capability_available"


def test_instagram_login_upgrade(session, vault):
    run(make_app_config(session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.INSTAGRAM_LOGIN, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.INSTAGRAM_LOGIN,
        provider_account_id="17841400000000000",
        account_type=AccountType.INSTAGRAM_BUSINESS,
        scopes=["instagram_business_basic", "instagram_business_content_publish"],
    )
    session.commit()
    started = start_consent(session, vault, account)
    assert started["status"] == ConsentStatus.AUTHORIZATION_STARTED.value
    mock = instagram_login_graph(
        permissions=[
            "instagram_business_basic",
            "instagram_business_content_publish",
            "instagram_business_manage_insights",
        ]
    )
    outcome = callback(
        session, vault, mock,
        route=OAuthRoute.INSTAGRAM_LOGIN,
        params={"code": "c", "state": state_from_url(started["authorization_url"])},
    )
    assert outcome["code"] == "CAPABILITY_AVAILABLE"
    session.commit()
    assert "instagram_business_manage_insights" in account.scopes_json
    capability = {row.capability: row.state for row in CapabilityService(session).current(account.id)}
    assert capability["instagram.read_insights"] is CapabilityState.AVAILABLE


def test_instagram_facebook_login_upgrade(session, vault):
    run(make_app_config(session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.FACEBOOK_LOGIN, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="17841400000000001",
        account_type=AccountType.INSTAGRAM_BUSINESS,
        scopes=["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"],
    )
    session.commit()
    started = start_consent(session, vault, account)
    mock = facebook_account_graph(
        ig_id="17841400000000001",
        scopes=["instagram_basic", "instagram_manage_insights", "pages_read_engagement"],
    )
    outcome = callback(
        session, vault, mock,
        route=OAuthRoute.FACEBOOK_LOGIN,
        params={"code": "c", "state": state_from_url(started["authorization_url"])},
    )
    assert outcome["code"] == "CAPABILITY_AVAILABLE"
    session.commit()
    assert "instagram_manage_insights" in account.scopes_json
    assert "instagram_business_manage_insights" not in account.scopes_json


def test_partial_grant_still_missing_one_scope_is_reported_blocked(session, vault):
    run(make_app_config(session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.INSTAGRAM_LOGIN, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.INSTAGRAM_LOGIN,
        provider_account_id="17841400000000000",
        account_type=AccountType.INSTAGRAM_BUSINESS,
        scopes=["instagram_business_basic"],
    )
    session.commit()
    started = start_consent(session, vault, account)
    # The provider grants the base but withholds the insight scope.
    mock = instagram_login_graph(permissions=["instagram_business_basic"])
    outcome = callback(
        session, vault, mock,
        route=OAuthRoute.INSTAGRAM_LOGIN,
        params={"code": "c", "state": state_from_url(started["authorization_url"])},
    )
    assert outcome["code"] == "CAPABILITY_STILL_BLOCKED"
    session.commit()
    capability = {row.capability: row.state for row in CapabilityService(session).current(account.id)}
    assert capability["instagram.read_insights"] is CapabilityState.NEEDS_USER_CONSENT


# ------------------------------------------------------------------ denial / safety


def test_user_denial_preserves_the_account_and_its_credentials(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    account_id = account.id
    before_ref = seed_credential(session, vault, account)
    before_scopes = list(account.scopes_json)
    session.commit()

    started = start_consent(session, vault, account)
    outcome = callback(
        session, vault, facebook_account_graph(),
        route=OAuthRoute.FACEBOOK_LOGIN,
        params={
            "error": "access_denied",
            "error_description": "The user denied",
            "state": state_from_url(started["authorization_url"]),
        },
    )
    assert outcome["ok"] is False
    assert outcome["code"] == "AUTHORIZATION_DENIED"
    session.commit()

    account = session.get(SocialAccount, account_id)
    assert account.connection_status is ConnectionStatus.CONNECTED
    assert account.scopes_json == before_scopes
    assert account.token_ref_id == before_ref
    assert account.disconnected_at is None
    assert session.get(SecretRef, before_ref).status.value == "active"
    # A denial is recorded, not swallowed.
    actions = [row.action for row in session.query(AuditEvent)]
    assert "analytics.consent.denied" in actions


def test_identity_mismatch_refuses_and_leaves_the_account_untouched(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="page-original",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    before_ref = seed_credential(session, vault, account)
    before_scopes = list(account.scopes_json)
    session.commit()
    started = start_consent(session, vault, account)

    # The provider returns a *different* page than the one being upgraded.
    outcome = callback(
        session, vault, facebook_account_graph(page_id="9999999999"),
        route=OAuthRoute.FACEBOOK_LOGIN,
        params={"code": "c", "state": state_from_url(started["authorization_url"])},
    )
    assert outcome["ok"] is False
    assert outcome["code"] == "ACCOUNT_IDENTITY_MISMATCH"
    session.commit()
    assert account.token_ref_id == before_ref
    assert account.scopes_json == before_scopes


def test_callback_is_single_use(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    started = start_consent(session, vault, account)
    mock = facebook_account_graph(
        scopes=["pages_read_engagement", "read_insights"], tasks=["ANALYZE", "CREATE_CONTENT"]
    )
    engine = engine_for(session, vault, mock)
    state = state_from_url(started["authorization_url"])
    first = run(engine.handle_callback(OAuthRoute.FACEBOOK_LOGIN.value, {"code": "c", "state": state}))
    replay = run(engine.handle_callback(OAuthRoute.FACEBOOK_LOGIN.value, {"code": "c", "state": state}))
    assert first["code"] == "CAPABILITY_AVAILABLE"
    assert replay["ok"] is False
    assert replay["code"] == "STATE_CONSUMED"


def test_two_concurrent_upgrades_are_distinct_transactions(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    first = start_consent(session, vault, account)
    second = start_consent(session, vault, account)
    assert first["session_id"] != second["session_id"]
    assert first["authorization_url"] != second["authorization_url"]
    rows = session.query(OAuthSession).filter(OAuthSession.account_id == account.id).all()
    assert len(rows) == 2
    assert len({row.state_hash for row in rows}) == 2


def test_vault_failure_during_adoption_preserves_the_old_credential(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    before_ref = seed_credential(session, vault, account)
    before_scopes = list(account.scopes_json)
    session.commit()
    started = start_consent(session, vault, account)

    class _PutFails(InMemoryVault):
        async def put(self, *, secret_id, value, purpose, provider=None, alias=None):
            if purpose == "access_token":
                raise VaultError("VAULT_UNAVAILABLE", "the broker is not answering")
            return await super().put(
                secret_id=secret_id, value=value, purpose=purpose, provider=provider, alias=alias
            )

    failing = _PutFails()
    failing.records.update({key: dict(record) for key, record in vault.records.items()})
    mock = facebook_account_graph(scopes=["pages_read_engagement", "read_insights"])
    outcome = callback(
        session, failing, mock,
        route=OAuthRoute.FACEBOOK_LOGIN,
        params={"code": "c", "state": state_from_url(started["authorization_url"])},
    )
    assert outcome["ok"] is False
    assert outcome["code"] == "VAULT_UNAVAILABLE"
    session.commit()
    account = session.get(SocialAccount, account.id)
    # The account still works: nothing was revoked, the scopes are unchanged.
    assert account.token_ref_id == before_ref
    assert account.scopes_json == before_scopes
    assert session.get(SecretRef, before_ref).status.value == "active"


# ------------------------------------------------------------------ start guards


def test_start_refuses_a_foreign_platform(session, vault):
    run(make_app_config(session, vault))
    run(
        make_app_config(
            session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.INSTAGRAM_LOGIN
        )
    )
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_read_engagement"],
    )
    session.commit()
    engine = OAuthEngine(session, vault, callback_port=CALLBACK_PORT)
    with pytest.raises(SocialError) as error:
        run(engine.start(Platform.INSTAGRAM, OAuthRoute.INSTAGRAM_LOGIN, account_id=account.id))
    assert error.value.code == "PLATFORM_MISMATCH"


def test_start_refuses_an_unknown_account(session, vault):
    run(make_app_config(session, vault))
    session.commit()
    engine = OAuthEngine(session, vault, callback_port=CALLBACK_PORT)
    with pytest.raises(SocialError) as error:
        run(engine.start(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, account_id="01JUNKNOWNACCOUNT0000000000"))
    assert error.value.code == "ACCOUNT_NOT_FOUND"


def test_start_refuses_a_different_login_path_for_the_same_platform(session, vault):
    run(make_app_config(session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.INSTAGRAM_LOGIN))
    run(make_app_config(session, vault, platform=Platform.INSTAGRAM, route=OAuthRoute.FACEBOOK_LOGIN))
    account = make_account(
        session,
        platform=Platform.INSTAGRAM,
        route=OAuthRoute.INSTAGRAM_LOGIN,
        provider_account_id="17841400000000000",
        account_type=AccountType.INSTAGRAM_BUSINESS,
        scopes=["instagram_business_basic"],
    )
    session.commit()
    engine = OAuthEngine(session, vault, callback_port=CALLBACK_PORT)
    with pytest.raises(SocialError) as error:
        run(engine.start(Platform.INSTAGRAM, OAuthRoute.FACEBOOK_LOGIN, account_id=account.id))
    assert error.value.code == "ROUTE_MISMATCH"


def test_tiktok_upgrade_uses_pkce_and_grants_user_info_stats(session, vault):
    run(
        make_app_config(
            session, vault, platform=Platform.TIKTOK, route=OAuthRoute.TIKTOK_LOGIN, api_version="v2", app_mode="live"
        )
    )
    account = make_account(
        session,
        platform=Platform.TIKTOK,
        route=OAuthRoute.TIKTOK_LOGIN,
        provider_account_id="tt-open-id-1",
        account_type=AccountType.TIKTOK_CREATOR,
        scopes=["user.info.basic", "video.list"],
        api_version="v2",
    )
    session.commit()
    started = start_consent(session, vault, account)
    assert started["pkce"] == "S256"
    mock = tiktok_graph(scope="user.info.basic,user.info.stats")
    outcome = callback(
        session, vault, mock,
        route=OAuthRoute.TIKTOK_LOGIN,
        params={"code": "c", "state": state_from_url(started["authorization_url"])},
    )
    assert outcome["code"] == "CAPABILITY_AVAILABLE"
    session.commit()
    assert "user.info.stats" in account.scopes_json
    capability = {row.capability: row.state for row in CapabilityService(session).current(account.id)}
    assert capability["tiktok.read_insights"] is CapabilityState.AVAILABLE


def test_consent_responses_never_leak_a_secret(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_read_engagement"],
    )
    session.commit()
    seed_credential(session, vault, account, token="OLD-ACCESS-TOKEN-SECRET")
    session.commit()
    secrets = ["OLD-ACCESS-TOKEN-SECRET", "app-secret-xyz"]
    plan = ConsentService(session, vault).plan(account).to_dict()
    started = start_consent(session, vault, account)
    assert_no_secret_in(plan, secrets)
    assert_no_secret_in(started, secrets)


def test_upgrade_session_records_the_full_upgrade_context(session, vault):
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    )
    session.commit()
    ref = seed_credential(session, vault, account)
    session.commit()
    start_consent(session, vault, account)
    row = latest_session(session, account)
    upgrade = row.result_json["upgrade"]
    assert upgrade["account_id"] == account.id
    assert upgrade["provider_account_id"] == "1234567890"
    assert upgrade["login_path"] == "facebook_login"
    assert upgrade["existing_credential_ref"] == ref
    assert upgrade["existing_scopes"] == list(account.scopes_json)
    assert upgrade["missing_scopes"] == ["read_insights"]
    assert row.account_id == account.id
    assert row.consumed_at is None
    assert row.status == "pending"


def test_setup_error_scope_is_never_requested(session, vault):
    """A scope outside the route surface can never reach the provider, even by accident."""
    run(make_app_config(session, vault, app_mode="live"))
    account = make_account(
        session,
        platform=Platform.FACEBOOK,
        route=OAuthRoute.FACEBOOK_LOGIN,
        provider_account_id="1234567890",
        account_type=AccountType.FACEBOOK_PAGE,
        scopes=["pages_read_engagement"],
    )
    session.commit()
    assert "ads_management" not in account_consent_scope_set(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN)
    started = start_consent(session, vault, account)
    assert "ads_management" not in started["scopes_requested"]


# ------------------------------------------------------------------ API


def test_consent_endpoints_require_authentication(unauth_client):
    assert unauth_client.get("/analytics/consent").status_code == 401
    assert unauth_client.get("/analytics/consent/whatever").status_code == 401
    assert unauth_client.post("/analytics/consent/whatever", json={}).status_code == 401


def test_consent_plan_for_an_unknown_account_is_404(client):
    response = client.get("/analytics/consent/01JNOSUCHACCOUNT0000000000")
    assert response.status_code == 404
    assert response.json()["code"] == "ACCOUNT_NOT_FOUND"


def test_consent_request_rejects_a_client_supplied_scope_list(client):
    response = client.post("/analytics/consent/whatever", json={"scopes": ["read_insights", "ads_management"]})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
