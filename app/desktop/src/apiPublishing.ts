/** Typed client for the publishing API.
 *
 * Shapes here are the ones the pages actually read. Deep ledgers (attempts, deliveries,
 * reconciliations) are typed loosely on purpose: the backend redacts them, and a page that wants a
 * new column should read `history.py` rather than re-declare the schema here.
 *
 * No method takes or returns a secret. The delivery bucket's credentials are a vault secret id the
 * Electron shell already sealed, which is why `configureDelivery` has no key/secret field.
 */
import { ApiRequestError } from './api';

export type Platform = 'facebook' | 'instagram' | 'tiktok';
export type PublishMode = 'video' | 'reels' | 'direct_post' | 'inbox';
export type PublishState =
  | 'draft'
  | 'preparing'
  | 'validation_pending'
  | 'awaiting_approval'
  | 'approved'
  | 'scheduled'
  | 'ready'
  | 'uploading'
  | 'uploaded'
  | 'processing'
  | 'published'
  | 'failed'
  | 'blocked'
  | 'requires_user_action'
  | 'unknown_remote_state'
  | 'needs_reconciliation'
  | 'cancelled'
  | 'partially_published';

export type ActionGate = { enabled: boolean; reason: string | null };

export type JobRow = {
  id: string;
  variant_id: string;
  project_id: string | null;
  platform: Platform;
  mode: PublishMode;
  account_id: string;
  account_name: string | null;
  state: PublishState;
  state_reason: string | null;
  title: string | null;
  caption: string;
  hashtags: string[];
  scheduled_at_utc: string | null;
  display_timezone: string;
  missed_at: string | null;
  attempts: number;
  provider_media_id: string | null;
  provider_permalink: string | null;
  provider_published_at: string | null;
  error_category: string | null;
  last_error_code: string | null;
  last_error_detail: string | null;
  upload: { bytes_total: number; bytes_done: number; percent: number | null };
  lease: { owner: string | null; expires_at: string | null; held: boolean };
  approval: { requested: boolean; approved: boolean; payload_hash: string | null };
  created_at: string;
  updated_at: string;
};

export type JobDetail = JobRow & {
  project: { id: string; title: string; campaign_id: string | null; status: string | null } | null;
  variant: Record<string, unknown>;
  media: Record<string, unknown>;
  approval_record: Record<string, unknown> | null;
  attempts_log: AttemptRow[];
  deliveries: DeliveryRow[];
  reconciliations: ReconciliationRow[];
  options: Record<string, unknown>;
  metadata: Record<string, unknown>;
};

export type AttemptRow = {
  id: string;
  attempt_no: number;
  started_at: string;
  finished_at: string | null;
  outcome: string;
  provider_operation: string | null;
  http_status: number | null;
  error_category: string | null;
  response_excerpt: string | null;
  remote_snapshot: Record<string, unknown>;
};

export type DeliveryRow = {
  id: string;
  provider: string;
  bucket: string;
  object_key: string;
  state: string;
  bytes: number;
  uploaded_at: string | null;
  expires_at: string | null;
  expired: boolean;
  deleted_at: string | null;
  error_detail: string | null;
};

export type ReconciliationRow = {
  id: string;
  checked_at: string;
  provider_state: string | null;
  decision: string;
  resolved_by: string | null;
  evidence: Record<string, unknown>;
};

export type DestinationRow = {
  id: string;
  project_id: string;
  platform: Platform;
  mode: PublishMode;
  account_id: string | null;
  job_id: string;
  state: PublishState;
  state_reason: string | null;
  title: string | null;
  caption: string;
  hashtags: string[];
  privacy_setting: string;
  aspect_ratio: string;
  duration_ms: number | null;
  scheduled_at_utc: string | null;
  display_timezone: string;
  validation: {
    outcome?: string;
    findings?: { code: string; severity: string; message: string; field?: string }[];
  };
  approval_status: string;
  provider_permalink: string | null;
};

export type IntentSummary = {
  id: string;
  title: string;
  brief: string | null;
  campaign_id: string | null;
  source_asset_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  destinations: number;
  states: PublishState[];
};

export type JobPage = { items: JobRow[]; total: number; limit: number; offset: number };

export type Overview = {
  summary: {
    by_state: Record<string, number>;
    total: number;
    open: number;
    needs_attention: number;
    published: number;
    published_last_7d: number;
    scheduled_upcoming: number;
    platforms: { platform: Platform; by_state: Record<string, number> }[];
  };
  delivery: { configured: boolean; missing: string[]; provider: string | null; bucket: string | null; health: Record<string, unknown> };
  schedule: { policy: SchedulePolicy; now: string };
  composer: { profiles: string[] };
  capabilities: { id: string; platform: Platform; display_name: string; connection_status: string; token_state: string }[];
};

export type SchedulePolicy = { policy: 'ask' | 'publish_now' | 'skip'; grace_seconds: number };

export type ApprovalStatus = {
  pending: boolean;
  request_id: string | null;
  state: string;
  payload_hash: string | null;
  approved_hash: string | null;
  payload: Record<string, unknown> | null;
  consent_text_version: string;
  can_request: boolean;
  reason: string | null;
};

export type ActionResult = {
  action: string;
  state: PublishState;
  queued: boolean;
  reason: string | null;
  detail: Record<string, unknown>;
};

export type Limits = {
  platform: string;
  mode: string;
  rules: {
    docs_url: string;
    requires_public_url: boolean;
    known_options: string[];
    text: Record<string, unknown>;
    media: Record<string, unknown>;
  };
};

export type DeliveryState = {
  configured: boolean;
  provider: string | null;
  bucket: string | null;
  region: string | null;
  public_base_url_present: boolean;
  prefix: string | null;
  ttl_seconds: number | null;
  credentials_present: boolean;
  reason: string | null;
  objects: DeliveryRow[];
  supported_providers: string[];
};

export type CatalogueAccount = {
  id: string;
  platform: Platform;
  display_name: string;
  connection_status: string;
  token_state: string;
  capabilities: Record<string, { state: string; reason: string | null }>;
};

export const STATE_LABELS: Record<PublishState, string> = {
  draft: 'Draft',
  preparing: 'Preparing',
  validation_pending: 'Validating',
  awaiting_approval: 'Awaiting approval',
  approved: 'Approved',
  scheduled: 'Scheduled',
  ready: 'Queued',
  uploading: 'Uploading',
  uploaded: 'Uploaded',
  processing: 'Processing',
  published: 'Published',
  failed: 'Failed',
  blocked: 'Blocked',
  requires_user_action: 'Needs you',
  unknown_remote_state: 'Unknown remote state',
  needs_reconciliation: 'Needs reconciliation',
  cancelled: 'Cancelled',
  partially_published: 'Partially published',
};

export function stateTone(state: PublishState): 'good' | 'warn' | 'bad' | 'info' {
  if (state === 'published') return 'good';
  if (state === 'failed' || state === 'unknown_remote_state' || state === 'blocked') return 'bad';
  if (state === 'requires_user_action' || state === 'needs_reconciliation') return 'warn';
  return 'info';
}

export function platformLabel(platform: Platform): string {
  return platform === 'facebook' ? 'Facebook' : platform === 'instagram' ? 'Instagram' : 'TikTok';
}

export function modeLabel(mode: PublishMode): string {
  return mode === 'video' ? 'Page video' : mode === 'reels' ? 'Reel' : mode === 'inbox' ? 'Inbox upload' : 'Direct post';
}

export function formatWhen(iso: string | null, zone?: string): string {
  if (!iso) return '—';
  const date = new Date(iso);
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: zone || undefined,
    }).format(date);
  } catch {
    return date.toLocaleString();
  }
}

export class PublishingApi {
  constructor(private base: string, private token: string) {}

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
      throw new ApiRequestError({
        code: body?.code ?? 'REQUEST_FAILED',
        message: body?.message ?? `HTTP ${response.status}`,
        status: response.status,
        correlationId: body?.correlation_id,
      });
    }
    return body as T;
  }

  private json(body: unknown): RequestInit {
    return { method: 'POST', body: JSON.stringify(body ?? {}) };
  }

  overview() {
    return this.request<Overview>('/publishing/overview');
  }

  // ---------------------------------------------------------------- composer
  intents(params: { campaign_id?: string; state?: string; platform?: string; search?: string; limit?: number; offset?: number } = {}) {
    const query = new URLSearchParams();
    if (params.campaign_id) query.set('campaign_id', params.campaign_id);
    if (params.state) query.set('state', params.state);
    if (params.platform) query.set('platform', params.platform);
    if (params.search) query.set('search', params.search);
    query.set('limit', String(params.limit ?? 50));
    query.set('offset', String(params.offset ?? 0));
    return this.request<{ items: IntentSummary[]; total: number; limit: number; offset: number }>(
      `/publishing/intents?${query.toString()}`,
    );
  }

  intent(id: string) {
    return this.request<{ project: IntentSummary; destinations: DestinationRow[] }>(`/publishing/intents/${id}`);
  }

  createIntent(body: { title: string; campaign_id?: string | null; source_asset_id?: string | null; notes?: string | null }) {
    return this.request<{ project: IntentSummary; destinations: DestinationRow[] }>('/publishing/intents', this.json(body));
  }

  updateIntent(id: string, body: Record<string, unknown>) {
    return this.request<{ project: IntentSummary; destinations: DestinationRow[] }>(`/publishing/intents/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  deleteIntent(id: string) {
    return this.request<void>(`/publishing/intents/${id}`, { method: 'DELETE' });
  }

  duplicateIntent(id: string, title?: string) {
    return this.request<{ project: IntentSummary; destinations: DestinationRow[] }>(
      `/publishing/intents/${id}/duplicate`,
      this.json({ title }),
    );
  }

  addDestination(
    projectId: string,
    body: {
      platform: Platform;
      account_id: string;
      caption?: string;
      title?: string;
      hashtags?: string[];
      scheduled_at_utc?: string | null;
      display_timezone?: string | null;
      asset_id?: string | null;
    },
  ) {
    return this.request<{ variant: DestinationRow; job: JobDetail }>(
      `/publishing/intents/${projectId}/destinations`,
      this.json(body),
    );
  }

  // ------------------------------------------------------------ destinations
  destination(variantId: string) {
    return this.request<{ variant: DestinationRow; job: JobDetail }>(`/publishing/destinations/${variantId}`);
  }

  destinationActions(variantId: string) {
    return this.request<{
      state: PublishState;
      publish_now: ActionGate;
      retry: ActionGate;
      cancel: ActionGate;
      reconcile: ActionGate;
      history: JobDetail;
    }>(`/publishing/destinations/${variantId}/actions`);
  }

  updateDestination(variantId: string, fields: Record<string, unknown>) {
    return this.request<{ variant: DestinationRow; job: JobDetail }>(`/publishing/destinations/${variantId}`, {
      method: 'PATCH',
      body: JSON.stringify({ fields }),
    });
  }

  deleteDestination(variantId: string) {
    return this.request<void>(`/publishing/destinations/${variantId}`, { method: 'DELETE' });
  }

  revalidate(variantId: string) {
    return this.request<{ report: DestinationRow['validation'] }>(
      `/publishing/destinations/${variantId}/revalidate`,
      this.json({}),
    );
  }

  // --------------------------------------------------------------- approval
  approval(variantId: string) {
    return this.request<ApprovalStatus>(`/publishing/destinations/${variantId}/approval`);
  }

  requestApproval(variantId: string, body: { summary?: string; ttl_hours?: number } = {}) {
    return this.request<ApprovalStatus>(`/publishing/destinations/${variantId}/approval`, this.json(body));
  }

  decideApproval(variantId: string, approve: boolean, note?: string) {
    return this.request<ApprovalStatus>(
      `/publishing/destinations/${variantId}/approval/decide`,
      this.json({ approve, note }),
    );
  }

  // --------------------------------------------------------------- schedule
  schedule(variantId: string, scheduled_at_utc: string | null, display_timezone?: string) {
    return this.request<ActionResult>(
      `/publishing/destinations/${variantId}/schedule`,
      this.json({ scheduled_at_utc, display_timezone }),
    );
  }

  scheduleView(days = 14) {
    return this.request<{
      now: string;
      scheduled: JobRow[];
      missed: JobRow[];
      missed_total: number;
      policy: SchedulePolicy;
    }>(`/publishing/schedule?days=${days}`);
  }

  policy() {
    return this.request<SchedulePolicy>('/publishing/schedule/policy');
  }

  setPolicy(policy: SchedulePolicy) {
    return this.request<SchedulePolicy>('/publishing/schedule/policy', {
      method: 'PUT',
      body: JSON.stringify(policy),
    });
  }

  sweep() {
    return this.request<{ scanned: number; enqueued: number; missed: number; future: number; decisions: unknown[] }>(
      '/publishing/schedule/tick',
      this.json({}),
    );
  }

  publishMissedNow(variantId: string) {
    return this.request<ActionResult>(`/publishing/destinations/${variantId}/publish-missed-now`, this.json({}));
  }

  // ---------------------------------------------------------------- actions
  publishNow(variantId: string) {
    return this.request<ActionResult>(`/publishing/destinations/${variantId}/publish-now`, this.json({}));
  }

  retry(variantId: string) {
    return this.request<ActionResult>(`/publishing/destinations/${variantId}/retry`, this.json({}));
  }

  cancel(variantId: string, reason?: string) {
    return this.request<ActionResult>(`/publishing/destinations/${variantId}/cancel`, this.json({ reason }));
  }

  reconcile(variantId: string) {
    return this.request<ActionResult>(`/publishing/destinations/${variantId}/reconcile`, this.json({}));
  }

  resolveReconciliation(variantId: string, decision: 'remote_exists' | 'no_remote_object', note: string) {
    return this.request<ActionResult>(
      `/publishing/destinations/${variantId}/reconcile/resolve`,
      this.json({ decision, note }),
    );
  }

  // ---------------------------------------------------------------- history
  jobs(params: {
    platform?: Platform;
    account_id?: string;
    state?: string;
    search?: string;
    since?: string;
    until?: string;
    limit?: number;
    offset?: number;
  } = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    }
    query.set('limit', String(params.limit ?? 50));
    query.set('offset', String(params.offset ?? 0));
    return this.request<JobPage>(`/publishing/jobs?${query.toString()}`);
  }

  job(jobId: string) {
    return this.request<{ job: JobDetail }>(`/publishing/jobs/${jobId}`);
  }

  attempts(jobId: string) {
    return this.request<{ items: AttemptRow[]; total: number }>(`/publishing/jobs/${jobId}/attempts`);
  }

  deliveries(jobId: string) {
    return this.request<{ items: DeliveryRow[]; total: number }>(`/publishing/jobs/${jobId}/deliveries`);
  }

  reconciliations(jobId: string) {
    return this.request<{ items: ReconciliationRow[]; total: number }>(`/publishing/jobs/${jobId}/reconciliations`);
  }

  auditTrail(jobId: string) {
    return this.request<{ items: Record<string, unknown>[] }>(`/publishing/jobs/${jobId}/audit`);
  }

  published(params: { platform?: Platform; account_id?: string; search?: string; limit?: number; offset?: number } = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    }
    query.set('limit', String(params.limit ?? 50));
    query.set('offset', String(params.offset ?? 0));
    return this.request<JobPage>(`/publishing/history?${query.toString()}`);
  }

  attention() {
    return this.request<JobPage>('/publishing/attention');
  }

  queue() {
    return this.request<{
      running: JobRow[];
      running_total: number;
      unknown: JobRow[];
      unknown_total: number;
      leases_held: number;
    }>('/publishing/queue');
  }

  // --------------------------------------------------------------- delivery
  delivery() {
    return this.request<DeliveryState>('/publishing/delivery');
  }

  configureDelivery(body: {
    provider: string;
    bucket: string;
    region: string;
    public_base_url?: string | null;
    endpoint_url?: string | null;
    prefix?: string;
    credentials_secret_id?: string | null;
    ttl_seconds?: number;
  }) {
    return this.request<DeliveryState>('/publishing/delivery', { method: 'PUT', body: JSON.stringify(body) });
  }

  clearDelivery() {
    return this.request<void>('/publishing/delivery', { method: 'DELETE' });
  }

  checkDelivery() {
    return this.request<{ ok: boolean; reason: string; detail: Record<string, unknown> }>(
      '/publishing/delivery/check',
      this.json({}),
    );
  }

  cleanupDelivery() {
    return this.request<{ released: number; kept: number }>('/publishing/delivery/cleanup', this.json({}));
  }

  // ------------------------------------------------------------- validation
  limits(platform: Platform, mode?: PublishMode) {
    const query = mode ? `?platform=${platform}&mode=${mode}` : `?platform=${platform}`;
    return this.request<Limits>(`/publishing/limits${query}`);
  }

  validate(body: { platform: Platform; caption?: string; title?: string; hashtags?: string[]; asset_id?: string }) {
    return this.request<{ report: DestinationRow['validation'] }>('/publishing/validate', this.json(body));
  }

  capabilities() {
    return this.request<{ accounts: CatalogueAccount[] }>('/publishing/capabilities');
  }
}
