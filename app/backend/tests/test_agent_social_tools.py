"""Social agent-tool tests: metadata only, no credential, no publishing.

The two Phase 6 tools exist so the 111-agent workforce can answer "which accounts do we have and
what may they do?" without any agent ever holding a token. These tests check the answer, the
grant discipline and the absence of anything that could publish or read a secret.
"""
from __future__ import annotations

import inspect
import json

import pytest

from creatoros.db.enums import (
    AccountType,
    CapabilityState,
    ConnectionStatus,
    OAuthRoute,
    Platform,
    TokenState,
)
from creatoros.db.models.platform import AccountCapability, SocialAccount
from creatoros.services import toolbox
from creatoros.services.social.agent_grants import (
    GRANT_KEY,
    SOCIAL_TOOL_GRANTS,
    apply_social_tool_grants,
    social_tools_by_department,
)
from creatoros.services.toolbox import TOOLS_BY_ID, ToolExecutor

SECRET_VALUE = "EAAB-page-token-NEEDLE"


def make_account(session, *, account_id="01M80SOCIAL00000000000001", platform=Platform.FACEBOOK, with_capabilities=True):
    account = SocialAccount(
        id=account_id,
        platform=platform,
        provider_account_id=f"provider-{account_id[-2:]}",
        display_name="Test Page",
        account_type=AccountType.FACEBOOK_PAGE,
        route=OAuthRoute.FACEBOOK_LOGIN,
        scopes_json=["pages_show_list", "pages_manage_posts"],
        connection_status=ConnectionStatus.CONNECTED,
        token_state=TokenState.VALID,
    )
    session.add(account)
    session.flush()
    if with_capabilities:
        session.add(
            AccountCapability(
                account_id=account.id,
                capability="facebook.page.publish_video",
                state=CapabilityState.AVAILABLE,
                reason="every required scope is granted",
                required_json=["pages_manage_posts", "pages_read_engagement"],
                granted_json=["pages_manage_posts"],
                missing_json=[],
                verified_at=__import__("creatoros.db.base", fromlist=["utcnow"]).utcnow(),
                api_version="v23.0",
            )
        )
        session.flush()
    return account


def execute(session, tool_id, payload, *, agent="publishing_director"):
    # The executor authorises against the database registry, so mirror the boot-time seeding.
    from creatoros.services.toolbox import ensure_tools_seeded

    ensure_tools_seeded(session)
    executor = ToolExecutor(session, enforce_grant=False)
    return executor.execute(tool_id, agent_id=agent, payload=payload)


def test_both_social_tools_are_registered_and_read_only():
    for tool_id in ("social.accounts.list", "social.capabilities.read"):
        spec = TOOLS_BY_ID[tool_id]
        assert spec.category == "read"
        assert spec.approval.value == "none"
    # No tool in the whole surface may be able to publish in Phase 6.
    assert "publish.post" not in TOOLS_BY_ID


def test_account_listing_returns_metadata_and_no_credential(session):
    make_account(session)
    outcome = execute(session, "social.accounts.list", {})
    assert outcome.ok is True, outcome.error
    payload = json.dumps(outcome.output)
    assert "Test Page" in payload
    assert "publish_video" in payload
    assert SECRET_VALUE not in payload
    assert "token_ref_id" not in payload
    assert "secret" not in payload.lower().replace("no credential", "")


def test_capability_reading_explains_the_state(session):
    account = make_account(session)
    outcome = execute(session, "social.capabilities.read", {"account_id": account.id})
    assert outcome.ok is True, outcome.error
    entry = outcome.output["capabilities"][0]
    assert entry["state"] == "available"
    assert entry["reason"]
    assert entry["required_scopes"] == ["pages_manage_posts", "pages_read_engagement"]
    assert entry["api_version"] == "v23.0"


def test_capability_reading_can_filter_by_platform(session):
    make_account(session)
    make_account(session, account_id="01M80SOCIAL00000000000002", platform=Platform.TIKTOK, with_capabilities=False)
    session.add(
        AccountCapability(
            account_id="01M80SOCIAL00000000000002",
            capability="tiktok.direct_post",
            state=CapabilityState.REQUIRES_DEVELOPER_APPROVAL,
            reason="video.publish is not granted",
            required_json=["video.publish"],
            granted_json=["user.info.basic"],
            missing_json=["video.publish"],
            verified_at=__import__("creatoros.db.base", fromlist=["utcnow"]).utcnow(),
        )
    )
    session.flush()
    outcome = execute(session, "social.capabilities.read", {"platform": "tiktok"})
    assert outcome.ok is True
    assert outcome.output["count"] == 1
    assert outcome.output["capabilities"][0]["capability"] == "tiktok.direct_post"


def test_account_listing_filters_by_requested_capability(session):
    make_account(session)
    make_account(session, account_id="01M80SOCIAL00000000000003", with_capabilities=False)
    outcome = execute(session, "social.accounts.list", {"capability": "facebook.page.publish_video"})
    assert outcome.ok is True
    assert outcome.output["count"] == 1
    assert outcome.output["accounts"][0]["requested_capability"] == "available"


def test_an_unknown_account_id_is_a_validation_error(session):
    outcome = execute(session, "social.capabilities.read", {"account_id": "01M80MISSING0000000000000"})
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_INPUT"


def test_unknown_fields_are_rejected_by_the_input_schema(session):
    """A caller cannot smuggle a field the tool does not declare."""
    from creatoros.services.toolbox import ToolInvalidInput

    with pytest.raises(ToolInvalidInput) as error:
        execute(session, "social.accounts.list", {"include_tokens": True})
    assert "include_tokens" in str(error.value)


def test_no_social_tool_accepts_a_secret_field():
    for tool_id in ("social.accounts.list", "social.capabilities.read"):
        schema = TOOLS_BY_ID[tool_id].input_model.model_json_schema()
        properties = set((schema.get("properties") or {}).keys())
        assert not {name for name in properties if "token" in name or "secret" in name}


def test_the_handlers_cannot_reach_the_vault_or_the_network():
    source = inspect.getsource(toolbox.social_accounts_list) + inspect.getsource(
        toolbox.social_capabilities_read
    )
    # No import, no vault call, no token read: the words only appear in the human-readable note
    # that explains exactly that.
    for forbidden in ("import httpx", "import requests", "secrets.read", "access_token", "refresh_token"):
        assert forbidden not in source, forbidden
    assert "vault" in source  # the note says the credential never leaves it
    assert "SecretStore" not in source
    assert "TokenService" not in source


def _seed(session):
    from creatoros.services.registry import AgentRegistry
    from creatoros.services.toolbox import ensure_tools_seeded

    AgentRegistry(session).ensure_seeded()
    ensure_tools_seeded(session)


def test_grants_are_additive_and_idempotent(session):
    from creatoros.db.models import AgentDefinition

    _seed(session)
    agent = session.query(AgentDefinition).first()
    before = list(agent.allowed_tools_json or [])
    first = apply_social_tool_grants(session)
    assert first["applied"] is True
    session.refresh(agent)
    after = list(agent.allowed_tools_json or [])
    assert set(before) <= set(after)
    second = apply_social_tool_grants(session)
    assert second["applied"] is False
    from creatoros.db.models import AppSetting as AppSettingRow

    assert session.get(AppSettingRow, GRANT_KEY) is not None


def test_grant_map_only_names_real_read_only_tools():
    for tools in SOCIAL_TOOL_GRANTS.values():
        for tool_id in tools:
            assert tool_id in TOOLS_BY_ID
            assert TOOLS_BY_ID[tool_id].category == "read"


def test_social_tools_by_department_reads_real_rows(session):
    from creatoros.db.models import AgentDefinition

    _seed(session)
    agent = session.query(AgentDefinition).first()
    agent.allowed_tools_json = [*(agent.allowed_tools_json or []), "social.accounts.list"]
    session.flush()
    by_department = social_tools_by_department(session)
    assert agent.agent_id in by_department.get(agent.team_id, [])


def test_the_catalogue_and_toolbox_agree():
    from creatoros.agents.catalog import DEPT_TOOLS

    for tools in DEPT_TOOLS.values():
        for tool_id in tools:
            assert tool_id in TOOLS_BY_ID, tool_id
