// P8.21 page-logic tests. Run with: node --test tests/analyticsModel.test.mjs
//
// `analyticsModel.ts` is the pure part of the publication / content / campaign pages. These tests pin
// the one rule the whole Analytics Center depends on: a metric that was not `observed` is absent,
// never zero — including an observed zero, which must stay distinct from an unavailable metric.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  availabilityBreakdown,
  conceptsForPlatform,
  countBy,
  indexDefinitions,
  isDelivered,
  latestTimestamp,
  metricKeyForConcept,
  metricsByPlatform,
  metricsByWindow,
  platformForMetric,
  publicationValues,
  rankPublications,
  retentionChart,
  scalarMetric,
  timelineSeries,
} from '../src/components/analyticsModel.ts';

function definition(overrides = {}) {
  return {
    metric_key: 'fb.post.media_views',
    platform: 'facebook',
    object_type: 'post',
    provider_metric_name: 'post_video_views',
    provider_object: 'post',
    source_doc_url: 'https://example',
    unit: 'count',
    value_kind: 'scalar',
    metric_class: 'cumulative',
    period_kinds_json: ['h24', 'lifetime'],
    definition_text: 'Number of times the video was viewed.',
    is_estimate: false,
    is_cumulative: true,
    is_deprecated: false,
    deprecated_note: null,
    supersedes_key: null,
    superseded_by_key: null,
    api_version_min: null,
    api_version_max: null,
    required_permissions_json: [],
    account_threshold_json: {},
    availability_default: 'observed',
    preferred_for: 'views',
    catalog_version: 'test',
    ...overrides,
  };
}

function metric(overrides = {}) {
  return {
    metric_key: 'fb.post.media_views',
    provider_metric_name: 'post_video_views',
    unit: 'count',
    value_kind: 'scalar',
    metric_class: 'cumulative',
    period_kind: 'lifetime',
    availability: 'observed',
    availability_reason: null,
    value: 100,
    value_json: null,
    collected_at: '2026-11-20T00:00:00Z',
    period_start: null,
    period_end: null,
    sample_size: 1,
    method: 'sum',
    confidence: null,
    snapshot_ids: ['s1'],
    aggregation_ids: ['a1'],
    ...overrides,
  };
}

function publication(id, overrides = {}) {
  return {
    publication_id: id,
    account_id: 'acc-fb',
    platform: 'facebook',
    remote_post_id: `remote-${id}`,
    provider_published_at: '2026-11-10T00:00:00Z',
    state: 'published',
    content_project_id: null,
    campaign_id: null,
    variant_id: null,
    title: `Publication ${id}`,
    hashtags: [],
    has_metrics: true,
    aggregation_ids: [],
    ...overrides,
  };
}

function aggregation(overrides = {}) {
  return {
    id: 'agg-1',
    account_id: 'acc-fb',
    platform: 'facebook',
    scope: 'publication',
    publication_key: 'pub-1',
    campaign_key: '',
    metric_key: 'fb.post.media_views',
    window_kind: 'h24',
    window_hours: 24,
    window_tolerance_minutes: 30,
    value: 100,
    value_json: null,
    unit: 'count',
    sample_size: 1,
    availability: 'observed',
    method: 'sum',
    input_snapshot_ids: [],
    confidence: {},
    computed_at: '2026-11-20T00:00:00Z',
    aggregation_version: 1,
    ...overrides,
  };
}

// ---------------------------------------------------------------- missing is not zero

test('scalarMetric keeps an observed number and never coerces an unavailable one', () => {
  assert.equal(scalarMetric(metric({ value: 0, availability: 'observed' })).value, 0);
  assert.equal(scalarMetric(metric({ value: 42 })).value, 42);
  // A non-observed state with a value present is still absent (a malformed payload cannot become 0).
  assert.equal(scalarMetric(metric({ value: 0, availability: 'not_authorized' })).value, null);
  assert.equal(scalarMetric(metric({ value: null, availability: 'not_supported' })).value, null);
  assert.equal(scalarMetric(metric({ value: 0, availability: 'not_collected' })).availability, 'not_collected');
});

test('availabilityBreakdown counts states without inventing a total', () => {
  const rows = [metric({ availability: 'observed' }), metric({ availability: 'observed' }), metric({ availability: 'not_authorized' })].map(scalarMetric);
  assert.deepEqual(availabilityBreakdown(rows), { observed: 2, not_authorized: 1 });
});

test('metricsByWindow groups by window and orders them canonically', () => {
  const groups = metricsByWindow([
    metric({ period_kind: 'lifetime' }),
    metric({ period_kind: 'h24', metric_key: 'fb.post.reach' }),
    metric({ period_kind: 'initial', metric_key: 'fb.post.impressions' }),
  ]);
  assert.deepEqual(groups.map((group) => group.window), ['initial', 'h24', 'lifetime']);
  assert.equal(groups[1].metrics[0].metric_key, 'fb.post.reach');
});

// ---------------------------------------------------------------- retention

test('retentionChart draws only an observed, numeric curve', () => {
  const retention = {
    publication_id: 'pub-1',
    available: true,
    reason: null,
    curves: [
      {
        metric_key: 'fb.post.video_retention_curve',
        unit: 'ratio',
        collected_at: '2026-11-20T00:00:00Z',
        availability: 'observed',
        value_json: { points: [100, 82, 60, 41, 20], bucket_count: 5, unit: 'ratio' },
      },
    ],
  };
  const { chart, reason } = retentionChart(retention);
  assert.ok(chart);
  assert.equal(chart.bucketCount, 5);
  assert.equal(chart.incomplete, false);
  assert.deepEqual(chart.series.points.map((point) => point.y), [100, 82, 60, 41, 20]);
  // Categorical x labels sort as the interval order (localeCompare-safe zero padding).
  assert.deepEqual(chart.series.points.map((point) => point.x), ['1', '2', '3', '4', '5']);
  assert.equal(reason, '');
});

test('retentionChart never invents a curve when retention is unavailable', () => {
  const unavailable = { publication_id: 'pub-2', available: false, reason: 'TikTok does not expose a retention curve', curves: [] };
  const result = retentionChart(unavailable);
  assert.equal(result.chart, null);
  assert.match(result.reason, /TikTok does not expose/);

  // A stored-but-not-observed curve is not drawn either, and neither is a malformed payload.
  const notObserved = {
    publication_id: 'pub-2',
    available: false,
    reason: 'the metric is stored but was not observed',
    curves: [{ metric_key: 'ig.media.retention_curve', unit: 'ratio', collected_at: null, availability: 'not_supported', value_json: null }],
  };
  assert.equal(retentionChart(notObserved).chart, null);

  const malformed = {
    publication_id: 'pub-3',
    available: true,
    reason: null,
    curves: [{ metric_key: 'fb.post.video_retention_curve', unit: 'ratio', collected_at: null, availability: 'observed', value_json: { bucket_count: 0 } }],
  };
  const malformedResult = retentionChart(malformed);
  assert.equal(malformedResult.chart, null);
  assert.match(malformedResult.reason, /no numeric points/);

  assert.equal(retentionChart(null).chart, null);
});

test('retentionChart flags an incomplete curve instead of padding it', () => {
  const retention = {
    publication_id: 'pub-1',
    available: true,
    reason: null,
    curves: [{ metric_key: 'fb.post.video_retention_curve', unit: 'ratio', collected_at: null, availability: 'observed', value_json: { points: [100, 90], bucket_count: 2, expected_buckets: 40, incomplete: true } }],
  };
  const { chart } = retentionChart(retention);
  assert.ok(chart);
  assert.equal(chart.bucketCount, 2);
  assert.equal(chart.incomplete, true);
});

// ---------------------------------------------------------------- ranking / values

test('publicationValues is keyed by publication and honours the window filter', () => {
  const rows = [
    aggregation({ publication_key: 'pub-1', window_kind: 'h24', value: 10 }),
    aggregation({ publication_key: 'pub-1', window_kind: 'lifetime', value: 999 }),
    aggregation({ publication_key: 'pub-2', window_kind: 'h24', availability: 'not_collected', value: null }),
    aggregation({ scope: 'campaign', publication_key: '', campaign_key: 'c1', window_kind: 'h24', value: 5 }),
  ];
  const h24 = publicationValues(rows, 'fb.post.media_views', 'h24');
  assert.equal(h24.get('pub-1').value, 10);
  assert.equal(h24.has('c1'), false);
  const lifetime = publicationValues(rows, 'fb.post.media_views', 'lifetime');
  assert.equal(lifetime.get('pub-1').value, 999);
});

test('rankPublications separates observed rows from unmeasured ones and keeps an observed zero', () => {
  const publications = [publication('pub-1'), publication('pub-2'), publication('pub-3')];
  const values = publicationValues(
    [
      aggregation({ publication_key: 'pub-1', value: 0, availability: 'observed' }),
      aggregation({ publication_key: 'pub-2', value: 500, availability: 'observed' }),
    ],
    'fb.post.media_views',
    'h24',
  );
  const descending = rankPublications(publications, values, { direction: 'desc' });
  assert.deepEqual(descending.ranked.map((row) => row.publication.publication_id), ['pub-2', 'pub-1']);
  assert.equal(descending.ranked[1].value, 0); // an observed zero is a real ranking value
  assert.deepEqual(descending.unmeasured.map((item) => item.publication_id), ['pub-3']);
  assert.equal(descending.observedCount, 2);
  assert.equal(descending.unavailableCount, 1);

  const ascending = rankPublications(publications, values, { direction: 'asc', limit: 1 });
  assert.deepEqual(ascending.ranked.map((row) => row.publication.publication_id), ['pub-1']);
});

// ---------------------------------------------------------------- timeline / grouping

test('timelineSeries keeps a gap as a gap and sorts points by time', () => {
  const definitions = indexDefinitions([definition({ metric_key: 'fb.post.media_views', preferred_for: 'views' })]);
  const points = [
    { collected_at: '2026-11-12T00:00:00Z', metric_key: 'fb.post.media_views', window_kind: null, availability: 'not_collected', value: null, value_json: null, unit: 'count', run_id: 'r2' },
    { collected_at: '2026-11-10T00:00:00Z', metric_key: 'fb.post.media_views', window_kind: null, availability: 'observed', value: 100, value_json: null, unit: 'count', run_id: 'r1' },
    { collected_at: '2026-11-11T00:00:00Z', metric_key: 'fb.post.media_views', window_kind: null, availability: 'observed', value: 140, value_json: null, unit: 'count', run_id: 'r1' },
  ];
  const series = timelineSeries(points, definitions);
  assert.equal(series.length, 1);
  assert.deepEqual(series[0].points.map((point) => point.y), [100, 140, null]);
  assert.equal(series[0].points[2].availability, 'not_collected');
});

test('metricsByPlatform never merges platforms and falls back to the key prefix', () => {
  const definitions = indexDefinitions([
    definition({ metric_key: 'fb.post.media_views', platform: 'facebook' }),
    definition({ metric_key: 'ig.media.views', platform: 'instagram', preferred_for: 'views' }),
  ]);
  const groups = metricsByPlatform(
    [
      metric({ metric_key: 'fb.post.media_views' }),
      metric({ metric_key: 'ig.media.views' }),
      metric({ metric_key: 'tt.video.views' }), // not in the definitions map -> prefix fallback
    ],
    definitions,
  );
  const byPlatform = Object.fromEntries(groups.map((group) => [group.platform, group.metrics.map((item) => item.metric_key)]));
  assert.deepEqual(byPlatform.facebook, ['fb.post.media_views']);
  assert.deepEqual(byPlatform.instagram, ['ig.media.views']);
  assert.deepEqual(byPlatform.tiktok, ['tt.video.views']);
});

test('platformForMetric uses the registry then the provider prefix', () => {
  const definitions = indexDefinitions([definition({ metric_key: 'fb.post.media_views', platform: 'facebook' })]);
  assert.equal(platformForMetric('fb.post.media_views', definitions), 'facebook');
  assert.equal(platformForMetric('tt.video.views', definitions), 'tiktok');
  assert.equal(platformForMetric('mystery.key', definitions), 'unknown');
});

test('countBy is deterministic (count desc, then key)', () => {
  const rows = [{ p: 'facebook' }, { p: 'facebook' }, { p: 'tiktok' }, { p: 'instagram' }];
  assert.deepEqual(countBy(rows, (row) => row.p), [
    { key: 'facebook', count: 2 },
    { key: 'instagram', count: 1 },
    { key: 'tiktok', count: 1 },
  ]);
  assert.deepEqual(countBy([{ p: null }], (row) => row.p), []);
});

// ---------------------------------------------------------------- concepts

test('metricKeyForConcept resolves per platform and prefers a non-deprecated definition', () => {
  const definitions = [
    definition({ metric_key: 'fb.post.media_views', platform: 'facebook', preferred_for: 'views' }),
    definition({ metric_key: 'tt.video.views', platform: 'tiktok', preferred_for: 'views' }),
    definition({ metric_key: 'ig.media.impressions', platform: 'instagram', preferred_for: 'impressions', is_deprecated: true }),
    definition({ metric_key: 'ig.media.views', platform: 'instagram', preferred_for: 'impressions' }),
  ];
  assert.equal(metricKeyForConcept(definitions, 'views', 'facebook'), 'fb.post.media_views');
  assert.equal(metricKeyForConcept(definitions, 'views', 'tiktok'), 'tt.video.views');
  assert.equal(metricKeyForConcept(definitions, 'impressions', 'instagram'), 'ig.media.views');
  assert.equal(metricKeyForConcept(definitions, 'reach', 'facebook'), null);
});

test('conceptsForPlatform lists a platform\'s exposed concepts once', () => {
  const definitions = [
    definition({ metric_key: 'fb.post.media_views', preferred_for: 'views' }),
    definition({ metric_key: 'fb.post.reach', preferred_for: 'reach' }),
    definition({ metric_key: 'tt.video.views', platform: 'tiktok', preferred_for: 'views' }),
  ];
  const concepts = conceptsForPlatform(definitions, 'facebook');
  assert.deepEqual(concepts.map((concept) => concept.concept), ['reach', 'views']);
  assert.equal(concepts.find((concept) => concept.concept === 'views').metric_key, 'fb.post.media_views');
});

// ---------------------------------------------------------------- misc

test('latestTimestamp ignores null and invalid values', () => {
  assert.equal(latestTimestamp([null, '2026-11-01T00:00:00Z', 'nonsense', '2026-11-10T00:00:00Z']), '2026-11-10T00:00:00Z');
  assert.equal(latestTimestamp([null, undefined]), null);
});

test('isDelivered requires a remote id and not a failed state', () => {
  assert.equal(isDelivered(publication('pub-1')), true);
  assert.equal(isDelivered(publication('pub-1', { remote_post_id: '' })), false);
  assert.equal(isDelivered(publication('pub-1', { state: 'failed' })), false);
});
