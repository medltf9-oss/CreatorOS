/**
 * Dev-only visual harness for the analytics pages (P8.19).
 *
 * It renders the **real** pages from `src/pages/analytics/*` against a `AnalyticsClient` stub built
 * from clearly-labelled synthetic fixtures. Nothing here reads the backend and nothing here ships in
 * the application bundle: it exists so the pages and their charts can be screenshotted and reviewed
 * without inventing rows in the production database.
 *
 * `?shot=<key>` selects one scenario; with no query all scenarios are shown for manual inspection.
 */
import { type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import type {
  AccountSummary,
  Aggregation,
  AnalyticsClient,
  Availability,
  CampaignDetail,
  CampaignSummary,
  CapabilityPlatform,
  Comparison,
  ConsentPlan,
  ConsentStart,
  FreshnessAccount,
  Growth,
  MetricDefinition,
  MetricValue,
  Overview,
  Platform,
  PlatformSummary,
  PublicationDetail,
  PublicationSummary,
  Retention,
  Recommendation,
  Report,
  Run,
  Series,
  Snapshot,
  Timeline,
} from './services/apiAnalytics';
import AnalyticsOverview from './pages/analytics/Overview';
import PlatformComparison from './pages/analytics/PlatformComparison';
import AccountPerformance from './pages/analytics/AccountPerformance';
import PublicationDetailPage from './pages/analytics/PublicationDetail';
import ContentComparison from './pages/analytics/ContentComparison';
import CampaignAnalytics from './pages/analytics/CampaignAnalytics';
import VideoPerformance from './pages/analytics/VideoPerformance';
import AudienceGrowth from './pages/analytics/AudienceGrowth';
import HashtagAnalysis from './pages/analytics/HashtagAnalysis';
import Recommendations from './pages/analytics/Recommendations';
import Reports from './pages/analytics/Reports';
import AnalyticsSettingsPage from './pages/analytics/AnalyticsSettings';
import './styles.css';

const START = Date.UTC(2026, 10, 10);
const DAY = 24 * 60 * 60 * 1000;
const day = (index: number) => new Date(START + index * DAY).toISOString();

function def(
  platform: Platform,
  key: string,
  unit: string,
  metric_class: string,
  preferred_for: string | null,
  provider_metric_name: string,
  definition_text: string,
  objectType: 'video' | 'post' | 'reel' | 'story' | 'account' = 'video',
): MetricDefinition {
  return {
    metric_key: key,
    platform,
    object_type: objectType,
    provider_metric_name,
    provider_object: 'video',
    source_doc_url: 'https://example.invalid/docs',
    unit,
    value_kind: 'scalar',
    metric_class,
    period_kinds_json: ['lifetime'],
    definition_text,
    is_estimate: false,
    is_cumulative: false,
    is_deprecated: false,
    deprecated_note: null,
    supersedes_key: null,
    superseded_by_key: null,
    api_version_min: null,
    api_version_max: null,
    required_permissions_json: [],
    account_threshold_json: {},
    availability_default: 'observed',
    preferred_for,
    catalog_version: 'synthetic',
  };
}

function account(platform: Platform, id: string, name: string, type: string): AccountSummary {
  return {
    id,
    platform,
    provider_account_id: `${platform}-${id}`,
    display_name: name,
    username: name.toLowerCase().replace(/\s+/g, ''),
    account_type: type,
    connection_status: 'connected',
    token_expires_at: day(40),
    last_verified_at: day(6),
    observations: 120,
    publications: 6,
    last_collected_at: day(13),
  };
}

const ACCOUNTS: AccountSummary[] = [
  account('facebook', 'acc-fb', 'CreatorOS Page', 'facebook_page'),
  account('instagram', 'acc-ig', 'CreatorOS Studio', 'instagram_business'),
  account('tiktok', 'acc-tt', 'creatoros', 'tiktok_creator'),
];

const METRICS: MetricDefinition[] = [
  def('facebook', 'fb.post.media_views', 'count', 'cumulative', 'views', 'post_video_views', 'Number of times the video was viewed.'),
  def('instagram', 'ig.media.views', 'count', 'cumulative', 'views', 'plays', 'Number of times the media was viewed.'),
  def('tiktok', 'tt.video.views', 'count', 'cumulative', 'views', 'view_count', 'Number of views of the video.'),
  def('facebook', 'fb.page.followers_total', 'count', 'point_in_time', 'followers', 'followers_count', 'Page followers.', 'account'),
  def('facebook', 'fb.page.likes_total', 'count', 'point_in_time', null, 'fan_count', 'Page likes (not followers).', 'account'),
  def('instagram', 'ig.account.followers', 'count', 'point_in_time', 'followers', 'followers_count', 'Instagram followers (not the same as follows/unfollows).', 'account'),
  def('tiktok', 'tt.account.followers', 'count', 'point_in_time', 'followers', 'follower_count', 'TikTok follower count.', 'account'),
  def('instagram', 'ig.account.follower_changes', 'count', 'period', 'follower_changes', 'follower_count', 'Follows and unfollows inside the period.', 'account'),
  def('facebook', 'fb.post.reach', 'people', 'point_in_time', 'reach', 'post_impressions_unique', 'People who saw the post at least once.'),
  def('instagram', 'ig.media.reach', 'people', 'point_in_time', 'reach', 'reach', 'Accounts reached (an estimate).'),
  def('facebook', 'fb.post.reactions', 'count', 'cumulative', null, 'reactions_total', 'Reactions of any type.'),
  def('instagram', 'ig.media.likes', 'count', 'cumulative', null, 'like_count', 'Likes on the media.'),
  def('tiktok', 'tt.video.likes', 'count', 'cumulative', null, 'like_count', 'Likes on the video.'),
  def('tiktok', 'tt.video.comments', 'count', 'cumulative', null, 'comment_count', 'Comments on the video.'),
  def('tiktok', 'tt.video.shares', 'count', 'cumulative', null, 'share_count', 'Shares of the video.'),
  def('facebook', 'fb.post.video_avg_watch_time', 'ms', 'average', 'avg_watch_time', 'post_video_avg_time_watched', 'Average watch time per view.'),
  def('tiktok', 'tt.video.watch_time', 'seconds', 'cumulative', 'watch_time', 'watch_time', 'Not exposed by any approved TikTok API.'),
];

function seriesFor(platform: Platform, withGaps = true): Series {
  const key = METRICS.find((metric) => metric.platform === platform && metric.preferred_for === 'views')!.metric_key;
  const points = Array.from({ length: 14 }, (_, index) => {
    const base = 800 + platform.length * 120 + index * 90 + Math.round(Math.sin(index) * 90);
    if (withGaps && (index === 3 || index === 4)) return { bucket_start: day(index), bucket_end: day(index + 1), value: null, availability: 'not_collected' as const, observations: 0 };
    if (withGaps && index === 9 && platform === 'instagram') return { bucket_start: day(index), bucket_end: day(index + 1), value: null, availability: 'not_authorized' as const, observations: 0 };
    return { bucket_start: day(index), bucket_end: day(index + 1), value: base, availability: 'observed' as const, observations: 1 };
  });
  return { account_id: `acc-${platform.slice(0, 2)}`, metric_key: key, bucket: 'day', points, availability_summary: { observed: 11, not_collected: 2 }, note: '' };
}

const OVERVIEW: Overview = {
  accounts_total: 3,
  accounts_connected: 3,
  publications_measured: 18,
  observations: 412,
  aggregations: 96,
  recommendations_by_status: { draft: 2, accepted: 1, rejected: 1 },
  latest_collection_at: day(13),
  generated_at: day(13),
};

const PLATFORMS: PlatformSummary[] = [
  { platform: 'facebook', connected_accounts: 1, publications: 6, observations: 168, aggregations: 40, observed_metrics: 9, last_collected_at: day(13) },
  { platform: 'instagram', connected_accounts: 1, publications: 7, observations: 154, aggregations: 34, observed_metrics: 11, last_collected_at: day(12) },
  { platform: 'tiktok', connected_accounts: 1, publications: 5, observations: 90, aggregations: 22, observed_metrics: 6, last_collected_at: day(9) },
];

const FRESHNESS: FreshnessAccount[] = [
  { account_id: 'acc-fb', platform: 'facebook', display_name: 'CreatorOS Page', last_collected_at: day(13), age_hours: 5, stale: false, next_due_at: day(14), state: 'current', blocked_reason: null },
  { account_id: 'acc-ig', platform: 'instagram', display_name: 'CreatorOS Studio', last_collected_at: day(12), age_hours: 29, stale: false, next_due_at: day(13), state: 'current', blocked_reason: null },
  { account_id: 'acc-tt', platform: 'tiktok', display_name: 'creatoros', last_collected_at: day(9), age_hours: 101, stale: true, next_due_at: day(10), state: 'stalled', blocked_reason: 'last error: rate_limit (x-buc 91%)' },
];

const CAPABILITIES: CapabilityPlatform[] = [
  { platform: 'facebook', connected_accounts: 1, metrics_total: 72, planned_account_metrics: { ok: 25, not_authorized: 2 }, configured_version: 'v23.0', latest_documented: 'v27.0', retired: false, note: 'planned from the scopes actually granted on the connected account(s)' },
  { platform: 'instagram', connected_accounts: 1, metrics_total: 40, planned_account_metrics: { ok: 16, not_authorized: 2 }, configured_version: 'v23.0', latest_documented: 'v27.0', retired: false, note: 'planned from the scopes actually granted on the connected account(s)' },
  { platform: 'tiktok', connected_accounts: 1, metrics_total: 17, planned_account_metrics: { ok: 2 }, configured_version: 'v2', latest_documented: 'v2', retired: false, note: 'planned from the scopes actually granted on the connected account(s)' },
];

const PUBLICATIONS: PublicationSummary[] = [
  { publication_id: 'pub-1', account_id: 'acc-fb', platform: 'facebook', remote_post_id: 'fb-post-1', provider_published_at: day(12), state: 'published', content_project_id: 'p1', campaign_id: 'c1', variant_id: 'v1', title: 'Launch week teaser', hashtags: ['launch', 'bts'], has_metrics: true, aggregation_ids: ['a1'] },
  { publication_id: 'pub-2', account_id: 'acc-ig', platform: 'instagram', remote_post_id: 'ig-media-2', provider_published_at: day(11), state: 'published', content_project_id: 'p2', campaign_id: null, variant_id: 'v2', title: 'Behind the scenes reel', hashtags: ['reels'], has_metrics: true, aggregation_ids: ['a2'] },
  { publication_id: 'pub-3', account_id: 'acc-tt', platform: 'tiktok', remote_post_id: 'tt-video-3', provider_published_at: day(9), state: 'published', content_project_id: 'p3', campaign_id: null, variant_id: 'v3', title: 'Quick tip', hashtags: ['tips'], has_metrics: false, aggregation_ids: [] },
  { publication_id: 'pub-4', account_id: 'acc-fb', platform: 'facebook', remote_post_id: 'fb-post-4', provider_published_at: day(8), state: 'published', content_project_id: 'p4', campaign_id: 'c1', variant_id: 'v4', title: 'Studio walkthrough', has_metrics: true, hashtags: ['launch', 'bts'], aggregation_ids: ['a4'] },
  { publication_id: 'pub-5', account_id: 'acc-tt', platform: 'tiktok', remote_post_id: 'tt-video-5', provider_published_at: day(7), state: 'published', content_project_id: 'p5', campaign_id: null, variant_id: 'v5', title: 'Three settings in 20 seconds', has_metrics: true, hashtags: ['tips', 'launch'], aggregation_ids: ['a5'] },
  { publication_id: 'pub-6', account_id: 'acc-ig', platform: 'instagram', remote_post_id: 'ig-media-6', provider_published_at: day(6), state: 'published', content_project_id: 'p6', campaign_id: null, variant_id: 'v6', title: 'Crew Q&A', has_metrics: true, hashtags: ['bts'], aggregation_ids: ['a6'] },
  { publication_id: 'pub-7', account_id: 'acc-tt', platform: 'tiktok', remote_post_id: 'tt-video-7', provider_published_at: day(5), state: 'published', content_project_id: 'p7', campaign_id: null, variant_id: 'v7', title: 'Client result in 20 seconds', has_metrics: true, hashtags: ['tips'], aggregation_ids: ['a7'] },
  { publication_id: 'pub-8', account_id: 'acc-fb', platform: 'facebook', remote_post_id: 'fb-post-8', provider_published_at: day(4), state: 'published', content_project_id: 'p8', campaign_id: null, variant_id: 'v8', title: 'Hiring the first editor', has_metrics: false, hashtags: ['launch'], aggregation_ids: [] },
];

function metricValue(
  key: string,
  unit: string,
  value: number | null,
  availability: MetricValue['availability'],
  window = 'lifetime',
  snapshotIds: string[] = ['s1'],
  collectedAt: string = day(13),
): MetricValue {
  return {
    metric_key: key,
    provider_metric_name: key.split('.').pop() ?? key,
    unit,
    value_kind: 'scalar',
    metric_class: 'cumulative',
    period_kind: window,
    availability,
    availability_reason: availability === 'observed' ? null : 'synthetic fixture',
    value,
    value_json: null,
    collected_at: collectedAt,
    period_start: null,
    period_end: null,
    sample_size: 1,
    method: 'direct',
    confidence: null,
    snapshot_ids: snapshotIds,
    aggregation_ids: [],
  };
}

const RUNS: Run[] = [
  { id: 'run-1', account_id: 'acc-fb', platform: 'facebook', target_kind: 'metrics', target_object_id: 'obj-1', window_kind: 'h24', started_at: day(13), finished_at: day(13), outcome: 'succeeded', error_class: null, error_code: null, http_calls: 2, metrics_attempted: 25, metrics_observed: 24, metrics_unavailable: 1, metrics_written: 24, api_version: 'v23.0' },
  { id: 'run-2', account_id: 'acc-fb', platform: 'facebook', target_kind: 'account_sync', target_object_id: null, window_kind: null, started_at: day(12), finished_at: day(12), outcome: 'partial', error_class: 'rate_limit', error_code: null, http_calls: 1, metrics_attempted: 25, metrics_observed: 20, metrics_unavailable: 5, metrics_written: 20, api_version: 'v23.0' },
];

const COMPARE: Comparison = {
  metric_key: 'tt.video.views',
  attribute: 'format',
  window: 'h24',
  groups: [
    { label: 'video', n: 9, availability: 'observed', median: 1420, evidence: [] },
    { label: 'reel', n: 6, availability: 'observed', median: 980, evidence: [] },
  ],
  verdicts: null,
  comparability: null,
  limitations: ['groups differ in more than the format; this is a median of a small sample'],
  sufficient: true,
  note: '',
};

/**
 * Hashtag comparison fixture: two hashtag groups with enough observations, one thin group, and one
 * pair whose intervals overlap (the backend reports that as `inconclusive` — never as a ranking).
 */
const HASHTAG_COMPARISON: Comparison = {
  metric_key: 'tt.video.views',
  attribute: 'hashtag',
  window: 'h24',
  groups: [
    { label: 'launch', n: 7, availability: 'observed', median: 4200, evidence: [] },
    { label: 'bts', n: 5, availability: 'observed', median: 3100, evidence: [] },
    { label: 'tips', n: 2, availability: 'observed', median: 1900, evidence: [] },
  ],
  verdicts: [
    { a: 'launch', b: 'bts', verdict: 'inconclusive', reason: 'the bootstrap intervals overlap', sample_a: 7, sample_b: 5 },
    { a: 'launch', b: 'tips', verdict: 'a_higher', reason: 'disjoint bootstrap intervals', sample_a: 7, sample_b: 2 },
  ],
  comparability: { refusals: [], checked_pairs: 3 },
  limitations: [
    'correlation is not causation: groups differ in more than the hashtag',
    'window: +24 h with a ±180 min tolerance',
    'below the 8-publication gate: launch, bts, tips',
  ],
  sufficient: false,
  note: '',
};

type Mode = 'populated' | 'empty' | 'loading' | 'permission' | 'unavailable' | 'nohistory' | 'error';

// ---- P8.21 fixtures (publication detail / content comparison / campaigns) --------------------

function aggregation(
  scope: Aggregation['scope'],
  keyField: { publication_key?: string; campaign_key?: string },
  metricKey: string,
  value: number | null,
  availability: Aggregation['availability'],
  window = 'h24',
): Aggregation {
  return {
    id: `agg-${metricKey}-${keyField.publication_key ?? keyField.campaign_key ?? 'x'}-${window}`,
    account_id: 'acc-fb',
    platform: 'facebook',
    scope,
    publication_key: keyField.publication_key ?? '',
    campaign_key: keyField.campaign_key ?? '',
    metric_key: metricKey,
    window_kind: window,
    window_hours: 24,
    window_tolerance_minutes: 30,
    value,
    value_json: null,
    unit: 'count',
    sample_size: 1,
    availability,
    method: 'sum',
    input_snapshot_ids: ['s1'],
    confidence: {},
    computed_at: day(13),
    aggregation_version: 1,
  };
}

const PUBLICATION_DETAILS: Record<string, PublicationDetail> = {
  'pub-1': {
    publication: PUBLICATIONS[0],
    metrics: [
      metricValue('fb.post.media_views', 'count', 12400, 'observed', 'h24'),
      metricValue('fb.post.media_views', 'count', 30120, 'observed', 'lifetime'),
      metricValue('fb.post.reach', 'people', 9800, 'observed', 'lifetime'),
      metricValue('fb.post.impressions', 'count', 15300, 'observed', 'lifetime'),
      metricValue('fb.post.reactions', 'count', 420, 'observed', 'lifetime'),
      metricValue('fb.post.comments', 'count', 38, 'observed', 'lifetime'),
      metricValue('fb.post.shares', 'count', 64, 'observed', 'lifetime'),
      metricValue('fb.post.video_avg_watch_time', 'ms', 7200, 'observed', 'lifetime'),
      metricValue('fb.post.video_complete_views', 'count', null, 'below_threshold', 'lifetime'),
    ],
  },
  'pub-2': {
    publication: PUBLICATIONS[1],
    metrics: [
      metricValue('ig.media.views', 'count', 8600, 'observed', 'lifetime'),
      metricValue('ig.media.reach', 'people', null, 'not_authorized', 'lifetime'),
      metricValue('ig.media.likes', 'count', 512, 'observed', 'lifetime'),
      metricValue('ig.media.comments', 'count', 24, 'observed', 'lifetime'),
      metricValue('ig.media.saves', 'count', null, 'not_supported', 'lifetime'),
    ],
  },
  'pub-3': {
    publication: PUBLICATIONS[2],
    metrics: [
      metricValue('tt.video.views', 'count', 15200, 'observed', 'lifetime'),
      metricValue('tt.video.likes', 'count', 940, 'observed', 'lifetime'),
      metricValue('tt.video.comments', 'count', 61, 'observed', 'lifetime'),
      metricValue('tt.video.watch_time', 'seconds', null, 'not_supported', 'lifetime'),
    ],
  },
};

const RETENTION: Record<string, Retention> = {
  'pub-1': {
    publication_id: 'pub-1',
    available: true,
    reason: null,
    curves: [
      {
        metric_key: 'fb.post.video_retention_curve',
        unit: 'ratio',
        collected_at: day(13),
        availability: 'observed',
        value_json: { points: Array.from({ length: 40 }, (_, index) => Math.max(8, Math.round(100 - index * 2.1 - (index % 6)))), bucket_count: 40, unit: 'ratio' },
      },
    ],
  },
  'pub-2': {
    publication_id: 'pub-2',
    available: false,
    reason: 'no retention curve is stored for this publication; the provider may not expose one',
    curves: [],
  },
  'pub-3': {
    publication_id: 'pub-3',
    available: false,
    reason: 'TikTok does not expose a retention curve in any approved API',
    curves: [],
  },
};

function timelineFor(publication: PublicationSummary, keys: string[]): Timeline {
  const points = [] as Timeline['points'];
  for (let step = 0; step < 6; step += 1) {
    keys.forEach((key, index) => {
      const base = (index + 1) * 500;
      const gap = key.includes('reach') && step === 2;
      points.push({
        collected_at: day(6 + step),
        metric_key: key,
        window_kind: null,
        availability: gap ? 'not_collected' : 'observed',
        value: gap ? null : base + step * 180 + index * 40,
        value_json: null,
        unit: key.includes('time') ? 'ms' : 'count',
        run_id: `run-${step}`,
      });
    });
  }
  return { publication_id: publication.publication_id, points };
}

const TIMELINES: Record<string, Timeline> = {
  'pub-1': timelineFor(PUBLICATIONS[0], ['fb.post.media_views', 'fb.post.reach', 'fb.post.reactions']),
  'pub-2': timelineFor(PUBLICATIONS[1], ['ig.media.views', 'ig.media.likes']),
  'pub-3': timelineFor(PUBLICATIONS[2], ['tt.video.views', 'tt.video.likes']),
};

const CAMPAIGNS: CampaignSummary[] = [
  { campaign_id: 'c1', name: 'Launch week', goal: 'Awareness', starts_on: day(6), ends_on: day(14), projects: 2, publications: 2, observations: 120 },
  { campaign_id: 'c2', name: 'Evergreen tips', goal: 'Engagement', starts_on: day(0), ends_on: null, projects: 3, publications: 1, observations: 18 },
];

const CAMPAIGN_DETAILS: Record<string, CampaignDetail> = {
  c1: {
    campaign: CAMPAIGNS[0],
    publications: [PUBLICATIONS[0], PUBLICATIONS[1]],
    metrics: [
      metricValue('fb.post.media_views', 'count', 30120, 'observed', 'lifetime'),
      metricValue('fb.post.reactions', 'count', 420, 'observed', 'lifetime'),
      metricValue('ig.media.views', 'count', 8600, 'observed', 'lifetime'),
      metricValue('ig.media.reach', 'people', null, 'not_authorized', 'lifetime'),
    ],
  },
  c2: {
    campaign: CAMPAIGNS[1],
    publications: [PUBLICATIONS[2]],
    metrics: [metricValue('tt.video.views', 'count', 15200, 'observed', 'lifetime')],
  },
};

const AGGREGATIONS: Aggregation[] = [
  aggregation('publication', { publication_key: 'pub-1' }, 'fb.post.media_views', 12400, 'observed'),
  aggregation('publication', { publication_key: 'pub-2' }, 'ig.media.views', 8600, 'observed'),
  aggregation('publication', { publication_key: 'pub-3' }, 'tt.video.views', 15200, 'observed'),
  aggregation('publication', { publication_key: 'pub-4' }, 'fb.post.media_views', 5480, 'observed'),
  aggregation('publication', { publication_key: 'pub-5' }, 'tt.video.views', 23900, 'observed'),
  aggregation('publication', { publication_key: 'pub-6' }, 'ig.media.views', 3270, 'observed'),
  aggregation('publication', { publication_key: 'pub-7' }, 'tt.video.views', 0, 'observed'),
  // pub-8 has no aggregation at this window: the leaderboard must show it as unmeasured, not as 0.
  aggregation('publication', { publication_key: 'pub-1' }, 'fb.post.reactions', 420, 'observed'),
  aggregation('publication', { publication_key: 'pub-4' }, 'fb.post.reactions', 96, 'observed'),
  aggregation('publication', { publication_key: 'pub-1' }, 'fb.post.reach', 9800, 'observed'),
  aggregation('publication', { publication_key: 'pub-4' }, 'fb.post.reach', null, 'not_authorized'),
  aggregation('publication', { publication_key: 'pub-1' }, 'fb.post.video_avg_watch_time', 7200, 'observed'),
  aggregation('publication', { publication_key: 'pub-5' }, 'tt.video.likes', 2140, 'observed'),
  aggregation('publication', { publication_key: 'pub-7' }, 'tt.video.likes', 0, 'observed'),
  aggregation('publication', { publication_key: 'pub-3' }, 'tt.video.likes', null, 'not_collected'),
  aggregation('publication', { publication_key: 'pub-5' }, 'tt.video.comments', 87, 'observed'),
  aggregation('publication', { publication_key: 'pub-5' }, 'tt.video.shares', 143, 'observed'),
  aggregation('publication', { publication_key: 'pub-7' }, 'tt.video.comments', 12, 'observed'),
  aggregation('publication', { publication_key: 'pub-7' }, 'tt.video.shares', 5, 'observed'),
  aggregation('publication', { publication_key: 'pub-2' }, 'ig.media.likes', 512, 'observed'),
  aggregation('publication', { publication_key: 'pub-2' }, 'ig.media.reach', 6100, 'observed'),
  aggregation('campaign', { campaign_key: 'c1' }, 'fb.post.media_views', 30120, 'observed'),
  aggregation('campaign', { campaign_key: 'c1' }, 'ig.media.views', 8600, 'observed'),
  aggregation('campaign', { campaign_key: 'c2' }, 'tt.video.views', 15200, 'observed'),
];

const SNAPSHOTS: Snapshot[] = [
  { id: 'snap-2', account_id: 'acc-fb', platform: 'facebook', object_type: 'post', provider_object_id: 'fb-post-4', publication_id: 'pub-4', metric_key: 'fb.post.media_views', value_kind: 'scalar', period_kind: 'd28', period_start: day(0), period_end: day(13), availability: 'observed', availability_reason: null, value: 5480, value_json: null, unit: 'count', collected_at: day(13), data_as_of: day(13), completeness: 'complete', sample_size: 1, breakdown_key: '', api_version: 'v23.0', run_id: 'run-1' },
  { id: 'snap-1', account_id: 'acc-fb', platform: 'facebook', object_type: 'post', provider_object_id: 'fb-post-1', publication_id: 'pub-1', metric_key: 'fb.post.media_views', value_kind: 'scalar', period_kind: 'lifetime', period_start: null, period_end: null, availability: 'observed', availability_reason: null, value: 30120, value_json: null, unit: 'count', collected_at: day(13), data_as_of: day(13), completeness: 'complete', sample_size: 1, breakdown_key: '', api_version: 'v23.0', run_id: 'run-1' },
];


// ---------------------------------------------------------------------------- P8.23 fixtures
//
// The report payloads below are byte-for-byte what the stub client serves, and each digest was computed
// over exactly those bytes. `rep-4` carries no digest and `rep-5` carries a wrong one, so the integrity
// gate can be screenshotted refusing a download instead of only accepting one.

const REPORT_BODY_1 = String.raw`{
  "report_id": "rep-1",
  "kind": "overview",
  "generated_at": "2026-11-22T09:15:00.000000",
  "period": {
    "start": "2026-08-25T00:00:00",
    "end": "2026-11-22T00:00:00"
  },
  "sections": [
    {
      "heading": "Accounts measured",
      "type": "table",
      "body": [
        {
          "account": "Northwind Studio (facebook)",
          "publications": 6
        },
        {
          "account": "northwind.studio (instagram)",
          "publications": 4
        }
      ]
    },
    {
      "heading": "Observations by metric class",
      "type": "table",
      "body": [
        {
          "metric_key": "fb.post.media_views",
          "observed": 12,
          "not_authorized": 2
        },
        {
          "metric_key": "ig.media.views",
          "observed": 9,
          "below_threshold": 1
        }
      ]
    },
    {
      "heading": "Limits of this report",
      "type": "prose",
      "body": "Reach and impressions were not available for two Facebook publications; they are recorded as not_authorized and are absent from the totals, not counted as zero."
    }
  ],
  "summary": "3 accounts, 10 publications, 129 stored observations. Two metrics unavailable by permission; no figure in this report treats them as zero."
}`;

const REPORT_BODY_2 = String.raw`{
  "report_id": "rep-2",
  "kind": "recommendations",
  "generated_at": "2026-11-22T09:40:00.000000",
  "period": {
    "start": "2026-10-01T00:00:00",
    "end": "2026-11-22T00:00:00"
  },
  "sections": [
    {
      "heading": "Draft recommendations",
      "type": "table",
      "body": [
        {
          "id": "rec-1",
          "kind": "topic",
          "sample": 14,
          "confidence": 0.91
        },
        {
          "id": "rec-2",
          "kind": "timing",
          "sample": 22,
          "confidence": 0.84
        }
      ]
    },
    {
      "heading": "Notes",
      "type": "prose",
      "body": "Drafts only. Accepting one creates an approval request for the experiment; nothing is published."
    }
  ],
  "summary": "Two draft recommendations, both above the sample gate."
}`;

const REPORT_BODY_3 = String.raw`{
  "report_id": "rep-3",
  "kind": "campaign",
  "generated_at": "2026-11-21T18:05:00.000000",
  "period": {
    "start": "2026-11-01T00:00:00",
    "end": "2026-11-21T00:00:00"
  },
  "sections": [
    {
      "heading": "Autumn launch",
      "type": "table",
      "body": [
        {
          "platform": "facebook",
          "media_views": 30120
        },
        {
          "platform": "instagram",
          "media_views": 8600
        }
      ]
    }
  ],
  "summary": "One campaign, two platforms, no TikTok publication in the window."
}`;

const REPORT_BODY_CSV = String.raw`section,field,value

report,report_id,"rep-6"

report,format,csv

report,platforms,"[""facebook"", ""tiktok""]"

report,accounts,"[""acc-fb"", ""acc-tt""]"

report,metric_keys,"[""fb.post.media_views"", ""tt.video.views""]"

report,aggregation_rows,4

Measured aggregations,metric_key,tt.video.views

Measured aggregations,value,23900.0

Measured aggregations,unit,count

Measured aggregations,availability,observed

Measured aggregations,metric_key,fb.post.media_views

Measured aggregations,value,12400.0

Measured aggregations,availability,observed

Data availability,observed,4

Warnings,warnings,[]

`;

const REPORTS_CSV: Report = {
  id: 'rep-6',
  kind: 'content_comparison',
  period_start: day(0),
  period_end: day(13),
  filters: { account_id: null, title: 'Cross-platform comparison (CSV)', format: 'csv' },
  sections: [{ heading: 'Measured aggregations', type: 'aggregations', body: [{ metric_key: 'tt.video.views', value: 23900 }] }],
  summary: 'CSV export of the comparison window: one flat table with a section column.',
  sha256: 'a05c47a078eaabc10d05f61697f190761385f34388ff5d1cb295b9b80869a70b',
  bytes: 605,
  status: 'final',
  generated_by: 'analytics.reports',
  format: 'csv',
  created_at: day(8),
};

const REPORT_BODIES: Record<string, string> = {
  'rep-1': REPORT_BODY_1,
  'rep-2': REPORT_BODY_2,
  'rep-3': REPORT_BODY_3,
  'rep-6': REPORT_BODY_CSV,
};

const REPORTS: Report[] = [
  {
    id: 'rep-1',
    kind: 'overview',
    period_start: day(0),
    period_end: day(13),
    filters: { account_id: null, title: 'Quarterly performance review' },
    sections: [
      { heading: 'Accounts measured', type: 'table', body: [{ account: 'Northwind Studio (facebook)', publications: 6 }, { account: 'northwind.studio (instagram)', publications: 4 }] },
      { heading: 'Observations by metric class', type: 'table', body: [{ metric_key: 'fb.post.media_views', observed: 12, not_authorized: 2 }, { metric_key: 'ig.media.views', observed: 9, below_threshold: 1 }] },
      { heading: 'Limits of this report', type: 'prose', body: 'Reach and impressions were not available for two Facebook publications; they are recorded as not_authorized and are absent from the totals, not counted as zero.' },
    ],
    summary: '3 accounts, 10 publications, 129 stored observations. Two metrics unavailable by permission; no figure in this report treats them as zero.',
    sha256: '9d036696a6fdf911f5b3b3caaa063c8f0f7d454b8da204b2e0f4ec68aa76f3e8',
    bytes: 1265,
    status: 'final',
    generated_by: 'analytics.reports',
    format: 'json',
    created_at: day(13),
  },
  {
    id: 'rep-2',
    kind: 'recommendations',
    period_start: day(4),
    period_end: day(13),
    filters: { account_id: 'acc-fb', title: 'Draft recommendations — Facebook page' },
    sections: [
      { heading: 'Draft recommendations', type: 'table', body: [{ id: 'rec-1', kind: 'topic', sample: 14, confidence: 0.91 }, { id: 'rec-2', kind: 'timing', sample: 22, confidence: 0.84 }] },
      { heading: 'Notes', type: 'prose', body: 'Drafts only. Accepting one creates an approval request for the experiment; nothing is published.' },
    ],
    summary: 'Two draft recommendations, both above the sample gate.',
    sha256: '6f403bc92154053bf5da51804e4526864245d2319820ae7be73273cfdfe94be7',
    bytes: 811,
    status: 'final',
    generated_by: 'analytics.reports',
    format: 'json',
    created_at: day(12),
  },
  {
    id: 'rep-3',
    kind: 'campaign',
    period_start: day(2),
    period_end: day(11),
    filters: { account_id: 'acc-tt', title: 'Autumn launch — campaign wrap' },
    sections: [
      { heading: 'Autumn launch', type: 'table', body: [{ platform: 'facebook', media_views: 30120 }, { platform: 'instagram', media_views: 8600 }] },
    ],
    summary: 'One campaign, two platforms, no TikTok publication in the window.',
    sha256: '0478868ab4fe90fef3b01c4d568db4187b3eb1f48a3e4bc36397086e70855fcb',
    bytes: 551,
    status: 'draft',
    generated_by: 'analytics.reports',
    format: 'json',
    created_at: day(11),
  },
  {
    id: 'rep-4',
    kind: 'platform_comparison',
    period_start: null,
    period_end: null,
    filters: { account_id: null, title: 'Comparison with no stored digest' },
    sections: [{ heading: 'Platforms', type: 'table', body: [{ platform: 'facebook', publications: 6 }, { platform: 'tiktok', publications: 3 }] }],
    summary: 'Stored without a digest: the download gate must refuse it rather than offer an unverifiable file.',
    sha256: null,
    bytes: 412,
    status: 'draft',
    generated_by: 'analytics.reports',
    format: 'json',
    created_at: day(10),
  },
  REPORTS_CSV,
  {
    id: 'rep-5',
    kind: 'account',
    period_start: day(1),
    period_end: day(9),
    filters: { account_id: 'acc-ig', title: 'Record whose stored digest does not match its file' },
    sections: [{ heading: 'Instagram account', type: 'table', body: [{ metric_key: 'ig.media.views', observed: 9 }] }],
    summary: 'Synthetic corruption case: the stored digest does not describe the bytes on disk.',
    sha256: '0000000000000000000000000000000000000000000000000000000000000000',
    bytes: 1265,
    status: 'final',
    generated_by: 'analytics.reports',
    format: 'json',
    created_at: day(9),
  },
];

const RECOMMENDATIONS: Recommendation[] = [
  {
    id: 'rec-1', account_id: 'acc-fb', platform: 'facebook', campaign_id: 'c1', content_project_id: 'proj-1',
    kind: 'topic', status: 'draft',
    observation: 'Publications about product education reached a median of 12,400 views, against 5,480 for the rest of the account, across 14 measured publications.',
    sample_size: { publications: 14, baseline_publications: 9, window: 'd28' },
    period_start: day(0), period_end: day(13), confidence: 0.91,
    confidence_method: 'bootstrap 95% interval on the median difference, 2000 resamples',
    limitations: [
      'observational comparison: the topic was not assigned at random',
      'the baseline includes publications from a different campaign',
    ],
    proposed_experiment: { change: 'publish two more product-education posts in the next cycle', success_metric: 'fb.post.media_views', duration: '14 days' },
    expected_benefit: { kind: 'estimate', direction: 'increase', metric_key: 'fb.post.media_views' },
    generated_by: 'analytics.recommendations', approval_request_id: null, decided_at: null, decision_note: null,
    created_at: day(13),
  },
  {
    id: 'rec-2', account_id: 'acc-tt', platform: 'tiktok', campaign_id: null, content_project_id: null,
    kind: 'timing', status: 'reviewed',
    observation: 'TikTok videos published between 18:00 and 21:00 UTC recorded a median of 23,900 views against 15,200 in the morning bucket, across 22 publications.',
    sample_size: { publications: 22, baseline_publications: 15, window: 'h24' },
    period_start: day(0), period_end: day(13), confidence: 0.84,
    confidence_method: 'bootstrap 95% interval on the median difference, 2000 resamples',
    limitations: ['time bucket correlates with weekday composition', 'view counts on TikTok are cumulative and keep growing'],
    proposed_experiment: { change: 'move one weekly slot to 18:30 UTC', success_metric: 'tt.video.views', duration: '21 days' },
    expected_benefit: { kind: 'estimate', direction: 'increase', metric_key: 'tt.video.views' },
    generated_by: 'analytics.recommendations', approval_request_id: null, decided_at: null, decision_note: null,
    created_at: day(12),
  },
  {
    id: 'rec-3', account_id: 'acc-ig', platform: 'instagram', campaign_id: 'c1', content_project_id: 'proj-2',
    kind: 'format', status: 'accepted',
    observation: 'Reels carried a median of 8,600 views against 3,270 for carousels across 11 publications.',
    sample_size: { publications: 11, baseline_publications: 7, window: 'd28' },
    period_start: day(0), period_end: day(11), confidence: 0.78,
    confidence_method: 'bootstrap 95% interval on the median difference, 2000 resamples',
    limitations: ['carousel count is small', 'reels were published mainly during the campaign'],
    proposed_experiment: { change: 'convert one carousel slot to a reel', success_metric: 'ig.media.views', duration: '14 days' },
    expected_benefit: { kind: 'estimate', direction: 'increase', metric_key: 'ig.media.views' },
    generated_by: 'analytics.recommendations', approval_request_id: 'apr-91f2', decided_at: day(12),
    decision_note: 'Accepted: try it on the next carousel slot, review after two weeks.', created_at: day(11),
  },
  {
    id: 'rec-4', account_id: 'acc-fb', platform: 'facebook', campaign_id: null, content_project_id: null,
    kind: 'hashtag', status: 'rejected',
    observation: 'Facebook posts carrying #launch recorded a median of 5,480 views against 4,900 elsewhere, across 6 publications.',
    sample_size: { publications: 6, baseline_publications: 42, window: 'd28' },
    period_start: day(0), period_end: day(9), confidence: 0.41,
    confidence_method: 'bootstrap 95% interval on the median difference, 2000 resamples',
    limitations: ['sample is small and the interval includes zero'],
    proposed_experiment: { change: 'add #launch to the next two posts', success_metric: 'fb.post.media_views', duration: '14 days' },
    expected_benefit: { kind: 'estimate', direction: 'increase', metric_key: 'fb.post.media_views' },
    generated_by: 'analytics.recommendations', approval_request_id: null, decided_at: day(11),
    decision_note: 'Rejected: the interval includes zero and the sample is too small this quarter.', created_at: day(10),
  },
  {
    id: 'rec-5', account_id: 'acc-tt', platform: 'tiktok', campaign_id: null, content_project_id: null,
    kind: 'growth', status: 'superseded',
    observation: 'Follower growth on TikTok averaged 31 per day over the window, with a collection gap on two days.',
    sample_size: { accounts: 1, points: 8 },
    period_start: day(3), period_end: day(11), confidence: null,
    confidence_method: 'descriptive: no interval is computed for a single account series',
    limitations: ['two days were not collected, so the daily rate is computed over observed days only'],
    proposed_experiment: { change: 'keep the current cadence and re-measure in 14 days', success_metric: 'tt.account.followers', duration: '14 days' },
    expected_benefit: { kind: 'unmeasured', direction: 'maintain' },
    generated_by: 'analytics.recommendations', approval_request_id: null, decided_at: null, decision_note: null,
    created_at: day(9),
  },
  {
    id: 'rec-6', account_id: 'acc-fb', platform: 'facebook', campaign_id: null, content_project_id: null,
    kind: 'retention', status: 'draft',
    observation: 'Average watch time on the longest Facebook video was 7,200 seconds per view, measured from the provider retention curve.',
    sample_size: { publications: 1 },
    period_start: null, period_end: null, confidence: null,
    confidence_method: 'descriptive: a single publication has no comparison group',
    limitations: [],
    proposed_experiment: {},
    expected_benefit: { kind: 'estimate', direction: 'increase' },
    generated_by: 'analytics.recommendations', approval_request_id: null, decided_at: null, decision_note: null,
    created_at: day(8),
  },
];

const RECOMMENDATION_EVIDENCE: Record<string, MetricValue[]> = {
  'rec-1': [
    metricValue('fb.post.media_views', 'count', 12400, 'observed', 'd28', ['snap-1'], day(13)),
    metricValue('fb.post.media_views', 'count', 5480, 'observed', 'd28', [], day(13)),
    metricValue('fb.post.reach', 'people', null, 'not_authorized', 'd28', [], day(13)),
    metricValue('fb.post.reactions', 'count', 420, 'observed', 'd28', ['snap-gone'], day(13)),
  ],
  'rec-2': [
    metricValue('tt.video.views', 'count', 23900, 'observed', 'h24', ['snap-1'], day(13)),
    metricValue('tt.video.views', 'count', 15200, 'observed', 'h24', [], day(13)),
    metricValue('tt.video.watch_time', 'seconds', null, 'not_supported', 'h24', [], day(13)),
  ],
  'rec-3': [metricValue('ig.media.views', 'count', 8600, 'observed', 'd28', ['snap-1'], day(13))],
  'rec-4': [metricValue('fb.post.media_views', 'count', 5480, 'observed', 'd28', ['snap-1'], day(-77))],
  'rec-5': [metricValue('tt.account.followers', 'count', 940, 'observed', 'lifetime', ['snap-1'])],
  'rec-6': [metricValue('fb.post.video_avg_watch_time', 'seconds', 7200, 'observed', 'lifetime', ['snap-1'])],
};


const RETENTION_PREVIEW = {
  policy: {
    retention_days: 365,
    report_retention_days: 730,
    batch_limit: 2000,
    cutoff: '2025-11-24T00:00:00Z',
    report_cutoff: '2024-11-24T00:00:00Z',
  },
  categories: [
    { name: 'aggregations', count: 0, reason: 'every observation this rollup cites is expiring or already gone', bytes: 0 },
    { name: 'observations', count: 1841, reason: 'collected before 2025-11-24 by a finished run', bytes: 0 },
    { name: 'collection_runs', count: 42, reason: 'finished before the cutoff and no observation still refers to it', bytes: 0 },
    { name: 'reports', count: 1, reason: 'generated before 2024-11-24 or explicitly archived', bytes: 551 },
    { name: 'superseded_recommendations', count: 1, reason: 'replaced by a newer recommendation before the cutoff; decisions are not deleted', bytes: 0 },
    { name: 'orphan_files', count: 0, reason: 'a report file with no record pointing at it, abandoned for over a day', bytes: 0 },
  ],
  total_rows: 1885,
  total_bytes: 551,
  protected: {
    publications: 8,
    campaigns: 2,
    accounts: 3,
    media_assets: 12,
    metric_definitions: 129,
    decided_recommendations: 2,
    runs_protected_by_observations: 3,
  },
  applied: false,
  reason: 'preview only: nothing was deleted; use POST /analytics/retention/execute to apply',
};

const SETTINGS_RESPONSE = {
  settings: {
    collection_enabled: true,
    account_sync_interval_s: 86400,
    object_window_months: 12,
    retention_days: 365,
    recommendation_min_publications: 8,
  },
  bounds: {
    account_sync_interval_s: { min: 300, max: 172800 },
    object_window_months: { min: 1, max: 36 },
    retention_days: { min: 7, max: 3650 },
    recommendation_min_publications: { min: 3, max: 50 },
  },
  note: 'Stored preferences. They do not change a running collect; the sweeper reads them on its next tick.',
};

function makeClient(mode: Mode): AnalyticsClient {
  const never = () => new Promise<never>(() => {});
  const populated = {
    overview: async () => OVERVIEW,
    freshness: async () => ({ accounts: FRESHNESS, generated_at: day(13) }),
    capabilities: async () => ({ platforms: CAPABILITIES, generated_at: day(13) }),
    versions: async () => ({ platforms: [], generated_at: day(13) }),
    windows: async () => ({ windows: [], note: '' }),
    metrics: async (params?: { platform?: Platform }) => ({
      items: METRICS.filter((metric) => !params?.platform || metric.platform === params.platform),
      total: METRICS.length,
      limit: 200,
      offset: 0,
    }),
    availability: async (accountId: string): Promise<Availability> => {
      const rows = METRICS.filter((metric) => metric.platform === ACCOUNTS.find((a) => a.id === accountId)?.platform).map((metric) => ({
        metric_key: metric.metric_key,
        allowed: true,
        reason: 'ok',
        availability_if_skipped: null,
        detail: 'granted',
      }));
      return { account_id: accountId, platform: 'facebook', object_type: 'account', api_version: 'v23.0', summary: { ok: rows.length }, metrics: rows };
    },
    accounts: async () => ({ items: ACCOUNTS, total: ACCOUNTS.length, limit: 100, offset: 0 }),
    account: async (accountId: string) => ({
      account: ACCOUNTS.find((a) => a.id === accountId) ?? ACCOUNTS[0],
      sync_states: [
        { account_id: accountId, platform: 'facebook', object_type: 'account', state: 'current', discovery_complete: true, backfill_until: null, last_full_sync_at: day(13), last_incremental_sync_at: day(13), next_due_at: day(14), poll_interval_s: 86400, consecutive_failures: 0, last_error_class: null, last_error_code: null, last_success_at: day(13), notes: null },
      ],
    }),
    growth: async (accountId: string): Promise<Growth> => {
      const platform = ACCOUNTS.find((a) => a.id === accountId)?.platform;
      const metricKey = METRICS.find((metric) => metric.preferred_for === 'followers' && metric.platform === platform)?.metric_key ?? 'tt.account.followers';
      // Each account has its own level and its own shape; two of them contain a collection gap so the
      // chart shows a break rather than a carried-forward line.
      const base = platform === 'facebook' ? 1520 : platform === 'instagram' ? 2480 : 940;
      const drift = platform === 'facebook' ? 12 : platform === 'instagram' ? -9 : 31;
      const gapAt = platform === 'instagram' ? [4, 5] : platform === 'tiktok' ? [7] : [];
      return {
        account_id: accountId,
        metric_keys: [metricKey],
        points: Array.from({ length: 10 }, (_, index) => ({
          bucket_start: day(index),
          metric_key: metricKey,
          value: gapAt.includes(index) ? null : base + index * drift + Math.round(Math.sin(index) * 8),
          value_json: null,
          availability: (gapAt.includes(index) ? 'not_collected' : 'observed') as 'not_collected' | 'observed',
          collected_at: day(index),
        })),
        availability_summary: { observed: 10 - gapAt.length, not_collected: gapAt.length },
      };
    },
    accountMetrics: async () => [metricValue('fb.page.followers_total', 'count', 1520, 'observed'), metricValue('fb.page.likes_total', 'count', null, 'not_authorized')],
    series: (params: { account_id: string }) => Promise.resolve(seriesFor(ACCOUNTS.find((a) => a.id === params.account_id)?.platform ?? 'tiktok')),
    platforms: async () => ({ platforms: PLATFORMS }),
    publications: async () => ({ items: PUBLICATIONS, total: PUBLICATIONS.length, limit: 8, offset: 0 }),
    publicationMetrics: async () => [metricValue('fb.post.media_views', 'count', 12400, 'observed'), metricValue('fb.post.likes', 'count', 0, 'observed'), metricValue('tt.video.watch_time', 'seconds', null, 'not_supported')],
    compare: async (body?: { metric_key?: string; attribute?: string }) => ({
      ...COMPARE,
      metric_key: body?.metric_key ?? COMPARE.metric_key,
      attribute: body?.attribute ?? COMPARE.attribute,
    }),
    hashtags: async (params: { metric_key?: string; window_kind?: string }) => ({
      ...HASHTAG_COMPARISON,
      metric_key: params.metric_key ?? HASHTAG_COMPARISON.metric_key,
      window: params.window_kind ?? HASHTAG_COMPARISON.window,
    }),
    runs: async () => ({ items: RUNS, total: RUNS.length, limit: 10, offset: 0 }),
    consent: async (accountId: string): Promise<ConsentPlan> => ({
      account_id: accountId,
      platform: 'facebook',
      route: 'facebook_login',
      account_type: 'facebook_page',
      provider_account_id: '1234567890',
      capability_key: 'facebook.page.read_insights',
      capability_label: 'Read Page insights',
      capability_state: 'needs_user_consent',
      status: 'consent_required',
      required_scopes: ['pages_read_engagement', 'read_insights'],
      granted_scopes: ['pages_show_list', 'pages_read_engagement', 'pages_manage_posts'],
      missing_scopes: ['read_insights'],
      requested_scopes: ['pages_read_engagement', 'read_insights'],
      scope_reasons: { read_insights: 'required to read analytics: Read Page insights' },
      page_tasks_required: ['ANALYZE'],
      page_tasks_held: ['ANALYZE', 'CREATE_CONTENT'],
      page_tasks_missing: [],
      page_tasks_known: true,
      app_mode: 'live',
      developer_approval_required: false,
      can_start: true,
      blocked_by: ['user_consent'],
      blockers_text: ['the account has not granted the insight permission yet'],
      metrics_blocked: [
        { metric_key: 'fb.page.impressions', object_type: 'account', missing_scopes: ['read_insights'], detail: 'missing scope(s): read_insights' },
      ],
      docs_url: 'https://developers.facebook.com/docs/graph-api/reference/page/insights',
      detail: 'analytics needs read_insights, which the account did not grant',
    }),
    consentOverview: async () => ({ accounts: [], totals: {}, generated_at: day(13) }),
    startConsent: async (accountId: string): Promise<ConsentStart> => ({
      status: 'authorization_started',
      account_id: accountId,
      plan: null,
      session_id: 'sess-1',
      authorization_url: 'https://www.facebook.com/v23.0/dialog/oauth?client_id=app-123&state=REDACTED',
      redirect_uri: 'http://127.0.0.1:47821/oauth/callback/facebook_login',
      expires_at: day(13),
      scopes_requested: ['pages_read_engagement', 'read_insights'],
      pkce: null,
      app_mode: 'live',
      poll_url: '/social/oauth/transactions/sess-1',
      message: 'Open the authorization page and approve the analytics permission.',
    }),
    publication: async (publicationId: string) => PUBLICATION_DETAILS[publicationId] ?? PUBLICATION_DETAILS['pub-1'],
    retention: async (publicationId: string) =>
      RETENTION[publicationId] ?? { publication_id: publicationId, available: false, reason: 'no retention curve is stored for this publication', curves: [] },
    timeline: async (publicationId: string) => TIMELINES[publicationId] ?? { publication_id: publicationId, points: [] },
    campaigns: async () => ({ items: CAMPAIGNS, total: CAMPAIGNS.length, limit: 100, offset: 0 }),
    campaign: async (campaignId: string) => CAMPAIGN_DETAILS[campaignId] ?? CAMPAIGN_DETAILS.c1,
    snapshots: async () => ({ items: SNAPSHOTS, total: SNAPSHOTS.length, limit: 200, offset: 0 }),
    aggregations: async () => ({ items: AGGREGATIONS, total: AGGREGATIONS.length, limit: 200, offset: 0 }),
    // P8.23
    recommendations: async (params?: { status?: string; kind?: string; account_id?: string }) => {
      const items = RECOMMENDATIONS.filter(
        (row) =>
          (!params?.status || row.status === params.status) &&
          (!params?.kind || row.kind === params.kind) &&
          (!params?.account_id || row.account_id === params.account_id),
      );
      return { items, total: items.length, limit: 100, offset: 0 };
    },
    recommendation: async (recommendationId: string) => {
      const recommendation = RECOMMENDATIONS.find((row) => row.id === recommendationId) ?? RECOMMENDATIONS[0];
      return { recommendation, evidence: RECOMMENDATION_EVIDENCE[recommendation.id] ?? [] };
    },
    acceptRecommendation: async (recommendationId: string, body?: { note?: string | null }) => ({
      ...(RECOMMENDATIONS.find((row) => row.id === recommendationId) ?? RECOMMENDATIONS[0]),
      status: 'accepted' as const,
      approval_request_id: 'apr-harness',
      decided_at: day(13),
      decision_note: body?.note ?? null,
    }),
    rejectRecommendation: async (recommendationId: string, body: { reason: string }) => ({
      ...(RECOMMENDATIONS.find((row) => row.id === recommendationId) ?? RECOMMENDATIONS[0]),
      status: 'rejected' as const,
      decided_at: day(13),
      decision_note: body.reason,
    }),
    refreshRecommendations: async (body: { account_id: string }) => ({
      account_id: body.account_id,
      created: 2,
      recommendation_ids: ['rec-1', 'rec-2'],
      note: 'Drafts only. Accepting one creates an approval request; nothing is published.',
    }),
    reports: async (params?: { kind?: string }) => {
      const items = REPORTS.filter((row) => !params?.kind || row.kind === params.kind);
      return { items, total: items.length, limit: 100, offset: 0 };
    },
    createReport: async (body: { kind?: string; title?: string | null }) => ({
      ...REPORTS[0],
      id: 'rep-new',
      kind: (body.kind ?? 'overview') as Report['kind'],
      filters: { ...REPORTS[0].filters, title: body.title ?? null },
      created_at: day(13),
    }),
    report: async (reportId: string) => REPORTS.find((row) => row.id === reportId) ?? REPORTS[0],
    downloadReport: async (reportId: string) => {
      const text = REPORT_BODIES[reportId] ?? REPORT_BODY_1;
      const bytes = new TextEncoder().encode(text);
      return { bytes, text, contentType: 'application/json' };
    },
    deleteReport: async () => undefined,
    retentionPreview: async () => RETENTION_PREVIEW,
    retentionExecute: async () => ({
      dry_run: false,
      deleted: { aggregations: 0, reports: 1, observations: 1841, collection_runs: 42, superseded_recommendations: 1, orphan_files: 0 },
      remaining: { observations: 0, collection_runs: 0, reports: 0 },
      bytes_freed: 551,
      audit_event_id: 'audit-harness-1',
      foreign_key_violations: 0,
      integrity_ok: true,
      duration_ms: 41,
      errors: [],
    }),
    settings: async () => SETTINGS_RESPONSE,
    updateSettings: async (settings: Record<string, unknown>) => ({
      ...SETTINGS_RESPONSE,
      settings: { ...SETTINGS_RESPONSE.settings, ...settings },
    }),
  } satisfies AnalyticsClient;

  if (mode === 'loading') {
    return {
      overview: never, freshness: never, capabilities: never, versions: never, windows: never,
      metrics: never, availability: never, accounts: never, account: never, growth: never,
      accountMetrics: never, series: never, platforms: never, publications: never,
      publicationMetrics: never, compare: never, hashtags: never, runs: never,
      consent: never, consentOverview: never, startConsent: never,
      publication: never, retention: never, timeline: never, campaigns: never, campaign: never,
      snapshots: never, aggregations: never,
      recommendations: never, recommendation: never, acceptRecommendation: never, rejectRecommendation: never,
      refreshRecommendations: never, reports: never, createReport: never, report: never, downloadReport: never,
      deleteReport: never, settings: never, updateSettings: never,
      retentionPreview: never, retentionExecute: never,
    } as unknown as AnalyticsClient;
  }

  if (mode === 'empty') {
    return {
      ...populated,
      overview: async () => ({ accounts_total: 0, accounts_connected: 0, publications_measured: 0, observations: 0, aggregations: 0, recommendations_by_status: {}, latest_collection_at: null, generated_at: day(13) }),
      freshness: async () => ({ accounts: [], generated_at: day(13) }),
      capabilities: async () => ({ platforms: [], generated_at: day(13) }),
      accounts: async () => ({ items: [], total: 0, limit: 100, offset: 0 }),
      platforms: async () => ({ platforms: [] }),
      publications: async () => ({ items: [], total: 0, limit: 8, offset: 0 }),
      metrics: async () => ({ items: [], total: 0, limit: 200, offset: 0 }),
      runs: async () => ({ items: [], total: 0, limit: 10, offset: 0 }),
      campaigns: async () => ({ items: [], total: 0, limit: 100, offset: 0 }),
      aggregations: async () => ({ items: [], total: 0, limit: 200, offset: 0 }),
      recommendations: async () => ({ items: [], total: 0, limit: 100, offset: 0 }),
      reports: async () => ({ items: [], total: 0, limit: 100, offset: 0 }),
    };
  }

  if (mode === 'error') {
    const fail = () => Promise.reject(new Error('the analytics API returned 500 (synthetic harness failure)'));
    return {
      ...populated,
      settings: fail,
      reports: fail,
      recommendations: fail,
    };
  }

  if (mode === 'unavailable') {
    return {
      ...populated,
      publication: async (publicationId: string) => ({
        publication: PUBLICATION_DETAILS[publicationId]?.publication ?? PUBLICATIONS[0],
        metrics: [
          metricValue('fb.post.media_views', 'count', null, 'not_authorized', 'h24'),
          metricValue('fb.post.reach', 'people', null, 'not_authorized', 'lifetime'),
          metricValue('fb.post.video_complete_views', 'count', null, 'below_threshold', 'lifetime'),
        ],
      }),
      retention: async (publicationId: string) => ({
        publication_id: publicationId,
        available: false,
        reason: 'no retention curve is stored for this publication; the provider may not expose one',
        curves: [],
      }),
      timeline: async (publicationId: string) => ({ publication_id: publicationId, points: [] }),
    };
  }

  if (mode === 'nohistory') {
    return {
      ...populated,
      growth: async (accountId: string): Promise<Growth> => ({
        account_id: accountId,
        metric_keys: [],
        points: [],
        availability_summary: {},
      }),
      accountMetrics: async () => [],
    };
  }

  if (mode === 'permission') {
    return {
      ...populated,
      availability: async (accountId: string): Promise<Availability> => ({
        account_id: accountId,
        platform: 'instagram',
        object_type: 'account',
        api_version: 'v23.0',
        summary: { not_authorized: 2, ok: 1 },
        metrics: [
          { metric_key: 'ig.account.reach', allowed: false, reason: 'not_authorized', availability_if_skipped: 'not_authorized', detail: 'needs instagram_manage_insights' },
          { metric_key: 'ig.account.follower_changes', allowed: false, reason: 'not_authorized', availability_if_skipped: 'not_authorized', detail: 'needs instagram_manage_insights' },
          // The follower metric itself is not authorized in this scenario: the page must say so rather
          // than reporting a registry gap or a zero.
          { metric_key: 'ig.account.followers', allowed: false, reason: 'not_authorized', availability_if_skipped: 'not_authorized', detail: 'needs instagram_manage_insights' },
        ],
      }),
      growth: async (accountId: string) => ({ account_id: accountId, metric_keys: [], points: [], availability_summary: {} }),
      accountMetrics: async () => [],
      runs: async () => ({ items: [], total: 0, limit: 10, offset: 0 }),
      accounts: async () => ({ items: [ACCOUNTS[1]], total: 1, limit: 100, offset: 0 }),
      account: async () => ({
        account: ACCOUNTS[1],
        sync_states: [{ account_id: 'acc-ig', platform: 'instagram', object_type: 'account', state: 'blocked', discovery_complete: false, backfill_until: null, last_full_sync_at: null, last_incremental_sync_at: null, next_due_at: null, poll_interval_s: 86400, consecutive_failures: 5, last_error_class: 'authorization', last_error_code: 'REAUTHORIZATION_REQUIRED', last_success_at: null, notes: 're-consent required for insights scopes' }],
      }),
      consent: async (accountId: string): Promise<ConsentPlan> => ({
        account_id: accountId,
        platform: 'instagram',
        route: 'facebook_login',
        account_type: 'instagram_business',
        provider_account_id: '17841400000000001',
        capability_key: 'instagram.read_insights',
        capability_label: 'Read Instagram insights',
        capability_state: 'requires_developer_approval',
        status: 'developer_approval_required',
        required_scopes: ['instagram_basic', 'pages_show_list', 'pages_read_engagement', 'instagram_manage_insights'],
        granted_scopes: ['instagram_basic', 'instagram_content_publish', 'pages_show_list', 'pages_read_engagement'],
        missing_scopes: ['instagram_manage_insights'],
        requested_scopes: ['instagram_basic', 'pages_show_list', 'pages_read_engagement', 'instagram_manage_insights'],
        scope_reasons: { instagram_manage_insights: 'required to read analytics: Read Instagram insights' },
        page_tasks_required: [],
        page_tasks_held: [],
        page_tasks_missing: [],
        page_tasks_known: true,
        app_mode: 'development',
        developer_approval_required: true,
        can_start: true,
        blocked_by: ['developer_approval'],
        blockers_text: ['the permission is granted by the user but the developer app has not been approved for it'],
        metrics_blocked: [
          { metric_key: 'ig.account.reach', object_type: 'account', missing_scopes: ['instagram_manage_insights'], detail: 'missing scope(s): instagram_manage_insights' },
          { metric_key: 'ig.account.follower_changes', object_type: 'account', missing_scopes: ['instagram_manage_insights'], detail: 'missing scope(s): instagram_manage_insights' },
        ],
        docs_url: 'https://developers.facebook.com/docs/instagram-platform/insights',
        detail: 'the developer app is in development mode, so this permission is only granted to app testers; user consent alone cannot make it available',
      }),
    };
  }

  return populated;
}

type Scenario = { id: string; title: string; node: ReactNode };

const scenarios: Record<string, Scenario> = {
  overview: { id: 'overview', title: 'Analytics Overview (synthetic)', node: <AnalyticsOverview api={makeClient('populated')} /> },
  compare: { id: 'compare', title: 'Platform Comparison (synthetic)', node: <PlatformComparison api={makeClient('populated')} /> },
  account: { id: 'account', title: 'Account Performance (synthetic)', node: <AccountPerformance api={makeClient('populated')} /> },
  loading: { id: 'loading', title: 'Loading state (synthetic)', node: <AnalyticsOverview api={makeClient('loading')} /> },
  nodata: { id: 'nodata', title: 'No-data state (synthetic)', node: <AnalyticsOverview api={makeClient('empty')} /> },
  permission: { id: 'permission', title: 'Permission-required state (synthetic)', node: <AccountPerformance api={makeClient('permission')} /> },
  responsive: { id: 'responsive', title: 'Responsive layout (synthetic, narrow)', node: <AnalyticsOverview api={makeClient('populated')} /> },
  publication: { id: 'publication', title: 'Publication detail (synthetic)', node: <PublicationDetailPage api={makeClient('populated')} initialPublicationId="pub-1" /> },
  'publication-unavailable': { id: 'publication-unavailable', title: 'Publication with unavailable metrics (synthetic)', node: <PublicationDetailPage api={makeClient('unavailable')} initialPublicationId="pub-2" /> },
  retention: { id: 'retention', title: 'Publication retention (synthetic)', node: <PublicationDetailPage api={makeClient('populated')} initialPublicationId="pub-1" /> },
  'publication-loading': { id: 'publication-loading', title: 'Publication loading state (synthetic)', node: <PublicationDetailPage api={makeClient('loading')} initialPublicationId="pub-1" /> },
  content: { id: 'content', title: 'Content comparison (synthetic)', node: <ContentComparison api={makeClient('populated')} /> },
  'content-nodata': { id: 'content-nodata', title: 'Content comparison, no data (synthetic)', node: <ContentComparison api={makeClient('empty')} /> },
  campaign: { id: 'campaign', title: 'Campaign analytics (synthetic)', node: <CampaignAnalytics api={makeClient('populated')} initialCampaignId="c1" /> },
  'campaign-partial': { id: 'campaign-partial', title: 'Campaign with partial publication (synthetic)', node: <CampaignAnalytics api={makeClient('populated')} initialCampaignId="c2" /> },
  'campaign-loading': { id: 'campaign-loading', title: 'Campaign loading state (synthetic)', node: <CampaignAnalytics api={makeClient('loading')} initialCampaignId="c1" /> },
  // P8.22 — video performance / audience growth / hashtag analysis.
  video: { id: 'video', title: 'Video performance — leaderboard (synthetic)', node: <VideoPerformance api={makeClient('populated')} /> },
  'video-detail': { id: 'video-detail', title: 'Video performance — retention and history (synthetic)', node: <VideoPerformance api={makeClient('populated')} initialSelectedId="pub-1" /> },
  'video-no-retention': { id: 'video-no-retention', title: 'Video with no provider retention curve (synthetic)', node: <VideoPerformance api={makeClient('populated')} initialSelectedId="pub-3" /> },
  'video-compare': { id: 'video-compare', title: 'Video comparison — compatible metrics only (synthetic)', node: <VideoPerformance api={makeClient('populated')} initialCompareIds={['pub-1', 'pub-4', 'pub-2', 'pub-5']} /> },
  'video-loading': { id: 'video-loading', title: 'Video loading state (synthetic)', node: <VideoPerformance api={makeClient('loading')} /> },
  'video-nodata': { id: 'video-nodata', title: 'Video with nothing measured (synthetic)', node: <VideoPerformance api={makeClient('empty')} /> },
  audience: { id: 'audience', title: 'Audience growth — per account (synthetic)', node: <AudienceGrowth api={makeClient('populated')} /> },
  'audience-multi': { id: 'audience-multi', title: 'Audience growth — TikTok selected (synthetic)', node: <AudienceGrowth api={makeClient('populated')} initialAccountId="acc-tt" /> },
  'audience-nohistory': { id: 'audience-nohistory', title: 'Audience growth — no historical snapshot (synthetic)', node: <AudienceGrowth api={makeClient('nohistory')} /> },
  'audience-permission': { id: 'audience-permission', title: 'Audience growth — follower metric not authorized (synthetic)', node: <AudienceGrowth api={makeClient('permission')} initialAccountId="acc-ig" /> },
  hashtags: { id: 'hashtags', title: 'Hashtag analysis — catalogue and comparison (synthetic)', node: <HashtagAnalysis api={makeClient('populated')} /> },
  'hashtags-detail': { id: 'hashtags-detail', title: 'Hashtag details — associated posts (synthetic)', node: <HashtagAnalysis api={makeClient('populated')} initialTag="launch" /> },
  'hashtags-nodata': { id: 'hashtags-nodata', title: 'Hashtag analysis — no data (synthetic)', node: <HashtagAnalysis api={makeClient('empty')} /> },
  // P8.23 — recommendations / reports / settings.
  recommendations: { id: 'recommendations', title: 'Recommendations — record and evidence (synthetic)', node: <Recommendations api={makeClient('populated')} initialRecommendationId="rec-1" /> },
  'recommendations-accept': { id: 'recommendations-accept', title: 'Recommendations — acceptance confirmation (synthetic)', node: <Recommendations api={makeClient('populated')} initialRecommendationId="rec-1" initialDecision="accept" /> },
  'recommendations-reject': { id: 'recommendations-reject', title: 'Recommendations — rejection confirmation (synthetic)', node: <Recommendations api={makeClient('populated')} initialRecommendationId="rec-2" initialDecision="reject" /> },
  'recommendations-decided': { id: 'recommendations-decided', title: 'Recommendations — a recorded decision (synthetic)', node: <Recommendations api={makeClient('populated')} initialRecommendationId="rec-3" /> },
  'recommendations-superseded': { id: 'recommendations-superseded', title: 'Recommendations — superseded record (synthetic)', node: <Recommendations api={makeClient('populated')} initialRecommendationId="rec-5" /> },
  'recommendations-defect': { id: 'recommendations-defect', title: 'Recommendations — record missing mandated fields (synthetic)', node: <Recommendations api={makeClient('populated')} initialRecommendationId="rec-6" /> },
  'recommendations-nodata': { id: 'recommendations-nodata', title: 'Recommendations — nothing to review (synthetic)', node: <Recommendations api={makeClient('empty')} /> },
  'recommendations-loading': { id: 'recommendations-loading', title: 'Recommendations — loading state (synthetic)', node: <Recommendations api={makeClient('loading')} /> },
  reports: { id: 'reports', title: 'Reports — library and detail (synthetic)', node: <Reports api={makeClient('populated')} /> },
  'reports-verified': { id: 'reports-verified', title: 'Reports — verified download (synthetic)', node: <Reports api={makeClient('populated')} autoDownloadId="rep-1" /> },
  'reports-mismatch': { id: 'reports-mismatch', title: 'Reports — digest mismatch refused (synthetic)', node: <Reports api={makeClient('populated')} autoDownloadId="rep-5" /> },
  'reports-nohash': { id: 'reports-nohash', title: 'Reports — no stored digest refused (synthetic)', node: <Reports api={makeClient('populated')} autoDownloadId="rep-4" /> },
  'settings-retention-executed': { id: 'settings-retention-executed', title: 'Analytics settings — retention executed, audited, re-previewed (synthetic)', node: <AnalyticsSettingsPage api={makeClient('populated')} autoRetention="execute" /> },
  'reports-csv': { id: 'reports-csv', title: 'Reports — CSV format record (synthetic)', node: <Reports api={makeClient('populated')} autoDownloadId="rep-6" /> },
  'reports-nodata': { id: 'reports-nodata', title: 'Reports — empty library (synthetic)', node: <Reports api={makeClient('empty')} /> },
  'reports-error': { id: 'reports-error', title: 'Reports — API failure (synthetic)', node: <Reports api={makeClient('error')} /> },
  settings: { id: 'settings', title: 'Analytics settings — stored values and bounds (synthetic)', node: <AnalyticsSettingsPage api={makeClient('populated')} /> },
  'settings-retention': { id: 'settings-retention', title: 'Analytics settings — retention preview and typed confirmation (synthetic)', node: <AnalyticsSettingsPage api={makeClient('populated')} autoRetention="preview" /> },
  'settings-loading': { id: 'settings-loading', title: 'Analytics settings — loading state (synthetic)', node: <AnalyticsSettingsPage api={makeClient('loading')} /> },
  'settings-error': { id: 'settings-error', title: 'Analytics settings — API failure (synthetic)', node: <AnalyticsSettingsPage api={makeClient('error')} /> },
};

function App() {
  const shot = new URLSearchParams(window.location.search).get('shot');
  const chosen = shot && scenarios[shot] ? [scenarios[shot]] : Object.values(scenarios);
  return (
    <main className="w-full space-y-8 p-6">
      <header className="space-y-1">
        <h1 className="text-base font-semibold text-slate-100">Analytics pages — visual review</h1>
        <p className="text-xs text-slate-400">
          CreatorOS P8.18–P8.23 harness. Every number is a synthetic, labelled fixture served by a stub API client. Nothing is read
          from the backend and nothing here ships in the application bundle.
        </p>
      </header>
      {chosen.map((scenario) => (
        <section key={scenario.id} id={`shot-${scenario.id}`} className="space-y-2">
          <div>
            <h2 className="text-sm font-semibold text-slate-200">{scenario.title}</h2>
            <p className="text-xs text-slate-500">Synthetic fixture — not production analytics data.</p>
          </div>
          {scenario.node}
        </section>
      ))}
    </main>
  );
}

createRoot(document.getElementById('analytics-preview-root')!).render(<App />);
