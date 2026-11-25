# Analytics reports and exports (P8.24)

A report is a saved snapshot of analytics the database already holds. It **computes nothing** and
invents no metric: `build_sections()` reads aggregation rows, their metric definitions, and the
recommendations that existed when the report was created. If nothing was measured for a metric, the
report says so in every format.

The table below is the whole contract.

| | |
| --- | --- |
| Service | `app/backend/src/creatoros/services/analytics/reports.py` |
| Formats | `json`, `csv`, `pdf` (`AnalyticsReportFormat` in `db/enums.py`) |
| PDF writer | `services/analytics/pdf.py` — dependency-free PDF 1.4, Helvetica, WinAnsi |
| Location | `<data_root>/media_exports/reports/{YYYYmmddTHHMMSSffffff}-analytics-{kind}-{report_id}.{ext}` |
| Integrity | `sha256` and `bytes` taken from the bytes **written to disk**, after rendering, before rename |
| Atomicity | written as `….part`, then `os.replace()`; a crash leaves no row pointing at a half file |
| Limits | `MAX_AGGREGATION_ROWS = 500`, `MAX_RECOMMENDATIONS = 100`, and the report states that a cap applied |
| Status | rows start `draft`; `final` / `archived` are explicit |

## Creating one

```
POST /analytics/reports
{"kind": "overview", "format": "csv", "account_id": null,
 "period_start": "2026-11-01T00:00:00Z", "period_end": "2026-11-24T00:00:00Z",
 "title": "November review"}
```

`kind` is one of `overview`, `platform_comparison`, `account`, `campaign`, `content_comparison`,
`recommendations`, `experiment`. `format` defaults to `json`; an unknown format is refused with HTTP
422 `REPORT_FORMAT_UNSUPPORTED` — it is never silently downgraded to JSON, because a person who asked
for a PDF and received JSON would have no way to tell.

The title is stored inside `filters_json` (`filters.title`) rather than in a column, so `ReportOut` has
no `title` field; the desktop page reads `filters.title` and explains that where a reader would look
for one.

## Downloading one — and why the digest matters

```
GET /analytics/reports/{id}        -> metadata, including sha256, bytes, format
GET /analytics/reports/{id}/file   -> the raw file bytes, Content-Type per format
```

The file is served only when the stored path is inside the configured data root; a row whose path
escapes the root is not served at all.

The desktop page (`pages/analytics/Reports.tsx`) refuses to offer the file until:

1. `sha256` is present in the metadata (`failure: no_expected_hash` otherwise);
2. the response has a body (`failure: no_bytes`);
3. `SHA-256(bytes)` equals the recorded digest (`failure: digest_mismatch`, both digests shown);
4. the body was fetched as bytes and **not** parsed and re-serialised (`requestBytes()` in
   `services/apiAnalytics.ts`).

Only a verified payload gets a "Save verified file" button, and the saved name keeps the extension of
the format that was actually generated. A mismatch is never retried, renamed or regenerated: a repeated
mismatch points at storage or transport, not at the download.

## What each format contains

| | JSON | CSV | PDF |
| --- | --- | --- | --- |
| Header facts | `report`, `scope`, `limits` keys | `header` section rows | first-page summary |
| Sections | preserved as arrays | one flat table, `section` column | one table per section, page breaks |
| A missing metric | `null` + availability state | empty cell + availability state | `—` + availability state |
| CRLF | n/a | row endings | n/a |

CSV is RFC 4180 with CRLF row endings so Excel opens it without a warning. Unavailable values are empty
cells, never `0` — the availability state sits in the same row so the absence keeps its reason.

## Exports versus reports

`GET /analytics/export/snapshots` (CSV) and `GET /analytics/export/aggregations` (CSV) stream the
underlying rows with no cap, for a spreadsheet workflow. They are not reports:

* an export is not stored, has no digest, and cannot be re-downloaded;
* an export is generated on request from the current table, so two exports a minute apart can differ;
* a report is a stored artefact with a digest, and it is kept for at least two years (see
  `ANALYTICS_RETENTION.md`).

## Verified on this machine

* `tests/test_analytics_retention_reports.py` — 40 tests: all three formats, digest equals the bytes on
  disk, cap rows present, PDF multi-page, a `.part` file never left behind, unknown format refused,
  file outside the data root not served, missing file reported by diagnostics.
* `app/desktop/tests/analyticsPagesP824.test.mjs` — CSV download verified, format forwarded on create,
  PDF bytes kept with their media type.
* Screenshots: `docs/evidence/p8-analytics-reports-csv{,-2,-3}.png`.

**Not verified here:** no report has been produced from live provider data, because this machine has no
connected Facebook Page, Instagram professional account or approved TikTok app. All report evidence is
fixture data (category B).
