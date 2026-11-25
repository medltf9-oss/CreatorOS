// P8.24 frontend logic + client. Run with: node --test tests/analyticsPagesP824.test.mjs
//
// The retention panel and the report-format selector exist to keep two claims honest:
//
// * a preview never deletes, and the button that does delete is armed only by typing the exact word;
// * the page offers exactly the formats the backend writes, and a verified download keeps the
//   extension of the format that was actually generated.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { AnalyticsApi, AnalyticsApiError } from '../src/services/apiAnalytics.ts';
import {
  REPORT_FORMATS,
  RETENTION_CONFIRM_WORD,
  reportFormatLabel,
  reportTitle,
  retentionArmed,
  retentionCountLabel,
  retentionView,
} from '../src/components/analyticsDecisions.ts';

const realFetch = globalThis.fetch;
let calls;

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body) };
}

function stub(handler) {
  calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, init });
    return handler(url, init);
  };
}

const api = () => new AnalyticsApi('http://127.0.0.1:9999', 'test-token');

const PREVIEW = {
  policy: {
    retention_days: 365,
    report_retention_days: 730,
    batch_limit: 2000,
    cutoff: '2025-11-24T00:00:00Z',
    report_cutoff: '2024-11-24T00:00:00Z',
  },
  categories: [
    { name: 'aggregations', count: 0, reason: 'no rollup is redundant', bytes: 0 },
    { name: 'observations', count: 1841, reason: 'collected before 2025-11-24 by a finished run', bytes: 0 },
    { name: 'reports', count: 1, reason: 'generated before 2024-11-24 or archived', bytes: 551 },
  ],
  total_rows: 1842,
  total_bytes: 551,
  protected: { publications: 8, campaigns: 2, accounts: 3 },
  applied: false,
  reason: 'preview only: nothing was deleted',
};

// ---------------------------------------------------------------- retention view model

test('the retention view keeps the backend reason for every category', () => {
  const view = retentionView(PREVIEW);
  assert.equal(view.rows, 1842);
  assert.equal(view.bytes, 551);
  const observations = view.categories.find((category) => category.name === 'observations');
  assert.equal(observations.count, 1841);
  assert.match(observations.reason, /collected before 2025-11-24/);
  // An empty category is dropped from the table, except observations which is always shown.
  assert.equal(view.categories.some((category) => category.name === 'aggregations'), false);
});

test('the retention view names what is protected', () => {
  const view = retentionView(PREVIEW);
  assert.deepEqual(view.protectedRows, [
    { name: 'publications', count: 8 },
    { name: 'campaigns', count: 2 },
    { name: 'accounts', count: 3 },
  ]);
});

test('the retention view exposes both cutoffs, which differ by design', () => {
  const view = retentionView(PREVIEW);
  assert.equal(view.policy.retentionDays, 365);
  assert.equal(view.policy.reportRetentionDays, 730);
  assert.notEqual(view.policy.cutoff, view.policy.reportCutoff);
});

test('deletion is armed only by typing the exact word', () => {
  assert.equal(RETENTION_CONFIRM_WORD, 'DELETE');
  assert.equal(retentionArmed('DELETE'), true);
  assert.equal(retentionArmed('  delete  '), true);
  assert.equal(retentionArmed('DELET'), false);
  assert.equal(retentionArmed('DELETE EVERYTHING'), false);
  assert.equal(retentionArmed(''), false);
  assert.equal(retentionArmed('yes'), false);
});

// ---------------------------------------------------------------- report formats

test('the page offers exactly json, csv and pdf', () => {
  assert.deepEqual([...REPORT_FORMATS], ['json', 'csv', 'pdf']);
  assert.equal(reportFormatLabel('json'), 'JSON');
  assert.equal(reportFormatLabel('pdf'), 'PDF');
});

test('a report title still comes from filters, whatever the format', () => {
  const report = {
    id: 'rep-1',
    kind: 'overview',
    filters: { title: 'Quarterly review', format: 'csv' },
    format: 'csv',
  };
  assert.equal(reportTitle(report), 'Quarterly review');
});

// ---------------------------------------------------------------- client: retention

test('the preview posts to /analytics/prune with dry_run forced on', async () => {
  stub(() => jsonResponse(PREVIEW));
  const preview = await api().retentionPreview({ older_than_days: 30 });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/analytics/prune');
  assert.equal(calls[0].init.method, 'POST');
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.dry_run, true, 'the preview path must never ask for a real deletion');
  assert.equal(body.older_than_days, 30);
  assert.equal(preview.total_rows, 1842);
});

test('the execution posts to /analytics/retention/execute and reports the audit record', async () => {
  stub(() =>
    jsonResponse({
      dry_run: false,
      deleted: { observations: 1841, collection_runs: 42 },
      remaining: { observations: 0 },
      bytes_freed: 551,
      audit_event_id: 'audit-1',
      foreign_key_violations: 0,
      integrity_ok: true,
      duration_ms: 41,
      errors: [],
    }),
  );
  const result = await api().retentionExecute({ dry_run: false });
  assert.equal(new URL(calls[0].url).pathname, '/analytics/retention/execute');
  assert.deepEqual(JSON.parse(calls[0].init.body), { dry_run: false });
  assert.equal(result.deleted.observations, 1841);
  assert.equal(result.audit_event_id, 'audit-1');
  assert.equal(result.integrity_ok, true);
});

test('a refused retention execution surfaces the backend error unchanged', async () => {
  stub(() => jsonResponse({ code: 'VALIDATION_ERROR', message: 'batch_limit must be between 1 and 20000' }, 422));
  await assert.rejects(
    () => api().retentionExecute({ dry_run: false, batch_limit: 999999 }),
    (error) => error instanceof AnalyticsApiError && error.status === 422 && /batch_limit/.test(error.message),
  );
});

// ---------------------------------------------------------------- client: report format

test('createReport forwards the requested format', async () => {
  stub(() =>
    jsonResponse(
      {
        id: 'rep-9',
        kind: 'overview',
        period_start: null,
        period_end: null,
        filters: { format: 'pdf' },
        sections: [],
        summary: null,
        sha256: 'b'.repeat(64),
        bytes: 10,
        status: 'final',
        generated_by: 'analytics.reports',
        created_at: '2026-11-24T00:00:00Z',
        format: 'pdf',
      },
      201,
    ),
  );
  const created = await api().createReport({ kind: 'overview', format: 'pdf' });
  assert.deepEqual(JSON.parse(calls[0].init.body), { kind: 'overview', format: 'pdf' });
  assert.equal(created.format, 'pdf');
});

test('a PDF download keeps its bytes and its media type', async () => {
  const payload = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x34]);
  stub(() => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/pdf' },
    arrayBuffer: async () => payload.buffer,
    text: async () => '',
  }));
  const download = await api().downloadReport('rep-9');
  assert.equal(download.bytes.byteLength, 8);
  assert.equal(download.contentType, 'application/pdf');
});

// ---------------------------------------------------------------- source invariants

const read = (name) => readFileSync(new URL(`../src/pages/analytics/${name}`, import.meta.url), 'utf8');
const withoutComments = (source) => source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const SETTINGS = withoutComments(read('AnalyticsSettings.tsx'));
const REPORTS = withoutComments(read('Reports.tsx'));

test('the settings page previews before it can execute, and the execute button is armed by typing', () => {
  assert.match(SETTINGS, /Preview what would be deleted/);
  assert.match(SETTINGS, /api\.retentionPreview\(\{\}\)/);
  assert.match(SETTINGS, /disabled=\{!retentionArmed\(typed\)\}/);
  assert.match(SETTINGS, /RETENTION_CONFIRM_WORD/);
  assert.match(SETTINGS, /Executing retention is permanent/);
});

test('the retention panel states what is never deleted', () => {
  assert.match(SETTINGS, /never touched|no\s+publication, campaign, account, media asset, metric definition or decided recommendation is ever touched/);
  assert.match(SETTINGS, /Retention also runs daily on the analytics lane/);
});

test('the settings page shows the execution result including the audit record', () => {
  assert.match(SETTINGS, /execution\.audit_event_id/);
  assert.match(SETTINGS, /foreign_key_violations/);
  assert.match(SETTINGS, /integrity check \{execution\.integrity_ok/);
});

test('the retention preview is read-only on the client too', () => {
  // The preview method must not accept a dry_run override from a caller.
  const client = readFileSync(new URL('../src/services/apiAnalytics.ts', import.meta.url), 'utf8');
  assert.match(client, /retentionPreview\(body: \{ older_than_days\?: number; batch_limit\?: number \} = \{\}/);
  assert.match(client, /body: JSON\.stringify\(\{ \.\.\.body, dry_run: true \}\)/);
});

test('the reports page generates its format choices from the implemented list', () => {
  assert.match(REPORTS, /REPORT_FORMATS\.map/);
  assert.match(REPORTS, /format: form\.format/);
  // The verified file name keeps the extension of the format that was written.
  assert.match(REPORTS, /\$\{report\.id\}-analytics-\$\{metadata\.kind\}\.\$\{metadata\.format\}/);
  assert.match(REPORTS, /className="py-1 pr-3 uppercase text-slate-500">\{report\.format\}/);
});

test('a deletion summary pluralises, because "1 reports" reads like a bug', () => {
  assert.equal(retentionCountLabel(0, 'aggregations'), '0 aggregations');
  assert.equal(retentionCountLabel(1, 'reports'), '1 report');
  assert.equal(retentionCountLabel(1841, 'observations'), '1,841 observations');
  assert.equal(retentionCountLabel(1, 'collection_runs'), '1 collection run');
  assert.equal(retentionCountLabel(2, 'superseded_recommendations'), '2 superseded recommendations');
});
