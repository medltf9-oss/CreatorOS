// P8.23 pure-logic tests. Run with: node --test tests/analyticsPagesP823.test.mjs
//
// Every case here pins one rule the three P8.23 pages exist to enforce:
//
// * a decision is offered only in `draft`/`reviewed`, and the reason a decision is blocked is stated;
// * every mandated recommendation field is checked for *usable* presence (an empty limitations list is
//   a defect, not compliance);
// * evidence is classified resolved / missing / not-observed / stale / unreferenced, and an
//   unavailable value is never reported as zero;
// * a report download is verified against the digest in the metadata record, an empty body is refused,
//   and a mismatch is a failure rather than a retry;
// * settings are validated against the published bounds — never clamped — and only real changes are
//   sent.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  DECIDABLE_STATUSES,
  EVIDENCE_STALE_DAYS,
  RECOMMENDATION_CATEGORIES,
  SETTING_SPECS,
  UNSETTLED_SETTINGS_AREAS,
  benefitIsEstimate,
  benefitKindLabel,
  canDecide,
  categoryLabel,
  decisionBlockedReason,
  evidenceSummary,
  evidenceView,
  filterRecommendations,
  hasUnsavedChanges,
  missingFields,
  presentFields,
  reportAccountId,
  reportTitle,
  sampleSizeLabel,
  sectionSummary,
  settingsFromResponse,
  validateSettingsDraft,
} from '../src/components/analyticsDecisions.ts';
import { sha256Hex, shortDigest, verifyReportBytes } from '../src/components/reportIntegrity.ts';

const DAY = 24 * 60 * 60 * 1000;

function recommendation(overrides = {}) {
  return {
    id: 'rec-1',
    account_id: 'acc-fb',
    platform: 'facebook',
    campaign_id: 'c1',
    content_project_id: 'proj-1',
    kind: 'topic',
    observation: 'Product-education posts reached a median of 12,400 views.',
    sample_size: { publications: 14, baseline_publications: 9 },
    period_start: '2026-11-10T00:00:00Z',
    period_end: '2026-11-23T00:00:00Z',
    confidence: 0.91,
    confidence_method: 'bootstrap 95% interval on the median difference',
    limitations: ['observational comparison'],
    proposed_experiment: { change: 'publish two more posts', success_metric: 'fb.post.media_views' },
    expected_benefit: { kind: 'estimate', direction: 'increase' },
    status: 'draft',
    generated_by: 'analytics.recommendations',
    approval_request_id: null,
    decided_at: null,
    decision_note: null,
    created_at: '2026-11-23T00:00:00Z',
    ...overrides,
  };
}

function metricValue(overrides = {}) {
  return {
    metric_key: 'fb.post.media_views',
    provider_metric_name: 'post_video_views',
    unit: 'count',
    value_kind: 'scalar',
    metric_class: 'cumulative',
    period_kind: 'd28',
    availability: 'observed',
    availability_reason: null,
    value: 12400,
    value_json: null,
    collected_at: '2026-11-23T00:00:00Z',
    period_start: null,
    period_end: null,
    sample_size: 1,
    method: 'direct',
    confidence: null,
    snapshot_ids: ['snap-1'],
    aggregation_ids: [],
    ...overrides,
  };
}

const NOW = new Date('2026-11-24T00:00:00Z');

// ---------------------------------------------------------------- decision gating

test('only draft and reviewed records can be decided', () => {
  assert.deepEqual(DECIDABLE_STATUSES, ['draft', 'reviewed']);
  assert.equal(canDecide(recommendation({ status: 'draft' })), true);
  assert.equal(canDecide(recommendation({ status: 'reviewed' })), true);
  for (const status of ['accepted', 'rejected', 'superseded']) {
    assert.equal(canDecide(recommendation({ status })), false, status);
  }
  assert.equal(canDecide(null), false);
  assert.equal(canDecide(undefined), false);
});

test('each blocked state explains itself instead of showing a dead button', () => {
  assert.match(decisionBlockedReason(recommendation({ status: 'accepted' })), /already accepted/);
  assert.match(decisionBlockedReason(recommendation({ status: 'rejected' })), /already rejected/);
  assert.match(decisionBlockedReason(recommendation({ status: 'superseded' })), /no longer actionable/);
  assert.match(decisionBlockedReason(recommendation({ status: 'draft' })), /can be accepted or rejected/);
});

// ---------------------------------------------------------------- field completeness

test('a complete record has no missing mandated field', () => {
  assert.deepEqual(missingFields(recommendation()), []);
  assert.deepEqual(presentFields(recommendation()).limitations, true);
});

test('an empty limitations list counts as missing, not as compliance', () => {
  assert.deepEqual(missingFields(recommendation({ limitations: [] })), ['limitations']);
});

test('an empty experiment or benefit object is reported as missing', () => {
  assert.deepEqual(missingFields(recommendation({ proposed_experiment: {} })), ['proposed_experiment']);
  assert.deepEqual(missingFields(recommendation({ expected_benefit: {} })), ['expected_benefit']);
});

test('a blank observation and an empty sample size are missing', () => {
  assert.deepEqual(missingFields(recommendation({ observation: '   ' })), ['observation']);
  assert.deepEqual(missingFields(recommendation({ sample_size: {} })), ['sample_size']);
});

test('a null confidence still needs a method to explain it', () => {
  assert.deepEqual(missingFields(recommendation({ confidence: null, confidence_method: 'descriptive' })), []);
  assert.deepEqual(missingFields(recommendation({ confidence: null, confidence_method: null })), ['confidence_method']);
});

test('an estimate is marked as an estimate and an unmeasured benefit is not', () => {
  assert.equal(benefitIsEstimate(recommendation()), true);
  assert.equal(benefitIsEstimate(recommendation({ expected_benefit: { kind: 'unmeasured' } })), false);
  assert.match(benefitKindLabel(recommendation()), /estimate — not a forecast/);
  assert.match(benefitKindLabel(recommendation({ expected_benefit: { kind: 'unmeasured' } })), /unmeasured/);
  assert.match(benefitKindLabel(recommendation({ expected_benefit: {} })), /does not state a kind/);
});

test('sample size is rendered from whatever keys the record carries', () => {
  assert.equal(sampleSizeLabel({ publications: 14, baseline_publications: 9 }), 'publications 14 · baseline publications 9');
  assert.equal(sampleSizeLabel({}), 'not stated');
});

// ---------------------------------------------------------------- filters

test('platform and date filters are applied client-side and keep the rest', () => {
  const rows = [
    recommendation({ id: 'a', platform: 'facebook', created_at: '2026-11-10T00:00:00Z' }),
    recommendation({ id: 'b', platform: 'tiktok', created_at: '2026-11-20T00:00:00Z' }),
    recommendation({ id: 'c', platform: null, created_at: '2026-11-22T00:00:00Z' }),
  ];
  const base = { status: '', kind: '', accountId: '', platform: '', start: '', end: '' };
  assert.equal(filterRecommendations(rows, base).length, 3);
  assert.deepEqual(filterRecommendations(rows, { ...base, platform: 'tiktok' }).map((r) => r.id), ['b']);
  assert.deepEqual(
    filterRecommendations(rows, { ...base, start: '2026-11-15T00:00:00Z' }).map((r) => r.id),
    ['b', 'c'],
  );
  assert.deepEqual(
    filterRecommendations(rows, { ...base, start: '2026-11-11T00:00:00Z', end: '2026-11-21T00:00:00Z' }).map((r) => r.id),
    ['b'],
  );
  // A record with no platform is not silently reclassified into a platform filter.
  assert.equal(filterRecommendations(rows, { ...base, platform: 'facebook' }).length, 1);
});

test('every recommendation category in the schema has a label', () => {
  const kinds = ['topic', 'format', 'hook', 'caption', 'hashtag', 'timing', 'campaign', 'growth', 'retention', 'experiment'];
  assert.deepEqual(RECOMMENDATION_CATEGORIES.map((c) => c.kind).sort(), kinds.slice().sort());
  assert.equal(categoryLabel('timing'), 'Timing');
  // An unknown kind is shown verbatim rather than dropped.
  assert.equal(categoryLabel('mystery'), 'mystery');
});

// ---------------------------------------------------------------- evidence

test('an observed value that resolves against the ledger is resolved', () => {
  const view = evidenceView(metricValue(), new Set(['snap-1']), NOW);
  assert.equal(view.state, 'resolved');
  assert.equal(view.value, 12400);
});

test('an observed zero stays a real value', () => {
  const view = evidenceView(metricValue({ value: 0 }), new Set(['snap-1']), NOW);
  assert.equal(view.state, 'resolved');
  assert.equal(view.value, 0);
});

test('an unavailable metric is not observed and never reads as zero', () => {
  const view = evidenceView(
    metricValue({ availability: 'not_authorized', value: null, availability_reason: 'missing scope' }),
    new Set(['snap-1']),
    NOW,
  );
  assert.equal(view.state, 'not_observed');
  assert.equal(view.value, null);
  assert.match(view.detail, /not_authorized/);
  assert.match(view.detail, /not a zero/);
});

test('a cited snapshot that is not in the ledger is missing', () => {
  const view = evidenceView(metricValue({ snapshot_ids: ['snap-gone'] }), new Set(['snap-1']), NOW);
  assert.equal(view.state, 'missing');
});

test('an item that cites no row at all is unreferenced', () => {
  const view = evidenceView(metricValue({ snapshot_ids: [], aggregation_ids: [] }), new Set(['snap-1']), NOW);
  assert.equal(view.state, 'unreferenced');
});

test('evidence older than the staleness window is stale, with the age stated', () => {
  const collected = new Date(NOW.getTime() - (EVIDENCE_STALE_DAYS + 5) * DAY).toISOString();
  const view = evidenceView(metricValue({ collected_at: collected }), new Set(['snap-1']), NOW);
  assert.equal(view.state, 'stale');
  assert.match(view.detail, new RegExp(`${EVIDENCE_STALE_DAYS + 5} days ago`));
});

test('evidence inside the window is not stale', () => {
  const collected = new Date(NOW.getTime() - 2 * DAY).toISOString();
  assert.equal(evidenceView(metricValue({ collected_at: collected }), new Set(['snap-1']), NOW).state, 'resolved');
});

test('the summary counts each state once', () => {
  const items = [
    evidenceView(metricValue(), new Set(['snap-1']), NOW),
    evidenceView(metricValue({ snapshot_ids: ['gone'] }), new Set(['snap-1']), NOW),
    evidenceView(metricValue({ availability: 'not_supported', value: null }), new Set(['snap-1']), NOW),
  ];
  assert.deepEqual(evidenceSummary(items), { resolved: 1, missing: 1, not_observed: 1, stale: 0, unreferenced: 0 });
});

// ---------------------------------------------------------------- report helpers

function report(overrides = {}) {
  return {
    id: 'rep-1',
    kind: 'overview',
    period_start: '2026-08-25T00:00:00Z',
    period_end: '2026-11-22T00:00:00Z',
    filters: { account_id: null, title: 'Quarterly performance review' },
    sections: [],
    summary: 'summary',
    sha256: 'a'.repeat(64),
    bytes: 1265,
    status: 'final',
    generated_by: 'analytics.reports',
    created_at: '2026-11-22T00:00:00Z',
    ...overrides,
  };
}

test('a report title comes from the stored filters, with a kind-based fallback', () => {
  assert.equal(reportTitle(report()), 'Quarterly performance review');
  assert.equal(reportTitle(report({ filters: {} })), 'overview analytics report');
  assert.equal(reportTitle(report({ filters: { title: '   ' } })), 'overview analytics report');
  assert.equal(reportAccountId(report()), null);
  assert.equal(reportAccountId(report({ filters: { account_id: 'acc-fb' } })), 'acc-fb');
});

test('section summaries describe the body shape', () => {
  assert.equal(sectionSummary({ heading: 'x', type: 'table', body: [1, 2, 3] }), '3 rows');
  assert.equal(sectionSummary({ heading: 'x', type: 'table', body: [1] }), '1 row');
  assert.equal(sectionSummary({ heading: 'x', type: 'object', body: { a: 1 } }), '1 field');
  assert.equal(sectionSummary({ heading: 'x', type: 'prose', body: 'hello' }), '5 characters');
});

test('a short digest keeps a readable prefix and never invents one', () => {
  assert.equal(shortDigest('9d036696a6fdf911'), '9d036696a6fd…');
  assert.equal(shortDigest(null), '—');
});

// ---------------------------------------------------------------- download integrity

const PAYLOAD = new TextEncoder().encode('{"report_id":"rep-1"}\n');

test('sha256Hex reproduces the published SHA-256 vectors', async () => {
  // FIPS 180-2 / NIST vectors, checked so the digest is known to be SHA-256 and not a lookalike.
  assert.equal(await sha256Hex(new Uint8Array()), 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  assert.equal(
    await sha256Hex(new TextEncoder().encode('abc')),
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
  );
  const digest = await sha256Hex(PAYLOAD);
  assert.match(digest, /^[0-9a-f]{64}$/);
  // The same bytes reached as an ArrayBuffer produce the same digest.
  assert.equal(await sha256Hex(PAYLOAD.buffer.slice(0)), digest);
  // A view into a larger buffer hashes only its own bytes.
  const padded = new Uint8Array(PAYLOAD.byteLength + 8);
  padded.set(PAYLOAD, 3);
  assert.equal(await sha256Hex(padded.subarray(3, 3 + PAYLOAD.byteLength)), digest);
});

test('a payload whose digest matches the record is accepted', async () => {
  const expected = await sha256Hex(PAYLOAD);
  const result = await verifyReportBytes(expected, PAYLOAD);
  assert.equal(result.ok, true);
  assert.equal(result.failure, null);
  assert.equal(result.actual, expected);
  assert.equal(result.bytes, PAYLOAD.byteLength);
  assert.match(result.message, /Verified/);
});

test('a digest mismatch is refused and both digests are reported', async () => {
  const result = await verifyReportBytes('0000000000000000000000000000000000000000000000000000000000000000', PAYLOAD);
  assert.equal(result.ok, false);
  assert.equal(result.failure, 'digest_mismatch');
  assert.equal(result.expected, '0'.repeat(64));
  assert.equal(result.actual, await sha256Hex(PAYLOAD));
  assert.match(result.message, /do not hash to the digest/);
});

test('a record with no digest refuses the download instead of trusting the file', async () => {
  const result = await verifyReportBytes(null, PAYLOAD);
  assert.equal(result.ok, false);
  assert.equal(result.failure, 'no_expected_hash');
  assert.equal(result.actual, null);
  assert.match(result.message, /cannot be verified/);
});

test('an empty body is refused even when a digest is present', async () => {
  const result = await verifyReportBytes(await sha256Hex(new Uint8Array()), new Uint8Array());
  assert.equal(result.ok, false);
  assert.equal(result.failure, 'no_bytes');
  assert.equal(result.bytes, 0);
  assert.match(result.message, /empty/);
});

test('verification compares lowercased, so an upper-case record digest still matches', async () => {
  const expected = (await sha256Hex(PAYLOAD)).toUpperCase();
  assert.equal((await verifyReportBytes(expected, PAYLOAD)).ok, true);
});

// ---------------------------------------------------------------- settings

const BOUNDS = {
  account_sync_interval_s: { min: 300, max: 172800 },
  object_window_months: { min: 1, max: 36 },
  retention_days: { min: 7, max: 3650 },
  recommendation_min_publications: { min: 3, max: 50 },
};

const SAVED = {
  collection_enabled: true,
  account_sync_interval_s: 86400,
  object_window_months: 12,
  retention_days: 365,
  recommendation_min_publications: 8,
};

test('the page only edits keys the endpoint stores', () => {
  assert.deepEqual(
    SETTING_SPECS.map((spec) => spec.key).sort(),
    ['account_sync_interval_s', 'collection_enabled', 'object_window_months', 'recommendation_min_publications', 'retention_days'],
  );
  assert.ok(SETTING_SPECS.every((spec) => spec.effect.length > 0));
});

test('an unchanged draft produces no changes and no errors', () => {
  const result = validateSettingsDraft({ ...SAVED }, SAVED, BOUNDS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.changes, {});
  assert.equal(hasUnsavedChanges({ ...SAVED }, SAVED), false);
});

test('only changed keys are sent', () => {
  const result = validateSettingsDraft({ ...SAVED, retention_days: 730 }, SAVED, BOUNDS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.changes, { retention_days: 730 });
  assert.equal(hasUnsavedChanges({ ...SAVED, retention_days: 730 }, SAVED), true);
});

test('a value below the published minimum is an error, not a clamp', () => {
  const result = validateSettingsDraft({ ...SAVED, account_sync_interval_s: 60 }, SAVED, BOUNDS);
  assert.equal(result.ok, false);
  assert.equal(result.changes.account_sync_interval_s, undefined);
  assert.deepEqual(result.errors, [
    { key: 'account_sync_interval_s', message: 'Account sync interval must be between 300 and 172800 seconds' },
  ]);
});

test('a value above the published maximum is an error too', () => {
  const result = validateSettingsDraft({ ...SAVED, recommendation_min_publications: 51 }, SAVED, BOUNDS);
  assert.equal(result.ok, false);
  assert.match(result.errors[0].message, /between 3 and 50 publications/);
});

test('the boundaries themselves are accepted', () => {
  for (const [key, bound] of Object.entries(BOUNDS)) {
    assert.equal(validateSettingsDraft({ ...SAVED, [key]: bound.min }, SAVED, BOUNDS).ok, true, `${key} min`);
    assert.equal(validateSettingsDraft({ ...SAVED, [key]: bound.max }, SAVED, BOUNDS).ok, true, `${key} max`);
  }
});

test('a fractional value is rejected rather than rounded', () => {
  const result = validateSettingsDraft({ ...SAVED, retention_days: 365.5 }, SAVED, BOUNDS);
  assert.equal(result.ok, false);
  assert.match(result.errors[0].message, /whole number/);
});

test('a blank field is required, not silently zero', () => {
  const result = validateSettingsDraft({ ...SAVED, retention_days: '' }, SAVED, BOUNDS);
  assert.equal(result.ok, false);
  assert.match(result.errors[0].message, /required/);
});

test('a non-boolean flag is rejected', () => {
  const result = validateSettingsDraft({ ...SAVED, collection_enabled: 'yes' }, SAVED, BOUNDS);
  assert.equal(result.ok, false);
  assert.match(result.errors[0].message, /true or false/);
});

test('toggling the flag is a change like any other', () => {
  const result = validateSettingsDraft({ ...SAVED, collection_enabled: false }, SAVED, BOUNDS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.changes, { collection_enabled: false });
});

test('a missing bound does not block a valid integer', () => {
  const result = validateSettingsDraft({ ...SAVED, retention_days: 400 }, SAVED, {});
  assert.equal(result.ok, true);
  assert.deepEqual(result.changes, { retention_days: 400 });
});

test('every error is reported, not just the first', () => {
  const result = validateSettingsDraft(
    { ...SAVED, retention_days: 1, object_window_months: 99, account_sync_interval_s: 0 },
    SAVED,
    BOUNDS,
  );
  assert.equal(result.errors.length, 3);
});

test('the response is unpacked into stored values, bounds and the note', () => {
  const unpacked = settingsFromResponse({
    settings: SAVED,
    bounds: BOUNDS,
    note: 'Stored preferences. They do not change a running collect; the sweeper reads them on its next tick.',
  });
  assert.deepEqual(unpacked.saved, SAVED);
  assert.deepEqual(unpacked.bounds, BOUNDS);
  assert.match(unpacked.note, /next tick/);
});

test('gaps are declared instead of exposed as controls', () => {
  const areas = UNSETTLED_SETTINGS_AREAS.map((item) => item.area);
  assert.ok(areas.includes('Reporting preferences'));
  assert.ok(areas.includes('Reset to defaults'));
  // Nothing claims to edit a key the endpoint would reject.
  assert.ok(UNSETTLED_SETTINGS_AREAS.every((item) => !SETTING_SPECS.some((spec) => item.note.includes(spec.key))));
});
