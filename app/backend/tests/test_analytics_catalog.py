"""The catalogue and registry are correct, complete and idempotent.

A golden snapshot is used on purpose: if somebody adds, removes or renames a provider metric the
test fails and a human decides whether an observation recorded under the old key is still
explainable. That is the whole point of a registry.
"""
from __future__ import annotations

from collections import Counter

import pytest

from creatoros.db.enums import AnalyticsObjectType, MetricAvailability, MetricClass, Platform
from creatoros.integrations.analytics import (
    CATALOG_VERSION,
    METRIC_CATALOG,
    catalogue_by_key,
    metrics_for,
    preferred,
)
from creatoros.services.analytics import registry, version_monitor


def test_catalogue_counts_are_pinned():
    counts = Counter((spec.platform.value, spec.object_type.value) for spec in METRIC_CATALOG)
    assert counts == {
        ("facebook", "account"): 25,
        ("facebook", "post"): 24,
        ("facebook", "video"): 13,
        ("facebook", "reel"): 10,
        ("instagram", "account"): 18,
        ("instagram", "post"): 15,
        ("instagram", "reel"): 5,
        ("instagram", "story"): 2,
        ("tiktok", "account"): 5,
        ("tiktok", "video"): 12,
    }
    assert len(METRIC_CATALOG) == 129


def test_every_metric_declares_identity_class_and_kind():
    for spec in METRIC_CATALOG:
        assert spec.metric_class in set(MetricClass), spec.key
        assert spec.unit, spec.key
        assert spec.period_kinds, spec.key
        assert spec.source_doc_url.startswith("https://"), spec.key
        assert spec.definition, spec.key
        assert spec.key.startswith(f"{spec.key.split('.')[0]}.")


def test_metric_keys_are_unique_and_stable():
    keys = [spec.key for spec in METRIC_CATALOG]
    assert len(keys) == len(set(keys))


def test_deprecated_metrics_explain_themselves():
    deprecated = [spec for spec in METRIC_CATALOG if spec.is_deprecated]
    assert deprecated, "the June 2026 retirement is real and must stay documented"
    for spec in deprecated:
        assert spec.deprecated_note, spec.key
        assert spec.deprecation_scope in {"all_versions", "api_version"}, spec.key
        if spec.deprecation_scope == "api_version":
            assert spec.api_version_max, spec.key


def test_tiktok_account_metrics_require_stats_scope_and_videos_require_list():
    """Requirement 1: account statistics and video listing are two different grants."""
    account = metrics_for(Platform.TIKTOK, AnalyticsObjectType.ACCOUNT)
    videos = metrics_for(Platform.TIKTOK, AnalyticsObjectType.VIDEO)
    assert account and videos
    for spec in account:
        assert "user.info.stats" in spec.required_permissions or "user.info.basic" in spec.required_permissions
    for spec in videos:
        assert "video.list" in spec.required_permissions
        assert "user.info.stats" not in spec.required_permissions


def test_numeric_tiktok_stats_are_account_level_only():
    stats = [spec for spec in METRIC_CATALOG if "user.info.stats" in spec.required_permissions]
    assert {spec.provider_metric_name for spec in stats} == {
        "follower_count",
        "following_count",
        "likes_count",
        "video_count",
    }
    assert all(spec.object_type is AnalyticsObjectType.ACCOUNT for spec in stats)


def test_unavailable_metrics_are_named_not_omitted():
    unsupported = [spec for spec in METRIC_CATALOG if spec.availability_default is MetricAvailability.NOT_SUPPORTED]
    keys = {spec.key for spec in unsupported}
    assert "tt.video.watch_time" in keys
    assert "tt.video.retention_curve" in keys
    assert "tt.video.reach" in keys
    # Named gaps must still say which grant *would* be needed.
    for spec in unsupported:
        assert spec.required_permissions, spec.key


def test_demographics_are_redacted_by_policy():
    redacted = [
        spec for spec in METRIC_CATALOG if spec.availability_default is MetricAvailability.REDACTED_BY_POLICY
    ]
    assert redacted
    for spec in redacted:
        assert spec.platform is Platform.INSTAGRAM


def test_preferred_metric_per_purpose_is_single():
    for platform in (Platform.FACEBOOK, Platform.INSTAGRAM, Platform.TIKTOK):
        for object_type in AnalyticsObjectType:
            for purpose in ("views", "reach", "avg_watch_time", "followers"):
                chosen = preferred(platform, object_type, purpose)
                if chosen is None:
                    continue
                assert not chosen.is_deprecated, (platform, object_type, purpose, chosen.key)
                same = [
                    spec
                    for spec in METRIC_CATALOG
                    if spec.preferred_for == purpose
                    and spec.platform is platform
                    and spec.object_type is object_type
                    and not spec.is_deprecated
                ]
                assert len(same) == 1, (platform, object_type, purpose, [s.key for s in same])


def test_seed_is_idempotent_and_reports_counts(session):
    first = registry.ensure_metric_catalog(session)
    assert first["created"] == len(METRIC_CATALOG)
    assert first["total"] == len(METRIC_CATALOG)
    session.commit()

    second = registry.ensure_metric_catalog(session)
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["stranded"] == 0
    session.commit()

    rows = registry.definitions_for(session, Platform.TIKTOK)
    assert len(rows) == 17
    assert {(row.metric_class.value, row.value_kind.value) for row in rows}


def test_seed_preserves_operator_verified_timestamp(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    row = registry.get_definition(session, "fb.post.media_views")
    assert row is not None
    definition = row.definition_text
    row.definition_text = "edited by an operator"
    session.commit()
    registry.ensure_metric_catalog(session)
    session.commit()
    refreshed = registry.get_definition(session, "fb.post.media_views")
    # The catalogue is the source of truth for definitions, so the edit is corrected, and
    # neither the row nor the docs timestamp is duplicated or destroyed.
    assert refreshed.definition_text == definition
    assert refreshed.docs_checked_at is not None
    assert refreshed.catalog_version == CATALOG_VERSION


def test_supersession_is_linked_both_ways(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    views = registry.get_definition(session, "ig.account.views")
    impressions = registry.get_definition(session, "ig.account.impressions")
    assert views.supersedes_key == "ig.account.impressions"
    assert impressions.superseded_by_key == "ig.account.views"


def test_resolve_key_is_qualified_by_platform_and_object(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    assert (
        registry.resolve_key(session, Platform.TIKTOK, AnalyticsObjectType.VIDEO, "like_count")
        == "tt.video.likes"
    )
    assert (
        registry.resolve_key(session, Platform.INSTAGRAM, AnalyticsObjectType.POST, "likes")
        == "ig.media.likes"
    )
    # The same provider field name must not resolve across platforms.
    assert registry.resolve_key(session, Platform.FACEBOOK, AnalyticsObjectType.VIDEO, "like_count") is None


def test_plan_skips_unauthorized_metrics_with_a_named_reason(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    planned = registry.plan(
        session,
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        granted_scopes={"instagram_business_basic"},
        api_version="v23.0",
    )
    by_key = {item.metric_key: item for item in planned}
    assert by_key["ig.account.followers"].allowed is True
    assert by_key["ig.account.reach"].allowed is False
    assert by_key["ig.account.reach"].reason == registry.REASON_NOT_AUTHORIZED
    assert by_key["ig.account.reach"].availability_if_skipped is MetricAvailability.NOT_AUTHORIZED
    assert "instagram_business_manage_insights" in by_key["ig.account.reach"].detail


def test_plan_skips_metrics_above_the_configured_version(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    planned = registry.plan(
        session,
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.POST,
        granted_scopes={"read_insights", "pages_read_engagement"},
        api_version="v23.0",
    )
    by_key = {item.metric_key: item for item in planned}
    assert by_key["fb.post.impressions"].allowed is False
    assert by_key["fb.post.impressions"].reason == registry.REASON_VERSION_ABOVE
    assert by_key["fb.post.media_views"].allowed is True


def test_plan_marks_not_supported_metrics_without_requesting_them(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    planned = registry.plan(
        session,
        platform=Platform.TIKTOK,
        object_type=AnalyticsObjectType.VIDEO,
        granted_scopes={"video.list"},
        api_version="v2",
    )
    by_key = {item.metric_key: item for item in planned}
    assert by_key["tt.video.watch_time"].allowed is False
    assert by_key["tt.video.watch_time"].reason == registry.REASON_NOT_SUPPORTED
    assert by_key["tt.video.views"].allowed is True


def test_plan_applies_account_thresholds_when_signal_is_known(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    small = registry.plan(
        session,
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        granted_scopes={"instagram_business_basic", "instagram_business_manage_insights"},
        api_version="v23.0",
        account_signals={"min_followers": 42},
    )
    by_key = {item.metric_key: item for item in small}
    assert by_key["ig.account.reach"].allowed is False
    assert by_key["ig.account.reach"].reason == registry.REASON_BELOW_THRESHOLD

    big = registry.plan(
        session,
        platform=Platform.INSTAGRAM,
        object_type=AnalyticsObjectType.ACCOUNT,
        granted_scopes={"instagram_business_basic", "instagram_business_manage_insights"},
        api_version="v23.0",
        account_signals={"min_followers": 4200},
    )
    assert {item.metric_key: item for item in big}["ig.account.reach"].allowed is True


def test_plan_unknown_threshold_is_not_reported_as_authorized_success(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    planned = registry.plan(
        session,
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.ACCOUNT,
        granted_scopes={"read_insights", "pages_read_engagement"},
        api_version="v23.0",
        account_signals={},
    )
    by_key = {item.metric_key: item for item in planned}
    assert by_key["fb.page.media_views"].reason == registry.REASON_THRESHOLD_UNKNOWN
    assert by_key["fb.page.media_views"].availability_if_skipped is None


def test_catalogue_by_key_matches_the_tuple():
    by_key = catalogue_by_key()
    assert set(by_key) == {spec.key for spec in METRIC_CATALOG}


def test_version_parse_rejects_nonsense():
    assert version_monitor.parse_version("v23.0") == (23, 0)
    assert version_monitor.parse_version("v2") == (2, 0)
    with pytest.raises(ValueError):
        version_monitor.parse_version("23.0")
    with pytest.raises(ValueError):
        version_monitor.parse_version("latest")
