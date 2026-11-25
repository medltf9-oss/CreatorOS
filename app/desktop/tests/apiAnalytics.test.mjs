// P8.19 API-client tests. Run with: node --test tests/apiAnalytics.test.mjs
//
// The client is runtime-dependency-free, so it is exercised against a stubbed `fetch` without a
// bundler. The last test is the chart-integration guard: an unavailable metric from the API must
// reach the chart as a gap, never as zero.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { AnalyticsApi, AnalyticsApiError, buildQuery } from '../src/services/apiAnalytics.ts';
import { pointsFromRows, segmentObserved } from '../src/components/chartModel.ts';

const realFetch = globalThis.fetch;
let calls;

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

function stub(handler) {
  calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, init });
    return handler(url, init);
  };
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

const api = () => new AnalyticsApi('http://127.0.0.1:9999', 'test-token');

test('a successful response is returned and the bearer token is sent', async () => {
  stub(() => jsonResponse({ accounts_total: 2, accounts_connected: 1, publications_measured: 5, observations: 40, aggregations: 3, recommendations_by_status: { draft: 1 }, latest_collection_at: null, generated_at: '2026-11-24T12:00:00Z' }));
  const body = await api().overview();
  assert.equal(body.accounts_connected, 1);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://127.0.0.1:9999/analytics/overview');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer test-token');
});

test('an error envelope becomes a typed AnalyticsApiError', async () => {
  stub(() => jsonResponse({ code: 'NO_PUBLICATIONS', message: 'this account has no published objects', correlation_id: 'c-1' }, 409));
  await assert.rejects(
    () => api().compare({ account_id: 'a', metric_key: 'tt.video.views' }),
    (error) => {
      assert.ok(error instanceof AnalyticsApiError);
      assert.equal(error.code, 'NO_PUBLICATIONS');
      assert.equal(error.status, 409);
      assert.equal(error.correlationId, 'c-1');
      return true;
    },
  );
});

test('an empty list is fine and not an error', async () => {
  stub(() => jsonResponse({ items: [], total: 0, limit: 10, offset: 0 }));
  const page = await api().publications();
  assert.deepEqual(page.items, []);
  assert.equal(page.total, 0);
});

test('query parameters serialise filters and skip empty ones', async () => {
  stub(() => jsonResponse({ account_id: 'a', metric_key: 'tt.video.views', bucket: 'day', points: [], availability_summary: {}, note: '' }));
  await api().series({ account_id: 'a', metric_key: 'tt.video.views', start: '2026-11-01T00:00:00Z', end: '2026-11-24T23:59:59Z' });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/analytics/series');
  assert.equal(url.searchParams.get('account_id'), 'a');
  assert.equal(url.searchParams.get('metric_key'), 'tt.video.views');
  assert.equal(url.searchParams.get('bucket'), 'day');
  assert.equal(url.searchParams.get('start'), '2026-11-01T00:00:00Z');
  assert.equal(url.searchParams.get('end'), '2026-11-24T23:59:59Z');
  assert.equal(url.searchParams.has('platform'), false);
});

test('account, platform, date and pagination filters all reach the URL', async () => {
  stub(() => jsonResponse({ items: [], total: 0, limit: 5, offset: 10 }));
  await api().publications({ account_id: 'acc-1', platform: 'tiktok', start: '2026-11-01', end: '2026-11-20', limit: 5, offset: 10 });
  const url = new URL(calls[0].url);
  assert.equal(url.searchParams.get('account_id'), 'acc-1');
  assert.equal(url.searchParams.get('platform'), 'tiktok');
  assert.equal(url.searchParams.get('start'), '2026-11-01');
  assert.equal(url.searchParams.get('end'), '2026-11-20');
  assert.equal(url.searchParams.get('limit'), '5');
  assert.equal(url.searchParams.get('offset'), '10');
});

test('metrics default to a bounded page and send platform/object filters', async () => {
  stub(() => jsonResponse({ items: [], total: 0, limit: 200, offset: 0 }));
  await api().metrics({ platform: 'tiktok', object_type: 'video' });
  const url = new URL(calls[0].url);
  assert.equal(url.searchParams.get('platform'), 'tiktok');
  assert.equal(url.searchParams.get('object_type'), 'video');
  assert.equal(url.searchParams.get('limit'), '200');
});

test('a malformed list is rejected instead of being trusted', async () => {
  stub(() => jsonResponse({ items: { nope: true }, total: 1, limit: 200, offset: 0 }));
  await assert.rejects(
    () => api().metrics(),
    (error) => error instanceof AnalyticsApiError && error.code === 'MALFORMED_RESPONSE',
  );
});

test('an abort signal is forwarded so a superseded request can be cancelled', async () => {
  stub(() => jsonResponse({ accounts: [], generated_at: '2026-11-24T12:00:00Z' }));
  const controller = new AbortController();
  await api().freshness(controller.signal);
  assert.equal(calls[0].init.signal, controller.signal);
});

test('compare is a POST with a JSON body', async () => {
  stub(() => jsonResponse({ metric_key: 'x', attribute: 'format', window: 'h24', groups: [], verdicts: null, comparability: null, limitations: [], sufficient: null, note: '' }));
  await api().compare({ account_id: 'a', metric_key: 'x', attribute: 'format', max_groups: 3 });
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].init.body), { account_id: 'a', metric_key: 'x', attribute: 'format', max_groups: 3 });
});

test('buildQuery omits empty values and encodes the rest', () => {
  assert.equal(buildQuery({ a: 1, b: undefined, c: null, d: '', e: 'x y' }), '?a=1&e=x+y');
  assert.equal(buildQuery({}), '');
});

test('consent reads the per-account plan and never invents a scope', async () => {
  stub(() =>
    jsonResponse({
      account_id: 'acc-1',
      platform: 'facebook',
      route: 'facebook_login',
      account_type: 'facebook_page',
      provider_account_id: '1234567890',
      capability_key: 'facebook.page.read_insights',
      capability_label: 'Read Page insights',
      capability_state: 'needs_user_consent',
      status: 'consent_required',
      required_scopes: ['pages_read_engagement', 'read_insights'],
      granted_scopes: ['pages_read_engagement'],
      missing_scopes: ['read_insights'],
      requested_scopes: ['pages_read_engagement', 'read_insights'],
      scope_reasons: { read_insights: 'required to read analytics' },
      page_tasks_required: ['ANALYZE'],
      page_tasks_held: [],
      page_tasks_missing: ['ANALYZE'],
      page_tasks_known: false,
      app_mode: 'live',
      developer_approval_required: false,
      can_start: true,
      blocked_by: ['user_consent'],
      blockers_text: ['the account has not granted the insight permission yet'],
      metrics_blocked: [],
      docs_url: null,
      detail: 'needs read_insights',
    }),
  );
  const plan = await api().consent('acc-1');
  assert.equal(calls[0].url, 'http://127.0.0.1:9999/analytics/consent/acc-1');
  assert.deepEqual(plan.missing_scopes, ['read_insights']);
  assert.equal(plan.status, 'consent_required');
});

test('startConsent sends only an intent note, never a scope list', async () => {
  stub(() => jsonResponse({ status: 'authorization_started', account_id: 'acc-1', plan: null, session_id: 's-1', authorization_url: 'https://example/authorize', redirect_uri: 'http://127.0.0.1:47821/oauth/callback/facebook_login', expires_at: '2026-11-24T12:10:00Z', scopes_requested: ['read_insights'], pkce: null, app_mode: 'live', poll_url: '/social/oauth/transactions/s-1', message: '' }));
  const started = await api().startConsent('acc-1', { intent: 'grant-analytics' });
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), { intent: 'grant-analytics' });
  assert.equal(started.status, 'authorization_started');
  assert.equal(started.session_id, 's-1');
});

test('consentOverview lists the accounts that need a permission', async () => {
  stub(() => jsonResponse({ accounts: [{ account_id: 'acc-1', platform: 'instagram', label: 'IG', status: 'consent_required', capability_key: 'instagram.read_insights', missing_scopes: ['instagram_manage_insights'], can_start: true, detail: '' }], totals: { consent_required: 1 }, generated_at: '2026-11-24T12:00:00Z' }));
  const overview = await api().consentOverview();
  assert.equal(calls[0].url, 'http://127.0.0.1:9999/analytics/consent');
  assert.equal(overview.accounts[0].account_id, 'acc-1');
  assert.equal(overview.totals.consent_required, 1);
});

// ---------------------------------------------------------------- P8.21: publication / campaign

test('publication detail is fetched by id and keeps an observed zero distinct from a gap', async () => {
  stub(() =>
    jsonResponse({
      publication: { publication_id: 'pub-1', account_id: 'acc-1', platform: 'facebook', remote_post_id: 'fb-1', provider_published_at: '2026-11-10T00:00:00Z', state: 'published', content_project_id: null, campaign_id: null, variant_id: null, title: 'Teaser', hashtags: [], has_metrics: true, aggregation_ids: [] },
      metrics: [
        { metric_key: 'fb.post.media_views', provider_metric_name: 'x', unit: 'count', value_kind: 'scalar', metric_class: 'cumulative', period_kind: 'h24', availability: 'observed', availability_reason: null, value: 0, value_json: null, collected_at: '2026-11-11T00:00:00Z', period_start: null, period_end: null, sample_size: 1, method: 'sum', confidence: null, snapshot_ids: [], aggregation_ids: [] },
        { metric_key: 'fb.post.reach', provider_metric_name: 'y', unit: 'people', value_kind: 'scalar', metric_class: 'point_in_time', period_kind: 'h24', availability: 'not_authorized', availability_reason: 'needs read_insights', value: null, value_json: null, collected_at: '2026-11-11T00:00:00Z', period_start: null, period_end: null, sample_size: null, method: 'direct', confidence: null, snapshot_ids: [], aggregation_ids: [] },
      ],
    }),
  );
  const detail = await api().publication('pub-1');
  assert.equal(calls[0].url, 'http://127.0.0.1:9999/analytics/publications/pub-1');
  assert.equal(detail.metrics[0].value, 0);
  assert.equal(detail.metrics[0].availability, 'observed');
  assert.equal(detail.metrics[1].value, null);
  assert.equal(detail.metrics[1].availability, 'not_authorized');
});

test('retention is returned verbatim and an unavailable result carries no curve', async () => {
  stub(() => jsonResponse({ publication_id: 'pub-2', available: false, reason: 'TikTok does not expose a retention curve', curves: [] }));
  const retention = await api().retention('pub-2');
  assert.equal(calls[0].url, 'http://127.0.0.1:9999/analytics/publications/pub-2/retention');
  assert.equal(retention.available, false);
  assert.deepEqual(retention.curves, []);
});

test('a served retention curve keeps its structured payload untouched', async () => {
  stub(() =>
    jsonResponse({
      publication_id: 'pub-1',
      available: true,
      reason: null,
      curves: [{ metric_key: 'fb.post.video_retention_curve', unit: 'ratio', collected_at: '2026-11-20T00:00:00Z', availability: 'observed', value_json: { points: [100, 80, 55], bucket_count: 3, unit: 'ratio' } }],
    }),
  );
  const retention = await api().retention('pub-1');
  assert.equal(retention.available, true);
  assert.deepEqual(retention.curves[0].value_json.points, [100, 80, 55]);
});

test('publication timeline is a plain list of points', async () => {
  stub(() => jsonResponse({ publication_id: 'pub-1', points: [{ collected_at: '2026-11-12T00:00:00Z', metric_key: 'fb.post.media_views', window_kind: null, availability: 'not_collected', value: null, value_json: null, unit: 'count', run_id: 'r1' }] }));
  const timeline = await api().timeline('pub-1');
  assert.equal(calls[0].url, 'http://127.0.0.1:9999/analytics/publications/pub-1/timeline');
  assert.equal(timeline.points[0].availability, 'not_collected');
  assert.equal(timeline.points[0].value, null);
});

test('campaigns list and detail are fetched and a malformed list is rejected', async () => {
  stub(() => jsonResponse({ items: [{ campaign_id: 'c1', name: 'Launch', goal: 'Awareness', starts_on: null, ends_on: null, projects: 2, publications: 1, observations: 4 }], total: 1, limit: 100, offset: 0 }));
  const page = await api().campaigns({ limit: 50 });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/analytics/campaigns');
  assert.equal(url.searchParams.get('limit'), '50');
  assert.equal(page.items[0].campaign_id, 'c1');

  stub(() => jsonResponse({ campaign: { campaign_id: 'c1', name: 'Launch', goal: null, starts_on: null, ends_on: null, projects: 1, publications: 1, observations: 2 }, publications: [], metrics: [] }));
  const detail = await api().campaign('c1');
  assert.equal(calls[0].url, 'http://127.0.0.1:9999/analytics/campaigns/c1');
  assert.equal(detail.campaign.name, 'Launch');

  stub(() => jsonResponse({ items: { bad: true }, total: 1, limit: 100, offset: 0 }));
  await assert.rejects(() => api().campaigns(), (error) => error.code === 'MALFORMED_RESPONSE');
});

test('snapshots and aggregations forward their filters and parse pages', async () => {
  stub(() => jsonResponse({ items: [], total: 0, limit: 200, offset: 0 }));
  await api().snapshots({ publication_id: 'pub-1', metric_key: 'fb.post.media_views' });
  const snapUrl = new URL(calls[0].url);
  assert.equal(snapUrl.pathname, '/analytics/snapshots');
  assert.equal(snapUrl.searchParams.get('publication_id'), 'pub-1');
  assert.equal(snapUrl.searchParams.get('metric_key'), 'fb.post.media_views');

  await api().aggregations({ scope: 'publication', metric_key: 'fb.post.media_views', window_kind: 'h24' });
  const aggUrl = new URL(calls[1].url);
  assert.equal(aggUrl.pathname, '/analytics/aggregations');
  assert.equal(aggUrl.searchParams.get('scope'), 'publication');
  assert.equal(aggUrl.searchParams.get('window_kind'), 'h24');
});

test('an unavailable metric is preserved and reaches the chart as a gap, never zero', async () => {
  stub(() =>
    jsonResponse({
      account_id: 'a',
      metric_key: 'tt.video.views',
      bucket: 'day',
      points: [
        { bucket_start: '2026-11-10T00:00:00Z', bucket_end: '2026-11-11T00:00:00Z', value: 100, availability: 'observed', observations: 1 },
        { bucket_start: '2026-11-11T00:00:00Z', bucket_end: '2026-11-12T00:00:00Z', value: null, availability: 'not_collected', observations: 0 },
        { bucket_start: '2026-11-12T00:00:00Z', bucket_end: '2026-11-13T00:00:00Z', value: 0, availability: 'observed', observations: 1 },
      ],
      availability_summary: { observed: 2, not_collected: 1 },
      note: '',
    }),
  );
  const series = await api().series({ account_id: 'a', metric_key: 'tt.video.views' });
  // The client must not touch the payload.
  assert.equal(series.points[1].value, null);
  assert.equal(series.points[1].availability, 'not_collected');

  const points = pointsFromRows(series.points.map((p) => ({ at: p.bucket_start, value: p.value, availability: p.availability })));
  assert.deepEqual(points.map((p) => p.y), [100, null, 0]);
  // 100 is a run, then a gap, then a separate run at the real zero: the missing day is not drawn.
  assert.deepEqual(segmentObserved(points).map((segment) => segment.length), [1, 1]);
});

// --------------------------------------------------------------- P8.22 hashtag endpoint

test('hashtags is a GET on the existing comparison endpoint with hashtag semantics', async () => {
  stub(() =>
    jsonResponse({
      metric_key: 'tt.video.views',
      attribute: 'hashtag',
      window: 'h24',
      groups: [
        { label: 'launch', n: 7, availability: 'observed', median: 4200, evidence: [] },
        { label: 'tips', n: 2, availability: 'observed', median: 1900, evidence: [] },
      ],
      verdicts: null,
      comparability: null,
      limitations: ['correlation is not causation'],
      sufficient: false,
      note: '',
    }),
  );
  const comparison = await api().hashtags({ account_id: 'acc-tt', metric_key: 'tt.video.views' });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/analytics/hashtags');
  assert.equal(calls[0].init.method, undefined, 'a GET carries no explicit method');
  assert.equal(url.searchParams.get('account_id'), 'acc-tt');
  assert.equal(url.searchParams.get('metric_key'), 'tt.video.views');
  assert.equal(url.searchParams.get('window_kind'), 'h24');
  assert.equal(url.searchParams.get('max_groups'), '3');
  // The client returns the backend's sample sizes and limitations untouched.
  assert.equal(comparison.groups[1].n, 2);
  assert.deepEqual(comparison.limitations, ['correlation is not causation']);
});

test('hashtags forwards an abort signal so a superseded comparison is cancelled', async () => {
  stub(() => jsonResponse({ metric_key: 'x', attribute: 'hashtag', window: 'h24', groups: [], verdicts: null, comparability: null, limitations: [], sufficient: null, note: '' }));
  const controller = new AbortController();
  await api().hashtags({ account_id: 'a', metric_key: 'x', window_kind: 'd7', max_groups: 5 }, controller.signal);
  assert.equal(calls[0].init.signal, controller.signal);
  assert.equal(new URL(calls[0].url).searchParams.get('window_kind'), 'd7');
  assert.equal(new URL(calls[0].url).searchParams.get('max_groups'), '5');
});

// --------------------------------------------------------------- P8.23 recommendations / reports / settings

const RECOMMENDATION = {
  id: 'rec-1',
  account_id: 'acc-fb',
  platform: 'facebook',
  campaign_id: null,
  content_project_id: null,
  kind: 'topic',
  observation: 'Product-education posts reached a higher median.',
  sample_size: { publications: 14 },
  period_start: '2026-11-10T00:00:00Z',
  period_end: '2026-11-23T00:00:00Z',
  confidence: 0.91,
  confidence_method: 'bootstrap',
  limitations: ['observational'],
  proposed_experiment: { change: 'publish two more' },
  expected_benefit: { kind: 'estimate' },
  status: 'draft',
  generated_by: 'analytics.recommendations',
  approval_request_id: null,
  decided_at: null,
  decision_note: null,
  created_at: '2026-11-23T00:00:00Z',
};

const REPORT = {
  id: 'rep-1',
  kind: 'overview',
  period_start: null,
  period_end: null,
  filters: { account_id: null, title: 'Review' },
  sections: [],
  summary: 'summary',
  sha256: 'a'.repeat(64),
  bytes: 120,
  status: 'final',
  generated_by: 'analytics.reports',
  created_at: '2026-11-23T00:00:00Z',
};

const SETTINGS = {
  settings: { collection_enabled: true, retention_days: 365 },
  bounds: { retention_days: { min: 7, max: 3650 } },
  note: 'Stored preferences. They do not change a running collect; the sweeper reads them on its next tick.',
};

test('recommendations list applies the API filters and defaults the page size', async () => {
  stub(() => jsonResponse({ items: [RECOMMENDATION], total: 1, limit: 50, offset: 0 }));
  const page = await api().recommendations({ status: 'draft', account_id: 'acc-fb', kind: 'topic' });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/analytics/recommendations');
  assert.equal(url.searchParams.get('status'), 'draft');
  assert.equal(url.searchParams.get('account_id'), 'acc-fb');
  assert.equal(url.searchParams.get('kind'), 'topic');
  assert.equal(url.searchParams.get('limit'), '50');
  assert.equal(url.searchParams.get('offset'), '0');
  assert.equal(page.items[0].id, 'rec-1');
});

test('an unfiltered recommendation list sends no status parameter', async () => {
  stub(() => jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }));
  await api().recommendations();
  const url = new URL(calls[0].url);
  assert.equal(url.searchParams.has('status'), false);
  assert.equal(url.searchParams.has('kind'), false);
  assert.equal(url.searchParams.get('limit'), '50');
});

test('the detail endpoint returns the record and its evidence together', async () => {
  stub(() =>
    jsonResponse({
      recommendation: RECOMMENDATION,
      evidence: [
        {
          metric_key: 'fb.post.media_views',
          provider_metric_name: 'post_video_views',
          unit: 'count',
          value_kind: 'scalar',
          metric_class: 'cumulative',
          period_kind: 'd28',
          availability: 'observed',
          value: 12400,
          value_json: null,
          collected_at: '2026-11-23T00:00:00Z',
          period_start: null,
          period_end: null,
          sample_size: 1,
          snapshot_ids: ['snap-1'],
          aggregation_ids: [],
        },
      ],
    }),
  );
  const detail = await api().recommendation('rec-1');
  assert.equal(new URL(calls[0].url).pathname, '/analytics/recommendations/rec-1');
  assert.equal(detail.recommendation.id, 'rec-1');
  assert.equal(detail.evidence[0].metric_key, 'fb.post.media_views');
  assert.deepEqual(detail.evidence[0].snapshot_ids, ['snap-1']);
});

test('accept posts to /accept with a note and never touches a publishing endpoint', async () => {
  stub(() => jsonResponse({ ...RECOMMENDATION, status: 'accepted', approval_request_id: 'apr-1' }));
  const updated = await api().acceptRecommendation('rec-1', { note: 'try it' });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/analytics/recommendations/rec-1/accept');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), { note: 'try it' });
  assert.equal(updated.status, 'accepted');
  assert.equal(calls.some((call) => /publish|schedul/i.test(call.url)), false);
});

test('accept with no note sends an explicit null, not an omitted field', async () => {
  stub(() => jsonResponse({ ...RECOMMENDATION, status: 'accepted' }));
  await api().acceptRecommendation('rec-1');
  assert.deepEqual(JSON.parse(calls[0].init.body), { note: null });
});

test('reject posts its own reason to /reject', async () => {
  stub(() => jsonResponse({ ...RECOMMENDATION, status: 'rejected', decision_note: 'too small' }));
  const updated = await api().rejectRecommendation('rec-1', { reason: 'too small' });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/analytics/recommendations/rec-1/reject');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), { reason: 'too small' });
  assert.equal(updated.status, 'rejected');
});

test('an already-decided recommendation surfaces the server conflict as a typed error', async () => {
  stub(() => jsonResponse({ code: 'CONFLICT', message: 'recommendation is already accepted' }, 409));
  await assert.rejects(
    () => api().acceptRecommendation('rec-1'),
    (error) => error instanceof AnalyticsApiError && error.status === 409 && /already accepted/.test(error.message),
  );
});

test('a refresh returns the created count and the backend note verbatim', async () => {
  stub(() =>
    jsonResponse({
      account_id: 'acc-fb',
      created: 2,
      recommendation_ids: ['r1', 'r2'],
      note: 'Drafts only. Accepting one creates an approval request; nothing is published.',
    }),
  );
  const result = await api().refreshRecommendations({ account_id: 'acc-fb', window_kind: 'd7' });
  assert.equal(new URL(calls[0].url).pathname, '/analytics/recommendations/refresh');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), { account_id: 'acc-fb', window_kind: 'd7', limit: 10 });
  assert.equal(result.created, 2);
  assert.match(result.note, /nothing is published/);
});

test('reports list, create, read and delete use their documented verbs', async () => {
  stub(() => jsonResponse({ items: [REPORT], total: 1, limit: 50, offset: 0 }));
  await api().reports({ kind: 'overview' });
  assert.equal(new URL(calls[0].url).searchParams.get('kind'), 'overview');
  assert.equal(new URL(calls[0].url).searchParams.get('limit'), '50');

  stub(() => jsonResponse(REPORT, 201));
  await api().createReport({ kind: 'overview', title: 'Review' });
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(new URL(calls[0].url).pathname, '/analytics/reports');
  assert.deepEqual(JSON.parse(calls[0].init.body), { kind: 'overview', title: 'Review' });

  stub(() => jsonResponse(REPORT));
  await api().report('rep-1');
  assert.equal(new URL(calls[0].url).pathname, '/analytics/reports/rep-1');

  stub(() => ({ ok: true, status: 204, text: async () => '' }));
  await api().deleteReport('rep-1');
  assert.equal(calls[0].init.method, 'DELETE');
  assert.equal(new URL(calls[0].url).pathname, '/analytics/reports/rep-1');
});

test('the report file is downloaded as bytes, not parsed', async () => {
  const payload = '{"report_id":"rep-1"}\n';
  stub(() => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    arrayBuffer: async () => new TextEncoder().encode(payload).buffer,
    text: async () => payload,
  }));
  const download = await api().downloadReport('rep-1');
  assert.equal(new URL(calls[0].url).pathname, '/analytics/reports/rep-1/file');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer test-token');
  assert.equal(download.bytes.byteLength, payload.length);
  assert.equal(download.text, payload);
  assert.equal(download.contentType, 'application/json');
});

test('a failed download raises a typed error instead of returning empty bytes', async () => {
  stub(() => jsonResponse({ code: 'REPORT_NOT_FOUND', message: 'report not found', correlation_id: 'c-1' }, 404));
  await assert.rejects(
    () => api().downloadReport('rep-x'),
    (error) =>
      error instanceof AnalyticsApiError &&
      error.code === 'REPORT_NOT_FOUND' &&
      error.status === 404 &&
      error.correlationId === 'c-1',
  );
});

test('settings read and write the documented shape', async () => {
  stub(() => jsonResponse(SETTINGS));
  const read = await api().settings();
  assert.equal(new URL(calls[0].url).pathname, '/analytics/settings');
  assert.deepEqual(read.bounds.retention_days, { min: 7, max: 3650 });
  assert.match(read.note, /next tick/);

  stub(() => jsonResponse({ ...SETTINGS, settings: { ...SETTINGS.settings, retention_days: 730 } }));
  const written = await api().updateSettings({ retention_days: 730 });
  assert.equal(calls[0].init.method, 'PUT');
  assert.equal(new URL(calls[0].url).pathname, '/analytics/settings');
  assert.deepEqual(JSON.parse(calls[0].init.body), { settings: { retention_days: 730 } });
  assert.equal(written.settings.retention_days, 730);
});

test('an out-of-range settings update surfaces the backend 422 unchanged', async () => {
  stub(() => jsonResponse({ code: 'VALIDATION_ERROR', message: 'retention_days must be between 7 and 3650' }, 422));
  await assert.rejects(
    () => api().updateSettings({ retention_days: 1 }),
    (error) => error instanceof AnalyticsApiError && error.status === 422 && /between 7 and 3650/.test(error.message),
  );
});
