"""P8.24: report formats, the retention engine, version watch and the diagnostics that report them.

The properties under test are the ones a release claim depends on:

* a report's digest is of the bytes that were written, in every format, and the download's
  content-type matches the format that was asked for;
* retention deletes only what its policy allows — observations, redundant rollups, expired reports,
  superseded drafts and orphan files — and leaves publications, campaigns, accounts, media, metric
  definitions and decided recommendations untouched;
* retention is bounded, repeatable and audited, and leaves an empty foreign-key check behind;
* a version check never bumps a version, and reports `stale`/`unavailable` rather than inventing one;
* diagnostics state what is stored, including "never collected" and "never executed".
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from publishing_fixtures import make_asset, make_published_job

from creatoros.db.enums import (
    AccountType,
    AnalyticsAggregationMethod,
    AnalyticsAggregationScope,
    AnalyticsObjectType,
    AnalyticsReportFormat,
    AnalyticsReportKind,
    AnalyticsReportStatus,
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
    AnalyticsAggregation,
    AnalyticsCollectionRun,
    AnalyticsMetricDefinition,
    AnalyticsRecommendation,
    AnalyticsReport,
    AnalyticsSnapshot,
    AuditEvent,
    MediaAsset,
    SocialAccount,
)
from creatoros.services.analytics import reports as reports_service
from creatoros.services.analytics import retention, version_watch
from creatoros.services.analytics.pdf import render_pdf

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
ACCOUNT_TYPE = {
    Platform.TIKTOK: AccountType.TIKTOK_CREATOR,
    Platform.FACEBOOK: AccountType.FACEBOOK_PAGE,
    Platform.INSTAGRAM: AccountType.INSTAGRAM_BUSINESS,
}


# --------------------------------------------------------------------- fixtures


def _account(session, *, platform: Platform = Platform.TIKTOK, name="P8.24 account") -> SocialAccount:
    # The observation ledger has a RESTRICT foreign key to the metric registry, so the catalogue must
    # exist before any snapshot can be written (same requirement as the real collector).
    from creatoros.services.analytics import registry as metric_registry

    metric_registry.ensure_metric_catalog(session)
    account = SocialAccount(
        platform=platform,
        provider_account_id=f"p824-{uuid4().hex[:10]}",
        display_name=name,
        account_type=ACCOUNT_TYPE[platform],
        scopes_json=["user.info.basic", "user.info.stats", "video.list"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        api_version="v2" if platform is Platform.TIKTOK else "v23.0",
        meta_json={},
    )
    session.add(account)
    session.commit()
    return account


def _run(session, account, *, started=NOW, finished=NOW + timedelta(minutes=1), outcome=AnalyticsRunOutcome.SUCCEEDED):
    run = AnalyticsCollectionRun(
        account_id=account.id,
        platform=account.platform,
        target_kind=AnalyticsRunTarget.METRICS,
        started_at=started,
        finished_at=finished,
        outcome=outcome,
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
    collected_at=None,
    availability=None,
    publication_id=None,
):
    row = AnalyticsSnapshot(
        account_id=account.id,
        platform=account.platform,
        object_type=AnalyticsObjectType.VIDEO,
        provider_object_id=f"obj-{uuid4().hex[:8]}",
        publication_id=publication_id,
        metric_key=metric_key,
        value_kind=MetricValueKind.SCALAR,
        period_kind=MetricPeriod.LIFETIME,
        value_num=value if (availability or MetricAvailability.OBSERVED) is MetricAvailability.OBSERVED else None,
        unit="count",
        availability=availability or MetricAvailability.OBSERVED,
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


def _aggregation(session, account, *, snapshot_ids, metric_key="tt.video.views", computed_at=None, publication_key=None):
    row = AnalyticsAggregation(
        account_id=account.id,
        platform=account.platform,
        scope=AnalyticsAggregationScope.PUBLICATION,
        publication_key=publication_key or f"pub-{uuid4().hex[:8]}",
        campaign_key="",
        metric_key=metric_key,
        window_kind=AnalyticsWindowKind.H24,
        value_num=100.0,
        unit="count",
        sample_size=len(snapshot_ids),
        availability=MetricAvailability.OBSERVED,
        method=AnalyticsAggregationMethod.DIRECT,
        input_snapshot_ids_json=list(snapshot_ids),
        input_metric_keys_json=[metric_key],
        confidence_json={},
        computed_at=computed_at or NOW,
        aggregation_version=1,
    )
    session.add(row)
    session.flush()
    return row


def _recommendation(session, account, *, status: RecommendationStatus, created_at=None):
    row = AnalyticsRecommendation(
        account_id=account.id,
        platform=account.platform,
        kind=RecommendationKind.TOPIC,
        observation="synthetic observation for the retention test",
        sample_size_json={"publications": 9},
        confidence=0.9,
        confidence_method="bootstrap",
        limitations_json=["observational"],
        proposed_experiment_json={"change": "x"},
        expected_benefit_json={"kind": "estimate"},
        status=status,
        generated_by="test",
        created_at=created_at or NOW,
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------- report formats


def _make_report(session, account, fmt, **kwargs):
    return reports_service.create_report(
        session,
        kind=AnalyticsReportKind.OVERVIEW,
        account_id=account.id,
        report_format=fmt,
        generated_by="test",
        **kwargs,
    )


def test_json_report_digest_is_the_digest_of_the_written_bytes(session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account, snapshot_ids=[])
    report = _make_report(session, account, "json")

    written = Path(report.path).read_bytes()
    assert hashlib.sha256(written).hexdigest() == report.sha256
    assert report.bytes == len(written)
    payload = json.loads(written)
    # The header block a reader needs, in the file itself.
    assert payload["report_id"] == report.id
    assert payload["format"] == "json"
    assert payload["platforms"] == ["tiktok"]
    assert payload["accounts"] == [account.id]
    assert payload["metric_keys"] == ["tt.video.views"]
    assert payload["observation_windows"] == ["h24"]
    assert "warnings" in payload
    assert payload["sources"]["registry"] == "analytics_metric_definition"


def test_csv_report_is_flat_rfc4180_with_a_section_column(session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account, snapshot_ids=[])
    report = _make_report(session, account, AnalyticsReportFormat.CSV)

    raw = Path(report.path).read_bytes()
    assert raw.startswith(b"section,field,value\r\n")
    assert hashlib.sha256(raw).hexdigest() == report.sha256
    text = raw.decode("utf-8")
    assert "\r\n" in text
    assert "Measured aggregations,metric_key,tt.video.views" in text
    assert "Platforms and accounts,platforms," in text


def test_pdf_report_is_a_structurally_valid_pdf(session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account, snapshot_ids=[])
    report = _make_report(session, account, "pdf")

    raw = Path(report.path).read_bytes()
    assert raw.startswith(b"%PDF-1.4")
    assert raw.rstrip().endswith(b"%%EOF")
    assert hashlib.sha256(raw).hexdigest() == report.sha256
    assert report.bytes == len(raw)

    # The xref table must point at the object headers it claims, or a viewer will refuse the file.
    match = re.search(rb"startxref\n(\d+)\n%%EOF", raw)
    assert match, "the file has no startxref"
    xref_at = int(match.group(1))
    assert raw[xref_at : xref_at + 4] == b"xref"
    entries = re.findall(rb"(\d{10}) 00000 n \n", raw[xref_at:])
    assert entries, "the xref table has no in-use entries"
    for index, offset in enumerate(entries, start=1):
        offset = int(offset)
        assert raw[offset:].startswith(f"{index} 0 obj".encode()), f"object {index} offset is wrong"
    assert b"/Type /Catalog" in raw and b"/Type /Pages" in raw
    assert b"Measured aggregations" in raw


def test_pdf_wrapper_wraps_long_lines_and_paginates():
    blocks = [("Rows", [f"row {index} " + "x" * 300 for index in range(80)])]
    data = render_pdf("A very long title", blocks, "footer")
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 1
    page_count = int(re.search(rb"/Count (\d+)", data).group(1))
    assert page_count >= 2, "80 long rows must span more than one page"
    # The content streams are latin-1 by construction, so every page object is decodable.
    for stream in re.findall(b"stream\n(.*?)\nendstream", data, re.DOTALL):
        stream.decode("latin-1")


def test_report_formats_of_an_unknown_kind_are_refused(session):
    account = _account(session)
    session.commit()
    try:
        _make_report(session, account, "docx")
    except reports_service.ReportFormatUnsupported as exc:
        assert "implemented: json, csv, pdf" in str(exc)
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("an unimplemented format must be refused")
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsReport)) == 0


def test_a_failed_write_leaves_no_row_and_no_partial_file(session, monkeypatch):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account, snapshot_ids=[])
    session.commit()

    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    try:
        _make_report(session, account, "json")
    except OSError:
        pass
    else:  # pragma: no cover
        raise AssertionError("the write failure must propagate")
    session.rollback()
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsReport)) == 0
    leftovers = list((Path(account and (reports_service.resolve_under_root("media_exports", "reports")))).glob("*.part"))
    assert leftovers == []


def test_report_records_which_metrics_were_not_observed(session):
    account = _account(session)
    _run(session, account)
    _aggregation(
        session,
        account,
        snapshot_ids=[],
        metric_key="tt.video.watch_time",
    )
    row = session.scalar(sa.select(AnalyticsAggregation))
    row.availability = MetricAvailability.NOT_SUPPORTED
    row.value_num = None
    session.commit()
    report = _make_report(session, account, "json")
    payload = json.loads(Path(report.path).read_bytes())
    assert any("never as zero" in warning for warning in payload["warnings"])
    assert payload["availability"] == {"not_supported": 1}


def test_report_create_route_honours_the_requested_format(client, session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account, snapshot_ids=[])
    session.commit()

    created = client.post("/analytics/reports", json={"kind": "overview", "account_id": account.id, "format": "pdf"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["format"] == "pdf"
    download = client.get(f"/analytics/reports/{body['id']}/file")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/pdf")
    assert download.content.startswith(b"%PDF-1.4")


def test_report_route_refuses_an_unimplemented_format(client, session):
    account = _account(session)
    session.commit()
    response = client.post("/analytics/reports", json={"kind": "overview", "account_id": account.id, "format": "docx"})
    assert response.status_code == 422


def test_reports_default_to_json_for_a_p8_17_client(client, session):
    account = _account(session)
    session.commit()
    created = client.post("/analytics/reports", json={"kind": "overview", "account_id": account.id})
    assert created.status_code == 201
    assert created.json()["format"] == "json"


# --------------------------------------------------------------------- retention


def _policy(days=365, limit=2000):
    return retention.RetentionPolicy(retention_days=days, batch_limit=limit, now=NOW)


def test_retention_removes_expired_observations_rollups_and_runs(session):
    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=800), finished=NOW - timedelta(days=800))
    fresh = _run(session, account, started=NOW - timedelta(days=2), finished=NOW - timedelta(days=2))
    old_snapshot = _snapshot(session, account, old, collected_at=NOW - timedelta(days=800))
    fresh_snapshot = _snapshot(session, account, fresh, collected_at=NOW - timedelta(days=2))
    expired_rollup = _aggregation(session, account, snapshot_ids=[old_snapshot.id], computed_at=NOW - timedelta(days=800))
    keep = _aggregation(session, account, snapshot_ids=[fresh_snapshot.id], computed_at=NOW - timedelta(days=2))
    session.commit()

    plan = retention.plan(session, _policy())
    assert plan.observations.ids == [old_snapshot.id]
    assert plan.aggregations.ids == [expired_rollup.id]
    assert plan.runs.ids == [old.id]

    result = retention.execute(session, _policy(), dry_run=False)
    session.commit()
    assert result.deleted["observations"] == 1
    assert result.deleted["aggregations"] == 1
    assert result.deleted["collection_runs"] == 1
    assert result.integrity_ok is True
    assert result.foreign_key_violations == 0

    assert session.get(AnalyticsSnapshot, fresh_snapshot.id) is not None
    assert session.get(AnalyticsAggregation, keep.id) is not None
    assert session.get(AnalyticsCollectionRun, fresh.id) is not None
    assert session.get(AnalyticsCollectionRun, old.id) is None


def test_retention_keeps_a_rollup_whose_observations_are_still_here(session):
    account = _account(session)
    run = _run(session, account, started=NOW - timedelta(days=800), finished=NOW - timedelta(days=800))
    live = _snapshot(session, account, run, collected_at=NOW - timedelta(days=2))
    _aggregation(session, account, snapshot_ids=[live.id], computed_at=NOW - timedelta(days=800))
    session.commit()

    plan = retention.plan(session, _policy())
    # The observation is fresh, so neither it nor the rollup that cites it may be deleted, even though
    # the rollup was computed long ago.
    assert plan.observations.count == 0
    assert plan.aggregations.count == 0
    # And the run it belongs to stays, because its observation stays.
    assert plan.runs.count == 0


def test_retention_never_touches_business_history(session):
    account = _account(session)
    job = make_published_job(session, remote_post_id="fb-p824", account=account)
    make_asset(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    _snapshot(session, account, old, collected_at=NOW - timedelta(days=900), publication_id=job.job_id)
    accepted = _recommendation(session, account, status=RecommendationStatus.ACCEPTED, created_at=NOW - timedelta(days=900))
    rejected = _recommendation(session, account, status=RecommendationStatus.REJECTED, created_at=NOW - timedelta(days=900))
    superseded = _recommendation(session, account, status=RecommendationStatus.SUPERSEDED, created_at=NOW - timedelta(days=900))
    definitions = session.scalar(sa.select(sa.func.count()).select_from(AnalyticsMetricDefinition))
    session.commit()

    before = {
        "jobs": session.scalar(sa.select(sa.func.count()).select_from(sa.table("publish_job"))),
        "accounts": session.scalar(sa.select(sa.func.count()).select_from(SocialAccount)),
        "media": session.scalar(sa.select(sa.func.count()).select_from(MediaAsset)),
        "campaigns": session.scalar(sa.select(sa.func.count()).select_from(sa.table("campaign"))),
        "definitions": definitions,
    }
    retention.execute(session, _policy(), dry_run=False)
    session.commit()

    assert session.scalar(sa.select(sa.func.count()).select_from(sa.table("publish_job"))) == before["jobs"]
    assert session.scalar(sa.select(sa.func.count()).select_from(SocialAccount)) == before["accounts"]
    assert session.scalar(sa.select(sa.func.count()).select_from(MediaAsset)) == before["media"]
    assert session.scalar(sa.select(sa.func.count()).select_from(sa.table("campaign"))) == before["campaigns"]
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsMetricDefinition)) == before["definitions"]
    assert session.get(AnalyticsRecommendation, accepted.id) is not None
    assert session.get(AnalyticsRecommendation, rejected.id) is not None
    # The superseded draft is the only recommendation the policy may remove.
    assert session.get(AnalyticsRecommendation, superseded.id) is None


def test_retention_removes_only_superseded_drafts(session):
    account = _account(session)
    for status in (RecommendationStatus.DRAFT, RecommendationStatus.REVIEWED, RecommendationStatus.SUPERSEDED):
        _recommendation(session, account, status=status, created_at=NOW - timedelta(days=900))
    session.commit()
    plan = retention.plan(session, _policy())
    assert plan.recommendations.count == 1
    retention.execute(session, _policy(), dry_run=False)
    session.commit()
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsRecommendation)) == 2
    assert plan.protected["decided_recommendations"] == 0


def test_retention_dry_run_changes_nothing_and_is_audited(session):
    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    _snapshot(session, account, old, collected_at=NOW - timedelta(days=900))
    session.commit()

    result = retention.execute(session, _policy(), dry_run=True)
    session.commit()
    assert result.dry_run is True
    assert result.deleted["observations"] == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot)) == 1
    event = session.get(AuditEvent, result.audit_event_id)
    assert event is not None
    assert event.action == retention.AUDIT_EXECUTE
    assert event.metadata_redacted_json["dry_run"] is True
    assert event.metadata_redacted_json["planned"]["observations"] == 1


def test_retention_execution_records_its_counts_and_is_repeatable(session):
    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    _snapshot(session, account, old, collected_at=NOW - timedelta(days=900))
    session.commit()

    first = retention.execute(session, _policy(), dry_run=False)
    session.commit()
    assert first.deleted["observations"] == 1
    assert first.remaining["observations"] == 0
    event = session.get(AuditEvent, first.audit_event_id)
    assert event.metadata_redacted_json["deleted"]["observations"] == 1

    second = retention.execute(session, _policy(), dry_run=False)
    session.commit()
    assert second.deleted["observations"] == 0
    assert second.integrity_ok is True


def test_retention_is_bounded_by_the_batch_limit(session):
    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    for _ in range(5):
        _snapshot(session, account, old, collected_at=NOW - timedelta(days=900))
    session.commit()

    plan = retention.plan(session, _policy(limit=2))
    assert plan.observations.count == 2
    result = retention.execute(session, _policy(limit=2), dry_run=False)
    session.commit()
    assert result.deleted["observations"] == 2
    assert result.remaining["observations"] == 3
    # The run stays because observations still reference it.
    assert session.get(AnalyticsCollectionRun, old.id) is not None


def test_retention_keeps_an_open_run_and_its_observations(session):
    account = _account(session)
    open_run = _run(session, account, started=NOW - timedelta(days=900), finished=None)
    _snapshot(session, account, open_run, collected_at=NOW - timedelta(days=900))
    session.commit()
    plan = retention.plan(session, _policy())
    assert plan.observations.count == 0
    assert plan.runs.count == 0


def test_retention_removes_expired_reports_and_their_files(session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account, snapshot_ids=[])
    session.commit()
    old_report = _make_report(session, account, "json")
    path = Path(old_report.path)
    assert path.is_file()
    old_report.created_at = NOW - timedelta(days=900)
    session.commit()

    plan = retention.plan(session, _policy())
    assert old_report.id in plan.reports.ids
    assert plan.reports.bytes > 0

    result = retention.execute(session, _policy(), dry_run=False)
    session.commit()
    assert result.deleted["reports"] == 1
    assert not path.exists()
    assert session.get(AnalyticsReport, old_report.id) is None


def test_a_report_is_kept_longer_than_the_observations_it_describes(session):
    account = _account(session)
    run = _run(session, account)
    _snapshot(session, account, run)
    _aggregation(session, account, snapshot_ids=[])
    session.commit()
    report = _make_report(session, account, "json")
    # 400 days old: past a 365-day observation window, inside the 730-day report floor.
    report.created_at = NOW - timedelta(days=400)
    session.commit()
    plan = retention.plan(session, _policy(365))
    assert plan.reports.count == 0
    assert plan.policy.as_dict()["report_retention_days"] == 730


def test_archived_reports_are_removable_whatever_their_age(session):
    account = _account(session)
    session.commit()
    report = _make_report(session, account, "json")
    report.status = AnalyticsReportStatus.ARCHIVED
    session.commit()
    plan = retention.plan(session, _policy())
    assert report.id in plan.reports.ids


def test_retention_removes_an_orphan_report_file(session):
    _account(session)
    session.commit()
    folder = reports_service.resolve_under_root("media_exports", "reports")
    folder.mkdir(parents=True, exist_ok=True)
    orphan = folder / "orphan-2020.json"
    orphan.write_text("{}", encoding="utf-8")
    import os

    old = (NOW - timedelta(days=3)).timestamp()
    os.utime(orphan, (old, old))

    plan = retention.plan(session, _policy())
    assert "orphan-2020.json" in plan.orphan_files.ids
    result = retention.execute(session, _policy(), dry_run=False)
    session.commit()
    assert result.deleted["orphan_files"] == 1
    assert not orphan.exists()


def test_a_referenced_report_file_is_never_treated_as_an_orphan(session):
    account = _account(session)
    session.commit()
    report = _make_report(session, account, "json")
    path = Path(report.path)
    import os

    old = (NOW - timedelta(days=3)).timestamp()
    os.utime(path, (old, old))
    plan = retention.plan(session, _policy())
    assert path.name not in plan.orphan_files.ids


def test_retention_preview_route_is_read_only_and_lists_categories(client, session):
    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    _snapshot(session, account, old, collected_at=datetime.now(UTC) - timedelta(days=900))
    session.commit()

    body = client.post("/analytics/prune", json={"older_than_days": 30, "dry_run": False}).json()
    assert body["applied"] is False
    assert "preview" in body["reason"].lower()
    names = {category["name"] for category in body["categories"]}
    assert {"observations", "aggregations", "collection_runs", "reports", "orphan_files"} <= names
    assert body["total_rows"] >= 1
    assert body["protected"]["accounts"] >= 1
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot)) == 1


def test_retention_execute_route_deletes_and_returns_its_audit_id(client, session):
    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    _snapshot(session, account, old, collected_at=datetime.now(UTC) - timedelta(days=900))
    session.commit()

    response = client.post("/analytics/retention/execute", json={"older_than_days": 30, "dry_run": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False
    assert body["deleted"]["observations"] == 1
    assert body["integrity_ok"] is True
    assert body["audit_event_id"]
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot)) == 0


def test_retention_execute_defaults_to_a_dry_run(client, session):
    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    _snapshot(session, account, old, collected_at=datetime.now(UTC) - timedelta(days=900))
    session.commit()
    body = client.post("/analytics/retention/execute", json={"older_than_days": 30}).json()
    assert body["dry_run"] is True
    assert body["deleted"]["observations"] == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(AnalyticsSnapshot)) == 1


# --------------------------------------------------------------------- version watch


def test_a_recorded_version_table_is_reported_as_knowledge_of_its_date(session):
    state = version_watch.load(session)
    assert state["checked_at"] is None
    assert state["status"] in {"never_checked", "stale"}
    assert state["documented"]["facebook"] == "v27.0"
    assert "not as a live verification" in state["note"]


def test_observing_versions_records_the_check_without_bumping_anything(session):
    from creatoros.db.enums import OAuthRoute
    from creatoros.db.models import ProviderAppConfig

    session.add(
        ProviderAppConfig(
            provider=Platform.FACEBOOK,
            route=OAuthRoute.FACEBOOK_LOGIN,
            client_id="cid",
            redirect_uri="http://127.0.0.1:47821/oauth/callback/facebook_login",
            api_version="v23.0",
            app_mode="development",
        )
    )
    session.commit()

    observation = version_watch.observe(session, fetch=False)
    session.commit()
    assert observation.status in {"current", "stale"}
    assert observation.method == "recorded_table"
    assert observation.documented["facebook"] == "v27.0"

    config = session.scalar(sa.select(ProviderAppConfig))
    assert config.api_version == "v23.0", "a version check must never change the configured version"
    stored = version_watch.load(session)
    assert stored["checked_at"] == observation.checked_at.isoformat()


def test_an_observation_older_than_ninety_days_is_stale(session):
    version_watch.observe(session, now=NOW - timedelta(days=120), fetch=False)
    session.commit()
    state = version_watch.load(session, now=NOW)
    assert state["status"] == "stale"
    assert state["age_days"] == 120
    assert "stale" in state["note"]


def test_a_failed_live_check_is_unavailable_not_a_fabricated_version(session, monkeypatch):
    import urllib.error

    def fail(url, *, timeout=5.0):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(version_watch, "_fetch_source", fail)
    observation = version_watch.observe(session, fetch=True)
    session.commit()
    assert observation.status == "unavailable"
    assert observation.method == "live_fetch"
    assert "could not reach any official source" in observation.note
    # The documented table is unchanged: an unreachable provider is not a new version.
    assert observation.documented["facebook"] == "v27.0"


def test_versions_route_reports_observation_age_and_sunset_dates(client, session):
    _account(session)
    session.commit()
    rows = client.get("/analytics/versions").json()["platforms"]
    facebook = next(row for row in rows if row["platform"] == "facebook")
    assert facebook["configured"]
    assert facebook["latest_documented"] == "v27.0"
    assert facebook["observation_status"] in {"never_checked", "current", "stale"}
    assert facebook["observation_age_days"] >= 0
    assert "v19.0" in facebook["sunset_dates"]
    assert facebook["endpoint_changes"], "endpoint changes are recorded per platform"
    assert facebook["configured_source"] in {"provider_app_config", "code_default"}


def test_version_check_route_records_an_observation(client, session):
    response = client.post("/analytics/versions/check")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"current", "stale", "unavailable"}
    assert body["documented"]["tiktok"] == "v2"
    assert session.scalar(sa.select(sa.func.count()).select_from(AuditEvent)) >= 1


# --------------------------------------------------------------------- diagnostics


def test_diagnostics_state_the_maintenance_facts(client, session):
    account = _account(session)
    run = _run(session, account, finished=datetime.now(UTC) - timedelta(hours=3))
    _snapshot(session, account, run, collected_at=datetime.now(UTC) - timedelta(hours=3))
    session.commit()

    body = client.get("/analytics/diagnostics").json()
    assert body["retention"]["policy"]["retention_days"] == 365
    assert "eligible" in body["retention"]
    assert body["retention"]["last_execution"] is None
    assert body["reports"]["total"] == 0
    assert body["reports"]["formats_implemented"] == ["json", "csv", "pdf"]
    assert body["collection"]["accounts"] == 1
    assert body["collection"]["newest_observation_age_hours"] is not None
    assert body["version_watch"]["status"] in {"never_checked", "current", "stale"}
    assert any("retention has never been executed" in warning for warning in body["warnings"])


def test_diagnostics_says_never_collected_instead_of_zero(client, session):
    account = _account(session)
    session.add(
        __import__("creatoros.db.models", fromlist=["AnalyticsSyncState"]).AnalyticsSyncState(
            account_id=account.id,
            platform=account.platform,
            object_type=AnalyticsObjectType.ACCOUNT,
        )
    )
    session.commit()
    body = client.get("/analytics/diagnostics").json()
    assert body["collection"]["never_collected_accounts"] == [account.id]
    assert body["collection"]["newest_observation_at"] is None
    assert body["collection"]["newest_observation_age_hours"] is None
    assert any("never completed a collection" in warning for warning in body["warnings"])


def test_diagnostics_flags_a_report_row_with_a_missing_file(client, session):
    account = _account(session)
    session.commit()
    report = _make_report(session, account, "json")
    Path(report.path).unlink()
    session.commit()
    body = client.get("/analytics/diagnostics").json()
    assert body["reports"]["rows_without_file"] == 1
    assert any("missing" in warning for warning in body["warnings"])


def test_version_reports_the_report_and_retention_capabilities(client):
    body = client.get("/version").json()
    assert body["api"] == "phase-8.24"
    capabilities = body["capabilities"]
    assert capabilities["analytics_reports"] == "available"
    assert capabilities["analytics_reports_formats"] == "json,csv,pdf"
    assert capabilities["analytics_retention"] in {"available", "never_executed"}
    assert capabilities["analytics_version_watch"] in {"current", "stale", "never_checked", "unavailable"}
    assert capabilities["analytics_version_watch_fetch"] == "recorded_table_only"


# --------------------------------------------------------------------- scheduler wiring


def test_the_sweeper_queues_retention_and_a_version_check_when_due(session):
    from creatoros.db.models import QueueJob
    from creatoros.services.analytics.scheduler import AnalyticsScheduler

    tick = AnalyticsScheduler(session, now=datetime.now(UTC)).tick()
    assert tick.retention_queued is True
    assert tick.version_check_queued is True
    session.commit()

    types = {job.job_type for job in session.scalars(sa.select(QueueJob))}
    assert "analytics.retention" in types
    assert "analytics.version_check" in types
    # They are on the analytics lane, not a fifth one.
    assert {job.queue for job in session.scalars(sa.select(QueueJob))} == {"analytics"}


def test_a_second_tick_does_not_queue_a_duplicate_retention_job(session):
    from creatoros.db.models import QueueJob
    from creatoros.services.analytics.scheduler import AnalyticsScheduler

    AnalyticsScheduler(session, now=datetime.now(UTC)).tick()
    session.commit()
    AnalyticsScheduler(session, now=datetime.now(UTC)).tick()
    session.commit()
    retention_jobs = [
        job for job in session.scalars(sa.select(QueueJob)) if job.job_type == "analytics.retention"
    ]
    assert len(retention_jobs) == 1


def test_the_retention_job_handler_applies_the_policy(session):
    from creatoros.services.analytics.jobs import build_analytics_handlers

    account = _account(session)
    old = _run(session, account, started=NOW - timedelta(days=900), finished=NOW - timedelta(days=900))
    _snapshot(session, account, old, collected_at=NOW - timedelta(days=900))
    session.commit()

    handlers = build_analytics_handlers(session)
    job = type("Job", (), {"payload": {"retention_days": 30, "batch_limit": 100}, "id": "job-1"})()
    payload = handlers["analytics.retention"](session, job)
    session.commit()
    assert payload["deleted"]["observations"] == 1
    assert payload["integrity_ok"] is True
    assert payload["work_remaining"] == 0


def test_the_version_check_job_handler_records_an_observation(session):
    from creatoros.services.analytics.jobs import build_analytics_handlers

    handlers = build_analytics_handlers(session)
    job = type("Job", (), {"payload": {}, "id": "job-2"})()
    payload = handlers["analytics.version_check"](session, job)
    session.commit()
    assert payload["documented"]["facebook"] == "v27.0"
    assert payload["status"] in {"current", "stale"}
