"""Capability engine tests: the state table, the evidence and the history.

The engine is pure: given an account type, granted scopes, app mode and token state it returns the
same verdicts every time. These tests pin the truth table so a later "just make it available"
change cannot slip through.
"""
from __future__ import annotations

from social_fixtures import make_app_config, run

from creatoros.db.enums import (
    CANONICAL_CAPABILITY_STATES,
    AccountType,
    CapabilityState,
    OAuthRoute,
    Platform,
    TokenState,
    normalize_capability_state,
)
from creatoros.integrations.social.base import CapabilityVerdict
from creatoros.integrations.social.capability_rules import evaluate, evaluate_platform, overall_state
from creatoros.integrations.social.config import capabilities_for, capability_by_key
from creatoros.services.social.capabilities import CapabilityService


def verdict_for(key: str, **kwargs) -> str:
    definition = capability_by_key(key)
    assert definition is not None
    arguments = {
        "route": definition.routes[0],
        "account_type": definition.account_types[0],
        "granted_scopes": list(definition.required_scopes),
        "token_state": TokenState.VALID,
        "app_mode": "live",
    }
    arguments.update(kwargs)
    return evaluate(definition, **arguments).state


def test_every_catalogue_state_is_canonical():
    for definition in capabilities_for(Platform.FACEBOOK) + capabilities_for(Platform.TIKTOK):
        state = CapabilityState(verdict_for(definition.key))
        assert state in CANONICAL_CAPABILITY_STATES


def test_legacy_state_strings_are_normalized():
    assert normalize_capability_state("eligible") == CapabilityState.AVAILABLE
    assert normalize_capability_state("missing_permission") == CapabilityState.NEEDS_USER_CONSENT
    assert normalize_capability_state("needs_app_review") == CapabilityState.REQUIRES_DEVELOPER_APPROVAL
    assert normalize_capability_state("token_expired") == CapabilityState.EXPIRED
    assert normalize_capability_state("rate_limited") == CapabilityState.UNAVAILABLE
    assert normalize_capability_state("nonsense") == CapabilityState.UNKNOWN
    assert normalize_capability_state(None) == CapabilityState.UNKNOWN


def test_available_requires_every_scope():
    assert verdict_for("facebook.page.publish_video") == "available"
    assert (
        verdict_for("facebook.page.publish_video", granted_scopes=["pages_manage_posts"]) == "needs_user_consent"
    )


def test_development_app_mode_reports_developer_approval_not_user_consent():
    """This is the distinction that stops "why can't I publish?" turning into a wrong answer."""
    state = verdict_for(
        "instagram.publish_reels",
        granted_scopes=["instagram_business_basic"],
        app_mode="development",
    )
    assert state == "requires_developer_approval"


def test_live_app_mode_reports_missing_consent():
    state = verdict_for(
        "instagram.publish_reels",
        granted_scopes=["instagram_business_basic"],
        app_mode="live",
    )
    assert state == "needs_user_consent"


def test_dead_tokens_dominate_scope_checks():
    assert verdict_for("facebook.page.publish_video", token_state=TokenState.EXPIRED) == "expired"
    assert verdict_for("facebook.page.publish_video", token_state=TokenState.REVOKED) == "revoked"
    assert (
        verdict_for("facebook.page.publish_video", token_state=TokenState.CREDENTIAL_UNAVAILABLE) == "unavailable"
    )
    assert verdict_for("facebook.page.publish_video", token_state=TokenState.REFRESH_FAILED) == "unavailable"


def test_unsupported_account_type_is_unsupported_not_available():
    assert (
        verdict_for("facebook.page.publish_video", account_type=AccountType.PERSONAL_UNSUPPORTED) == "unsupported"
    )


def test_instagram_insight_scopes_are_per_login_path():
    """The two Instagram login paths use different vocabularies; neither satisfies the other."""
    assert (
        verdict_for(
            "instagram.read_insights",
            route=OAuthRoute.INSTAGRAM_LOGIN,
            granted_scopes=["instagram_business_manage_insights"],
        )
        == "available"
    )
    assert (
        verdict_for(
            "instagram.read_insights",
            route=OAuthRoute.FACEBOOK_LOGIN,
            granted_scopes=["instagram_manage_insights"],
        )
        == "available"
    )
    assert (
        verdict_for(
            "instagram.read_insights",
            route=OAuthRoute.FACEBOOK_LOGIN,
            granted_scopes=["instagram_business_manage_insights"],
        )
        == "needs_user_consent"
    )


def test_capability_unreachable_from_this_route_is_unsupported():
    assert verdict_for("facebook.page.read_insights", route=OAuthRoute.INSTAGRAM_LOGIN) == "unsupported"


def test_manual_only_capability_is_never_available():
    assert verdict_for("manual.export_package") == "manual_only"


def test_route_specific_scopes_are_used():
    assert (
        verdict_for("instagram.publish_reels", route=OAuthRoute.FACEBOOK_LOGIN, granted_scopes=["instagram_content_publish"])
        == "available"
    )
    assert (
        verdict_for(
            "instagram.publish_reels", route=OAuthRoute.INSTAGRAM_LOGIN, granted_scopes=["instagram_content_publish"]
        )
        == "needs_user_consent"
    )


def test_overall_state_prefers_the_blocking_condition():
    verdicts = [
        CapabilityVerdict("a", "available", "", [], [], [], ""),
        CapabilityVerdict("b", "revoked", "", [], [], [], ""),
    ]
    assert overall_state(verdicts) == CapabilityState.REVOKED
    assert overall_state([]) == CapabilityState.UNKNOWN


def test_reasons_are_specific_and_mention_the_scope():
    definition = capability_by_key("facebook.page.publish_video")
    verdict = evaluate(
        definition,
        route=OAuthRoute.FACEBOOK_LOGIN,
        account_type=AccountType.FACEBOOK_PAGE,
        granted_scopes=["pages_read_engagement"],
        token_state=TokenState.VALID,
        app_mode="development",
    )
    assert "pages_manage_posts" in verdict.reason
    assert verdict.evidence["app_mode"] == "development"
    assert verdict.docs_reference.startswith("https://")


# ------------------------------------------------------------------ persistence


def _account(session, vault, mock_account_type=AccountType.FACEBOOK_PAGE, scopes=None):
    from creatoros.db.enums import ConnectionStatus
    from creatoros.db.models.platform import SocialAccount

    account = SocialAccount(
        platform=Platform.FACEBOOK,
        provider_account_id="page-1",
        display_name="Test Page",
        account_type=mock_account_type,
        route=OAuthRoute.FACEBOOK_LOGIN,
        scopes_json=scopes or ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        connection_status=ConnectionStatus.CONNECTED,
    )
    session.add(account)
    session.flush()
    return account


def _verdicts(**kwargs):
    arguments = {
        "account_type": AccountType.FACEBOOK_PAGE,
        "granted_scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        "token_state": TokenState.VALID,
        "app_mode": "live",
    }
    arguments.update(kwargs)
    return evaluate_platform(Platform.FACEBOOK, OAuthRoute.FACEBOOK_LOGIN, **arguments)


def test_record_stores_current_state_and_a_snapshot(session, vault):
    account = _account(session, vault)
    service = CapabilityService(session)
    report = service.record(account, _verdicts(), trigger="test", api_version="v23.0")
    session.commit()
    assert report.overall == CapabilityState.AVAILABLE
    current = {row.capability: row.state for row in service.current(account.id)}
    assert current["facebook.page.publish_video"] == CapabilityState.AVAILABLE
    snapshots = service.snapshots(account.id)
    assert len(snapshots) == 1
    assert snapshots[0].trigger == "test"
    assert snapshots[0].checks_json["facebook.page.publish_video"]["state"] == "available"
    assert account.capabilities_json["facebook.page.publish_video"] == "available"


def test_re_evaluating_updates_in_place_and_keeps_history(session, vault):
    account = _account(session, vault)
    service = CapabilityService(session)
    service.record(account, _verdicts(), trigger="first", api_version="v23.0")
    session.commit()
    service.record(
        account,
        _verdicts(granted_scopes=["pages_read_engagement"]),
        trigger="second",
        api_version="v23.0",
    )
    session.commit()
    rows = service.current(account.id)
    assert len(rows) == len(capabilities_for(Platform.FACEBOOK))
    assert len(service.snapshots(account.id)) == 2
    publish = next(row for row in rows if row.capability == "facebook.page.publish_video")
    assert publish.state == CapabilityState.NEEDS_USER_CONSENT
    assert publish.missing_json == ["pages_manage_posts"]


def test_connection_status_tracks_the_credential_not_the_permission_set(session, vault):
    from creatoros.db.enums import ConnectionStatus

    account = _account(session, vault)
    CapabilityService(session).record(
        account,
        _verdicts(granted_scopes=["pages_read_engagement"]),
        trigger="test",
        api_version="v23.0",
    )
    session.commit()
    # One capability still works (profile read), so the account is connected even though
    # publishing needs consent.
    assert account.connection_status == ConnectionStatus.CONNECTED


def test_an_account_with_no_working_capability_reports_the_reason(session, vault):
    from creatoros.db.enums import ConnectionStatus

    account = _account(session, vault, scopes=[])
    CapabilityService(session).record(
        account,
        _verdicts(granted_scopes=[]),
        trigger="test",
        api_version="v23.0",
    )
    session.commit()
    assert account.connection_status in {
        ConnectionStatus.MISSING_PERMISSION,
        ConnectionStatus.NEEDS_APP_REVIEW,
    }


def test_matrix_lists_every_account_and_capability(session, vault):
    account = _account(session, vault)
    service = CapabilityService(session)
    service.record(account, _verdicts(), trigger="test", api_version="v23.0")
    session.commit()
    matrix = service.matrix()
    assert "facebook.page.publish_video" in matrix["capabilities"]
    assert matrix["accounts"][0]["id"] == account.id
    assert matrix["totals"].get("available")


def test_capability_records_survive_an_app_config_change(session, vault):
    run(
        make_app_config(
            session,
            vault,
            platform=Platform.FACEBOOK,
            route=OAuthRoute.FACEBOOK_LOGIN,
            app_mode="development",
        )
    )
    session.commit()
    account = _account(session, vault)
    service = CapabilityService(session)
    service.record(account, _verdicts(app_mode="development"), trigger="test", api_version="v23.0")
    session.commit()
    publish = next(
        row for row in service.current(account.id) if row.capability == "facebook.page.publish_video"
    )
    assert publish.state == CapabilityState.AVAILABLE
    assert publish.evidence_ref is None or isinstance(publish.evidence_ref, str)
