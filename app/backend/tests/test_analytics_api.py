"""P8.17: the analytics API.

Every route is exercised through the real FastAPI app with the real auth dependency and a real
(isolated) database. The properties that matter:

* nothing is reachable without the session token;
* a metric payload carries its availability and never turns "not observed" into 0;
* structured payloads survive;
* expensive work is queued and reported as queued, not completed;
* a response never contains a credential reference or a token.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from publishing_fixtures import make_published_job

from creatoros.db.enums import (
    AccountType,
    AnalyticsAggregationMethod,
    AnalyticsAggregationScope,
    AnalyticsObjectType,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsWindowKind,
    ConnectionStatus,
    MetricAvailability,
    MetricCompleteness,
    MetricPeriod,
    MetricValueKind,
    Platform,
    RecommendationKind,
    RecommendationStatus,
)
from creatoros.db.models import (
    AgentObjective,
    AnalyticsAggregation,
    AnalyticsCollectionRun,
    AnalyticsRecommendation,
    AnalyticsSnapshot,
    AnalyticsSyncState,
    PlatformVariant,
    PublishJob,
    QueueJob,
    SocialAccount,
)

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
ACCOUNT_TYPE = {
    Platform.TIKTOK: AccountType.TIKTOK_CREATOR,
    Platform.FACEBOOK: AccountType.FACEBOOK_PAGE,
    Platform.INSTAGRAM: AccountType.INSTAGRAM_BUSINESS,
}


# --------------------------------------------------------------------- fixtures


def _account(session, *, platform: Platform = Platform.TIKTOK, scopes=None, name="API account") -> SocialAccount:
    account = SocialAccount(
        platform=platform,
        provider_account_id=f"api-{uuid4().hex[:10]}",
        display_name=name,
        account_type=ACCOUNT_TYPE[platform],
        scopes_json=scopes if scopes is not None else ["user.info.basic", "user.info.stats", "video.list"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2" if platform is Platform.TIKTOK else "v23.0",
        meta_json={},
    )
    session.add(account)
    session.commit()
    return account


def _run(session, account) -> AnalyticsCollectionRun:
    run = AnalyticsCollectionRun(
        account_id=account.id,
        platform=account.platform,
        target_kind=AnalyticsRunTarget.METRICS,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
        outcome=AnalyticsRunOutcome.SUCCEEDED,
        dedupe_key=f"run-{uuid4().hex}",
        api_version=account.api_version,
    )
    session.add(run)
    session.flush()
    return run


def _snapshot(
    session,
    account,
    run,
    *,
    metric_key="tt.video.views",
    value=100.0,
    availability=MetricAvailability.OBSERVED,
    object_type=AnalyticsObjectType.VIDEO,
    provider_object_id="obj-1",
    publication_id=None,
    collected_at=None,
    value_json=None,
) -> AnalyticsSnapshot:
    if availability is not MetricAvailability.OBSERVED:
        value = None
    kind = MetricValueKind.SERIES if value_json is not None else MetricValueKind.SCALAR
    row = AnalyticsSnapshot(
        account_id=account.id,
        platform=account.platform,
        object_type=object_type,
        provider_object_id=provider_object_id,
        publication_id=publication_id,
        metric_key=metric_key,
        value_kind=kind,
        period_kind=MetricPeriod.LIFETIME,
        value_num=value if kind is MetricValueKind.SCALAR else None,
        value_json=value_json,
        unit="count",
        availability=availability,
        breakdown_key="",
        api_version=account.api_version or "v2",
        api_host="open.tiktokapis.com",
        source_ref="test",
        run_id=run.id,
        collected_at=collected_at or NOW,
        completeness=MetricCompleteness.COMPLETE,
    )
    session.add(row)
    session.flush()
    return row


def _aggregation(
    session,
    account,
    *,
    metric_key="tt.video.views",
    value=100.0,
    availability=MetricAvailability.OBSERVED,
    scope=AnalyticsAggregationScope.PUBLICATION,
    publication_key="pub-1",
    campaign_key="",
    window=AnalyticsWindowKind.H24,
    snapshot_ids=None,
    value_json=None,
) -> AnalyticsAggregation:
    row = AnalyticsAggregation(
        account_id=account.id,
        platform=account.platform,
        scope=scope,
        publication_key=publication_key,
        campaign_key=campaign_key,
        metric_key=metric_key,
        window_kind=window,
        window_hours=24,
        window_tolerance_minutes=180,
        value_num=value if availability is MetricAvailability.OBSERVED else None,
        value_json=value_json,
        unit="count",
        availability=availability,
        method=AnalyticsAggregationMethod.DIRECT,
        input_snapshot_ids_json=list(snapshot_ids or []),
        input_metric_keys_json=[metric_key],
        confidence_json={},
        computed_at=NOW,
        aggregation_version=1,
    )
    session.add(row)
    session.flush()
    return row


def _published(session, account, *, platform=Platform.TIKTOK, published_at=None, topic="bts", hashtags=("launch",)):
    fixture = make_published_job(session, platform=platform, account=account, published_at=published_at or NOW - timedelta(days=30))
    variant = session.get(PlatformVariant, fixture.variant_id)
    variant.metadata_json = {"topic": topic}
    job = session.get(PublishJob, fixture.job_id)
    job.hashtags_json = list(hashtags)
    session.flush()
    return fixture


# --------------------------------------------------------------------- auth


def _analytics_routes():
    from creatoros.main import app

    found: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/analytics"):
            continue
        for method in operations:
            found.append((method.upper(), path))
    return found


def test_every_analytics_route_requires_the_session_token(unauth_client):
    routes = _analytics_routes()
    assert len(routes) >= 50, f"expected the full inventory, found {len(routes)}"
    for method, path in routes:
        concrete = path
        for name in re.findall(r"\{[^}]+\}", path):
            concrete = concrete.replace(name, "does-not-exist")
        response = unauth_client.request(method, concrete, json={} if method in {"POST", "PUT", "PATCH"} else None)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code} (must be 401)"


# --------------------------------------------------------------------- overview / registry / versions / windows


def test_overview_counts_real_rows(client, session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account)
    session.commit()

    body = client.get("/analytics/overview").json()
    assert body["accounts_total"] >= 1
    assert body["observations"] >= 1
    assert body["recommendations_by_status"]["draft"] == 0
    assert "generated_at" in body


def test_freshness_flags_an_account_without_observations(client, session):
    account = _account(session)
    session.commit()
    body = client.get("/analytics/freshness").json()
    mine = next(item for item in body["accounts"] if item["account_id"] == account.id)
    assert mine["last_collected_at"] is None
    assert mine["stale"] is True


def test_capabilities_versions_and_windows(client):
    capabilities = client.get("/analytics/capabilities").json()
    assert {row["platform"] for row in capabilities["platforms"]} == {"facebook", "instagram", "tiktok"}

    versions = client.get("/analytics/versions").json()
    assert versions["platforms"]
    assert {"platform", "configured", "latest_documented", "retired"} <= set(versions["platforms"][0])

    windows = client.get("/analytics/windows").json()["windows"]
    assert [row["kind"] for row in windows] == ["initial", "h24", "h48", "d7", "d28"]
    assert windows[1]["tolerance_minutes"] == 180


def test_metric_catalogue_paginates_and_filters(client):
    page = client.get("/analytics/metrics", params={"limit": 5, "offset": 0}).json()
    assert len(page["items"]) == 5
    assert page["total"] >= 100
    assert page["limit"] == 5

    deprecated = client.get("/analytics/metrics", params={"include_deprecated": True, "platform": "tiktok"}).json()
    assert all(item["platform"] == "tiktok" for item in deprecated["items"])

    first = page["items"][0]["metric_key"]
    detail = client.get(f"/analytics/metrics/{first}").json()
    assert detail["metric_key"] == first
    assert "metric_class" in detail and "value_kind" in detail

    missing = client.get("/analytics/metrics/does.not.exist")
    assert missing.status_code == 404
    assert missing.json()["code"] == "METRIC_NOT_FOUND"


def test_availability_reports_scope_reasons(client, session):
    account = _account(session, scopes=["user.info.basic"])
    session.commit()
    body = client.get("/analytics/availability", params={"account_id": account.id, "object_type": "account"}).json()
    assert body["account_id"] == account.id
    assert sum(body["summary"].values()) == len(body["metrics"])
    reasons = {row["reason"] for row in body["metrics"]}
    assert "not_authorized" in reasons, "a scope the token does not hold must be named"

    missing = client.get("/analytics/availability", params={"account_id": "nope"})
    assert missing.status_code == 404


# --------------------------------------------------------------------- accounts


def test_accounts_list_detail_and_isolation(client, session):
    a = _account(session, name="A")
    b = _account(session, name="B")
    run = _run(session, a)
    _snapshot(session, a, run)
    _published(session, a)
    _published(session, b)
    session.commit()

    accounts = client.get("/analytics/accounts").json()
    assert accounts["total"] >= 2

    detail = client.get(f"/analytics/accounts/{a.id}").json()
    assert detail["account"]["id"] == a.id
    assert detail["account"]["observations"] >= 1

    only_a = client.get("/analytics/publications", params={"account_id": a.id}).json()
    assert only_a["items"] and all(item["account_id"] == a.id for item in only_a["items"])

    assert client.get("/analytics/accounts/nope").status_code == 404


def test_account_growth_uses_the_registry_follower_metric(client, session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(
        session,
        account,
        run,
        metric_key="tt.account.followers",
        value=1234.0,
        object_type=AnalyticsObjectType.ACCOUNT,
        provider_object_id=account.provider_account_id,
    )
    session.commit()
    body = client.get(f"/analytics/accounts/{account.id}/growth").json()
    assert "tt.account.followers" in body["metric_keys"]
    assert body["points"][0]["value"] == 1234.0


def test_account_metrics_returns_only_account_scope(client, session):
    account = _account(session)
    _aggregation(session, account, scope=AnalyticsAggregationScope.ACCOUNT, publication_key="")
    _aggregation(session, account, scope=AnalyticsAggregationScope.PUBLICATION, publication_key="pub-1")
    session.commit()
    body = client.get(f"/analytics/accounts/{account.id}/metrics").json()
    assert len(body) == 1
    assert body[0]["metric_key"] == "tt.video.views"


# --------------------------------------------------------------------- publications


def test_publications_detail_metrics_retention_timeline(client, session):
    account = _account(session)
    fixture = _published(session, account, published_at=NOW - timedelta(days=2))
    run = _run(session, account)
    snapshot = _snapshot(session, account, run, publication_id=fixture.job_id, provider_object_id=fixture.remote_post_id)
    _aggregation(session, account, publication_key=fixture.job_id, snapshot_ids=[snapshot.id])
    _snapshot(
        session,
        account,
        run,
        metric_key="tt.video.retention_curve",
        publication_id=fixture.job_id,
        provider_object_id=fixture.remote_post_id,
        value_json=[1.0, 0.8, 0.6, 0.4],
    )
    session.commit()

    listed = client.get("/analytics/publications", params={"account_id": account.id}).json()
    assert listed["items"][0]["has_metrics"] is True

    detail = client.get(f"/analytics/publications/{fixture.job_id}").json()
    assert detail["publication"]["publication_id"] == fixture.job_id
    assert detail["metrics"]

    metrics = client.get(f"/analytics/publications/{fixture.job_id}/metrics").json()
    assert metrics and metrics[0]["availability"] == "observed"

    retention = client.get(f"/analytics/publications/{fixture.job_id}/retention").json()
    assert retention["available"] is True
    assert retention["curves"][0]["value_json"] == [1.0, 0.8, 0.6, 0.4]

    timeline = client.get(f"/analytics/publications/{fixture.job_id}/timeline").json()
    assert len(timeline["points"]) >= 2

    assert client.get("/analytics/publications/nope").status_code == 404


def test_retention_is_unavailable_without_flattening(client, session):
    account = _account(session)
    fixture = _published(session, account)
    run = _run(session, account)
    _snapshot(session, account, run, publication_id=fixture.job_id, provider_object_id=fixture.remote_post_id)
    session.commit()
    body = client.get(f"/analytics/publications/{fixture.job_id}/retention").json()
    assert body["available"] is False
    assert body["curves"] == []
    assert "not" in body["reason"].lower() or "no retention" in body["reason"].lower()


def test_missing_metric_is_null_not_zero(client, session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run, metric_key="tt.video.likes", availability=MetricAvailability.NOT_AUTHORIZED)
    session.commit()
    body = client.get("/analytics/snapshots", params={"account_id": account.id, "metric_key": "tt.video.likes"}).json()
    assert body["items"][0]["availability"] == "not_authorized"
    assert body["items"][0]["value"] is None


# --------------------------------------------------------------------- series / snapshots / aggregations / platforms


def test_series_reports_a_gap_without_zero_filling(client, session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run, collected_at=NOW)
    session.commit()
    body = client.get(
        "/analytics/series",
        params={
            "account_id": account.id,
            "metric_key": "tt.video.views",
            "start": (NOW - timedelta(days=1)).isoformat(),
            "end": (NOW + timedelta(days=1)).isoformat(),
            "bucket": "day",
        },
    ).json()
    assert body["points"]
    assert all(point["availability"] in {"observed", "not_collected"} for point in body["points"])
    empty = client.get(
        "/analytics/series",
        params={
            "account_id": account.id,
            "metric_key": "tt.video.views",
            "start": (NOW - timedelta(days=90)).isoformat(),
            "end": (NOW - timedelta(days=80)).isoformat(),
        },
    ).json()
    assert empty["points"][0]["value"] is None
    assert empty["points"][0]["availability"] == "not_collected"


def test_series_rejects_a_huge_range(client, session):
    account = _account(session)
    session.commit()
    response = client.get(
        "/analytics/series",
        params={
            "account_id": account.id,
            "metric_key": "tt.video.views",
            "start": (NOW - timedelta(days=900)).isoformat(),
            "end": NOW.isoformat(),
        },
    )
    assert response.status_code == 422


def test_snapshots_and_aggregations_paginate(client, session):
    account = _account(session)
    run = _run(session, account)
    for index in range(3):
        _snapshot(session, account, run, value=float(index))
        _aggregation(session, account, publication_key=f"pub-{index}")
    session.commit()

    snapshots = client.get("/analytics/snapshots", params={"account_id": account.id, "limit": 2}).json()
    assert len(snapshots["items"]) == 2
    assert snapshots["total"] == 3

    aggregations = client.get("/analytics/aggregations", params={"account_id": account.id, "limit": 2}).json()
    assert len(aggregations["items"]) == 2
    assert aggregations["total"] == 3
    assert "aggregation_version" in aggregations["items"][0]

    platforms = client.get("/analytics/platforms").json()["platforms"]
    tiktok = next(row for row in platforms if row["platform"] == "tiktok")
    assert tiktok["observations"] >= 3


# --------------------------------------------------------------------- campaigns


def test_campaigns_list_and_detail(client, session):
    account = _account(session)
    fixture = _published(session, account)
    session.commit()
    listed = client.get("/analytics/campaigns").json()
    assert listed["total"] >= 1
    detail = client.get(f"/analytics/campaigns/{fixture.campaign_id}").json()
    assert detail["campaign"]["campaign_id"] == fixture.campaign_id
    assert detail["publications"][0]["publication_id"] == fixture.job_id
    assert client.get("/analytics/campaigns/nope").status_code == 404


# --------------------------------------------------------------------- comparison / hashtags / timing


def test_compare_returns_groups_and_refuses_an_empty_account(client, session):
    account = _account(session)
    for index in range(10):
        published_at = NOW - timedelta(days=30, minutes=index)
        fixture = _published(session, account, published_at=published_at, topic="bts")
        run = _run(session, account)
        _snapshot(
            session,
            account,
            run,
            value=float(100 + index),
            provider_object_id=fixture.remote_post_id,
            publication_id=fixture.job_id,
            collected_at=published_at + timedelta(hours=24),
        )
    session.commit()
    body = client.post(
        "/analytics/compare",
        json={"account_id": account.id, "metric_key": "tt.video.views", "attribute": "topic", "max_groups": 2},
    ).json()
    assert body["groups"]
    assert body["limitations"] or body["sufficient"] is not None

    empty = _account(session, name="Empty")
    session.commit()
    refused = client.post(
        "/analytics/compare", json={"account_id": empty.id, "metric_key": "tt.video.views"}
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "NO_PUBLICATIONS"


def test_compare_rejects_unexpected_fields(client, session):
    account = _account(session)
    session.commit()
    response = client.post(
        "/analytics/compare",
        json={"account_id": account.id, "metric_key": "tt.video.views", "unexpected": 1},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_timing_with_too_few_publications_is_empty_but_valid(client, session):
    account = _account(session)
    _published(session, account)
    session.commit()
    body = client.get("/analytics/timing", params={"account_id": account.id}).json()
    assert body["buckets"] == []


# --------------------------------------------------------------------- recommendations


def _recommendation(session, account, *, status=RecommendationStatus.DRAFT) -> AnalyticsRecommendation:
    row = AnalyticsRecommendation(
        account_id=account.id,
        platform=account.platform,
        kind=RecommendationKind.TOPIC,
        observation="Topic 'bts' reached 500 views at the 24-hour window.",
        evidence_json=[
            {
                "metric_key": "tt.video.views",
                "provider_metric_name": "view_count",
                "unit": "count",
                "period_kind": "lifetime",
                "availability": "observed",
                "value": 500.0,
                "snapshot_ids": ["snap-1"],
                "aggregation_ids": [],
            }
        ],
        sample_size_json={"group": 10, "baseline": 12},
        confidence=0.8,
        confidence_method="bootstrap_median_95",
        limitations_json=["observational"],
        proposed_experiment_json={"variable": "topic"},
        expected_benefit_json={"kind": "estimate"},
        status=status,
        generated_by="deterministic",
    )
    session.add(row)
    session.commit()
    return row


def test_recommendations_list_detail_accept_reject(client, session):
    account = _account(session)
    row = _recommendation(session, account)
    session.commit()

    listed = client.get("/analytics/recommendations", params={"account_id": account.id}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == row.id

    detail = client.get(f"/analytics/recommendations/{row.id}").json()
    assert detail["evidence"][0]["value"] == 500.0
    assert detail["recommendation"]["limitations"] == ["observational"]

    accepted = client.post(f"/analytics/recommendations/{row.id}/accept", json={"note": "try it"})
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["status"] == "accepted"
    assert body["approval_request_id"], "accepting must open a hash-bound approval request"

    again = client.post(f"/analytics/recommendations/{row.id}/accept", json={})
    assert again.status_code == 409

    other = _recommendation(session, account)
    rejected = client.post(f"/analytics/recommendations/{other.id}/reject", json={"reason": "not now"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    assert client.post("/analytics/recommendations/nope/reject", json={"reason": "x"}).status_code == 404
    assert client.post("/analytics/recommendations/nope/accept", json={}).status_code == 404


def test_recommendation_refresh_is_idempotent_and_draft_only(client, session):
    account = _account(session)
    session.commit()
    first = client.post("/analytics/recommendations/refresh", json={"account_id": account.id}).json()
    second = client.post("/analytics/recommendations/refresh", json={"account_id": account.id}).json()
    assert first["created"] == 0  # no measured publications yet -> nothing to detect
    assert second["created"] == 0
    assert client.post("/analytics/recommendations/refresh", json={"account_id": "nope"}).status_code == 404


# --------------------------------------------------------------------- reports / exports


def test_reports_create_download_and_delete(client, session):
    account = _account(session)
    _aggregation(session, account)
    session.commit()

    created = client.post(
        "/analytics/reports",
        json={"kind": "overview", "account_id": account.id, "title": "Week 47"},
    )
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["status"] == "final"
    assert report["sha256"] and report["bytes"] > 0
    assert report["sections"]

    listed = client.get("/analytics/reports").json()
    assert listed["total"] >= 1
    assert client.get(f"/analytics/reports/{report['id']}").json()["id"] == report["id"]

    download = client.get(f"/analytics/reports/{report['id']}/file")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
    assert b"Measured aggregations" in download.content

    assert client.delete(f"/analytics/reports/{report['id']}").status_code == 204
    assert client.get(f"/analytics/reports/{report['id']}").status_code == 404


def test_report_rejects_a_reversed_period(client, session):
    account = _account(session)
    session.commit()
    response = client.post(
        "/analytics/reports",
        json={
            "kind": "overview",
            "account_id": account.id,
            "period_start": NOW.isoformat(),
            "period_end": (NOW - timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 422


def test_exports_match_the_query(client, session):
    account = _account(session)
    fixture = _published(session, account)
    _aggregation(session, account, publication_key=fixture.job_id)
    session.commit()

    metrics = client.get("/analytics/export/metrics.csv", params={"account_id": account.id})
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/csv")
    assert "metric_key" in metrics.text
    assert "tt.video.views" in metrics.text

    publications = client.get("/analytics/export/publications.csv", params={"account_id": account.id})
    assert publications.status_code == 200
    assert fixture.remote_post_id in publications.text


# --------------------------------------------------------------------- collection control


def test_sync_and_backfill_enqueue_real_jobs(client, session):
    account = _account(session)
    session.commit()

    first = client.post(f"/analytics/sync/{account.id}")
    assert first.status_code == 202
    body = first.json()
    assert body["queue"] == "analytics"
    assert body["state"] == "queued"
    assert body["created"] is True
    job = session.get(QueueJob, body["job_id"])
    assert job is not None and job.queue == "analytics"

    second = client.post(f"/analytics/sync/{account.id}")
    assert second.status_code == 202
    assert second.json()["created"] is False
    assert second.json()["state"] == "already_queued"

    backfill = client.post(f"/analytics/backfill/{account.id}")
    assert backfill.status_code == 202
    assert backfill.json()["job_type"] == "analytics.backfill"

    assert client.post("/analytics/sync/nope").status_code == 404


def test_prune_is_a_preview_and_deletes_nothing(client, session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    session.commit()
    body = client.post("/analytics/prune", json={"older_than_days": 30}).json()
    assert body["applied"] is False
    assert "preview" in body["reason"].lower()
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot)) >= 1


def test_runs_and_sync_state(client, session):
    account = _account(session)
    run = _run(session, account)
    session.add(
        AnalyticsSyncState(
            account_id=account.id,
            platform=account.platform,
            object_type=AnalyticsObjectType.ACCOUNT,
            next_due_at=NOW,
        )
    )
    session.commit()

    runs = client.get("/analytics/runs", params={"account_id": account.id}).json()
    assert runs["total"] == 1
    assert runs["items"][0]["outcome"] == "succeeded"
    assert client.get(f"/analytics/runs/{run.id}").json()["id"] == run.id
    assert client.get("/analytics/runs/nope").status_code == 404

    states = client.get("/analytics/sync-state", params={"account_id": account.id}).json()
    assert states["total"] == 1
    assert states["items"][0]["state"] == "not_started"


# --------------------------------------------------------------------- settings


def test_settings_round_trip_and_bounds(client):
    current = client.get("/analytics/settings").json()
    assert current["settings"]["account_sync_interval_s"] == 86400
    assert "bounds" in current

    updated = client.put(
        "/analytics/settings", json={"settings": {"account_sync_interval_s": 3600, "collection_enabled": False}}
    )
    assert updated.status_code == 200
    assert updated.json()["settings"]["account_sync_interval_s"] == 3600

    too_small = client.put("/analytics/settings", json={"settings": {"account_sync_interval_s": 1}})
    assert too_small.status_code == 422
    unknown = client.put("/analytics/settings", json={"settings": {"nope": 1}})
    assert unknown.status_code == 422


# --------------------------------------------------------------------- workflows


def test_workflows_list_and_enqueue(client, session):
    account = _account(session)
    session.commit()

    listed = client.get("/analytics/workflows").json()["workflows"]
    assert {row["key"] for row in listed} == {
        "weekly_growth_review",
        "single_publication_diagnosis",
        "experiment_design",
    }
    weekly = next(row for row in listed if row["key"] == "weekly_growth_review")
    assert len(weekly["stages"]) == 12

    created = client.post(
        "/analytics/workflows", json={"key": "experiment_design", "account_id": account.id}
    )
    assert created.status_code == 202
    body = created.json()
    assert body["state"] == "pending"
    assert body["queue_job_id"]
    assert session.get(AgentObjective, body["objective_id"]) is not None

    assert client.post("/analytics/workflows", json={"key": "nope", "account_id": account.id}).status_code == 404
    assert client.post("/analytics/workflows", json={"key": "experiment_design", "account_id": "nope"}).status_code == 404


# --------------------------------------------------------------------- diagnostics / secrets


def test_diagnostics_reports_facts(client, session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    session.commit()
    body = client.get("/analytics/diagnostics").json()
    assert body["observations"] >= 1
    assert body["accounts"] >= 1
    assert isinstance(body["versions"], list)
    assert "warnings" in body


def test_no_credential_reference_appears_in_account_responses(client, session):
    account = _account(session)
    session.commit()
    raw = client.get(f"/analytics/accounts/{account.id}").text.lower()
    for forbidden in ("token_ref", "refresh_ref", "access_token", "refresh_token", "client_secret", "vault"):
        assert forbidden not in raw
    raw = client.get("/analytics/accounts").text.lower()
    for forbidden in ("token_ref", "refresh_ref", "access_token", "refresh_token", "client_secret"):
        assert forbidden not in raw