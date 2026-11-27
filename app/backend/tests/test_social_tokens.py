"""Token lifecycle tests: states, refresh locking, the rotation journal and crash recovery.

TikTok rotates its refresh token, so a half-finished refresh is a real failure mode. These tests
prove the service never guesses: it journals before the call, marks the account exactly once, and
resolves an interrupted rotation from stored facts.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from social_fixtures import make_app_config, run, tiktok_graph

from creatoros.db.base import utcnow
from creatoros.db.enums import AccountType, ConnectionStatus, OAuthRoute, Platform, RotationState, TokenState
from creatoros.db.models.platform import CredentialRotation, SecretRef, SocialAccount
from creatoros.integrations.social.base import AdapterRegistry
from creatoros.integrations.social.errors import SocialError
from creatoros.services.social.tokens import TokenService, compute_status


def make_account(session, vault, *, expires_in_hours: float | None = 48, refresh_days: float | None = 300, with_token=True):
    from creatoros.services.social.secrets import SecretStore

    ref = None
    refresh_ref = None
    if with_token:
        store = SecretStore(session, vault)
        stored = run(store.create(value="tt-access-1", purpose="access_token", provider="tiktok", alias="a"))
        ref = stored.ref
        stored_refresh = run(
            store.create(value="tt-refresh-1", purpose="refresh_token", provider="tiktok", alias="r")
        )
        refresh_ref = stored_refresh.ref
    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="tt-open-id-1",
        display_name="Test TikTok",
        account_type=AccountType.TIKTOK_CREATOR,
        route=OAuthRoute.TIKTOK_LOGIN,
        scopes_json=["user.info.basic", "video.list"],
        token_ref_id=ref.id if ref else None,
        refresh_ref_id=refresh_ref.id if refresh_ref else None,
        token_expires_at=utcnow() + timedelta(hours=expires_in_hours) if expires_in_hours else None,
        refresh_expires_at=utcnow() + timedelta(days=refresh_days) if refresh_days else None,
        token_state=TokenState.VALID,
        connection_status=ConnectionStatus.CONNECTED,
    )
    session.add(account)
    session.flush()
    return account


def service_for(session, vault, mock) -> TokenService:
    service = TokenService(session, vault)
    # The service builds its own adapter; point the registry at the mock.
    import creatoros.services.social.tokens as tokens_module

    tokens_module._adapter = lambda account: AdapterRegistry(  # type: ignore[assignment]
        transport_factory=lambda provider: mock.transport(provider)
    ).get(account.platform, account.route)
    return service


def prepare(session, vault, mock):
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


# ------------------------------------------------------------------ pure states


def test_valid_token_reports_valid():
    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="x",
        display_name="x",
        token_ref_id="01M8ZZZZZZZZZZZZZZZZZZZZZ",
        token_expires_at=utcnow() + timedelta(days=2),
        token_state=TokenState.VALID,
    )
    status = compute_status(account)
    assert status.state == TokenState.VALID
    assert status.can_use is True
    assert status.can_refresh is False


def test_expiring_soon_within_the_warning_window():
    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="x",
        display_name="x",
        token_ref_id="01M8ZZZZZZZZZZZZZZZZZZZZZ",
        refresh_ref_id="01M8ZZZZZZZZZZZZZZZZZZZZY",
        token_expires_at=utcnow() + timedelta(hours=3),
        refresh_expires_at=utcnow() + timedelta(days=100),
        token_state=TokenState.VALID,
    )
    status = compute_status(account)
    assert status.state == TokenState.EXPIRING_SOON
    assert status.can_refresh is True


def test_expired_without_refresh_needs_reauthorization():
    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="x",
        display_name="x",
        token_ref_id="01M8ZZZZZZZZZZZZZZZZZZZZZ",
        token_expires_at=utcnow() - timedelta(minutes=1),
        token_state=TokenState.VALID,
    )
    status = compute_status(account)
    assert status.state == TokenState.REAUTHORIZATION_REQUIRED
    assert status.can_use is False


def test_refresh_token_near_expiry_forces_reauthorization():
    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="x",
        display_name="x",
        token_ref_id="01M8ZZZZZZZZZZZZZZZZZZZZZ",
        refresh_ref_id="01M8ZZZZZZZZZZZZZZZZZZZZY",
        token_expires_at=utcnow() - timedelta(minutes=1),
        refresh_expires_at=utcnow() + timedelta(hours=2),
        token_state=TokenState.VALID,
    )
    status = compute_status(account)
    assert status.state == TokenState.REAUTHORIZATION_REQUIRED
    assert "reconnect" in (status.reason or "")


def test_missing_token_is_credential_unavailable():
    account = SocialAccount(platform=Platform.TIKTOK, provider_account_id="x", display_name="x")
    status = compute_status(account)
    assert status.state == TokenState.CREDENTIAL_UNAVAILABLE


# ------------------------------------------------------------------ refresh


def test_refresh_rotates_and_revokes_the_old_refresh_token(session, vault):
    mock = tiktok_graph()
    mock.routes = [route for route in mock.routes if "oauth/token" not in route[0]]
    mock.json(r"oauth/token/", 200, {
        "access_token": "tt-access-2",
        "expires_in": 86400,
        "open_id": "tt-open-id-1",
        "refresh_expires_in": 31536000,
        "refresh_token": "tt-refresh-2",
        "scope": "user.info.basic,video.list",
        "token_type": "Bearer",
    })
    prepare(session, vault, mock)
    account = make_account(session, vault)
    session.commit()
    old_refresh = account.refresh_ref_id
    old_access = account.token_ref_id

    service = service_for(session, vault, mock)
    status = run(service.refresh(account))
    session.commit()

    assert status.state == TokenState.VALID
    assert account.refresh_ref_id != old_refresh
    assert account.token_ref_id != old_access
    assert vault.records[account.refresh_ref_id]["value"] == "tt-refresh-2"
    # The provider invalidated the old refresh token, so we must not keep a live reference to it.
    assert old_refresh not in vault.records
    assert session.get(SecretRef, old_refresh).status.value == "revoked"
    rotation = session.query(CredentialRotation).one()
    assert rotation.state == RotationState.COMPLETED
    assert rotation.new_ref_id == account.token_ref_id


def test_refresh_failure_marks_the_account_and_the_journal(session, vault):
    mock = tiktok_graph()
    mock.routes = []
    mock.json(r"oauth/token/", 400, {"error": {"code": "invalid_grant", "message": "refresh expired"}})
    prepare(session, vault, mock)
    account = make_account(session, vault, expires_in_hours=-1)
    session.commit()
    service = service_for(session, vault, mock)
    with pytest.raises(SocialError) as error:
        run(service.refresh(account))
    session.commit()
    assert error.value.code == "REAUTHORIZATION_REQUIRED"
    assert account.token_state == TokenState.REAUTHORIZATION_REQUIRED
    rotation = session.query(CredentialRotation).one()
    assert rotation.state == RotationState.FAILED
    assert rotation.error_code == "REAUTHORIZATION_REQUIRED"
    # The journal is cleaned up once the rotation is resolved.
    assert rotation.journal_ref_id is None or rotation.journal_ref_id not in vault.records


def test_a_second_refresh_is_refused_while_one_is_running(session, vault):
    mock = tiktok_graph()
    prepare(session, vault, mock)
    account = make_account(session, vault)
    session.add(
        CredentialRotation(
            account_id=account.id,
            provider=Platform.TIKTOK,
            kind="refresh",
            state=RotationState.IN_PROGRESS,
            lock_token="lock",
            started_at=utcnow(),
        )
    )
    session.commit()
    service = service_for(session, vault, mock)
    with pytest.raises(SocialError) as error:
        run(service.refresh(account))
    assert error.value.code == "REFRESH_IN_PROGRESS"


def test_a_stale_lock_is_abandoned_and_the_refresh_proceeds(session, vault):
    mock = tiktok_graph()
    mock.routes = [route for route in mock.routes if "oauth/token" not in route[0]]
    mock.json(r"oauth/token/", 200, {
        "access_token": "tt-access-3",
        "expires_in": 86400,
        "open_id": "tt-open-id-1",
        "refresh_expires_in": 31536000,
        "refresh_token": "tt-refresh-3",
        "scope": "user.info.basic",
        "token_type": "Bearer",
    })
    prepare(session, vault, mock)
    account = make_account(session, vault)
    stale = CredentialRotation(
        account_id=account.id,
        provider=Platform.TIKTOK,
        kind="refresh",
        state=RotationState.IN_PROGRESS,
        lock_token="stale",
        started_at=utcnow() - timedelta(minutes=30),
    )
    session.add(stale)
    session.commit()
    service = service_for(session, vault, mock)
    run(service.refresh(account))
    session.commit()
    session.refresh(stale)
    assert stale.state == RotationState.ABANDONED
    assert stale.error_code == "STALE_LOCK"


def test_refresh_requires_a_refresh_token(session, vault):
    mock = tiktok_graph()
    prepare(session, vault, mock)
    account = make_account(session, vault, refresh_days=None)
    account.refresh_ref_id = None
    session.commit()
    service = service_for(session, vault, mock)
    with pytest.raises(SocialError) as error:
        run(service.refresh(account))
    assert error.value.code == "REFRESH_NOT_SUPPORTED"


# ------------------------------------------------------------------ recovery


def test_interrupted_rotation_with_a_usable_token_is_abandoned(session, vault):
    mock = tiktok_graph()
    prepare(session, vault, mock)
    account = make_account(session, vault)
    rotation = CredentialRotation(
        account_id=account.id,
        provider=Platform.TIKTOK,
        kind="refresh",
        state=RotationState.IN_PROGRESS,
        lock_token="crashed",
        started_at=utcnow() - timedelta(minutes=1),
    )
    session.add(rotation)
    session.commit()
    result = service_for(session, vault, mock).resolve_interrupted(account)
    session.commit()
    assert result["resolved"]
    session.refresh(rotation)
    assert rotation.state == RotationState.ABANDONED
    assert rotation.error_code == "INTERRUPTED_BUT_USABLE"


def test_interrupted_rotation_without_a_token_requires_reauthorization(session, vault):
    mock = tiktok_graph()
    prepare(session, vault, mock)
    account = make_account(session, vault, expires_in_hours=-1)
    rotation = CredentialRotation(
        account_id=account.id,
        provider=Platform.TIKTOK,
        kind="refresh",
        state=RotationState.IN_PROGRESS,
        lock_token="crashed",
        started_at=utcnow() - timedelta(minutes=1),
    )
    session.add(rotation)
    session.commit()
    service_for(session, vault, mock).resolve_interrupted(account)
    session.commit()
    assert rotation.state == RotationState.FAILED
    assert account.token_state == TokenState.REAUTHORIZATION_REQUIRED


def test_access_token_refreshes_before_use_when_expiring(session, vault):
    mock = tiktok_graph()
    mock.routes = [route for route in mock.routes if "oauth/token" not in route[0]]
    mock.json(r"oauth/token/", 200, {
        "access_token": "tt-access-refreshed",
        "expires_in": 86400,
        "open_id": "tt-open-id-1",
        "refresh_expires_in": 31536000,
        "refresh_token": "tt-refresh-4",
        "scope": "user.info.basic",
        "token_type": "Bearer",
    })
    prepare(session, vault, mock)
    account = make_account(session, vault, expires_in_hours=-2)
    session.commit()
    service = service_for(session, vault, mock)
    token = run(service.access_token(account))
    session.commit()
    assert token == "tt-access-refreshed"
    assert account.token_state == TokenState.VALID


def test_access_token_refuses_a_dead_credential(session, vault):
    mock = tiktok_graph()
    prepare(session, vault, mock)
    account = make_account(session, vault)
    account.token_state = TokenState.REVOKED
    session.commit()
    service = service_for(session, vault, mock)
    with pytest.raises(SocialError) as error:
        run(service.access_token(account))
    assert error.value.code == "REAUTHORIZATION_REQUIRED"


def test_pending_rotations_lists_open_rows(session, vault):
    mock = tiktok_graph()
    prepare(session, vault, mock)
    account = make_account(session, vault)
    session.add(
        CredentialRotation(
            account_id=account.id,
            provider=Platform.TIKTOK,
            kind="refresh",
            state=RotationState.PENDING,
            lock_token="l",
            started_at=utcnow(),
        )
    )
    session.commit()
    assert len(service_for(session, vault, mock).pending_rotations()) == 1
