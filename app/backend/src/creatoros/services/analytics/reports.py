"""Analytics report generation: one content model, three file formats.

A report is a *snapshot of what was stored at the moment it was asked for*: the aggregation rows in
scope, the metric definitions they came from, and the recommendations that existed. It computes nothing
new and invents no metric — if nothing was measured, the report says so, in every format.

The contract P8.23 verifies on download is a property of this module: ``sha256`` and ``bytes`` are taken
from the **bytes actually written to disk**, so the digest a client checks is the digest of the file it
received. The file is written to a ``.part`` name and renamed only after both values are known, so a
crash cannot leave a report row pointing at a half-written file.

Formats (P8.24):

* **json** — the machine-readable form, sections preserved.
* **csv** — RFC 4180, one flat table with a ``section`` column, CRLF row endings for spreadsheet
  compatibility, and a header block carrying the same facts as the other two formats.
* **pdf** — rendered by the local dependency-free writer in ``pdf.py``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.paths import ensure_dir, is_inside_root, resolve_under_root
from ...db.enums import (
    AnalyticsReportFormat,
    AnalyticsReportKind,
    AnalyticsReportStatus,
    MetricAvailability,
    RecommendationStatus,
)
from ...db.models import (
    AnalyticsAggregation,
    AnalyticsMetricDefinition,
    AnalyticsRecommendation,
    AnalyticsReport,
    SocialAccount,
)
from . import pdf

#: Rows per file. A report is a readable artefact, not a full dump; the CSV export endpoints exist for
#: the complete ledger, and the report says which cap applied.
MAX_AGGREGATION_ROWS = 500
MAX_RECOMMENDATIONS = 100


class ReportNotFound(LookupError):
    code = "REPORT_NOT_FOUND"


class ReportFormatUnsupported(ValueError):
    code = "REPORT_FORMAT_UNSUPPORTED"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def resolve_format(value: str | AnalyticsReportFormat | None) -> AnalyticsReportFormat:
    if value is None:
        return AnalyticsReportFormat.JSON
    try:
        return AnalyticsReportFormat(str(value))
    except ValueError as exc:
        implemented = ", ".join(item.value for item in AnalyticsReportFormat)
        raise ReportFormatUnsupported(f"unsupported report format {value!r}; implemented: {implemented}") from exc


def _scope(session: Session, account_id: str | None, period_start, period_end) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "account_id": account_id,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
    }
    if account_id:
        account = session.get(SocialAccount, account_id)
        if account is None:
            raise ReportNotFound(f"account {account_id} does not exist")
        scope["platform"] = str(account.platform)
        scope["display_name"] = account.display_name
        scope["username"] = account.username
    return scope


def build_sections(
    session: Session,
    *,
    kind: AnalyticsReportKind,
    account_id: str | None,
    period_start: datetime | None,
    period_end: datetime | None,
) -> list[dict[str, Any]]:
    scope = _scope(session, account_id, period_start, period_end)

    stmt = sa.select(AnalyticsAggregation)
    if account_id:
        stmt = stmt.where(AnalyticsAggregation.account_id == account_id)
    if period_start:
        stmt = stmt.where(AnalyticsAggregation.computed_at >= period_start)
    if period_end:
        stmt = stmt.where(AnalyticsAggregation.computed_at <= period_end)
    stmt = stmt.order_by(AnalyticsAggregation.metric_key, AnalyticsAggregation.window_kind)
    total_aggregations = int(session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery())) or 0)
    aggregations = list(session.scalars(stmt.limit(MAX_AGGREGATION_ROWS)))

    rec_stmt = sa.select(AnalyticsRecommendation).where(
        AnalyticsRecommendation.status != RecommendationStatus.SUPERSEDED
    )
    if account_id:
        rec_stmt = rec_stmt.where(AnalyticsRecommendation.account_id == account_id)
    recommendations = list(
        session.scalars(rec_stmt.order_by(AnalyticsRecommendation.created_at.desc()).limit(MAX_RECOMMENDATIONS))
    )

    platforms = sorted({str(row.platform) for row in aggregations} | ({scope["platform"]} if scope.get("platform") else set()))
    accounts = sorted({row.account_id for row in aggregations} | ({account_id} if account_id else set()))
    metric_keys = sorted({row.metric_key for row in aggregations})
    windows = sorted({row.window_kind.value for row in aggregations})

    definitions = {
        row.metric_key: row
        for row in session.scalars(
            sa.select(AnalyticsMetricDefinition).where(AnalyticsMetricDefinition.metric_key.in_(metric_keys or [""]))
        )
    }
    availability: dict[str, int] = {}
    for row in aggregations:
        key = row.availability.value
        availability[key] = availability.get(key, 0) + 1

    unavailable = sorted(
        {row.metric_key for row in aggregations if row.availability is not MetricAvailability.OBSERVED}
    )
    warnings: list[str] = []
    if total_aggregations > len(aggregations):
        warnings.append(
            f"the report includes the first {len(aggregations)} of {total_aggregations} stored aggregation "
            "rows; use the CSV export endpoints for the complete ledger"
        )
    if unavailable:
        warnings.append(
            f"{len(unavailable)} metric(s) were not observed in this period and are recorded as absent, "
            "never as zero: " + ", ".join(unavailable)
        )
    if not aggregations:
        warnings.append("no aggregation rows matched this scope; the report contains no measured values")
    if not platforms:
        warnings.append("no platform is in scope for this report")

    return [
        {"heading": "Scope", "type": "scope", "body": scope},
        {
            "heading": "Platforms and accounts",
            "type": "coverage",
            "body": {
                "platforms": platforms,
                "account_ids": accounts,
                "observation_windows": windows,
                "metric_keys": metric_keys,
                "aggregation_rows_included": len(aggregations),
                "aggregation_rows_available": total_aggregations,
            },
        },
        {
            "heading": "Metric definitions used",
            "type": "definitions",
            "body": [
                {
                    "metric_key": key,
                    "provider_metric_name": definitions[key].provider_metric_name if key in definitions else None,
                    "unit": definitions[key].unit if key in definitions else None,
                    "metric_class": definitions[key].metric_class if key in definitions else None,
                    "is_estimate": definitions[key].is_estimate if key in definitions else None,
                    "source_doc_url": definitions[key].source_doc_url if key in definitions else None,
                }
                for key in metric_keys
            ],
        },
        {
            "heading": "Measured aggregations",
            "type": "aggregations",
            "body": [
                {
                    "metric_key": row.metric_key,
                    "scope": row.scope.value,
                    "window_kind": row.window_kind.value,
                    "value": row.value_num,
                    "value_json": row.value_json,
                    "unit": row.unit,
                    "availability": row.availability.value,
                    "sample_size": row.sample_size,
                    "method": str(row.method),
                    "input_snapshot_ids": list(row.input_snapshot_ids_json or [])[:20],
                    "computed_at": row.computed_at.isoformat() if row.computed_at else None,
                    "aggregation_version": row.aggregation_version,
                }
                for row in aggregations
            ],
        },
        {"heading": "Data availability", "type": "availability", "body": availability},
        {"heading": "Warnings", "type": "warnings", "body": warnings},
        {
            "heading": "Recommendations",
            "type": "recommendations",
            "body": [
                {
                    "recommendation_id": row.id,
                    "kind": row.kind.value,
                    "status": row.status.value,
                    "observation": row.observation,
                    "limitations": list(row.limitations_json or []),
                    "confidence": row.confidence,
                    "confidence_method": row.confidence_method,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in recommendations
            ],
        },
        {
            "heading": "Sources",
            "type": "sources",
            "body": {
                "ledger": "analytics_snapshot / analytics_aggregation",
                "registry": "analytics_metric_definition",
                "recommendations": "analytics_recommendation",
                "provider_docs": sorted({definitions[key].source_doc_url for key in metric_keys if key in definitions}),
            },
        },
    ]


# ---------------------------------------------------------------------- renderers


def _csv_text(header: dict[str, Any], sections: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["section", "field", "value"])
    for key, value in header.items():
        writer.writerow(["report", key, json.dumps(value, default=str) if isinstance(value, (dict, list)) else value])
    for section in sections:
        heading = section["heading"]
        body = section.get("body")
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    for key, value in item.items():
                        rendered = (
                            json.dumps(value, default=str, sort_keys=True)
                            if isinstance(value, (dict, list))
                            else value
                        )
                        writer.writerow([heading, key, rendered])
                else:
                    writer.writerow([heading, "", item])
        elif isinstance(body, dict):
            for key, value in body.items():
                writer.writerow([heading, key, json.dumps(value, default=str) if isinstance(value, (dict, list)) else value])
        else:
            writer.writerow([heading, "", body])
    return buffer.getvalue()


def _pdf_bytes(header: dict[str, Any], sections: list[dict[str, Any]], *, title: str, footer: str) -> bytes:
    blocks: list[tuple[str, list[str]]] = []
    header_lines = [f"{key.replace('_', ' ')}: {value}" for key, value in header.items()]
    blocks.append(("Report", header_lines))
    for section in sections:
        heading = str(section["heading"])
        body = section.get("body")
        lines: list[str] = []
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    lines.append("  ".join(f"{key}={item[key]}" for key in item))
                else:
                    lines.append(str(item))
            if not lines:
                lines.append("(none)")
        elif isinstance(body, dict):
            lines.extend(f"{key}: {json.dumps(value) if isinstance(value, (dict, list)) else value}" for key, value in body.items())
            if not lines:
                lines.append("(none)")
        else:
            lines.append(str(body))
        blocks.append((heading, lines))
    return pdf.render_pdf(title, blocks, footer)


def _json_bytes(header: dict[str, Any], sections: list[dict[str, Any]], *, summary: str, observations: int) -> bytes:
    payload = {
        "title": header["title"],
        "report_id": header["report_id"],
        "format": header["format"],
        "generated_at": header["generated_at"],
        "kind": header["kind"],
        "period": {"start": header["period_start"], "end": header["period_end"]},
        "platforms": header["platforms"],
        "accounts": header["accounts"],
        "metric_keys": header["metric_keys"],
        "observation_windows": header["observation_windows"],
        "availability": header["availability"],
        "warnings": header["warnings"],
        "sources": header["sources"],
        "summary": summary,
        "observation_count": observations,
        "sections": sections,
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")


# ---------------------------------------------------------------------- service


def create_report(
    session: Session,
    *,
    kind: AnalyticsReportKind,
    account_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    title: str | None = None,
    generated_by: str = "local-user",
    report_format: str | AnalyticsReportFormat | None = AnalyticsReportFormat.JSON,
) -> AnalyticsReport:
    """Build, write and record a report. The digest is of the bytes written, and nothing else."""
    fmt = resolve_format(report_format)
    sections = build_sections(
        session, kind=kind, account_id=account_id, period_start=period_start, period_end=period_end
    )
    coverage = sections[1]["body"]
    measured = len(sections[3]["body"])
    observations = sum(int(item.get("sample_size") or 0) for item in sections[3]["body"])
    summary = f"{kind.value} report: {measured} aggregation row(s), {len(sections[6]['body'])} recommendation(s)."

    report = AnalyticsReport(
        kind=kind,
        period_start=period_start,
        period_end=period_end,
        filters_json={"account_id": account_id, "title": title, "format": fmt.value},
        sections_json=sections,
        summary=summary,
        status=AnalyticsReportStatus.DRAFT,
        generated_by=generated_by,
    )
    session.add(report)
    session.flush()

    report_id = report.id
    generated_at = datetime.now(UTC)
    header: dict[str, Any] = {
        "report_id": report_id,
        "format": fmt.value,
        "kind": kind.value,
        "title": title or f"{kind.value} analytics report",
        "generated_at": generated_at.isoformat(),
        "period_start": sections[0]["body"]["period_start"],
        "period_end": sections[0]["body"]["period_end"],
        "platforms": coverage["platforms"],
        "accounts": coverage["account_ids"],
        "metric_keys": coverage["metric_keys"],
        "observation_windows": coverage["observation_windows"],
        "aggregation_rows": coverage["aggregation_rows_included"],
        "availability": sections[4]["body"],
        "warnings": sections[5]["body"],
        "sources": sections[7]["body"],
    }
    footer = f"CreatorOS report {report_id} · {fmt.value.upper()} · generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')}"

    if fmt is AnalyticsReportFormat.JSON:
        payload = _json_bytes(header, sections, summary=summary, observations=observations)
    elif fmt is AnalyticsReportFormat.CSV:
        payload = _csv_text(header, sections).encode("utf-8")
    else:
        payload = _pdf_bytes(header, sections, title=header["title"], footer=footer)

    digest = _sha256_bytes(payload)
    ensure_dir("media_exports", "reports")
    stamp = generated_at.strftime("%Y%m%dT%H%M%S%f")
    filename = f"{stamp}-analytics-{kind.value}-{report_id}{fmt.extension}"
    final_path = resolve_under_root("media_exports", "reports", filename)
    part_path = Path(str(final_path) + ".part")
    try:
        part_path.write_bytes(payload)
        if not is_inside_root(part_path):
            raise ReportNotFound("refusing to write a report outside the data root")
        part_path.replace(final_path)
    except Exception:
        # Never leave a partial file behind, and never let the row point at one.
        for candidate in (part_path, final_path):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
        session.delete(report)
        session.flush()
        raise

    report.path = str(final_path)
    report.sha256 = digest
    report.bytes = len(payload)
    report.status = AnalyticsReportStatus.FINAL
    session.flush()
    return report


def report_file(report: AnalyticsReport) -> Path:
    """The stored file, but only if it is inside the data root (never an arbitrary path)."""
    if not report.path or not is_inside_root(report.path):
        raise ReportNotFound("this report has no stored file inside the data root")
    path = Path(report.path)
    if not path.is_file():
        raise ReportNotFound("the report file is missing")
    return path


def report_format(report: AnalyticsReport) -> AnalyticsReportFormat:
    return resolve_format((report.filters_json or {}).get("format"))


def report_media_type(report: AnalyticsReport) -> str:
    return report_format(report).media_type


def delete_report_file(report: AnalyticsReport) -> None:
    try:
        path = report_file(report)
    except ReportNotFound:
        return
    path.unlink(missing_ok=True)


__all__ = [
    "MAX_AGGREGATION_ROWS",
    "MAX_RECOMMENDATIONS",
    "ReportFormatUnsupported",
    "ReportNotFound",
    "build_sections",
    "create_report",
    "delete_report_file",
    "report_file",
    "report_format",
    "report_media_type",
    "resolve_format",
]
