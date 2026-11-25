# ADR-29 — One analytics content model, three renderers, digest over the bytes written

**Status:** Accepted, **implemented + tested** (P8.24 — 40 retention/report tests, 17 frontend
tests).

## Context

P8.17 built `services/analytics/reports.py` as a JSON skeleton: `create_report()` assembled a dict and
`json.dumps(..., indent=2)` wrote it. The record stored `path`, `sha256` and `bytes`, and the desktop
"Reports" page verified a download against that digest (P8.23). Three things were therefore already
true and had to stay true:

1. the digest is the *only* integrity guarantee the GUI has a use for;
2. the GUI never parses the file, so its shape is not part of the desktop contract;
3. the report content is derived from stored aggregations and recommendations, never recomputed at
   download time.

The remaining P8.24 requirement was "report formats" — CSV and PDF — without turning the report
service into three divergent code paths, and without letting the digest describe something other than
the bytes a person actually receives.

## Decision

**One content model, three renderers, digest over the rendered bytes.**

* `build_content()` produces a single ordered structure of sections, each a list of flat rows
  (`{field: value}`), with `None` preserved rather than rendered as `0`.
* `render_json`, `render_csv`, `render_pdf` each consume that structure. A format cannot report a
  number the content model does not carry, because it does not query anything.
* The digest is `sha256` of the bytes that were written to disk, computed **after** rendering and
  before the rename, so `GET /analytics/reports/{id}/file` and `ReportOut.sha256` can never disagree.
* Writes are `name.part` → `os.replace()`, so a crashed write leaves no half report with a valid
  digest.
* The format is stored in `analytics_report.filters_json["format"]` and mirrored on `ReportOut`; no
  column was added. `AnalyticsReportFormat` in `db/enums.py` is the list of formats that are
  *actually implemented*, and `resolve_format()` refuses anything else with HTTP 422 rather than
  silently falling back to JSON.
* PDF is written by `services/analytics/pdf.py`, a dependency-free PDF 1.4 writer (Helvetica,
  WinAnsi encoding, correct xref offsets). Adding a PDF library would have meant a new runtime
  dependency and a font-bundling question for an unsigned installer; a table-and-text report does not
  need either.
* Limits are recorded *in the report*, not silently applied: `MAX_AGGREGATION_ROWS = 500` and
  `MAX_RECOMMENDATIONS = 100` add a row stating that the list was truncated and how many rows exist.

## Consequences

* A report can be regenerated identically for the same data, but is never regenerated automatically —
  the stored file is the artefact.
* CSV is a single flat table with a `section` column, so a section's rows stay together in a
  spreadsheet without a second header row.
* An unavailable metric appears in every format with its availability state and an empty value, never
  as `0`. The JSON renderer emits `null`, CSV emits an empty cell, PDF emits `—` plus the state.
* If a future format (XLSX, HTML) is added, it must be added to `AnalyticsReportFormat`, to a
  renderer, and to the frontend `REPORT_FORMATS` list; the desktop page renders its dropdown from
  that list rather than from prose, so a format the backend cannot write is not offered.
