/** Typed client for the Social Accounts API.
 *
 * Two rules encoded here:
 *  - no method returns a credential, because no endpoint returns one;
 *  - the only way a secret enters the system is `storeProviderSecret`, which hands the value to
 *    the Electron main process (DPAPI vault) and returns metadata. It never touches the backend.
 */
import { ApiRequestError } from './api';

export type Platform = 'facebook' | 'instagram' | 'tiktok';
export type LoginRoute = 'facebook_login' | 'instagram_login' | 'tiktok_login';
export type CapabilityState =
  | 'available'
  | 'needs_user_consent'
  | 'requires_developer_approval'
  | 'unsupported'
  | 'unavailable'
  | 'unknown'
  | 'expired'
  | 'revoked'
  | 'manual_only';
export type TokenState =
  | 'valid'
  | 'expiring_soon'
  | 'expired'
  | 'refresh_required'
  | 'refresh_in_progress'
  | 'refresh_failed'
  | 'revoked'
  | 'reauthorization_required'
  | 'credential_unavailable'
  | 'unknown';

export type AccountSummary = {
  id: string;
  platform: Platform;
  route: LoginRoute | null;
  provider_account_id: string;
  display_name: string;
  label: string;
  group: string | null;
  username: string | null;
  avatar_url: string | null;
  account_type: string;
  connection_status: string;
  token_state: TokenState;
  token_state_reason: string | null;
  token_expires_at: string | null;
  refresh_expires_at: string | null;
  scopes: string[];
  scope_source: string | null;
  api_version: string | null;
  last_verified_at: string | null;
  last_refresh_at: string | null;
  disconnected_at: string | null;
  capabilities: Record<string, CapabilityState>;
  meta: Record<string, unknown>;
};

export type CapabilityRow = {
  capability: string;
  state: CapabilityState;
  reason: string | null;
  required_scopes: string[];
  granted_scopes: string[];
  missing_scopes: string[];
  verified_at: string | null;
  api_version: string | null;
  docs_reference: string | null;
  evidence_ref: string | null;
};

export type TokenStatus = {
  state: TokenState;
  reason: string | null;
  expires_at: string | null;
  refresh_expires_at: string | null;
  has_refresh_token: boolean;
  can_refresh: boolean;
  can_use: boolean;
};

export type AccountDetail = AccountSummary & {
  capabilities_detail: CapabilityRow[];
  token: TokenStatus | null;
  secret_health: {
    kind: string;
    present: boolean;
    status: string;
    fingerprint?: string;
    bytes?: number | null;
    rotated_at?: string | null;
    last_verified_at?: string | null;
    code?: string | null;
  }[];
  latest_snapshot: { checked_at: string; trigger: string; overall_state: string | null; api_version: string | null } | null;
};

export type AccountPage = {
  items: AccountSummary[];
  total: number;
  limit: number;
  offset: number;
  summary: {
    total: number;
    by_platform: Record<string, Record<string, number>>;
    needs_attention: number;
    disconnected: number;
  };
};

export type VaultHealth = {
  available: boolean;
  backend: string;
  schema_version?: number | null;
  alg?: string | null;
  entries?: number | null;
  corrupt?: number | null;
  reencrypted?: number | null;
  operations?: number | null;
  failures?: number | null;
  last_operation?: string | null;
  last_operation_at?: string | null;
  protection: string;
  reason?: string | null;
  directory?: string | null;
  secrets?: { total: number; by_status: Record<string, number>; active: number; unavailable: number; revoked: number };
  callbacks?: { running: boolean; port: number | null; url_base?: string | null };
};

export type ProviderRoute = {
  platform: Platform;
  route: LoginRoute;
  label: string;
  scopes: string[];
  extra_scopes?: string[];
  pkce_required: boolean;
  account_types?: string[];
  docs_url: string;
  notes: string;
  configured: boolean;
  app_mode?: string | null;
  redirect_uri?: string | null;
};

export type ProviderApp = {
  id: string;
  provider: Platform;
  route: LoginRoute;
  label: string;
  client_id: string;
  client_id_suffix: string | null;
  redirect_uri: string;
  scopes: string[];
  api_version: string | null;
  app_mode: string;
  login_config_id: string | null;
  has_client_secret: boolean;
  client_secret_status: string | null;
  last_verified_at: string | null;
  notes: string | null;
  docs_url: string;
  pkce_required: boolean;
  requires_app_review_for_live: boolean;
};

export type OAuthStart = {
  session_id: string;
  authorization_url: string;
  redirect_uri: string;
  expires_at: string;
  scopes_requested: string[];
  platform: Platform;
  route: LoginRoute;
  pkce: string | null;
  label: string;
  notes: string;
  app_mode: string;
  poll_url: string;
};

export type OAuthTransaction = {
  session_id: string;
  platform: Platform;
  route: LoginRoute | null;
  status: 'pending' | 'succeeded' | 'failed' | 'expired' | 'cancelled' | string;
  created_at: string;
  expires_at: string;
  consumed_at: string | null;
  completed_at: string | null;
  error_code: string | null;
  error_message: string | null;
  accounts: { id: string; label: string; platform: Platform; route: string | null; connection_status: string; capabilities: Record<string, string> }[];
  scopes: string[];
  summary: Record<string, unknown>;
  unsupported: string[];
};

export type Overview = {
  accounts: AccountPage['summary'];
  vault: VaultHealth;
  callbacks: { running: boolean; port: number | null; url_base?: string | null };
  oauth_recent: {
    session_id: string;
    platform: Platform;
    route: string | null;
    status: string;
    created_at: string;
    completed_at: string | null;
    error_code: string | null;
  }[];
  providers: ProviderRoute[];
  phase7: {
    accounts_connected: number;
    accounts_needing_attention: number;
    publish_capable_accounts: number;
    publish_capable_account_ids: string[];
    approval_gate: string;
    blockers: string[];
  };
};

export type CapabilityMatrix = {
  capabilities: string[];
  accounts: { id: string; platform: Platform; label: string; route: string | null; states: Record<string, CapabilityState> }[];
  totals: Record<string, number>;
  catalogue: {
    key: string;
    label: string;
    platform: Platform;
    routes: string[];
    required_scopes: string[];
    docs_url: string;
    manual_only: boolean;
    notes: string;
  }[];
};

export type RotationRow = {
  id: string;
  account_id: string;
  provider: Platform;
  state: string;
  started_at: string;
  finished_at: string | null;
  attempts: number;
  error_code: string | null;
  error_detail: string | null;
};

export class SocialApi {
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

  overview() {
    return this.request<Overview>('/social/overview');
  }

  accounts(params: { platform?: Platform; includeDisconnected?: boolean; limit?: number; offset?: number } = {}) {
    const query = new URLSearchParams();
    if (params.platform) query.set('platform', params.platform);
    if (params.includeDisconnected === false) query.set('include_disconnected', 'false');
    query.set('limit', String(params.limit ?? 100));
    query.set('offset', String(params.offset ?? 0));
    return this.request<AccountPage>(`/social/accounts?${query.toString()}`);
  }

  account(id: string) {
    return this.request<AccountDetail>(`/social/accounts/${id}`);
  }

  rename(id: string, label: string | null, group: string | null) {
    return this.request<AccountSummary>(`/social/accounts/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ label, group }),
    });
  }

  disconnect(id: string, revokeAtProvider = true) {
    return this.request<{ account_id: string; connection_status: string; revoked_at_provider: boolean; detail: string; secrets_revoked: number }>(
      `/social/accounts/${id}/disconnect`,
      { method: 'POST', body: JSON.stringify({ revoke_at_provider: revokeAtProvider }) },
    );
  }

  deleteAccount(id: string) {
    return this.request<void>(`/social/accounts/${id}`, { method: 'DELETE' });
  }

  verify(id: string) {
    return this.request<AccountDetail>(`/social/accounts/${id}/verify`, { method: 'POST' });
  }

  capabilities(id: string) {
    return this.request<{
      account_id: string;
      platform: Platform;
      route: LoginRoute | null;
      overall_state: CapabilityState;
      capabilities: CapabilityRow[];
      snapshots: { id: string; checked_at: string; trigger: string; overall_state: string | null; api_version: string | null; granted_scopes: string[] }[];
    }>(`/social/accounts/${id}/capabilities`);
  }

  refreshCapabilities(id: string) {
    return this.request<{ overall_state: CapabilityState; capabilities: CapabilityRow[] }>(
      `/social/accounts/${id}/capabilities/refresh`,
      { method: 'POST' },
    );
  }

  matrix() {
    return this.request<CapabilityMatrix>('/social/capabilities/matrix');
  }

  tokenStatus(id: string) {
    return this.request<TokenStatus>(`/social/accounts/${id}/token`);
  }

  refreshToken(id: string) {
    return this.request<{ account_id: string; token: TokenStatus; rotated: boolean; message: string }>(
      `/social/accounts/${id}/token/refresh`,
      { method: 'POST' },
    );
  }

  rotations() {
    return this.request<{ items: RotationRow[]; pending: number }>('/social/rotations');
  }

  resolveRotations(accountId: string) {
    return this.request<{ account_id: string; resolved: { rotation_id: string; outcome: string }[] }>(
      `/social/rotations/${accountId}/resolve`,
      { method: 'POST' },
    );
  }

  routes() {
    return this.request<{ items: ProviderRoute[]; callback: { running: boolean; port: number | null }; vault_available: boolean }>(
      '/social/oauth/routes',
    );
  }

  startOAuth(platform: Platform, route?: LoginRoute) {
    return this.request<OAuthStart>('/social/oauth/start', {
      method: 'POST',
      body: JSON.stringify({ platform, route }),
    });
  }

  transaction(sessionId: string) {
    return this.request<OAuthTransaction>(`/social/oauth/transactions/${sessionId}`);
  }

  transactions() {
    return this.request<{ items: Record<string, unknown>[] }>('/social/oauth/transactions');
  }

  cancelTransaction(sessionId: string) {
    return this.request<OAuthTransaction>(`/social/oauth/transactions/${sessionId}/cancel`, { method: 'POST' });
  }

  callbackStatus() {
    return this.request<{ running: boolean; port: number | null; url_base: string | null; hint?: string }>('/social/callback');
  }

  providerApps() {
    return this.request<{ items: ProviderApp[]; routes: ProviderRoute[] }>('/social/provider-apps');
  }

  saveProviderApp(
    platform: Platform,
    route: LoginRoute,
    body: { client_id: string; client_secret?: string; redirect_uri?: string; scopes?: string[]; api_version?: string; app_mode: string; notes?: string },
  ) {
    return this.request<ProviderApp>(`/social/provider-apps/${platform}/${route}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  adoptSecret(id: string, purpose: string, provider: Platform, alias: string) {
    return this.request<{
      id: string;
      purpose: string;
      provider: string;
      status: string;
      fingerprint: string;
      bytes: number;
      alg: string;
    }>(`/social/vault/secrets/${id}/adopt`, {
      method: 'POST',
      body: JSON.stringify({ purpose, provider, alias }),
    });
  }

  linkAppSecret(platform: Platform, route: LoginRoute, secretId: string) {
    return this.request<ProviderApp>(`/social/provider-apps/${platform}/${route}/secret-ref`, {
      method: 'PUT',
      body: JSON.stringify({ secret_id: secretId }),
    });
  }

  verifyProviderApp(platform: Platform, route: LoginRoute) {
    return this.request<{
      provider: Platform;
      route: LoginRoute;
      configured: boolean;
      checks: { name: string; ok: boolean | null; detail: string }[];
      problems: string[];
      docs_url: string;
      pkce_required: boolean;
    }>(`/social/provider-apps/${platform}/${route}/verify`, { method: 'POST' });
  }

  deleteProviderApp(platform: Platform, route: LoginRoute) {
    return this.request<void>(`/social/provider-apps/${platform}/${route}`, { method: 'DELETE' });
  }

  vaultHealth() {
    return this.request<VaultHealth>('/social/vault/health');
  }

  reserveSecret() {
    return this.request<{ secret_id: string; purpose: string; max_bytes: number; purpose_options: string[] }>(
      '/social/vault/secrets/reserve',
      { method: 'POST' },
    );
  }

  verifySecret(id: string) {
    return this.request<{ id: string; present: boolean; ok: boolean; code: string | null; purpose: string | null; bytes: number | null; alg: string | null }>(
      `/social/vault/secrets/${id}`,
    );
  }

  deleteSecret(id: string) {
    return this.request<void>(`/social/vault/secrets/${id}`, { method: 'DELETE' });
  }

  /**
   * Store a provider app secret in the DPAPI vault through the Electron main process.
   *
   * The renderer never hands the value to Python: it reserves an id (metadata only), asks the
   * shell to encrypt and write it, then tells the API that the id exists. A backend without the
   * shell returns `VAULT_UNAVAILABLE` from `reserveSecret()` and nothing is stored.
   */
  async storeProviderSecret(platform: Platform, route: LoginRoute, secret: string): Promise<VaultWriteResult> {
    const reserved = await this.reserveSecret();
    const bridge = window.creatoros;
    if (!bridge?.vaultStore) {
      return { ok: false, code: 'VAULT_UNAVAILABLE', error: 'the desktop shell is not available' };
    }
    const alias = `app:${platform}:${route}`;
    const sealed = await bridge.vaultStore({
      id: reserved.secret_id,
      secret,
      purpose: 'provider_app_secret',
      provider: platform,
      alias,
    });
    if (!sealed?.ok) return { ok: false, code: sealed?.code, error: sealed?.error ?? 'the vault refused the value' };
    // The value is now sealed. Adoption records its metadata; linking points the app at it.
    await this.adoptSecret(reserved.secret_id, 'provider_app_secret', platform, alias);
    await this.linkAppSecret(platform, route, reserved.secret_id);
    return { ok: true, record: sealed.record };
  }

  diagnostics(id: string) {
    return this.request<{
      account_id: string;
      platform: Platform;
      route: LoginRoute | null;
      provider_report: Record<string, unknown>;
      local: Record<string, unknown>;
      generated_at: string;
    }>(`/social/accounts/${id}/diagnostics`);
  }
}

/** Details the shell needs to write one secret into the DPAPI vault. */
export type VaultWriteResult = {
  ok: boolean;
  code?: string;
  error?: string;
  record?: { id: string; alg: string; bytes: number; fingerprint: string; version: number };
};

export function capabilityTone(state: CapabilityState | string): 'good' | 'warn' | 'bad' | 'info' {
  if (state === 'available') return 'good';
  if (state === 'manual_only' || state === 'needs_user_consent' || state === 'requires_developer_approval') return 'warn';
  if (state === 'expired' || state === 'revoked' || state === 'unavailable') return 'bad';
  return 'info';
}

export function tokenTone(state: TokenState | string): 'good' | 'warn' | 'bad' | 'info' {
  if (state === 'valid') return 'good';
  if (state === 'expiring_soon' || state === 'refresh_required' || state === 'refresh_in_progress') return 'warn';
  if (state === 'expired' || state === 'revoked' || state === 'reauthorization_required' || state === 'credential_unavailable' || state === 'refresh_failed') {
    return 'bad';
  }
  return 'info';
}

export function routeLabel(route: string | null | undefined): string {
  if (route === 'facebook_login') return 'Facebook Login';
  if (route === 'instagram_login') return 'Instagram Login';
  if (route === 'tiktok_login') return 'TikTok Login Kit';
  return 'unknown login path';
}

export function platformLabel(platform: Platform): string {
  return platform === 'facebook' ? 'Facebook' : platform === 'instagram' ? 'Instagram' : 'TikTok';
}
