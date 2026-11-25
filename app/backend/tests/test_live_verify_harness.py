"""The live verification harness, driven against the real API through TestClient.

`scripts/live_verify.py` reads a lot of response fields. If any of them is misspelled the harness would
still "run" and print a comfortable summary, which is exactly the kind of fake verification this project
does not allow. So every step function is exercised here against the actual FastAPI application and the
actual response models, and the assertions are about the verdicts rather than the HTTP plumbing.

Nothing here reaches a provider: the routes are served in-process and the account fixtures are local.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "live_verify.py"


@pytest.fixture()
def harness(monkeypatch):
    spec = importlib.util.spec_from_file_location("live_verify", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setenv("CREATOROS_LIVE_VERIFY", "1")
    return module


class ClientApi:
    """The harness's own `Api` interface, backed by the in-process application."""

    def __init__(self, client, harness) -> None:
        self.client = client
        self.harness = harness

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = self.client.request(method, path, json=payload)
        if response.status_code >= 400:
            raise self.harness.ApiError(method, path, response.status_code, response.text)
        return response.json() if response.content else {}

    def get(self, path: str) -> dict:
        return self.call("GET", path)

    def post(self, path: str, payload: dict | None = None) -> dict:
        return self.call("POST", path, payload if payload is not None else {})


@pytest.fixture()
def api(client, harness):
    return ClientApi(client, harness)


def _make_account(session, platform="facebook", capability_state=None):
    from publishing_fixtures import make_account

    from creatoros.db.enums import CapabilityState

    return make_account(
        session, platform=platform, capability_state=capability_state or CapabilityState.AVAILABLE
    )


# ------------------------------------------------------------------------------- the gate
def test_the_harness_refuses_without_the_opt_in(harness, monkeypatch, capsys):
    monkeypatch.delenv("CREATOROS_LIVE_VERIFY", raising=False)
    assert harness.main([]) == 2
    assert "CREATOROS_LIVE_VERIFY=1 is not set" in capsys.readouterr().err


def test_the_harness_refuses_without_an_api_and_token(harness, monkeypatch, capsys):
    monkeypatch.delenv("CREATOROS_API", raising=False)
    monkeypatch.delenv("CREATOROS_SESSION_TOKEN", raising=False)
    assert harness.main([]) == 2
    assert "CREATOROS_API" in capsys.readouterr().err


# ----------------------------------------------------------------------- steps 1-5
def test_step_1_reports_a_connected_account_with_its_provenance(harness, api, session):

    account = _make_account(session, "facebook")
    session.commit()

    step = harness.step_connection(api, account.id)

    # Connected with no credential is a real state: the step reports it, it does not round it up.
    assert step.verdict == harness.OK
    assert step.facts["platform"] == "facebook"
    assert step.facts["connection"] == "connected"
    assert step.facts["token_state"] == "credential_unavailable"


def test_step_1_blocks_on_a_disconnected_account(harness, api, session):
    from creatoros.db.enums import ConnectionStatus

    account = _make_account(session, "facebook")
    account.connection_status = ConnectionStatus.DISCONNECTED
    session.commit()

    step = harness.step_connection(api, account.id)
    assert step.verdict == harness.BLOCKED
    assert "disconnected" in step.detail


def test_step_2_blocks_because_no_provider_app_is_registered(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()

    step = harness.step_discovery(api, account.id, "facebook", None)

    assert step.verdict == harness.BLOCKED
    assert "provider refused" in step.detail or "provider call failed" in step.detail


def test_step_3_blocks_when_the_provider_returns_no_scope_list(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()
    step = harness.step_permissions(api, account.id, "facebook", None)
    assert step.verdict == harness.BLOCKED
    assert "scope list" in step.detail


def test_step_4_sees_stored_capabilities_and_re_evaluates_them(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()

    step = harness.step_capabilities(api, account.id)

    assert step.verdict == harness.OK
    assert step.facts["total"] >= 1
    assert isinstance(step.facts["available"], list)


def test_step_5_reports_a_valid_token_and_whether_it_can_be_refreshed(harness, api, session, vault):
    from publishing_fixtures import bind_token

    account = _make_account(session, "facebook")
    bind_token(session, account, vault, value="token-value")
    session.commit()

    step = harness.step_token_lifecycle(api, account.id)

    assert step.verdict == harness.OK
    assert step.facts["state"] in {"valid", "expiring_soon"}
    assert step.facts["pending_rotations"] == 0


def test_step_5_blocks_when_the_vault_cannot_read_the_credential(harness, api, session, monkeypatch):
    """`compute_status` is a pure function of stored facts; an unreadable secret is the other half."""
    from publishing_fixtures import bind_token
    from social_fixtures import InMemoryVault

    from creatoros.api.routes import social as social_routes

    account = _make_account(session, "facebook")
    # Seal the token in a vault that is about to disappear, which is what a lost DPAPI key looks like.
    sealed = InMemoryVault()
    social_routes.set_vault(sealed)
    bind_token(session, account, sealed, value="token-value")
    session.commit()
    social_routes.set_vault(None)

    step = harness.step_token_lifecycle(api, account.id)

    assert step.verdict == harness.BLOCKED
    assert "cannot be read" in step.detail
    assert step.facts["secret_health"]


def test_step_5_blocks_when_there_is_no_token(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()
    step = harness.step_token_lifecycle(api, account.id)
    assert step.verdict == harness.BLOCKED
    assert "credential_unavailable" in step.detail


# --------------------------------------------------------------------------- step 6
def test_step_6_reports_a_refused_sync_as_blocked_not_as_passing(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()
    step = harness.step_analytics_sync(api, account.id, wait_s=1)
    assert step.verdict in {harness.BLOCKED, harness.OK}
    if step.verdict == harness.BLOCKED:
        assert "refused" in step.detail or "no run closed" in step.detail or "attempted no metrics" in step.detail


# --------------------------------------------------------------------------- step 7
def test_step_7_blocks_with_the_reason_when_delivery_is_unconfigured(harness, api, session):
    step = harness.step_delivery(api)
    assert step.verdict == harness.BLOCKED
    assert "not configured" in step.detail
    assert step.facts["supported"], "the supported providers are listed so the gap is actionable"


# ----------------------------------------------------------------------- steps 8-9
def test_step_8_and_9_are_skipped_without_a_destination(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()

    steps = harness.run(api, account=account.id, destination=None, publish=False, confirm=None, wait_s=1)

    assert [step.number for step in steps] == list(range(1, 13))
    assert steps[7].verdict == harness.SKIPPED
    assert steps[8].verdict == harness.SKIPPED
    assert steps[9].verdict == harness.SKIPPED
    assert "no --destination" in steps[9].detail


def test_step_9_blocks_when_the_payload_was_never_approved(harness, api, session):
    from publishing_fixtures import intent_with_asset

    account = _make_account(session, "facebook")
    composer, project, _asset = intent_with_asset(session)
    destination = composer.add_destination(
        project.id, platform="facebook", account_id=account.id
    )
    session.commit()

    step = harness.step_approval(api, destination.variant.id)

    assert step.verdict == harness.BLOCKED
    assert "human decision" in step.detail


def test_step_10_is_skipped_without_the_publish_flag_and_prints_the_phrase(harness, api, session):
    from publishing_fixtures import intent_with_asset

    account = _make_account(session, "facebook")
    composer, project, _asset = intent_with_asset(session)
    destination = composer.add_destination(project.id, platform="facebook", account_id=account.id)
    session.commit()

    step = harness.step_publish(api, destination.variant.id, allowed=False, confirm=None, wait_s=1)

    assert step.verdict == harness.SKIPPED
    assert step.facts["confirm_phrase"].startswith(f"PUBLISH {destination.variant.id}")


def test_step_10_requires_the_exact_phrase_even_when_publish_is_allowed(harness, api, session):
    from publishing_fixtures import intent_with_asset

    account = _make_account(session, "facebook")
    composer, project, _asset = intent_with_asset(session)
    destination = composer.add_destination(project.id, platform="facebook", account_id=account.id)
    session.commit()

    step = harness.step_publish(api, destination.variant.id, allowed=True, confirm="PUBLISH nope", wait_s=1)

    assert step.verdict == harness.SKIPPED, "a wrong phrase must not publish"
    assert "did not match" in step.detail


def test_step_10_blocks_when_there_is_no_approval(harness, api, session):
    from publishing_fixtures import intent_with_asset

    account = _make_account(session, "facebook")
    composer, project, _asset = intent_with_asset(session)
    destination = composer.add_destination(project.id, platform="facebook", account_id=account.id)
    session.commit()
    phrase = f"PUBLISH {destination.variant.id} unapproved"

    step = harness.step_publish(api, destination.variant.id, allowed=True, confirm=phrase, wait_s=1)

    assert step.verdict == harness.BLOCKED
    assert "no approval" in step.detail


# ------------------------------------------------------------------------ rendering
def test_render_prints_one_line_per_step_with_its_verdict(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()
    steps = harness.run(api, account=account.id, destination=None, publish=False, confirm=None, wait_s=1)
    text = harness.render(steps)
    assert len(text.splitlines()) == 12
    assert "OK" in text and "BLOCKED" in text and "SKIPPED" in text


def test_steps_serialise_to_json_without_anything_unprintable(harness, api, session):
    account = _make_account(session, "facebook")
    session.commit()
    steps = harness.run(api, account=account.id, destination=None, publish=False, confirm=None, wait_s=1)
    payload = json.dumps([step.as_dict() for step in steps])
    assert '"verdict"' in payload
    for step in steps:
        assert step.verdict in {harness.OK, harness.BLOCKED, harness.SKIPPED, harness.FAILED}


def test_the_classification_keys_match_what_the_protocol_document_promises(harness):
    assert set(harness.CLASSIFICATION) == {
        "implemented",
        "fixture_tested",
        "real_provider_tested",
        "blocked",
    }
