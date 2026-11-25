"""The renderer-seals-it path: reserve an id, seal in the vault, adopt the metadata, link it.

The point of these tests is the claim they protect: a provider app secret can reach the vault
without ever appearing in an HTTP body to Python. That only holds if adoption refuses records it
cannot actually read, and if linking revokes the previous credential *after* the new one is in
place.
"""
from __future__ import annotations

import pytest
from social_fixtures import assert_no_secret_in, make_app_config, run

from creatoros.db.enums import OAuthRoute, Platform
from creatoros.db.models.platform import ProviderAppConfig, SecretRef
from creatoros.integrations.social.errors import SocialError
from creatoros.services.social.apps import ProviderAppService
from creatoros.services.social.secrets import SecretStore
from creatoros.services.social.vault import UnavailableVault

SEALED = "rendered-in-the-shell-secret"


@pytest.fixture()
def sealed(vault, session):
    """A record the shell already wrote, with no metadata row pointing at it."""
    secret_id = "01JVWZSEALEDRENDERER000001"
    run(
        vault.put(
            secret_id=secret_id,
            value=SEALED,
            purpose="provider_app_secret",
            provider="facebook",
            alias="app:facebook:facebook_login",
        )
    )
    assert session.get(SecretRef, secret_id) is None
    return secret_id


def test_adopt_records_metadata_for_a_sealed_record(session, vault, sealed):
    ref = run(
        SecretStore(session, vault).adopt(
            sealed, purpose="provider_app_secret", provider="facebook", alias="app:facebook:facebook_login"
        )
    )
    session.commit()
    stored = session.get(SecretRef, sealed)
    assert stored is not None
    assert stored.status.value == "active"
    assert stored.purpose == "provider_app_secret"
    # The fingerprint comes from the vault's digest of the ciphertext, not from the value.
    assert stored.fingerprint
    assert SEALED not in (stored.fingerprint or "")
    assert stored.byte_size == len(SEALED)
    assert ref.id == sealed


def test_adopt_refuses_an_id_the_vault_does_not_hold(session, vault):
    with pytest.raises(SocialError) as error:
        run(SecretStore(session, vault).adopt("01JVWZMISSINGRECORD0000001", purpose="provider_app_secret", provider="facebook", alias="a"))
    assert error.value.code == "VAULT_NOT_FOUND"
    assert session.query(SecretRef).count() == 0


def test_adopt_refuses_a_purpose_the_record_does_not_have(session, vault):
    run(vault.put(secret_id="01JVWZWRONGKIND0000000001", value="x", purpose="access_token", provider="facebook", alias="a"))
    with pytest.raises(SocialError) as error:
        run(SecretStore(session, vault).adopt("01JVWZWRONGKIND0000000001", purpose="provider_app_secret", provider="facebook", alias="a"))
    assert error.value.code == "VAULT_PURPOSE_MISMATCH"


def test_adopt_refuses_an_unsupported_purpose(session, vault, sealed):
    with pytest.raises(SocialError) as error:
        run(SecretStore(session, vault).adopt(sealed, purpose="social_password", provider="facebook", alias="a"))
    assert error.value.code == "VAULT_INVALID_PURPOSE"


def test_adopt_is_idempotent_for_the_same_record(session, vault, sealed):
    store = SecretStore(session, vault)
    first = run(store.adopt(sealed, purpose="provider_app_secret", provider="facebook", alias="a"))
    second = run(store.adopt(sealed, purpose="provider_app_secret", provider="facebook", alias="a"))
    assert first.id == second.id
    assert session.query(SecretRef).count() == 1


def test_adopt_without_a_vault_is_refused(session, sealed):
    with pytest.raises(SocialError) as error:
        run(SecretStore(session, UnavailableVault("no broker")).adopt(sealed, purpose="provider_app_secret", provider="facebook", alias="a"))
    assert error.value.code == "VAULT_UNAVAILABLE"


def test_linking_a_sealed_secret_makes_the_app_usable(session, vault, sealed):
    config = run(make_app_config(session, vault, client_secret=None))
    session.commit()
    assert config.client_secret_ref_id is None
    service = ProviderAppService(session, vault)
    run(SecretStore(session, vault).adopt(sealed, purpose="provider_app_secret", provider="facebook", alias="a"))
    linked = run(service.attach_secret_ref(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, sealed))
    session.commit()
    assert linked.client_secret_ref_id == sealed
    described = service.describe(session.get(ProviderAppConfig, linked.id))
    assert described["has_client_secret"] is True
    assert described["client_secret_status"] == "active"


def test_linking_revokes_the_previous_secret_after_the_new_one(session, vault):
    config = run(make_app_config(session, vault, client_secret="the-old-one"))
    session.commit()
    previous = config.client_secret_ref_id
    assert previous
    new_id = "01JVWZNEWERSEALED0000000001"
    run(vault.put(secret_id=new_id, value="the-new-one", purpose="provider_app_secret", provider="facebook", alias="a"))
    store = SecretStore(session, vault)
    run(store.adopt(new_id, purpose="provider_app_secret", provider="facebook", alias="a"))
    service = ProviderAppService(session, vault)
    linked = run(service.attach_secret_ref(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, new_id))
    session.commit()
    assert linked.client_secret_ref_id == new_id
    old_ref = session.get(SecretRef, previous)
    assert old_ref.status.value == "revoked"
    assert previous not in vault.records
    assert new_id in vault.records


def test_linking_refuses_a_secret_that_is_not_an_app_secret(session, vault):
    run(make_app_config(session, vault, client_secret=None))
    session.commit()
    run(vault.put(secret_id="01JVWZTOKENNOTAPP000000001", value="t", purpose="access_token", provider="facebook", alias="a"))
    store = SecretStore(session, vault)
    run(store.adopt("01JVWZTOKENNOTAPP000000001", purpose="access_token", provider="facebook", alias="a"))
    with pytest.raises(SocialError) as error:
        run(ProviderAppService(session, vault).attach_secret_ref(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, "01JVWZTOKENNOTAPP000000001"))
    assert error.value.code == "VAULT_PURPOSE_MISMATCH"


def test_deleting_an_app_revokes_its_secret(session, vault):
    """A deleted registration must not leave an encrypted credential behind."""
    config = run(make_app_config(session, vault, client_secret="delete-me-please"))
    session.commit()
    secret_id = config.client_secret_ref_id
    assert secret_id in vault.records
    removed = run(ProviderAppService(session, vault).delete(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN))
    session.commit()
    assert removed is True
    assert secret_id not in vault.records
    assert session.get(SecretRef, secret_id).status.value == "revoked"


def test_api_delete_removes_the_vault_record(client, vault):
    created = client.put(
        "/social/provider-apps/tiktok/tiktok_login",
        json={"client_id": "tt-123", "client_secret": "api-delete-secret", "app_mode": "development"},
    )
    assert created.status_code == 200, created.text
    assert len(vault.records) == 1
    deleted = client.delete("/social/provider-apps/tiktok/tiktok_login")
    assert deleted.status_code == 204
    assert vault.records == {}


def test_linking_an_unadopted_id_is_refused(session, vault):
    run(make_app_config(session, vault, client_secret=None))
    session.commit()
    with pytest.raises(SocialError) as error:
        run(ProviderAppService(session, vault).attach_secret_ref(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, "01JVWZGHOST000000000000001"))
    assert error.value.code == "VAULT_NOT_FOUND"


def test_linking_needs_an_app_registration(session, vault, sealed):
    run(SecretStore(session, vault).adopt(sealed, purpose="provider_app_secret", provider="facebook", alias="a"))
    with pytest.raises(SocialError) as error:
        run(ProviderAppService(session, vault).attach_secret_ref(Platform.TIKTOK, OAuthRoute.TIKTOK_LOGIN, sealed))
    assert error.value.code == "APP_NOT_CONFIGURED"


# ------------------------------------------------------------------ over HTTP


def test_api_adopts_and_links_without_a_secret_in_the_body(client, vault, sealed, session):
    # The app is registered with a client id only: no secret field at all.
    created = client.put(
        "/social/provider-apps/facebook/facebook_login",
        json={"client_id": "app-999", "app_mode": "development"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["has_client_secret"] is False

    adopted = client.post(
        f"/social/vault/secrets/{sealed}/adopt",
        json={"purpose": "provider_app_secret", "provider": "facebook", "alias": "app:facebook:facebook_login"},
    )
    assert adopted.status_code == 200, adopted.text
    body = adopted.json()
    assert body["id"] == sealed
    assert body["status"] == "active"
    assert body["bytes"] == len(SEALED)
    assert body["fingerprint"]
    assert_no_secret_in(body, [SEALED])

    linked = client.put(
        "/social/provider-apps/facebook/facebook_login/secret-ref", json={"secret_id": sealed}
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["has_client_secret"] is True
    assert_no_secret_in(linked.json(), [SEALED])


def test_api_refuses_to_adopt_a_record_it_cannot_read(client, vault):
    response = client.post(
        "/social/vault/secrets/01JVWZNOTINVAULT00000000001/adopt",
        json={"purpose": "provider_app_secret", "provider": "facebook"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "VAULT_NOT_FOUND"


def test_api_reports_the_vault_being_unavailable_as_503(client, monkeypatch):
    from creatoros.api.routes import social as social_routes
    from creatoros.services.social.vault import UnavailableVault

    monkeypatch.setattr(social_routes, "_VAULT", UnavailableVault("no shell"))
    response = client.post(
        "/social/vault/secrets/01JVWZANYID0000000000000001/adopt",
        json={"purpose": "provider_app_secret", "provider": "facebook"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "VAULT_UNAVAILABLE"
