// P8.18 chart-model tests. Run with: node --test tests/chartModel.test.mjs
//
// These are the assertions that make the "missing is never zero" rule mechanical rather than a
// promise. The module under test is pure TypeScript with no JSX, so Node's type stripping runs it
// directly (Node >= 22.18).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  AVAILABILITY_LABELS,
  bucketStepMs,
  chartEmptyReason,
  computeAxis,
  decimalsFor,
  describeChart,
  formatCompact,
  formatMetricValue,
  formatTimeLabel,
  isObserved,
  niceStep,
  observedValues,
  pointsFromRows,
  scaleValue,
  segmentObserved,
  selectTickIndices,
  seriesFromRows,
  summariseAvailability,
  toAvailability,
} from '../src/components/chartModel.ts';

const observed = (x, y) => ({ x, y, availability: 'observed' });
const gap = (x, availability = 'not_collected') => ({ x, y: null, availability });

test('observed zero is a real observation', () => {
  const point = observed(0, 0);
  assert.equal(isObserved(point), true);
  assert.equal(formatMetricValue(0, 'views'), '0 views');
});

test('an unavailable metric is never substituted with zero', () => {
  // A malformed payload that carries y: 0 with a non-observed state must still be a gap.
  const malformed = { x: 0, y: 0, availability: 'not_collected' };
  assert.equal(isObserved(malformed), false);

  const segments = segmentObserved([observed(0, 5), malformed, observed(2, 7)]);
  assert.equal(segments.length, 2);
  assert.deepEqual(segments.map((segment) => segment.length), [1, 1]);

  const reason = chartEmptyReason([{ key: 'a', label: 'Watch time', points: [{ x: 0, y: null, availability: 'not_supported' }] }]);
  assert.match(reason, /Not exposed by the provider/);
});

test('a non-observed point breaks the line segment', () => {
  const points = [observed(0, 1), observed(1, 2), gap(2), observed(3, 4), observed(4, 5), gap(5, 'not_authorized'), gap(6)];
  const segments = segmentObserved(points);
  assert.deepEqual(segments.map((segment) => segment.map((point) => point.x)), [[0, 1], [3, 4]]);
});

test('an absent time bucket breaks the line so a week of missing data is not bridged', () => {
  const day = bucketStepMs('day');
  const points = [observed(0, 1), observed(day, 2), observed(10 * day, 3), observed(11 * day, 4)];
  const withThreshold = segmentObserved(points, { gapThresholdMs: day * 1.5 });
  assert.deepEqual(withThreshold.map((segment) => segment.length), [2, 2]);
  // Without the threshold only explicit gaps break; the chart component always passes the bucket step.
  assert.equal(segmentObserved(points).length, 1);
});

test('a categorical series is never broken by index distance', () => {
  const points = [observed('a', 1), observed('b', 2), observed('c', 3)];
  const segments = segmentObserved(points, { gapThresholdMs: 1 });
  assert.equal(segments.length, 1);
});

test('empty dataset produces an explicit empty axis and a no-data sentence', () => {
  const axis = computeAxis([]);
  assert.equal(axis.empty, true);
  assert.deepEqual([axis.min, axis.max], [0, 1]);
  assert.equal(chartEmptyReason([]), 'No data collected for this range.');
});

test('all-zero observations keep a readable axis and are not mistaken for missing', () => {
  const axis = computeAxis([0, 0, 0]);
  assert.equal(axis.allZero, true);
  assert.deepEqual([axis.min, axis.max], [0, 1]);
  assert.deepEqual(axis.ticks, [0, 0.25, 0.5, 0.75, 1]);
  const reason = chartEmptyReason([{ key: 'a', label: 'Views', points: [observed(0, 0)] }]);
  assert.equal(reason, null);
});

test('a single observation still yields a sane axis', () => {
  const axis = computeAxis([500]);
  assert.equal(axis.empty, false);
  assert.equal(axis.min, 0);
  assert.ok(axis.max >= 500);
  assert.ok(axis.ticks.includes(0));
});

test('positive-only data starts at zero and rounds up to a nice maximum', () => {
  const axis = computeAxis([120, 340, 980]);
  assert.equal(axis.min, 0);
  assert.ok(axis.max >= 980);
  assert.ok(axis.ticks.length >= 3);
  assert.equal(axis.ticks[0], 0);
});

test('negative values are included when the metric permits them', () => {
  const axis = computeAxis([-120, 40, -15]);
  assert.ok(axis.min <= -120);
  assert.ok(axis.max >= 40);
  assert.ok(axis.ticks.some((tick) => tick < 0));
});

test('decimal and large values format without losing the order of magnitude', () => {
  const axis = computeAxis([0.12, 0.5, 0.87]);
  assert.equal(axis.ticks[0], 0);
  assert.ok(axis.ticks[axis.ticks.length - 1] >= 0.87);
  assert.ok(axis.ticks.every((tick) => Number.isFinite(tick)));
  assert.ok(axis.step < 1 && axis.step > 0);

  const big = computeAxis([2_500_000, 9_100_000]);
  assert.ok(big.max >= 9_100_000);
  assert.equal(formatCompact(big.max), '10M');
});

test('the same data produces identical axes, in any order', () => {
  const first = computeAxis([3, 900, 12, 480]);
  const second = computeAxis([480, 3, 900, 12]);
  assert.deepEqual(first, second);
  assert.deepEqual(computeAxis([3, 900, 12, 480]), first);
});

test('formatting: compact numbers, units and the unavailable wording', () => {
  assert.equal(formatCompact(0), '0');
  assert.equal(formatCompact(42), '42');
  assert.equal(formatCompact(1500), '1.5k');
  assert.equal(formatCompact(2_000_000), '2M');
  assert.equal(formatCompact(1_234_567_890), '1.2B');
  assert.equal(formatCompact(-3200), '-3.2k');
  assert.equal(formatCompact(3.456), '3.46');
  assert.equal(formatCompact(null), '—');

  assert.equal(formatMetricValue(null, 'views', 'not_authorized'), 'unavailable (Permission not granted)');
  assert.equal(formatMetricValue(12.34, '%'), '12.3%');
  assert.equal(formatMetricValue(1500, 'views'), '1.5k views');
});

test('nice steps and decimals are stable', () => {
  assert.equal(niceStep(0.23), 0.25);
  assert.equal(niceStep(1.1), 2);
  assert.equal(niceStep(230), 250);
  assert.equal(decimalsFor(0.25), 2);
  assert.equal(decimalsFor(0.5), 1);
  assert.equal(decimalsFor(1), 0);
  assert.equal(decimalsFor(1000), 0);
});

test('tick index selection always includes the ends', () => {
  assert.deepEqual(selectTickIndices(7, 3), [0, 3, 6]);
  assert.deepEqual(selectTickIndices(1, 5), [0]);
  assert.deepEqual(selectTickIndices(0, 5), []);
  assert.deepEqual(selectTickIndices(2, 5), [0, 1]);
});

test('scaleValue is deterministic and clamps to the axis', () => {
  const axis = computeAxis([0, 100]);
  assert.equal(scaleValue(axis.min, axis, 100), 100);
  assert.equal(scaleValue(axis.max, axis, 100), 0);
  assert.equal(scaleValue(-50, axis, 100), 100);
  assert.equal(scaleValue(500, axis, 100), 0);
});

test('multiple series: availability summary and accessible description', () => {
  const series = [
    { key: 'views', label: 'Views', unit: 'views', points: [observed(0, 10), gap(1), observed(2, 30)] },
    { key: 'likes', label: 'Likes', unit: 'likes', points: [gap(0, 'not_authorized'), gap(1, 'not_authorized'), gap(2, 'not_authorized')] },
  ];
  const summary = summariseAvailability(series);
  assert.equal(summary.observed, 2);
  assert.equal(summary.gaps, 4);
  assert.equal(summary.byState.not_authorized, 3);

  const text = describeChart('Views and likes', series);
  assert.match(text, /Views: 2 observation\(s\), 1 unavailable, range 10 to 30 views/);
  assert.match(text, /Likes: no observations/);
});

test('observedValues ignores gaps', () => {
  assert.deepEqual(observedValues([observed(0, 1), gap(1), observed(2, 3)]), [1, 3]);
});

test('availability strings normalise and keep their labels', () => {
  assert.equal(toAvailability('not_authorized'), 'not_authorized');
  assert.equal(toAvailability('  NOT_SUPPORTED '), 'not_supported');
  assert.equal(toAvailability('something_new'), 'unknown');
  assert.equal(toAvailability(null), 'unknown');
  for (const state of Object.keys(AVAILABILITY_LABELS)) {
    assert.equal(typeof AVAILABILITY_LABELS[state], 'string');
  }
});

test('time labels are deterministic UTC', () => {
  const moment = Date.UTC(2026, 10, 24, 13, 30);
  assert.equal(formatTimeLabel(moment, 'day'), '2026-11-24');
  assert.equal(formatTimeLabel(moment, 'hour'), '2026-11-24 13:00');
  assert.equal(formatTimeLabel('bts'), 'bts');
});

test('API rows map to chart points without converting a gap into zero', () => {
  const rows = [
    { at: '2026-11-10T00:00:00Z', value: 120, availability: 'observed' },
    { at: '2026-11-11T00:00:00Z', value: null, availability: 'not_authorized' },
    { at: '2026-11-12T00:00:00Z', value: 0, availability: 'observed' },
    // a malformed row that carries a number with a non-observed state must still be a gap
    { at: '2026-11-13T00:00:00Z', value: 0, availability: 'not_collected' },
  ];
  const points = pointsFromRows(rows);
  assert.equal(points[0].x, Date.UTC(2026, 10, 10));
  assert.deepEqual(points.map((point) => point.y), [120, null, 0, 0]);
  assert.deepEqual(points.map((point) => isObserved(point)), [true, false, true, false]);
  assert.deepEqual(
    segmentObserved(points).map((segment) => segment.length),
    [1, 1],
  );
});

test('a series built from API rows keeps its label and unit', () => {
  const series = seriesFromRows('tt.video.views', 'view_count', 'views', [
    { at: '2026-11-10T00:00:00Z', value: 10, availability: 'observed' },
  ]);
  assert.equal(series.key, 'tt.video.views');
  assert.equal(series.label, 'view_count');
  assert.equal(series.unit, 'views');
  assert.equal(series.points.length, 1);
});
