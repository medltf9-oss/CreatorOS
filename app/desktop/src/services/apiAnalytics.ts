/**
 * Typed client for the P8.17 `/analytics` API.
 *
 * Conventions match the other clients (`apiPublishing.ts`): a class holding a bearer token, one
 * `request<T>` helper, `URLSearchParams` for filters, and the error envelope turned into a typed
 * error. Two deliberate differences:
 *
 * * it is **runtime-dependency-free** (only `import type`), so the pure data path can be unit-tested
 *   with `node --test` against a stubbed `fetch` without a bundler;
 * * most read methods accept an `AbortSignal` so a page can cancel an obsolete request when a filter
 *   changes — the requirement is "prevent rapid filter changes from rendering stale responses".
 *
 * Availability is never normalised to zero here: a metric payloads keeps its `availability` string
 * and a possibly-null `value`, exactly as stored.
 */
import type { ObservationAvailability } from '../components/chartModel';

export type Platform = 'facebook' | 'instagram' | 'tiktok';
export type ObjectType = 'account' | 'post' | 'video' | 'reel' | 'story';
export type MetricValueKind = 'scalar' | 'breakdown' | 'series' | 'object';

export type Page<T> = { items: T[]; total: number; limit: number; offset: number };

export type Overview = {
  accounts_total: number;
  accounts_connected: number;
  publications_measured: number;
  observations: number;
  aggregations: number;
  recommendations_by_status: Record<string, number>;
  latest_collection_at: string | null;
  generated_at: string;
};

export type FreshnessAccount = {
  account_id: string;
  platform: Platform;
  display_name: string;
  last_collected_at: string | null;
  age_hours: number | null;
  stale: boolean;
  next_due_at: string | null;
  state: string | null;
  blocked_reason: string | null;
};

export type Freshness = { accounts: FreshnessAccount[]; generated_at: string };

export type CapabilityPlatform = {
  platform: Platform;
  connected_accounts: number;
  metrics_total: number;
  planned_account_metrics: Record<string, number>;
  configured_version: string;
  latest_documented: string;
  retired: boolean;
  note: string;
};

export type Capabilities = { platforms: CapabilityPlatform[]; generated_at: string };

export type VersionPlatform = {
  platform: Platform;
  configured: string;
  latest_documented: string;
  retired: boolean;
  retirement_date: string | null;
  metrics_above_version: string[];
  note: string;
};

export type Versions = { platforms: VersionPlatform[]; generated_at: string };

export type WindowSpec = { kind: string; hours: number; tolerance_minutes: number };
export type Windows = { windows: WindowSpec[]; note: string };

export type MetricDefinition = {
  metric_key: string;
  platform: Platform;
  object_type: ObjectType;
  provider_metric_name: string;
  provider_object: string;
  source_doc_url: string;
  unit: string;
  value_kind: MetricValueKind;
  metric_class: string;
  period_kinds_json: string[];
  definition_text: string;
  is_estimate: boolean;
  is_cumulative: boolean;
  is_deprecated: boolean;
  deprecated_note: string | null;
  supersedes_key: string | null;
  superseded_by_key: string | null;
  api_version_min: string | null;
  api_version_max: string | null;
  required_permissions_json: string[] | { any_of: string[][] };
  account_threshold_json: Record<string, unknown>;
  availability_default: ObservationAvailability;
  preferred_for: string | null;
  catalog_version: string;
};

export type AvailabilityRow = {
  metric_key: string;
  allowed: boolean;
  reason: string;
  availability_if_skipped: ObservationAvailability | null;
  detail: string;
};

export type Availability = {
  account_id: string;
  platform: Platform;
  object_type: ObjectType;
  api_version: string | null;
  summary: Record<string, number>;
  metrics: AvailabilityRow[];
};

// ---------------------------------------------------------------- P8.23: recommendations

/** `draft` and `reviewed` are the only states a human may decide; the backend enforces it too. */
export type RecommendationStatus = 'draft' | 'reviewed' | 'accepted' | 'rejected' | 'superseded';

export type RecommendationKind =
  | 'topic'
  | 'format'
  | 'hook'
  | 'caption'
  | 'hashtag'
  | 'timing'
  | 'campaign'
  | 'growth'
  | 'retention'
  | 'experiment';

/**
 * Exactly `RecommendationOut`. The schema has no title, priority or expiry field: `kind` is the
 * category and `observation` is the summary, and the UI says so rather than inventing those fields.
 */
export type Recommendation = {
  id: string;
  account_id: string | null;
  platform: Platform | null;
  campaign_id: string | null;
  content_project_id: string | null;
  kind: RecommendationKind;
  observation: string;
  sample_size: Record<string, unknown>;
  period_start: string | null;
  period_end: string | null;
  confidence: number | null;
  confidence_method: string | null;
  limitations: string[];
  proposed_experiment: Record<string, unknown>;
  expected_benefit: Record<string, unknown>;
  status: RecommendationStatus;
  generated_by: string;
  approval_request_id: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string;
};

/** `GET /analytics/recommendations/{id}` — the record plus its evidence, which the list omits. */
export type RecommendationDetail = {
  recommendation: Recommendation;
  evidence: MetricValue[];
};

export type RecommendationRefresh = {
  account_id: string;
  created: number;
  recommendation_ids: string[];
  note: string;
};

// ---------------------------------------------------------------- P8.23: reports

export type ReportKind =
  | 'overview'
  | 'platform_comparison'
  | 'account'
  | 'campaign'
  | 'content_comparison'
  | 'recommendations'
  | 'experiment';

export type ReportStatus = 'draft' | 'final' | 'archived';

/** Only formats the backend can actually write (P8.24). Anything else is a 422, not a fallback. */
export type ReportFormat = 'json' | 'csv' | 'pdf';

export type RetentionCategory = { name: string; count: number; reason: string; bytes: number };

export type RetentionPreview = {
  policy: {
    retention_days: number;
    report_retention_days: number;
    batch_limit: number;
    cutoff: string;
    report_cutoff: string;
  };
  categories: RetentionCategory[];
  total_rows: number;
  total_bytes: number;
  protected: Record<string, number>;
  applied: boolean;
  reason: string;
};

export type RetentionExecution = {
  dry_run: boolean;
  deleted: Record<string, number>;
  remaining: Record<string, number>;
  bytes_freed: number;
  audit_event_id: string | null;
  foreign_key_violations: number;
  integrity_ok: boolean;
  duration_ms: number;
  errors: string[];
};

export type ReportSection = { heading: string; type: string; body: unknown };

/**
 * Exactly `ReportOut`. `sha256` is the digest of the stored file bytes, computed by the backend when
 * the file was written; a download is verified against it before it is offered for saving.
 */
export type Report = {
  id: string;
  kind: ReportKind;
  period_start: string | null;
  period_end: string | null;
  filters: Record<string, unknown>;
  sections: ReportSection[];
  summary: string | null;
  sha256: string | null;
  bytes: number;
  status: ReportStatus;
  generated_by: string;
  created_at: string;
  format: ReportFormat;
};

export type ReportCreate = {
  kind?: ReportKind;
  format?: ReportFormat;
  account_id?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  title?: string | null;
};

/** A downloaded report: the bytes exactly as received, plus what the server said they are. */
export type ReportDownload = { bytes: Uint8Array; text: string; contentType: string | null };

// ---------------------------------------------------------------- P8.23: settings

export type SettingBound = { min: number; max: number };

/** Exactly `AnalyticsSettingsOut`: stored preferences, the bounds the backend enforces, and a note. */
export type AnalyticsSettings = {
  settings: Record<string, unknown>;
  bounds: Record<string, SettingBound>;
  note: string;
};

export type AccountSummary = {
  id: string;
  platform: Platform;
  provider_account_id: string;
  display_name: string;
  username: string | null;
  account_type: string;
  connection_status: string;
  token_expires_at: string | null;
  last_verified_at: string | null;
  observations: number;
  publications: number;
  last_collected_at: string | null;
};

export type SyncState = {
  account_id: string;
  platform: Platform;
  object_type: ObjectType;
  state: string;
  discovery_complete: boolean;
  backfill_until: string | null;
  last_full_sync_at: string | null;
  last_incremental_sync_at: string | null;
  next_due_at: string | null;
  poll_interval_s: number;
  consecutive_failures: number;
  last_error_class: string | null;
  last_error_code: string | null;
  last_success_at: string | null;
  notes: string | null;
};

export type AccountDetail = { account: AccountSummary; sync_states: SyncState[] };

export type GrowthPoint = {
  bucket_start: string;
  metric_key: string;
  value: number | null;
  value_json: unknown;
  availability: ObservationAvailability;
  collected_at: string | null;
};

export type Growth = {
  account_id: string;
  metric_keys: string[];
  points: GrowthPoint[];
  availability_summary: Record<string, number>;
};

export type MetricValue = {
  metric_key: string;
  provider_metric_name: string | null;
  unit: string;
  value_kind: MetricValueKind;
  metric_class: string | null;
  period_kind: string;
  availability: ObservationAvailability;
  availability_reason: string | null;
  value: number | null;
  value_json: unknown;
  collected_at: string | null;
  period_start: string | null;
  period_end: string | null;
  sample_size: number | null;
  method: string | null;
  confidence: Record<string, unknown> | null;
  snapshot_ids: string[];
  aggregation_ids: string[];
};

export type SeriesPoint = {
  bucket_start: string;
  bucket_end: string;
  value: number | null;
  availability: ObservationAvailability;
  observations: number;
};

export type Series = {
  account_id: string;
  metric_key: string;
  bucket: 'hour' | 'day' | 'week';
  points: SeriesPoint[];
  availability_summary: Record<string, number>;
  note: string;
};

export type PlatformSummary = {
  platform: Platform;
  connected_accounts: number;
  publications: number;
  observations: number;
  aggregations: number;
  observed_metrics: number;
  last_collected_at: string | null;
};

export type Platforms = { platforms: PlatformSummary[] };

export type PublicationSummary = {
  publication_id: string;
  account_id: string;
  platform: Platform;
  remote_post_id: string;
  provider_published_at: string | null;
  state: string;
  content_project_id: string | null;
  campaign_id: string | null;
  variant_id: string | null;
  title: string | null;
  hashtags: string[];
  has_metrics: boolean;
  aggregation_ids: string[];
};

export type PublicationDetail = {
  publication: PublicationSummary;
  metrics: MetricValue[];
};

export type RetentionCurve = {
  metric_key: string;
  unit: string;
  collected_at: string | null;
  value_json: unknown;
  availability: ObservationAvailability;
};

export type Retention = {
  publication_id: string;
  available: boolean;
  reason: string | null;
  curves: RetentionCurve[];
};

export type TimelinePoint = {
  collected_at: string;
  metric_key: string;
  window_kind: string | null;
  availability: ObservationAvailability;
  value: number | null;
  value_json: unknown;
  unit: string;
  run_id: string;
};

export type Timeline = { publication_id: string; points: TimelinePoint[] };

export type AggregateScope = 'account' | 'publication' | 'campaign';

export type Aggregation = {
  id: string;
  account_id: string;
  platform: Platform;
  scope: AggregateScope | string;
  publication_key: string;
  campaign_key: string;
  metric_key: string;
  window_kind: string;
  window_hours: number | null;
  window_tolerance_minutes: number | null;
  value: number | null;
  value_json: unknown;
  unit: string;
  sample_size: number | null;
  availability: ObservationAvailability;
  method: string;
  input_snapshot_ids: string[];
  confidence: Record<string, unknown>;
  computed_at: string;
  aggregation_version: number;
};

export type Snapshot = {
  id: string;
  account_id: string;
  platform: Platform;
  object_type: string;
  provider_object_id: string;
  publication_id: string | null;
  metric_key: string;
  value_kind: MetricValueKind;
  period_kind: string;
  period_start: string | null;
  period_end: string | null;
  availability: ObservationAvailability;
  availability_reason: string | null;
  value: number | null;
  value_json: unknown;
  unit: string;
  collected_at: string;
  data_as_of: string | null;
  completeness: string;
  sample_size: number | null;
  breakdown_key: string;
  api_version: string;
  run_id: string;
};

export type CampaignSummary = {
  campaign_id: string;
  name: string;
  goal: string | null;
  starts_on: string | null;
  ends_on: string | null;
  projects: number;
  publications: number;
  observations: number;
};

export type CampaignDetail = {
  campaign: CampaignSummary;
  publications: PublicationSummary[];
  metrics: MetricValue[];
};

export type ComparisonGroup = {
  label: string;
  n: number;
  availability: ObservationAvailability;
  median: number | null;
  evidence: Record<string, unknown>[];
};

export type Comparison = {
  metric_key: string;
  attribute: string;
  window: string;
  groups: ComparisonGroup[];
  verdicts: unknown;
  comparability: unknown;
  limitations: string[];
  sufficient: boolean | null;
  note: string;
};

export type Run = {
  id: string;
  account_id: string;
  platform: Platform;
  target_kind: string;
  target_object_id: string | null;
  window_kind: string | null;
  started_at: string;
  finished_at: string | null;
  outcome: string;
  error_class: string | null;
  error_code: string | null;
  http_calls: number;
  metrics_attempted: number;
  metrics_observed: number;
  metrics_unavailable: number;
  metrics_written: number;
  api_version: string | null;
};

export type ConsentStatus =
  | 'consent_not_needed'
  | 'consent_required'
  | 'developer_approval_required'
  | 'page_task_required'
  | 'unsupported'
  | 'authorization_started'
  | 'authorization_pending'
  | 'authorization_denied'
  | 'insufficient_app_access'
  | 'capability_available'
  | 'capability_still_blocked'
  | 'account_identity_mismatch'
  | 'authorization_failed';

export type BlockedMetric = {
  metric_key: string;
  object_type: string;
  missing_scopes: string[];
  detail: string;
};

/** What analytics is missing on one account, why, and whether a user can act on it (P8.20). */
export type ConsentPlan = {
  account_id: string;
  platform: Platform;
  route: string | null;
  account_type: string;
  provider_account_id: string;
  capability_key: string | null;
  capability_label: string | null;
  capability_state: string;
  status: ConsentStatus;
  required_scopes: string[];
  granted_scopes: string[];
  missing_scopes: string[];
  requested_scopes: string[];
  scope_reasons: Record<string, string>;
  page_tasks_required: string[];
  page_tasks_held: string[];
  page_tasks_missing: string[];
  page_tasks_known: boolean;
  app_mode: string | null;
  developer_approval_required: boolean;
  can_start: boolean;
  blocked_by: string[];
  blockers_text: string[];
  metrics_blocked: BlockedMetric[];
  docs_url: string | null;
  detail: string;
};

export type ConsentStart = {
  status: ConsentStatus;
  account_id: string;
  plan: ConsentPlan | null;
  session_id: string | null;
  authorization_url: string | null;
  redirect_uri: string | null;
  expires_at: string | null;
  scopes_requested: string[];
  pkce: string | null;
  app_mode: string | null;
  poll_url: string | null;
  message: string;
};

export type ConsentAccount = {
  account_id: string;
  platform: Platform;
  label: string;
  status: ConsentStatus;
  capability_key: string | null;
  missing_scopes: string[];
  can_start: boolean;
  detail: string;
};

export type ConsentOverview = {
  accounts: ConsentAccount[];
  totals: Record<string, number>;
  generated_at: string;
};

export type DateRange = { start?: string; end?: string };

export type SeriesParams = DateRange & {
  account_id: string;
  metric_key: string;
  bucket?: 'hour' | 'day' | 'week';
};

export class AnalyticsApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly correlationId?: string;

  constructor(error: { code: string; message: string; status: number; correlationId?: string }) {
    super(error.message);
    this.name = 'AnalyticsApiError';
    this.code = error.code;
    this.status = error.status;
    this.correlationId = error.correlationId;
  }
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `?${text}` : '';
}

function asArray<T>(value: unknown, what: string): T[] {
  if (!Array.isArray(value)) throw new AnalyticsApiError({ code: 'MALFORMED_RESPONSE', message: `${what} was not a list`, status: 0 });
  return value as T[];
}

/** The P8.19 pages depend on this interface, not the concrete class, so it can be stubbed in tests. */
export interface AnalyticsClient {
  overview(signal?: AbortSignal): Promise<Overview>;
  freshness(signal?: AbortSignal): Promise<Freshness>;
  capabilities(signal?: AbortSignal): Promise<Capabilities>;
  versions(signal?: AbortSignal): Promise<Versions>;
  windows(signal?: AbortSignal): Promise<Windows>;
  metrics(params?: { platform?: Platform; object_type?: ObjectType; preferred_for?: string; include_deprecated?: boolean; limit?: number; offset?: number }, signal?: AbortSignal): Promise<Page<MetricDefinition>>;
  availability(accountId: string, objectType?: ObjectType, signal?: AbortSignal): Promise<Availability>;
  accounts(params?: { platform?: Platform; limit?: number; offset?: number }, signal?: AbortSignal): Promise<Page<AccountSummary>>;
  account(accountId: string, signal?: AbortSignal): Promise<AccountDetail>;
  growth(accountId: string, params?: DateRange, signal?: AbortSignal): Promise<Growth>;
  accountMetrics(accountId: string, params?: { metric_key?: string; window_kind?: string }, signal?: AbortSignal): Promise<MetricValue[]>;
  series(params: SeriesParams, signal?: AbortSignal): Promise<Series>;
  platforms(signal?: AbortSignal): Promise<Platforms>;
  publications(params?: { account_id?: string; platform?: Platform; limit?: number; offset?: number } & DateRange, signal?: AbortSignal): Promise<Page<PublicationSummary>>;
  publicationMetrics(publicationId: string, params?: { window_kind?: string; metric_key?: string }, signal?: AbortSignal): Promise<MetricValue[]>;
  publication(publicationId: string, signal?: AbortSignal): Promise<PublicationDetail>;
  retention(publicationId: string, signal?: AbortSignal): Promise<Retention>;
  timeline(publicationId: string, signal?: AbortSignal): Promise<Timeline>;
  campaigns(params?: { limit?: number; offset?: number }, signal?: AbortSignal): Promise<Page<CampaignSummary>>;
  campaign(campaignId: string, signal?: AbortSignal): Promise<CampaignDetail>;
  snapshots(params?: { account_id?: string; metric_key?: string; publication_id?: string; object_type?: ObjectType; limit?: number; offset?: number } & DateRange, signal?: AbortSignal): Promise<Page<Snapshot>>;
  aggregations(params?: { account_id?: string; scope?: AggregateScope; metric_key?: string; window_kind?: string; version?: number; limit?: number; offset?: number }, signal?: AbortSignal): Promise<Page<Aggregation>>;
  compare(body: { account_id: string; metric_key: string; attribute?: string; group_by?: string; window_kind?: string; max_groups?: number }, signal?: AbortSignal): Promise<Comparison>;
  hashtags(params: { account_id: string; metric_key: string; window_kind?: string; max_groups?: number }, signal?: AbortSignal): Promise<Comparison>;
  runs(params?: { account_id?: string; outcome?: string; limit?: number; offset?: number }, signal?: AbortSignal): Promise<Page<Run>>;
  recommendations(params?: { status?: RecommendationStatus; account_id?: string; kind?: RecommendationKind; limit?: number; offset?: number }, signal?: AbortSignal): Promise<Page<Recommendation>>;
  recommendation(recommendationId: string, signal?: AbortSignal): Promise<RecommendationDetail>;
  acceptRecommendation(recommendationId: string, body?: { note?: string | null }, signal?: AbortSignal): Promise<Recommendation>;
  rejectRecommendation(recommendationId: string, body: { reason: string }, signal?: AbortSignal): Promise<Recommendation>;
  refreshRecommendations(body: { account_id: string; window_kind?: string; limit?: number }, signal?: AbortSignal): Promise<RecommendationRefresh>;
  reports(params?: { kind?: ReportKind; limit?: number; offset?: number }, signal?: AbortSignal): Promise<Page<Report>>;
  createReport(body: ReportCreate, signal?: AbortSignal): Promise<Report>;
  report(reportId: string, signal?: AbortSignal): Promise<Report>;
  downloadReport(reportId: string, signal?: AbortSignal): Promise<ReportDownload>;
  deleteReport(reportId: string, signal?: AbortSignal): Promise<void>;
  retentionPreview(body?: { older_than_days?: number; batch_limit?: number }, signal?: AbortSignal): Promise<RetentionPreview>;
  retentionExecute(body: { older_than_days?: number; batch_limit?: number; dry_run: boolean }, signal?: AbortSignal): Promise<RetentionExecution>;
  settings(signal?: AbortSignal): Promise<AnalyticsSettings>;
  updateSettings(settings: Record<string, unknown>, signal?: AbortSignal): Promise<AnalyticsSettings>;
  consent(accountId: string, signal?: AbortSignal): Promise<ConsentPlan>;
  consentOverview(signal?: AbortSignal): Promise<ConsentOverview>;
  startConsent(accountId: string, body?: { intent?: string }, signal?: AbortSignal): Promise<ConsentStart>;
}

export class AnalyticsApi implements AnalyticsClient {
  private readonly base: string;
  private readonly token: string;

  // Declared and assigned explicitly (not TS parameter properties): the class is loaded by Node's
  // strip-only type erasure in tests, which rejects parameter properties.
  constructor(base: string, token: string) {
    this.base = base;
    this.token = token;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.base}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.token}`,
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(init.headers ?? {}),
      },
    });
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    const body = text ? JSON.parse(text) : null;
    if (!response.ok) {
      throw new AnalyticsApiError({
        code: body?.code ?? 'REQUEST_FAILED',
        message: body?.message ?? `HTTP ${response.status}`,
        status: response.status,
        correlationId: body?.correlation_id,
      });
    }
    return body as T;
  }

  /**
   * Read a response as bytes. The report file is JSON, but it is treated as an opaque payload: the
   * caller hashes exactly what arrived, so a parse/re-serialise step can never change the digest.
   */
  private async requestBytes(path: string, init: RequestInit = {}): Promise<ReportDownload> {
    const response = await fetch(`${this.base}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${this.token}`, ...(init.headers ?? {}) },
    });
    if (!response.ok) {
      const text = await response.text();
      let body: { code?: string; message?: string; correlation_id?: string; detail?: unknown } | null = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = null;
      }
      const detail = typeof body?.detail === 'object' && body.detail !== null ? (body.detail as { code?: string; message?: string }) : null;
      throw new AnalyticsApiError({
        code: body?.code ?? detail?.code ?? 'REQUEST_FAILED',
        message: body?.message ?? detail?.message ?? `HTTP ${response.status}`,
        status: response.status,
        correlationId: body?.correlation_id,
      });
    }
    const buffer = new Uint8Array(await response.arrayBuffer());
    return {
      bytes: buffer,
      text: new TextDecoder('utf-8').decode(buffer),
      contentType: response.headers?.get?.('content-type') ?? null,
    };
  }

  // ------------------------------------------------------------- overview / system
  overview(signal?: AbortSignal) {
    return this.request<Overview>('/analytics/overview', { signal });
  }

  freshness(signal?: AbortSignal) {
    return this.request<Freshness>('/analytics/freshness', { signal });
  }

  capabilities(signal?: AbortSignal) {
    return this.request<Capabilities>('/analytics/capabilities', { signal });
  }

  versions(signal?: AbortSignal) {
    return this.request<Versions>('/analytics/versions', { signal });
  }

  windows(signal?: AbortSignal) {
    return this.request<Windows>('/analytics/windows', { signal });
  }

  // ------------------------------------------------------------- registry
  metrics(
    params: { platform?: Platform; object_type?: ObjectType; preferred_for?: string; include_deprecated?: boolean; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) {
    const query = buildQuery({ ...params, limit: params.limit ?? 200, offset: params.offset ?? 0 });
    return this.request<Page<MetricDefinition>>(`/analytics/metrics${query}`, { signal }).then((page) => ({
      ...page,
      items: asArray<MetricDefinition>(page.items, 'metrics'),
    }));
  }

  availability(accountId: string, objectType: ObjectType = 'account', signal?: AbortSignal) {
    const query = buildQuery({ account_id: accountId, object_type: objectType });
    return this.request<Availability>(`/analytics/availability${query}`, { signal });
  }

  // ------------------------------------------------------------- accounts
  accounts(params: { platform?: Platform; limit?: number; offset?: number } = {}, signal?: AbortSignal) {
    const query = buildQuery({ ...params, limit: params.limit ?? 100, offset: params.offset ?? 0 });
    return this.request<Page<AccountSummary>>(`/analytics/accounts${query}`, { signal });
  }

  account(accountId: string, signal?: AbortSignal) {
    return this.request<AccountDetail>(`/analytics/accounts/${encodeURIComponent(accountId)}`, { signal });
  }

  growth(accountId: string, params: DateRange = {}, signal?: AbortSignal) {
    const query = buildQuery({ start: params.start, end: params.end });
    return this.request<Growth>(`/analytics/accounts/${encodeURIComponent(accountId)}/growth${query}`, { signal });
  }

  accountMetrics(accountId: string, params: { metric_key?: string; window_kind?: string } = {}, signal?: AbortSignal) {
    const query = buildQuery({ metric_key: params.metric_key, window_kind: params.window_kind });
    return this.request<MetricValue[]>(`/analytics/accounts/${encodeURIComponent(accountId)}/metrics${query}`, { signal }).then(
      (rows) => asArray<MetricValue>(rows, 'account metrics'),
    );
  }

  // ------------------------------------------------------------- series / platforms
  series(params: SeriesParams, signal?: AbortSignal) {
    const query = buildQuery({
      account_id: params.account_id,
      metric_key: params.metric_key,
      bucket: params.bucket ?? 'day',
      start: params.start,
      end: params.end,
    });
    return this.request<Series>(`/analytics/series${query}`, { signal });
  }

  platforms(signal?: AbortSignal) {
    return this.request<Platforms>('/analytics/platforms', { signal });
  }

  // ------------------------------------------------------------- publications
  publications(
    params: { account_id?: string; platform?: Platform; limit?: number; offset?: number } & DateRange = {},
    signal?: AbortSignal,
  ) {
    const query = buildQuery({ ...params, limit: params.limit ?? 10, offset: params.offset ?? 0 });
    return this.request<Page<PublicationSummary>>(`/analytics/publications${query}`, { signal });
  }

  publicationMetrics(publicationId: string, params: { window_kind?: string; metric_key?: string } = {}, signal?: AbortSignal) {
    const query = buildQuery({ window_kind: params.window_kind, metric_key: params.metric_key });
    return this.request<MetricValue[]>(
      `/analytics/publications/${encodeURIComponent(publicationId)}/metrics${query}`,
      { signal },
    ).then((rows) => asArray<MetricValue>(rows, 'publication metrics'));
  }

  publication(publicationId: string, signal?: AbortSignal) {
    return this.request<PublicationDetail>(`/analytics/publications/${encodeURIComponent(publicationId)}`, { signal });
  }

  retention(publicationId: string, signal?: AbortSignal) {
    return this.request<Retention>(`/analytics/publications/${encodeURIComponent(publicationId)}/retention`, { signal });
  }

  timeline(publicationId: string, signal?: AbortSignal) {
    return this.request<Timeline>(`/analytics/publications/${encodeURIComponent(publicationId)}/timeline`, { signal });
  }

  campaigns(params: { limit?: number; offset?: number } = {}, signal?: AbortSignal) {
    const query = buildQuery({ limit: params.limit ?? 100, offset: params.offset ?? 0 });
    return this.request<Page<CampaignSummary>>(`/analytics/campaigns${query}`, { signal }).then((page) => ({
      ...page,
      items: asArray<CampaignSummary>(page.items, 'campaigns'),
    }));
  }

  campaign(campaignId: string, signal?: AbortSignal) {
    return this.request<CampaignDetail>(`/analytics/campaigns/${encodeURIComponent(campaignId)}`, { signal });
  }

  snapshots(
    params: { account_id?: string; metric_key?: string; publication_id?: string; object_type?: ObjectType; limit?: number; offset?: number } & DateRange = {},
    signal?: AbortSignal,
  ) {
    const query = buildQuery({ ...params, limit: params.limit ?? 200, offset: params.offset ?? 0 });
    return this.request<Page<Snapshot>>(`/analytics/snapshots${query}`, { signal }).then((page) => ({
      ...page,
      items: asArray<Snapshot>(page.items, 'snapshots'),
    }));
  }

  aggregations(
    params: { account_id?: string; scope?: AggregateScope; metric_key?: string; window_kind?: string; version?: number; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) {
    const query = buildQuery({ ...params, limit: params.limit ?? 200, offset: params.offset ?? 0 });
    return this.request<Page<Aggregation>>(`/analytics/aggregations${query}`, { signal }).then((page) => ({
      ...page,
      items: asArray<Aggregation>(page.items, 'aggregations'),
    }));
  }

  // ------------------------------------------------------------- comparison / runs
  compare(
    body: { account_id: string; metric_key: string; attribute?: string; group_by?: string; window_kind?: string; max_groups?: number },
    signal?: AbortSignal,
  ) {
    return this.request<Comparison>('/analytics/compare', { method: 'POST', body: JSON.stringify(body), signal });
  }

  hashtags(
    params: { account_id: string; metric_key: string; window_kind?: string; max_groups?: number },
    signal?: AbortSignal,
  ) {
    const query = buildQuery({
      account_id: params.account_id,
      metric_key: params.metric_key,
      window_kind: params.window_kind ?? 'h24',
      max_groups: params.max_groups ?? 3,
    });
    return this.request<Comparison>(`/analytics/hashtags${query}`, { signal });
  }

  runs(params: { account_id?: string; outcome?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal) {
    const query = buildQuery({ ...params, limit: params.limit ?? 20, offset: params.offset ?? 0 });
    return this.request<Page<Run>>(`/analytics/runs${query}`, { signal });
  }

  // ------------------------------------------------------------- recommendations (P8.23)
  recommendations(
    params: { status?: RecommendationStatus; account_id?: string; kind?: RecommendationKind; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) {
    const query = buildQuery({ ...params, limit: params.limit ?? 50, offset: params.offset ?? 0 });
    return this.request<Page<Recommendation>>(`/analytics/recommendations${query}`, { signal }).then((page) => ({
      ...page,
      items: asArray<Recommendation>(page.items, 'recommendations'),
    }));
  }

  recommendation(recommendationId: string, signal?: AbortSignal) {
    return this.request<RecommendationDetail>(
      `/analytics/recommendations/${encodeURIComponent(recommendationId)}`,
      { signal },
    );
  }

  acceptRecommendation(recommendationId: string, body: { note?: string | null } = {}, signal?: AbortSignal) {
    return this.request<Recommendation>(`/analytics/recommendations/${encodeURIComponent(recommendationId)}/accept`, {
      method: 'POST',
      body: JSON.stringify({ note: body.note ?? null }),
      signal,
    });
  }

  rejectRecommendation(recommendationId: string, body: { reason: string }, signal?: AbortSignal) {
    return this.request<Recommendation>(`/analytics/recommendations/${encodeURIComponent(recommendationId)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason: body.reason }),
      signal,
    });
  }

  refreshRecommendations(body: { account_id: string; window_kind?: string; limit?: number }, signal?: AbortSignal) {
    return this.request<RecommendationRefresh>('/analytics/recommendations/refresh', {
      method: 'POST',
      body: JSON.stringify({ window_kind: 'h24', limit: 10, ...body }),
      signal,
    });
  }

  // ------------------------------------------------------------- reports (P8.23)
  reports(params: { kind?: ReportKind; limit?: number; offset?: number } = {}, signal?: AbortSignal) {
    const query = buildQuery({ ...params, limit: params.limit ?? 50, offset: params.offset ?? 0 });
    return this.request<Page<Report>>(`/analytics/reports${query}`, { signal }).then((page) => ({
      ...page,
      items: asArray<Report>(page.items, 'reports'),
    }));
  }

  createReport(body: ReportCreate, signal?: AbortSignal) {
    return this.request<Report>('/analytics/reports', { method: 'POST', body: JSON.stringify(body), signal });
  }

  report(reportId: string, signal?: AbortSignal) {
    return this.request<Report>(`/analytics/reports/${encodeURIComponent(reportId)}`, { signal });
  }

  downloadReport(reportId: string, signal?: AbortSignal) {
    return this.requestBytes(`/analytics/reports/${encodeURIComponent(reportId)}/file`, { signal });
  }

  deleteReport(reportId: string, signal?: AbortSignal) {
    return this.request<void>(`/analytics/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE', signal });
  }

  // ------------------------------------------------------------- retention (P8.24)
  retentionPreview(body: { older_than_days?: number; batch_limit?: number } = {}, signal?: AbortSignal) {
    return this.request<RetentionPreview>('/analytics/prune', {
      method: 'POST',
      body: JSON.stringify({ ...body, dry_run: true }),
      signal,
    });
  }

  retentionExecute(body: { older_than_days?: number; batch_limit?: number; dry_run: boolean }, signal?: AbortSignal) {
    return this.request<RetentionExecution>('/analytics/retention/execute', {
      method: 'POST',
      body: JSON.stringify(body),
      signal,
    });
  }

  // ------------------------------------------------------------- settings (P8.23)
  settings(signal?: AbortSignal) {
    return this.request<AnalyticsSettings>('/analytics/settings', { signal });
  }

  updateSettings(settings: Record<string, unknown>, signal?: AbortSignal) {
    return this.request<AnalyticsSettings>('/analytics/settings', {
      method: 'PUT',
      body: JSON.stringify({ settings }),
      signal,
    });
  }

  // ------------------------------------------------------------- consent (P8.20)
  consent(accountId: string, signal?: AbortSignal) {
    return this.request<ConsentPlan>(`/analytics/consent/${encodeURIComponent(accountId)}`, { signal });
  }

  consentOverview(signal?: AbortSignal) {
    return this.request<ConsentOverview>('/analytics/consent', { signal });
  }

  startConsent(accountId: string, body: { intent?: string } = {}, signal?: AbortSignal) {
    // Only an intent note is accepted; the backend computes the scopes. A client may never name one.
    return this.request<ConsentStart>(`/analytics/consent/${encodeURIComponent(accountId)}`, {
      method: 'POST',
      body: JSON.stringify(body),
      signal,
    });
  }
}

export { buildQuery };
