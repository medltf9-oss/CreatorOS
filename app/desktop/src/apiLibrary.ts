/** Typed client for the Content Library API. Every field comes from the backend. */

import { ApiRequestError } from './api';

export type MediaKind = 'video' | 'audio' | 'image' | 'unknown';
export type DerivativeKind =
  | 'thumbnail'
  | 'timeline'
  | 'preview'
  | 'proxy'
  | 'transcode'
  | 'audio'
  | 'waveform'
  | 'waveform_image';

export type AssetSummary = {
  id: string;
  original_filename: string;
  sha256: string;
  size_bytes: number;
  kind: MediaKind;
  intake_state: string;
  mime_type: string | null;
  format_name: string | null;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_audio: boolean;
  video_codec: string | null;
  audio_codec: string | null;
  rights_status: string;
  archive_state: string;
  tags_json: string[];
  created_at: string;
  updated_at: string;
  thumbnail_url: string | null;
  poster_url: string | null;
  stream_url: string | null;
};

export type DerivativeRow = {
  id: string;
  asset_id: string;
  kind: DerivativeKind;
  profile: string;
  version: number;
  state: string;
  rel_path: string | null;
  size_bytes: number;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  mime_type: string | null;
  job_id: string | null;
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
  urls: Record<string, string>;
};

export type AssetDetail = AssetSummary & {
  source_path: string;
  rotation: number;
  bitrate: number | null;
  probe_version: string | null;
  quarantine_reason: string | null;
  last_processed_at: string | null;
  probe: Record<string, unknown>;
  derivatives: DerivativeRow[];
  project_count: number;
  jobs: { job_id: string; job_type: string; state: string; created_at: string | null }[];
  storage: Record<string, unknown>;
};

export type AssetPage = { items: AssetSummary[]; total: number; limit: number; offset: number };

export type LibraryStats = {
  assets_total: number;
  by_kind: Record<string, number>;
  by_state: Record<string, number>;
  bytes_total: number;
  bytes_by_kind: Record<string, number>;
  derivatives_total: number;
  derivatives_by_kind: Record<string, number>;
  jobs_active: number;
  jobs_failed: number;
  jobs_completed: number;
  storage: Record<string, { files: number; bytes: number }>;
  disk: {
    free_gb: number;
    total_gb: number;
    percent_used: number;
    min_free_gb?: number;
    below_minimum?: boolean;
  };
  recent: AssetSummary[];
  capabilities: {
    h264_encoder?: { name: string; hardware: boolean; verified: boolean; detail?: string; note?: string | null };
    ffmpeg?: { available: boolean; version?: string | null; path?: string };
    probe?: { available: boolean; version?: string | null };
    encoder_verification?: Record<string, { available: boolean; hardware: boolean; verified: boolean }>;
    [key: string]: unknown;
  };
};

export type JobRow = {
  job_id: string;
  job_type: string;
  state: string;
  attempts: number;
  max_attempts: number;
  progress: number | null;
  stage: string | null;
  asset_id: string | null;
  last_error: string | null;
  cancel_requested: boolean;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
  result: Record<string, unknown>;
};

export type JobPage = { items: JobRow[]; total: number; limit: number; offset: number };

export type ProfileRow = {
  id: string;
  kind: string;
  label: string;
  purpose: string;
  container: string;
  video_codec: string;
  audio_codec: string | null;
  max_width: number | null;
  max_height: number | null;
  crf: number;
  preset: string;
  audio_bitrate_k: number;
  fps_cap: number | null;
  version: number;
  extension: string;
  hardware_capable: boolean;
};

export type ImportPreviewRow = {
  path: string;
  original_filename: string;
  size_bytes: number;
  status: 'new' | 'probable_duplicate' | 'rejected';
  reason: string;
  existing_asset_id: string | null;
  existing_filename: string | null;
};

export type ImportResult = {
  jobs: { job_id: string; path: string; original_filename: string; size_bytes: number }[];
  duplicates: { path: string; existing_asset_id: string; original_filename: string; reason: string }[];
  rejected: { path: string; reason: string }[];
};

export type StorageReport = {
  areas: Record<string, { files: number; bytes: number; path?: string }>;
  disk: {
    free_gb: number;
    total_gb: number;
    percent_used: number;
    min_free_gb?: number;
    below_minimum?: boolean;
  };
  spacing_note: string;
};

export type RecoveryReport = {
  staging_removed: number;
  bytes_freed: number;
  assets_quarantined: number;
  derivatives_missing: number;
  older_than_minutes: number;
};

/** Exactly the shape `GET /library/capabilities` returns, measured at request time. */
export type LibraryCapabilities = {
  ffmpeg: {
    available: boolean;
    path?: string;
    version?: string | null;
    encoders?: Record<string, boolean>;
    filters?: Record<string, boolean>;
    error?: string;
  };
  h264_encoder: { name: string; hardware: boolean; verified: boolean; detail: string; note?: string | null };
  encoder_verification: Record<
    string,
    { available: boolean; hardware: boolean; verified: boolean; listed?: boolean; note?: string | null }
  >;
  thumbnail_format: 'webp' | 'jpeg';
  probe: { available: boolean; path: string; version?: string | null };
  policy: {
    max_concurrent_ffmpeg: number;
    max_import_bytes: number;
    min_free_gb: number;
    thumbnail_size: number;
    thumbnail_timeout_s: number;
    transcode_timeout_s: number;
  };
  profiles: ProfileRow[];
};

export type AssetQuery = {
  kind?: MediaKind | '';
  state?: string;
  q?: string;
  sort?: 'created_at' | 'size' | 'duration' | 'name' | 'kind';
  order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
};

export class LibraryApi {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.token}`,
        ...(init.headers ?? {}),
      },
    });
    const text = await response.text();
    let payload: unknown = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { message: text };
      }
    }
    if (!response.ok) {
      const body = (payload ?? {}) as { code?: string; message?: string; correlation_id?: string };
      const detail = typeof (payload as { detail?: unknown })?.detail === 'string' ? (payload as { detail: string }).detail : '';
      throw new ApiRequestError({
        code: body.code ?? (detail.split(':')[0] || 'HTTP_ERROR'),
        message: body.message ?? (detail || `HTTP ${response.status}`),
        status: response.status,
        correlationId: body.correlation_id,
      });
    }
    return payload as T;
  }

  /**
   * Media URLs are returned as API-relative paths. The renderer is served from the Vite
   * origin in development and from file:// when packaged, so a bare "/library/..." would
   * resolve against the wrong host and every thumbnail would break. Always resolve media
   * URLs through the API base.
   */
  mediaUrl(path: string | null | undefined): string | null {
    if (!path) return null;
    if (/^https?:\/\//i.test(path)) return path;
    return `${this.baseUrl}${path.startsWith('/') ? '' : '/'}${path}`;
  }

  stats(): Promise<LibraryStats> {
    return this.request<LibraryStats>('/library/stats');
  }

  assets(query: AssetQuery = {}): Promise<AssetPage> {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    }
    return this.request<AssetPage>(`/library/assets?${params.toString()}`);
  }

  asset(id: string): Promise<AssetDetail> {
    return this.request<AssetDetail>(`/library/assets/${id}`);
  }

  inspect(id: string): Promise<Record<string, unknown>> {
    return this.request(`/library/assets/${id}/inspect`, { method: 'POST' });
  }

  probe(id: string): Promise<{ stored: Record<string, unknown>; recommendation: { profile: string; reason: string } }> {
    return this.request(`/library/assets/${id}/probe`);
  }

  updateAsset(id: string, patch: { tags_json?: string[]; rights_status?: string; archive_state?: string }): Promise<{ changes: Record<string, number> }> {
    return this.request(`/library/assets/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });
  }

  deleteAsset(id: string, force = false): Promise<{ original_removed: boolean }> {
    return this.request(`/library/assets/${id}?confirm=${encodeURIComponent(id)}${force ? '&force=true' : ''}`, {
      method: 'DELETE',
    });
  }

  previewImport(paths: string[]): Promise<{ results: ImportPreviewRow[] }> {
    return this.request('/library/import/preview', {
      method: 'POST',
      body: JSON.stringify({ paths: paths.map((path) => ({ path })) }),
    });
  }

  importPaths(paths: string[]): Promise<ImportResult> {
    return this.request('/library/import', {
      method: 'POST',
      body: JSON.stringify({ paths: paths.map((path) => ({ path })) }),
    });
  }

  requestThumbnail(id: string, options: { size?: number; timeline?: number; force?: boolean } = {}): Promise<{ job_id: string }> {
    return this.request(`/library/assets/${id}/thumbnail`, { method: 'POST', body: JSON.stringify(options) });
  }

  requestPreview(id: string): Promise<{ job_id: string }> {
    return this.request(`/library/assets/${id}/preview`, { method: 'POST' });
  }

  requestProxy(id: string, height?: number): Promise<{ job_id: string }> {
    return this.request(`/library/assets/${id}/proxy`, {
      method: 'POST',
      body: JSON.stringify(height ? { height } : {}),
    });
  }

  requestTranscode(id: string, profile: string, height?: number): Promise<{ job_id: string }> {
    return this.request(`/library/assets/${id}/transcode`, {
      method: 'POST',
      body: JSON.stringify(height ? { profile, height } : { profile }),
    });
  }

  requestAudio(id: string, format: 'wav' | 'aac' = 'aac'): Promise<{ job_id: string }> {
    return this.request(`/library/assets/${id}/audio`, { method: 'POST', body: JSON.stringify({ format }) });
  }

  requestWaveform(id: string, buckets = 400): Promise<{ job_id: string }> {
    return this.request(`/library/assets/${id}/waveform`, { method: 'POST', body: JSON.stringify({ buckets }) });
  }

  deleteDerivative(id: string): Promise<{ deleted: boolean }> {
    return this.request(`/library/derivatives/${id}?confirm=true`, { method: 'DELETE' });
  }

  jobs(options: { state?: string; job_type?: string; limit?: number; offset?: number } = {}): Promise<JobPage> {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(options)) {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    }
    return this.request<JobPage>(`/library/jobs?${params.toString()}`);
  }

  job(id: string): Promise<JobRow> {
    return this.request<JobRow>(`/library/jobs/${id}`);
  }

  cancelJob(id: string): Promise<{ state: string }> {
    return this.request(`/library/jobs/${id}/cancel`, { method: 'POST' });
  }

  retryJob(id: string): Promise<{ state: string }> {
    return this.request(`/library/jobs/${id}/retry`, { method: 'POST' });
  }

  storage(): Promise<StorageReport> {
    return this.request<StorageReport>('/library/storage');
  }

  recoveryScan(olderThanMinutes = 0): Promise<RecoveryReport> {
    return this.request<RecoveryReport>(`/library/recovery/scan?older_than_minutes=${olderThanMinutes}`, {
      method: 'POST',
    });
  }

  profiles(): Promise<{ profiles: ProfileRow[] }> {
    return this.request('/library/profiles');
  }

  capabilities(): Promise<LibraryCapabilities> {
    return this.request<LibraryCapabilities>('/library/capabilities');
  }
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (!ms) return '—';
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const stamp = new Date(iso).getTime();
  if (Number.isNaN(stamp)) return iso;
  const delta = Math.max(0, Date.now() - stamp) / 1000;
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}
