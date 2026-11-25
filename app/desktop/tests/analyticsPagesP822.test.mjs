// P8.22 page-logic tests. Run with: node --test tests/analyticsPagesP822.test.mjs
//
// The pure part of Video performance, Audience growth and Hashtag analysis. Every test here exists to
// pin one of the metric-integrity rules the three pages were built around:
//
// * an unavailable value is absent — never zero — and an **observed** zero stays a real value;
// * a leaderboard never ranks a video on a metric it does not have;
// * two videos are comparable only when they share the metric's platform and window;
// * a follower count with one observation shows a level, never a "net change" or a direction;
// * a hashtag group below the sample gate is disclosed as excluded, never ranked;
// * nothing in hashtag analysis claims causation.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  bucketStart,
  bucketGrowthSeries,
  comparableGroups,
  compatiblePublications,
  conceptsForPlatform,
  datedGrowthPoints,
  growthSeries,
  hashtagCatalogue,
  indexDefinitions,
  metricKeyForConcept,
  observedDelta,
  rankVideos,
  valuesByMetric,
  videoMetricColumns,
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
    definition_text: 'Views of a post video.',
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
    preferred_for: null,
    catalog_version: 'test',
    ...overrides,
  };
}

const DEFINITIONS = [
  definition({ metric_key: 'fb.post.media_views', platform: 'facebook', preferred_for: 'views' }),
  definition({ metric_key: 'ig.media.views', platform: 'instagram', preferred_for: 'views' }),
  definition({ metric_key: 'tt.video.views', platform: 'tiktok', preferred_for: 'views' }),
  definition({ metric_key: 'fb.post.reach', platform: 'facebook', preferred_for: 'reach', unit: 'people' }),
  definition({ metric_key: 'tt.video.watch_time', platform: 'tiktok', preferred_for: 'watch_time', unit: 'seconds' }),
  definition({ metric_key: 'fb.post.reactions', platform: 'facebook', unit: 'count' }),
  definition({ metric_key: 'fb.page.followers_total', platform: 'facebook', object_type: 'account', preferred_for: 'followers' }),
  definition({ metric_key: 'tt.account.followers', platform: 'tiktok', object_type: 'account', preferred_for: 'followers' }),
];

const DEFINITION_MAP = indexDefinitions(DEFINITIONS);

function publication(overrides = {}) {
  return {
    publication_id: 'pub-1',
    account_id: 'acc-fb',
    platform: 'facebook',
    remote_post_id: 'fb-post-1',
    provider_published_at: '2026-11-20T10:00:00.000Z',
    state: 'published',
    content_project_id: 'p1',
    campaign_id: null,
    variant_id: 'v1',
    title: 'Launch teaser',
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
    window_tolerance_minutes: 180,
    value: 1000,
    value_json: null,
    unit: 'count',
    sample_size: 1,
    availability: 'observed',
    method: 'direct',
    input_snapshot_ids: ['s1'],
    confidence: {},
    computed_at: '2026-11-21T10:00:00.000Z',
    aggregation_version: 1,
    ...overrides,
  };
}

function growthPoint(overrides = {}) {
  return {
    bucket_start: '2026-11-01T00:00:00.000Z',
    metric_key: 'fb.page.followers_total',
    value: 1000,
    value_json: null,
    availability: 'observed',
    collected_at: '2026-11-01T00:00:00.000Z',
    ...overrides,
  };
}

// ============================================================== VIDEO PERFORMANCE

test('video columns come from the registry per platform, and a provider that omits a concept has no column', () => {
  const facebook = videoMetricColumns(DEFINITIONS, 'facebook');
  assert.deepEqual(
    facebook.map((column) => column.key),
    ['fb.post.media_views', 'fb.post.reach', 'fb.post.reactions'],
  );
  const tiktok = videoMetricColumns(DEFINITIONS, 'tiktok');
  assert.deepEqual(
    tiktok.map((column) => column.key),
    ['tt.video.views', 'tt.video.watch_time'],
  );
  // Instagram declares only the views concept here, so it gets exactly that one column.
  assert.deepEqual(
    videoMetricColumns(DEFINITIONS, 'instagram').map((column) => column.key),
    ['ig.media.views'],
  );
  // A platform with no metric in the registry gets no column at all: nothing is invented for it.
  assert.deepEqual(videoMetricColumns(DEFINITIONS, 'youtube'), []);
});

test('an account-level metric never becomes a video column', () => {
  const keys = videoMetricColumns(DEFINITIONS, 'facebook').map((column) => column.key);
  assert.equal(keys.includes('fb.page.followers_total'), false);
});

test('the registry concept resolves per platform instead of falling back to another platform', () => {
  assert.equal(metricKeyForConcept(DEFINITIONS, 'views', 'tiktok'), 'tt.video.views');
  assert.equal(metricKeyForConcept(DEFINITIONS, 'watch_time', 'facebook'), null);
  assert.equal(conceptsForPlatform(DEFINITIONS, 'tiktok').some((item) => item.concept === 'views'), true);
});

test('publication values keep an unobserved metric absent and an observed zero real', () => {
  const matrix = valuesByMetric([
    aggregation({ publication_key: 'pub-1', metric_key: 'fb.post.media_views', value: 1000 }),
    aggregation({ publication_key: 'pub-2', metric_key: 'fb.post.media_views', value: 0 }),
    aggregation({ publication_key: 'pub-3', metric_key: 'fb.post.media_views', value: null, availability: 'not_authorized' }),
    aggregation({ publication_key: 'pub-4', metric_key: 'fb.post.media_views', value: null, availability: 'not_collected' }),
  ]);
  const views = matrix.get('fb.post.media_views');
  assert.equal(views.get('pub-1').value, 1000);
  assert.equal(views.get('pub-2').value, 0, 'an observed zero is a measurement');
  assert.equal(views.get('pub-3').value, null, 'not_authorized is never 0');
  assert.equal(views.get('pub-3').availability, 'not_authorized');
  assert.equal(views.get('pub-4').value, null, 'not_collected is never 0');
});

test('valuesByMetric ignores other windows and other scopes', () => {
  const matrix = valuesByMetric(
    [
      aggregation({ publication_key: 'pub-1', window_kind: 'h24', value: 10 }),
      aggregation({ publication_key: 'pub-1', window_kind: 'lifetime', value: 900 }),
      aggregation({ publication_key: 'pub-1', scope: 'account', value: 5000 }),
    ],
    'h24',
  );
  assert.equal(matrix.get('fb.post.media_views').get('pub-1').value, 10);
});

test('the leaderboard ranks only measured videos and lists the rest as unmeasured', () => {
  const publications = [
    publication({ publication_id: 'pub-1' }),
    publication({ publication_id: 'pub-2' }),
    publication({ publication_id: 'pub-3' }),
  ];
  const values = valuesByMetric([
    aggregation({ publication_key: 'pub-1', value: 100 }),
    aggregation({ publication_key: 'pub-2', value: 900 }),
    aggregation({ publication_key: 'pub-3', value: null, availability: 'not_authorized' }),
  ]).get('fb.post.media_views');
  const board = rankVideos(publications, values);
  assert.deepEqual(board.ranked.map((row) => row.publication.publication_id), ['pub-2', 'pub-1']);
  assert.deepEqual(board.unmeasured.map((row) => row.publication_id), ['pub-3']);
  assert.equal(board.observedCount, 2);
  assert.equal(board.unavailableCount, 1);
});

test('sorting is by observed value in both directions, ascending puts the smallest measured first', () => {
  const publications = [publication({ publication_id: 'a' }), publication({ publication_id: 'b' }), publication({ publication_id: 'c' })];
  const values = valuesByMetric([
    aggregation({ publication_key: 'a', value: 500 }),
    aggregation({ publication_key: 'b', value: 50 }),
    aggregation({ publication_key: 'c', value: 0 }),
  ]).get('fb.post.media_views');
  assert.deepEqual(rankVideos(publications, values, { direction: 'asc' }).ranked.map((row) => row.publication.publication_id), ['c', 'b', 'a']);
  assert.deepEqual(rankVideos(publications, values).ranked.map((row) => row.publication.publication_id), ['a', 'b', 'c']);
});

test('a video with no row at this window is unmeasured rather than ranked at zero', () => {
  const board = rankVideos([publication({ publication_id: 'x' })], new Map());
  assert.deepEqual(board.ranked, []);
  assert.equal(board.unmeasured.length, 1);
  assert.equal(board.unmeasured[0].publication_id, 'x');
});

test('a comparison admits only publications on the metric platform and says why the rest were excluded', () => {
  const publications = [
    publication({ publication_id: 'fb-1', platform: 'facebook' }),
    publication({ publication_id: 'tt-1', platform: 'tiktok' }),
    publication({ publication_id: 'ig-1', platform: 'instagram' }),
  ];
  const { comparable, excluded } = compatiblePublications(publications, DEFINITION_MAP, 'fb.post.media_views');
  assert.deepEqual(comparable.map((row) => row.publication_id), ['fb-1']);
  assert.deepEqual(excluded.map((row) => row.publication.publication_id), ['tt-1', 'ig-1']);
  assert.match(excluded[0].reason, /defined for facebook/);
});

test('an unknown metric excludes every publication instead of comparing them anyway', () => {
  const { comparable, excluded } = compatiblePublications([publication()], DEFINITION_MAP, 'fb.post.nope');
  assert.deepEqual(comparable, []);
  assert.match(excluded[0].reason, /not in the registry/);
});

// ============================================================== AUDIENCE GROWTH

test('growth series keeps one series per metric, ordered by collection time', () => {
  const series = growthSeries(
    [
      growthPoint({ collected_at: '2026-11-03T00:00:00.000Z', value: 1200 }),
      growthPoint({ collected_at: '2026-11-01T00:00:00.000Z', value: 1000 }),
      growthPoint({ collected_at: '2026-11-02T00:00:00.000Z', metric_key: 'fb.page.followers_total', value: 1100 }),
    ],
    DEFINITION_MAP,
  );
  assert.equal(series.length, 1);
  assert.deepEqual(series[0].points.map((point) => point.y), [1000, 1100, 1200]);
  assert.equal(series[0].unit, 'count');
});

test('an unobserved growth row reaches the chart as a gap, not as a zero', () => {
  const series = growthSeries(
    [
      growthPoint({ collected_at: '2026-11-01T00:00:00.000Z', value: 1000 }),
      growthPoint({ collected_at: '2026-11-02T00:00:00.000Z', value: null, availability: 'not_authorized' }),
    ],
    DEFINITION_MAP,
  );
  assert.equal(series[0].points[1].y, null);
  assert.equal(series[0].points[1].availability, 'not_authorized');
});

test('a growth row with no collection time is left out rather than dated now', () => {
  assert.equal(datedGrowthPoints([growthPoint({ collected_at: null })]).length, 0);
  const series = growthSeries([growthPoint({ collected_at: null })], DEFINITION_MAP);
  assert.deepEqual(series, []);
});

test('bucketing keeps the latest observed value in each bucket and never carries one forward', () => {
  const points = [
    growthPoint({ collected_at: '2026-11-02T08:00:00.000Z', value: 1000 }),
    growthPoint({ collected_at: '2026-11-02T20:00:00.000Z', value: 1100 }),
    // No observation in the week of 9 November at all: that bucket must be absent.
    growthPoint({ collected_at: '2026-11-16T09:00:00.000Z', value: 1300 }),
  ];
  const weekly = bucketGrowthSeries(points, 'week', DEFINITION_MAP);
  assert.deepEqual(weekly[0].points.map((point) => point.y), [1100, 1300]);
  assert.equal(weekly[0].points.length, 2, 'the empty week is a gap, not a carried-forward value');
});

test('bucketing drops unobserved rows so a weekly point is always a real measurement', () => {
  const weekly = bucketGrowthSeries(
    [
      growthPoint({ collected_at: '2026-11-02T08:00:00.000Z', value: 1000 }),
      growthPoint({ collected_at: '2026-11-02T20:00:00.000Z', value: null, availability: 'not_collected' }),
    ],
    'week',
    DEFINITION_MAP,
  );
  assert.deepEqual(weekly[0].points.map((point) => point.y), [1000]);
});

test('bucketStart is UTC and starts weeks on Monday', () => {
  assert.equal(bucketStart('2026-11-04T23:30:00.000Z', 'day'), '2026-11-04T00:00:00.000Z');
  assert.equal(bucketStart('2026-11-04T23:30:00.000Z', 'week'), '2026-11-02T00:00:00.000Z');
  assert.equal(bucketStart('2026-11-04T23:30:00.000Z', 'month'), '2026-11-01T00:00:00.000Z');
});

test('a single observed follower count is a level, not a change', () => {
  assert.equal(observedDelta([growthPoint()]), null);
  assert.equal(observedDelta([]), null);
});

test('two observed follower counts produce a derived net change and a direction', () => {
  const delta = observedDelta([
    growthPoint({ collected_at: '2026-11-01T00:00:00.000Z', value: 1000 }),
    growthPoint({ collected_at: '2026-11-08T00:00:00.000Z', value: 940 }),
  ]);
  assert.equal(delta.net, -60);
  assert.equal(delta.direction, 'down');
  assert.equal(delta.observations, 2);
  assert.equal(delta.firstAt, '2026-11-01T00:00:00.000Z');
});

test('an unobserved row cannot turn a level into a change', () => {
  const delta = observedDelta([
    growthPoint({ collected_at: '2026-11-01T00:00:00.000Z', value: 1000 }),
    growthPoint({ collected_at: '2026-11-08T00:00:00.000Z', value: null, availability: 'not_authorized' }),
  ]);
  assert.equal(delta, null, 'one observation plus one gap is still a level');
});

test('a zero-valued observation is a measurement and delta uses it', () => {
  const delta = observedDelta([
    growthPoint({ collected_at: '2026-11-01T00:00:00.000Z', value: 4 }),
    growthPoint({ collected_at: '2026-11-08T00:00:00.000Z', value: 0 }),
  ]);
  assert.equal(delta.net, -4);
  assert.equal(delta.last, 0);
});

test('follower metrics keep their own identity per platform instead of collapsing into one', () => {
  const fb = metricKeyForConcept(DEFINITIONS, 'followers', 'facebook');
  const tt = metricKeyForConcept(DEFINITIONS, 'followers', 'tiktok');
  assert.notEqual(fb, tt);
  assert.equal(DEFINITION_MAP.get(fb).provider_metric_name, 'post_video_views');
  assert.equal(DEFINITION_MAP.get(tt).metric_key, 'tt.account.followers');
});

// ============================================================== HASHTAG ANALYSIS

test('the catalogue counts publications per hashtag case-insensitively and keeps the display form', () => {
  const catalogue = hashtagCatalogue([
    publication({ publication_id: 'p1', hashtags: ['Launch', 'bts'], account_id: 'acc-1' }),
    publication({ publication_id: 'p2', hashtags: ['launch'], account_id: 'acc-2' }),
    publication({ publication_id: 'p3', hashtags: ['#Launch'], account_id: 'acc-1' }),
    publication({ publication_id: 'p4', hashtags: [] }),
  ]);
  const launch = catalogue.find((entry) => entry.hashtag === 'launch');
  assert.equal(launch.publications, 3);
  assert.equal(launch.accounts, 2, 'the account count is distinct accounts, not publications');
  assert.equal(launch.label, 'Launch', 'the first display form is kept, not rewritten');
  assert.equal(catalogue.find((entry) => entry.hashtag === 'bts').publications, 1);
  assert.equal(catalogue.some((entry) => entry.hashtag === ''), false);
});

test('the catalogue records the observation period and the platforms a hashtag appeared on', () => {
  const catalogue = hashtagCatalogue([
    publication({ publication_id: 'p1', hashtags: ['launch'], platform: 'facebook', provider_published_at: '2026-11-20T00:00:00.000Z' }),
    publication({ publication_id: 'p2', hashtags: ['launch'], platform: 'tiktok', provider_published_at: '2026-11-10T00:00:00.000Z' }),
  ]);
  assert.deepEqual(catalogue[0].platforms, ['facebook', 'tiktok']);
  assert.equal(catalogue[0].firstPublished, '2026-11-10T00:00:00.000Z');
  assert.equal(catalogue[0].lastPublished, '2026-11-20T00:00:00.000Z');
});

test('a hashtag with no publish date reports no period rather than a guessed one', () => {
  const catalogue = hashtagCatalogue([publication({ hashtags: ['launch'], provider_published_at: null })]);
  assert.equal(catalogue[0].firstPublished, null);
  assert.equal(catalogue[0].lastPublished, null);
});

test('groups below the sample gate are excluded and disclosed instead of ranked', () => {
  const comparison = {
    metric_key: 'tt.video.views',
    attribute: 'hashtag',
    window: 'h24',
    groups: [
      { label: 'launch', n: 7, availability: 'observed', median: 4200, evidence: [] },
      { label: 'tips', n: 2, availability: 'observed', median: 1900, evidence: [] },
      { label: 'quiet', n: 6, availability: 'not_authorized', median: null, evidence: [] },
    ],
    verdicts: [],
    comparability: null,
    limitations: [],
    sufficient: false,
    note: '',
  };
  const { ranked, excluded } = comparableGroups(comparison, 5);
  assert.deepEqual(ranked.map((group) => group.label), ['launch']);
  assert.deepEqual(excluded.map((item) => item.group.label), ['tips', 'quiet']);
  assert.match(excluded[0].reason, /fewer than 5/);
  assert.match(excluded[1].reason, /not observed/);
});

test('ranking is by median and an observed group with a null median is excluded', () => {
  const comparison = {
    metric_key: 'tt.video.views',
    attribute: 'hashtag',
    window: 'h24',
    groups: [
      { label: 'a', n: 9, availability: 'observed', median: 100, evidence: [] },
      { label: 'b', n: 9, availability: 'observed', median: 900, evidence: [] },
      { label: 'c', n: 9, availability: 'observed', median: null, evidence: [] },
    ],
    verdicts: [],
    comparability: null,
    limitations: [],
    sufficient: true,
    note: '',
  };
  const { ranked, excluded } = comparableGroups(comparison, 1);
  assert.deepEqual(ranked.map((group) => group.label), ['b', 'a']);
  assert.equal(excluded[0].reason, 'no median');
});

test('no comparison at all is an empty ranking, not an error', () => {
  assert.deepEqual(comparableGroups(null, 3), { ranked: [], excluded: [] });
  assert.deepEqual(comparableGroups(undefined, 3), { ranked: [], excluded: [] });
});

test('a hashtag missing an observation for a metric is not given a value', () => {
  const matrix = valuesByMetric([
    aggregation({ publication_key: 'pub-1', metric_key: 'tt.video.views', value: 500 }),
    aggregation({ publication_key: 'pub-2', metric_key: 'tt.video.views', value: null, availability: 'not_collected' }),
  ]);
  const views = matrix.get('tt.video.views');
  assert.equal(views.get('pub-1').value, 500);
  assert.equal(views.get('pub-2').value, null);
});
