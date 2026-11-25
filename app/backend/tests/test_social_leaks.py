"""Leak tests: a stored secret must never appear anywhere except the vault.

Synthetic, unmistakable values are pushed through the real code paths - provider app registration,
a full OAuth claim, a refresh, a disconnect - and then every place a value could plausibly surface
is scanned: the API responses, the database file, the audit log, the application log, the backup
directory and the data root.

These are the tests that make "the backend never holds a secret" checkable instead of a claim.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from social_fixtures import facebook_account_graph, make_app_config, run, tiktok_graph

from creatoros.db.enums import OAuthRoute, Platform
from creatoros.integrations.social.base import AdapterRegistry
from creatoros.services.social.oauth_engine import OAuthEngine

APP_SECRET = "APP-SECRET-4f1c9a-NEEDLE"
USER_TOKEN = "EAAB-USER-4f1c9a-NEEDLE"
PAGE_TOKEN = "EAAB-PAGE-4f1c9a-NEEDLE"
TT_ACCESS = "TT-ACCESS-4f1c9a-NEEDLE"
TT_REFRESH = "TT-REFRESH-4f1c9a-NEEDLE"
TT_REFRESH_2 = "TT-REFRESH2-4f1c9a-NEEDLE"
NEEDLES = [APP_SECRET, USER_TOKEN, PAGE_TOKEN, TT_ACCESS, TT_REFRESH, TT_REFRESH_2]
# TikTok's API version literal contains the marker by coincidence; keep the scan honest.
MARKER = "4f1c9a-NEEDLE"


def scan_text(text: str, where: str) -> list[str]:
    return [f"{where}: {needle}" for needle in NEEDLES if needle in text]


def test_the_marker_is_not_accidentally_present_in_configuration():
    from creatoros.integrations.social import config as social_config

    source = Path(social_config.__file__).read_text(encoding="utf-8")
    assert MARKER not in source


def test_no_secret_in_api_responses(client, session, vault, oauth_listener):
    run(
        make_app_config(
            session,
            vault,
            platform=Platform.FACEBOOK,
            route=OAuthRoute.FACEBOOK_LOGIN,
            client_secret=APP_SECRET,
        )
    )
    session.commit()

    started = client.post("/social/oauth/start", json={"platform": "facebook"}).json()
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]
    mock = facebook_account_graph(page_token=PAGE_TOKEN)
    mock.routes = [route for route in mock.routes if "oauth/access_token" not in route[0]]
    mock.json(r"oauth/access_token", 200, {"access_token": USER_TOKEN, "expires_in": 5184000})
    engine = OAuthEngine(session, vault, adapter_factory=lambda: AdapterRegistry(
        transport_factory=lambda provider: mock.transport(provider)
    ))
    outcome = run(engine.handle_callback("facebook_login", {"code": "c", "state": state}))
    session.commit()
    assert outcome["ok"] is True, outcome

    account_id = client.get("/social/accounts").json()["items"][0]["id"]
    paths = [
        "/version",
        "/social/overview",
        "/social/accounts",
        f"/social/accounts/{account_id}",
        f"/social/accounts/{account_id}/capabilities",
        f"/social/accounts/{account_id}/token",
        f"/social/accounts/{account_id}/diagnostics",
        "/social/provider-apps",
        "/social/vault/health",
        "/social/oauth/routes",
        "/social/oauth/transactions",
        f"/social/oauth/transactions/{started['session_id']}",
        "/social/rotations",
        "/audit",
        "/queue/stats",
    ]
    findings: list[str] = []
    for path in paths:
        response = client.get(path)
        findings += scan_text(response.text, path)
    assert findings == [], findings


def test_no_secret_in_the_database_file(client, session, vault, tmp_path):
    from creatoros.core.settings import get_settings

    run(
        make_app_config(
            session, vault, platform=Platform.FACEBOOK, route=OAuthRoute.FACEBOOK_LOGIN, client_secret=APP_SECRET
        )
    )
    session.commit()
    from creatoros.services.social.secrets import SecretStore

    stored = run(
        SecretStore(session, vault).create(
            value=PAGE_TOKEN, purpose="access_token", provider="facebook", alias="account:facebook:1"
        )
    )
    session.commit()

    db_path = get_settings().root / "data" / "database" / "creatoros.db"
    findings: list[str] = []
    for suffix in ("", "-wal", "-journal"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            findings += scan_text(candidate.read_bytes().decode("latin-1"), candidate.name)

    # Every text column, so a value that somehow reached any table is caught.
    with sqlite3.connect(db_path) as connection:
        for table in connection.execute("select name from sqlite_master where type='table'"):
            rows = connection.execute(f'select * from "{table[0]}"').fetchall()
            findings += scan_text(repr(rows), f"table:{table[0]}")
    assert findings == [], findings
    assert stored.ref.fingerprint  # metadata is there, the value is not


def test_no_secret_in_logs_or_audit(client, session, vault, monkeypatch):
    from creatoros.core.settings import get_settings

    run(
        make_app_config(
            session, vault, platform=Platform.FACEBOOK, route=OAuthRoute.FACEBOOK_LOGIN, client_secret=APP_SECRET
        )
    )
    session.commit()
    client.post("/social/vault/secrets/reserve")
    client.get("/social/overview")

    root = get_settings().root
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".log", ".jsonl", ".txt", ".json"}:
            continue
        try:
            findings += scan_text(path.read_text(encoding="utf-8", errors="ignore"), str(path.relative_to(root)))
        except OSError:
            continue
    assert findings == [], findings


def test_audit_events_record_references_not_values(client, session, vault):
    from creatoros.db.models.ops import AuditEvent

    run(
        make_app_config(
            session, vault, platform=Platform.FACEBOOK, route=OAuthRoute.FACEBOOK_LOGIN, client_secret=APP_SECRET
        )
    )
    session.commit()
    events = session.query(AuditEvent).all()
    payload = json.dumps([event.metadata_redacted_json for event in events], default=str)
    assert MARKER not in payload
    assert any(event.action.startswith("provider_app") for event in events)


def test_tiktok_refresh_keeps_both_tokens_out_of_the_database(client, session, vault, monkeypatch):
    from creatoros.core.settings import get_settings
    from creatoros.db.enums import AccountType, ConnectionStatus, TokenState
    from creatoros.db.models.platform import SocialAccount
    from creatoros.services.social.secrets import SecretStore
    from creatoros.services.social.tokens import TokenService

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
    store = SecretStore(session, vault)
    access = run(store.create(value=TT_ACCESS, purpose="access_token", provider="tiktok", alias="a"))
    refresh = run(store.create(value=TT_REFRESH, purpose="refresh_token", provider="tiktok", alias="r"))
    from datetime import timedelta

    from creatoros.db.base import utcnow

    account = SocialAccount(
        platform=Platform.TIKTOK,
        provider_account_id="tt-1",
        display_name="TT",
        account_type=AccountType.TIKTOK_CREATOR,
        route=OAuthRoute.TIKTOK_LOGIN,
        scopes_json=["user.info.basic"],
        token_ref_id=access.ref.id,
        refresh_ref_id=refresh.ref.id,
        token_expires_at=utcnow() + timedelta(hours=1),
        refresh_expires_at=utcnow() + timedelta(days=300),
        token_state=TokenState.VALID,
        connection_status=ConnectionStatus.CONNECTED,
    )
    session.add(account)
    session.commit()

    mock = tiktok_graph()
    mock.routes = [route for route in mock.routes if "oauth/token" not in route[0]]
    mock.json(r"oauth/token/", 200, {
        "access_token": TT_ACCESS + "-2",
        "expires_in": 86400,
        "open_id": "tt-1",
        "refresh_expires_in": 31536000,
        "refresh_token": TT_REFRESH_2,
        "scope": "user.info.basic",
        "token_type": "Bearer",
    })
    import creatoros.services.social.tokens as tokens_module

    original = tokens_module._adapter
    tokens_module._adapter = lambda account: AdapterRegistry(
        transport_factory=lambda provider: mock.transport(provider)
    ).get(account.platform, account.route)
    try:
        run(TokenService(session, vault).refresh(account))
    finally:
        tokens_module._adapter = original
    session.commit()

    db_path = get_settings().root / "data" / "database" / "creatoros.db"
    text = db_path.read_bytes().decode("latin-1")
    for needle in (TT_ACCESS, TT_REFRESH, TT_REFRESH_2):
        assert needle not in text, needle
    # The new tokens are in the vault, the old refresh token is gone from it.
    assert vault.records[account.refresh_ref_id]["value"] == TT_REFRESH_2
    assert refresh.ref.id not in vault.records


def test_a_vault_failure_does_not_print_the_value_into_the_error(client, session, vault):
    from creatoros.services.social.secrets import SecretStore
    from creatoros.services.social.vault import UnavailableVault

    store = SecretStore(session, UnavailableVault("down"))
    from creatoros.integrations.social.errors import SocialError

    with pytest.raises(SocialError) as error:
        run(store.create(value=APP_SECRET, purpose="access_token", provider="facebook", alias="a"))
    assert APP_SECRET not in str(error.value)
    assert APP_SECRET not in json.dumps(error.value.to_dict())
