// P8.23 page-source invariants. Run with: node --test tests/pagesP823.source.test.mjs
//
// The rules that cannot be expressed as a pure function are asserted against the shipped markup:
//
// * Recommendations has no publish control, no publish-job call, and does not mark a record decided
//   before the server answers;
// * acceptance and rejection have genuinely separate confirmations (not one shared dialog);
// * the download hash is computed from the received bytes and compared with the digest taken from the
//   report *record*, never with a value read out of the downloaded document;
// * settings render the bounds the API publishes and show the sweeper-tick caveat.
//
// Comments are stripped first: prose in a comment cannot satisfy a requirement.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (name) => readFileSync(new URL(`../src/pages/analytics/${name}`, import.meta.url), 'utf8');
const withoutComments = (source) => source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

const RECOMMENDATIONS = withoutComments(read('Recommendations.tsx'));
const REPORTS = withoutComments(read('Reports.tsx'));
const SETTINGS = withoutComments(read('AnalyticsSettings.tsx'));

test('no P8.23 page offers a publish, schedule or approval-decision control', () => {
  for (const [name, source] of [
    ['Recommendations.tsx', RECOMMENDATIONS],
    ['Reports.tsx', REPORTS],
    ['AnalyticsSettings.tsx', SETTINGS],
  ]) {
    assert.doesNotMatch(source, /\bpublish\s*\(/i, `${name} must not call a publish function`);
    assert.doesNotMatch(source, /publishingClient|publishJob|createJob\(/i, `${name} must not reach the publishing service`);
    assert.doesNotMatch(source, /\bqueueJob|enqueue\(/i, `${name} must not enqueue work`);
    assert.doesNotMatch(source, /approveApproval|approvalService/i, `${name} must not decide a publishing approval`);
  }
});

test('the recommendations page states that accepting is not publishing', () => {
  assert.match(RECOMMENDATIONS, /Accepting a recommendation records an approval request for the/);
  assert.match(RECOMMENDATIONS, /does not\s+publish, schedule, or approve publishing/);
  assert.match(RECOMMENDATIONS, /Nothing was published, scheduled or approved for publishing/);
  // And it offers no button whose label promises to publish anything.
  assert.doesNotMatch(RECOMMENDATIONS, />\s*(Publish|Schedule|Post now)\b/i);
});

test('accept and reject have distinct confirmations, not one shared dialog', () => {
  assert.match(RECOMMENDATIONS, /data-confirmation="accept"/);
  assert.match(RECOMMENDATIONS, /data-confirmation="reject"/);
  assert.match(RECOMMENDATIONS, /Confirm acceptance/);
  assert.match(RECOMMENDATIONS, /Confirm rejection/);
  // Two different entry points, two different panels.
  assert.match(RECOMMENDATIONS, /function AcceptConfirmation\(/);
  assert.match(RECOMMENDATIONS, /function RejectConfirmation\(/);
  assert.doesNotMatch(RECOMMENDATIONS, /window\.confirm/);
});

test('a rejection requires a reason before the confirm button works', () => {
  assert.match(RECOMMENDATIONS, /disabled=\{!reason\.trim\(\)\}/);
  assert.match(RECOMMENDATIONS, /A rejection needs a reason/);
});

test('the decision is never applied optimistically', () => {
  // The row is replaced by the server response; nothing sets status locally.
  assert.doesNotMatch(RECOMMENDATIONS, /setStatus\(|status:\s*'accepted'|status:\s*'rejected'/);
  assert.match(RECOMMENDATIONS, /const updated = await api\.acceptRecommendation/);
  assert.match(RECOMMENDATIONS, /const updated = await api\.rejectRecommendation/);
  // A conflict is explained, not swallowed.
  assert.match(RECOMMENDATIONS, /status === 409/);
  assert.match(RECOMMENDATIONS, /already decided|cannot be \$\{decision\}ed|can no longer be/);
});

test('the recommendations page renders every mandated field of the record', () => {
  for (const field of [
    'observation',
    'sample_size',
    'confidence',
    'confidence_method',
    'limitations',
    'proposed_experiment',
    'expected_benefit',
    'period_start',
    'period_end',
    'approval_request_id',
    'decided_at',
    'decision_note',
    'generated_by',
  ]) {
    assert.match(RECOMMENDATIONS, new RegExp(`recommendation\\.${field}`), `record must show ${field}`);
  }
  // Missing fields are surfaced, not hidden.
  assert.match(RECOMMENDATIONS, /missing mandated field/);
});

test('evidence resolution is reported honestly', () => {
  assert.match(RECOMMENDATIONS, /re-resolved against the stored ledger/);
  assert.match(RECOMMENDATIONS, /snapshot ids against the account/);
  assert.match(RECOMMENDATIONS, /never a zero/);
  assert.match(RECOMMENDATIONS, /treated as a defect/);
});

test('the report download hashes the received bytes and compares them with the record digest', () => {
  // The expected digest is read from the metadata record...
  assert.match(REPORTS, /const metadata = await api\.report\(report\.id\)/);
  // ...and the payload is treated as bytes...
  assert.match(REPORTS, /const payload = await api\.downloadReport\(report\.id\)/);
  assert.match(REPORTS, /verifyReportBytes\(metadata\.sha256, payload\.bytes\)/);
  // ...so nothing ever hashes a re-serialised object or a field inside the file.
  assert.doesNotMatch(REPORTS, /JSON\.parse\(.*\)\.sha256/);
  assert.doesNotMatch(REPORTS, /metadata\.sha256\s*=[^=]/);
});

test('an unverified download is never offered for saving', () => {
  assert.match(REPORTS, /if \(!integrity\.ok\) \{/);
  assert.match(REPORTS, /Save verified file/);
  assert.match(REPORTS, /The file is not offered for saving and was not renamed or regenerated/);
  assert.match(REPORTS, /Download not accepted/);
});

test('the reports page offers exactly the formats the backend writes', () => {
  // P8.24 added CSV and PDF, so this guard changed shape: the dropdown is generated from the client's
  // REPORT_FORMATS list, and no format the backend cannot write may appear as a choice.
  assert.match(REPORTS, /REPORT_FORMATS\.map/);
  assert.doesNotMatch(REPORTS, /docx|xlsx|pptx|epub/i);
  assert.match(REPORTS, /JSON, CSV and PDF are generated from the same content model/);
  assert.match(REPORTS, /Nothing else is offered because nothing else is implemented/);
});

test('deleting a report needs its own typed confirmation', () => {
  assert.match(REPORTS, /Type the report id to confirm/);
  assert.match(REPORTS, /disabled=\{deleteConfirmText\.trim\(\) !== deleteFor\.id\}/);
  assert.doesNotMatch(REPORTS, /window\.confirm/);
});

test('settings are validated against the published bounds and never clamped', () => {
  assert.match(SETTINGS, /validateSettingsDraft\(draft, saved, bounds\)/);
  assert.doesNotMatch(SETTINGS, /Math\.min\(|Math\.max\(/);
  assert.match(SETTINGS, /bounds\[spec\.key\]/);
  // The Save button is disabled while a value is out of bounds.
  assert.match(SETTINGS, /disabled=\{!dirty \|\| !validation\.ok\}/);
  assert.match(SETTINGS, /Nothing was saved: one or more values are outside the published bounds/);
});

test('settings show current value, bounds, unit and when a change takes effect', () => {
  assert.match(SETTINGS, /Current <span/);
  assert.match(SETTINGS, /Allowed <span/);
  assert.match(SETTINGS, /Takes effect: \{spec\.effect\}/);
  assert.match(SETTINGS, /unsaved changes/);
  assert.match(SETTINGS, /in sync with stored values/);
});

test('settings state the sweeper-tick caveat and the untouched guard rails', () => {
  assert.match(SETTINGS, /sweeper reads them on its next tick/);
  assert.match(SETTINGS, /Resource Governor/);
  assert.match(SETTINGS, /rate limits/);
});

test('settings do not fake a reset the API cannot perform', () => {
  assert.match(SETTINGS, /Reset to defaults \(not available\)/);
  assert.match(SETTINGS, /disabled/);
  assert.match(SETTINGS, /a reset would be a guess/);
});

test('all three pages expose loading, empty and error states', () => {
  assert.match(RECOMMENDATIONS, /LoadingBlock/);
  assert.match(RECOMMENDATIONS, /EmptyState/);
  assert.match(RECOMMENDATIONS, /ErrorBlock/);
  assert.match(REPORTS, /LoadingBlock/);
  assert.match(REPORTS, /EmptyState/);
  assert.match(REPORTS, /ErrorBlock/);
  assert.match(SETTINGS, /LoadingBlock/);
  assert.match(SETTINGS, /ErrorBlock/);
});

test('every request in the three pages goes through the cancelling loader or an explicit action', () => {
  for (const [name, source] of [
    ['Recommendations.tsx', RECOMMENDATIONS],
    ['Reports.tsx', REPORTS],
    ['AnalyticsSettings.tsx', SETTINGS],
  ]) {
    assert.match(source, /useAsyncData\(/, `${name} must use the cancelling loader`);
    assert.match(source, /signal/, `${name} must pass the abort signal to the API`);
  }
});

test('the three pages are routed in the app shell', () => {
  const app = withoutComments(readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'));
  assert.match(app, /an-recommendations/);
  assert.match(app, /an-reports/);
  assert.match(app, /an-settings/);
  for (const page of ['Recommendations', 'Reports', 'AnalyticsSettings']) {
    assert.match(app, new RegExp(`<${page} api=\\{analytics\\}`), `${page} must be rendered with the analytics client`);
  }
});
