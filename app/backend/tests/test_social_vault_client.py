"""Vault client tests: the Python half of the broker protocol.

The real encryption lives in the Electron main process (see `electron/vault.test.cjs`). What is
verified here is that the backend:

* refuses to store anything when no broker is configured (no plaintext fallback exists);
* speaks the broker's protocol correctly (headers, purpose, one value per call);
* maps broker failures onto honest codes instead of pretending success;
* never writes a secret to the database, only metadata.
"""
from __future__ import annotations

import json

import httpx
import pytest
from social_fixtures import run

from creatoros.core.ids import new_ulid
from creatoros.db.models.platform import SecretRef
from creatoros.services.social.secrets import SecretStore
from creatoros.services.social.vault import (
    BrokerVault,
    UnavailableVault,
    VaultError,
    vault_from_env,
)

TOKEN = "broker-token-" + "a" * 40


def broker_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_no_broker_means_no_vault(monkeypatch):
    monkeypatch.delenv("CREATOROS_VAULT_URL", raising=False)
    monkeypatch.delenv("CREATOROS_VAULT_TOKEN", raising=False)
    vault = vault_from_env()
    assert isinstance(vault, UnavailableVault)
    assert vault.available is False
    with pytest.raises(VaultError) as error:
        run(vault.put(secret_id=new_ulid(), value="x", purpose="access_token", provider="facebook", alias="a"))
    assert error.value.code == "VAULT_UNAVAILABLE"


def test_url_without_token_is_still_unavailable(monkeypatch):
    monkeypatch.setenv("CREATOROS_VAULT_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("CREATOROS_VAULT_TOKEN", raising=False)
    vault = vault_from_env()
    assert isinstance(vault, UnavailableVault)
    assert "token" in vault.reason


def test_broker_put_sends_the_protocol_headers():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {key.lower(): value for key, value in request.headers.items()}
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "record": {
                    "id": seen["body"]["id"],
                    "alg": "dpapi-safeStorage",
                    "created_at": "2026-11-23T00:00:00Z",
                    "rotated_at": None,
                    "bytes": 12,
                    "fingerprint": "f" * 32,
                    "purpose": "access_token",
                    "provider": "tiktok",
                    "alias": "account:tiktok:1",
                    "version": 1,
                },
            },
        )

    vault = BrokerVault("http://127.0.0.1:1", TOKEN, client=broker_client(handler))
    secret_id = new_ulid()
    record = run(
        vault.put(
            secret_id=secret_id,
            value="super-secret-token",
            purpose="access_token",
            provider="tiktok",
            alias="account:tiktok:1",
        )
    )
    assert seen["headers"]["x-creatoros-vault"] == TOKEN
    assert seen["headers"]["x-creatoros-vault-proto"] == "1"
    assert seen["body"]["purpose"] == "access_token"
    assert record.fingerprint == "f" * 32
    assert record.alg == "dpapi-safeStorage"


def test_broker_rejection_becomes_a_vault_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "VAULT_UNAUTHORIZED", "message": "broker token rejected"})

    vault = BrokerVault("http://127.0.0.1:1", TOKEN, client=broker_client(handler))
    with pytest.raises(VaultError) as error:
        run(vault.get(new_ulid(), "access_token"))
    assert error.value.code == "VAULT_UNAUTHORIZED"


def test_unreachable_broker_is_reported_as_unreachable():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    vault = BrokerVault("http://127.0.0.1:1", TOKEN, client=broker_client(handler))
    with pytest.raises(VaultError) as error:
        run(vault.get(new_ulid(), "access_token"))
    assert error.value.code == "VAULT_UNREACHABLE"


def test_broker_refuses_an_unknown_purpose_locally():
    vault = BrokerVault("http://127.0.0.1:1", TOKEN, client=broker_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(VaultError) as error:
        run(vault.put(secret_id=new_ulid(), value="x", purpose="everything", provider=None, alias=None))
    assert error.value.code == "VAULT_INVALID_PURPOSE"


def test_secret_store_writes_metadata_only(session, vault):
    store = SecretStore(session, vault)
    stored = run(
        store.create(
            value="EAAB-secret-value",
            purpose="access_token",
            provider="facebook",
            alias="account:facebook:1",
        )
    )
    session.commit()
    ref = session.get(SecretRef, stored.ref.id)
    assert ref is not None
    assert ref.purpose == "access_token"
    assert ref.status.value == "active"
    # The fingerprint is a digest of the value, not the value, and the vault holds the value.
    assert "EAAB-secret-value" not in (ref.fingerprint or "")
    assert vault.records[ref.id]["value"] == "EAAB-secret-value"


def test_secret_store_refuses_to_create_without_a_vault(session):
    store = SecretStore(session, UnavailableVault("no broker"))
    from creatoros.integrations.social.errors import SocialError

    with pytest.raises(SocialError) as error:
        run(store.create(value="x", purpose="access_token", provider="facebook", alias="a"))
    assert error.value.code == "VAULT_UNAVAILABLE"
    assert session.query(SecretRef).count() == 0


def test_lost_vault_record_marks_the_reference_unavailable(session, vault):
    store = SecretStore(session, vault)
    stored = run(store.create(value="token", purpose="access_token", provider="tiktok", alias="a"))
    session.commit()
    vault.fail_get.add(stored.ref.id)
    from creatoros.integrations.social.errors import SocialError

    with pytest.raises(SocialError) as error:
        run(store.read(stored.ref, "access_token"))
    assert error.value.code == "CREDENTIAL_UNAVAILABLE"
    session.refresh(stored.ref)
    assert stored.ref.status.value == "unavailable"
    assert "decrypt" in (stored.ref.unavailable_reason or "")


def test_rotating_a_secret_keeps_the_same_reference(session, vault):
    store = SecretStore(session, vault)
    stored = run(store.create(value="first", purpose="provider_app_secret", provider="facebook", alias="a"))
    session.commit()
    run(store.rotate(stored.ref, "second"))
    session.commit()
    assert vault.records[stored.ref.id]["value"] == "second"
    assert stored.ref.rotated_at is not None


def test_revoking_a_secret_removes_the_blob_and_keeps_the_row(session, vault):
    store = SecretStore(session, vault)
    stored = run(store.create(value="bye", purpose="access_token", provider="facebook", alias="a"))
    session.commit()
    removed = run(store.revoke(stored.ref, reason="test"))
    session.commit()
    assert removed is True
    assert stored.ref.id not in vault.records
    assert session.get(SecretRef, stored.ref.id).status.value == "revoked"


def test_secret_summary_counts_states(session, vault):
    store = SecretStore(session, vault)
    run(store.create(value="a", purpose="access_token", provider="facebook", alias="a"))
    second = run(store.create(value="b", purpose="access_token", provider="facebook", alias="b"))
    session.commit()
    run(store.revoke(second.ref, reason="test"))
    session.commit()
    summary = store.summary()
    assert summary["total"] == 2
    assert summary["active"] == 1
    assert summary["revoked"] == 1
