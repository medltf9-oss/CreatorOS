"""Account service tests: claims, duplicates, labels, disconnects and no-merge behaviour."""
from __future__ import annotations

import pytest
from social_fixtures import facebook_account_graph, make_app_config, run

from creatoros.db.enums import AccountType, ConnectionStatus, OAuthRoute, Platform, TokenState
from creatoros.db.models.platform import SecretRef, SocialAccount
from creatoros.integrations.social.account_discovery import plan_discovery
from creatoros.integrations.social.base import DiscoveredAccount, TokenBundle
from creatoros.integrations.social.errors import SocialError
from creatoros.services.social.accounts import AccountService


def discovered(provider_id="1234567890", *, account_type=AccountType.FACEBOOK_PAGE, **overrides):
    base = {
        "provider_account_id": provider_id,
        "account_type": account_type,
        "display_name": "Test Page",
        "username": "testpage",
        "avatar_url": "https://cdn.example/a.png",
        "access_token": "EAAB-page-token",
        "expires_at": None,
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        "tasks": ["CREATE_CONTENT"],
        "meta": {"category": "Creator"},
    }
    base.update(overrides)
    return DiscoveredAccount(**base)


def bundle(**overrides):
    base = {
        "access_token": "EAAB-user-token",
        "token_type": "bearer",
        "expires_at": None,
        "scopes": ["pages_show_list"],
        "meta": {"scope_source": "token_response"},
    }
    base.update(overrides)
    return TokenBundle(**base)


def prepare(session, vault, **kwargs):
    config = run(make_app_config(session, vault, **kwargs))
    session.commit()
    return config


def claim(session, vault, config, account=None, **kwargs):
    service = AccountService(session, vault)
    return run(
        service.claim(
            platform=Platform.FACEBOOK,
            route=OAuthRoute.FACEBOOK_LOGIN,
            discovered=account or discovered(),
            bundle=kwargs.pop("bundle", bundle()),
            app_config=config,
            granted_scopes=kwargs.pop("granted_scopes", ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]),
            token_state=TokenState.VALID,
            expires_at=None,
            refresh_expires_at=None,
            scope_source="token_response",
            api_version="v23.0",
        )
    )


def test_claim_creates_an_account_and_stores_the_token_in_the_vault(session, vault):
    config = prepare(session, vault)
    result = claim(session, vault, config)
    session.commit()
    assert result.created is True
    account = result.account
    assert account.connection_status == ConnectionStatus.CONNECTED
    assert account.route == OAuthRoute.FACEBOOK_LOGIN
    assert vault.records[account.token_ref_id]["value"] == "EAAB-page-token"
    assert session.get(SecretRef, account.token_ref_id).fingerprint


def test_reconnecting_updates_in_place_and_revokes_the_old_secret(session, vault):
    config = prepare(session, vault)
    first = claim(session, vault, config)
    session.commit()
    old_ref = first.account.token_ref_id
    second = claim(session, vault, config, discovered(display_name="Renamed Page"))
    session.commit()
    assert second.created is False
    assert second.account.id == first.account.id
    assert second.account.display_name == "Renamed Page"
    assert second.account.token_ref_id != old_ref
    assert old_ref not in vault.records
    assert session.get(SecretRef, old_ref).status.value == "revoked"
    assert session.query(SocialAccount).count() == 1


def test_a_claim_without_a_token_is_refused(session, vault):
    config = prepare(session, vault)
    with pytest.raises(SocialError) as error:
        claim(session, vault, config, discovered(access_token=None), bundle=bundle(access_token=""))
    assert error.value.code == "NO_ACCOUNT_TOKEN"
    assert session.query(SocialAccount).count() == 0


def test_labels_and_groups_are_user_owned(session, vault):
    config = prepare(session, vault)
    account = claim(session, vault, config).account
    session.commit()
    service = AccountService(session, vault)
    service.rename(account, label="Client A - main page", group="Client A")
    session.commit()
    assert account.internal_label == "Client A - main page"
    assert account.group_label == "Client A"
    # A reconnect must not overwrite the label the user chose.
    claim(session, vault, config, discovered(display_name="Changed by provider"))
    session.commit()
    assert account.internal_label == "Client A - main page"


def test_label_validation_rejects_overlong_values(session, vault):
    config = prepare(session, vault)
    account = claim(session, vault, config).account
    with pytest.raises(SocialError) as error:
        AccountService(session, vault).rename(account, label="x" * 201, group=None)
    assert error.value.code == "VALIDATION_ERROR"


def test_disconnect_revokes_the_secret_and_records_the_reason(session, vault):
    config = prepare(session, vault)
    account = claim(session, vault, config).account
    session.commit()
    service = AccountService(session, vault)
    result = run(service.disconnect(account, revoke_at_provider=False))
    session.commit()
    assert account.connection_status == ConnectionStatus.DISCONNECTED
    assert account.token_state == TokenState.REVOKED
    assert account.disconnected_at is not None
    assert account.token_ref_id not in vault.records
    assert result["revoked_at_provider"] is False


def test_disconnect_at_provider_uses_the_adapter(session, vault):
    mock = facebook_account_graph()
    config = prepare(session, vault)
    account = claim(session, vault, config).account
    session.commit()

    import creatoros.services.social.accounts as accounts_module

    original = accounts_module.AccountService._adapter
    accounts_module.AccountService._adapter = lambda self, account: __import__(
        "creatoros.integrations.social.facebook", fromlist=["FacebookAdapter"]
    ).FacebookAdapter(transport=mock.transport("facebook"))
    try:
        result = run(AccountService(session, vault).disconnect(account))
    finally:
        accounts_module.AccountService._adapter = original
    assert result["revoked_at_provider"] is True
    assert "confirmed" in result["detail"]


def test_token_problems_map_to_connection_status(session, vault):
    config = prepare(session, vault)
    account = claim(session, vault, config).account
    session.commit()
    service = AccountService(session, vault)
    service.mark_token_problem(account, TokenState.REAUTHORIZATION_REQUIRED, "expired")
    assert account.connection_status == ConnectionStatus.TOKEN_EXPIRED
    service.mark_token_problem(account, TokenState.CREDENTIAL_UNAVAILABLE, "vault lost it")
    assert account.connection_status == ConnectionStatus.UNKNOWN


def test_summary_counts_states(session, vault):
    config = prepare(session, vault)
    claim(session, vault, config)
    claim(session, vault, config, discovered("9876543210", display_name="Second"))
    session.commit()
    summary = AccountService(session, vault).summary()
    assert summary["total"] == 2
    assert summary["by_platform"]["facebook"]["connected"] == 2


def test_routes_by_provider_id_reports_the_login_path(session, vault):
    config = prepare(session, vault)
    claim(session, vault, config)
    session.commit()
    routes = AccountService(session, vault).routes_by_provider_id(Platform.FACEBOOK)
    assert routes["1234567890"] == "facebook_login"


# ------------------------------------------------------------------ discovery rules


def test_two_instagram_identities_are_kept_apart():
    first = discovered("ig-1", account_type=AccountType.INSTAGRAM_BUSINESS)
    second = discovered("ig-2", account_type=AccountType.INSTAGRAM_BUSINESS)
    plan = plan_discovery(Platform.INSTAGRAM, OAuthRoute.INSTAGRAM_LOGIN, [first, second])
    assert {account.provider_account_id for account in plan.claimed} == {"ig-1", "ig-2"}
    assert plan.duplicates == []


def test_same_username_on_two_ids_is_reported_not_merged():
    first = discovered("ig-1", account_type=AccountType.INSTAGRAM_BUSINESS, username="same")
    second = discovered("ig-2", account_type=AccountType.INSTAGRAM_BUSINESS, username="same")
    plan = plan_discovery(Platform.INSTAGRAM, OAuthRoute.INSTAGRAM_LOGIN, [first, second])
    assert len(plan.claimed) == 2
    assert any("maps to two provider ids" in note for note in plan.observations)


def test_duplicate_discovery_of_one_id_is_ignored():
    account = discovered("ig-1", account_type=AccountType.INSTAGRAM_BUSINESS)
    plan = plan_discovery(Platform.INSTAGRAM, OAuthRoute.INSTAGRAM_LOGIN, [account, account])
    assert len(plan.claimed) == 1
    assert any("appeared twice" in note for note in plan.observations)


def test_unsupported_account_types_are_reported_not_claimed():
    personal = discovered("p-1", account_type=AccountType.PERSONAL_UNSUPPORTED)
    plan = plan_discovery(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, [personal])
    assert plan.claimed == []
    assert plan.unsupported[0].provider_account_id == "p-1"


def test_a_second_login_path_is_flagged_as_a_duplicate_connection():
    account = discovered("ig-1", account_type=AccountType.INSTAGRAM_BUSINESS)
    plan = plan_discovery(
        Platform.INSTAGRAM,
        OAuthRoute.FACEBOOK_LOGIN,
        [account],
        existing={"ig-1": "instagram_login"},
    )
    assert plan.duplicates[0]["already_connected_via"] == "instagram_login"
    assert plan.duplicates[0]["new_route"] == "facebook_login"


def test_discovery_without_a_token_is_not_claimable():
    plan = plan_discovery(
        Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, [discovered(access_token=None)]
    )
    assert plan.usable == []
    assert any("cannot be claimed" in note for note in plan.observations)
