/** Typed client for the CreatorOS backend. No mock data: every call hits the API. */

export type ApiError = {
  code: string;
  message: string;
  status: number;
  correlationId?: string;
};

export class ApiRequestError extends Error {
  readonly code: string;
  readonly status: number;
  readonly correlationId?: string;

  constructor(error: ApiError) {
    super(error.message);
    this.name = 'ApiRequestError';
    this.code = error.code;
    this.status = error.status;
    this.correlationId = error.correlationId;
  }
}

export type GpuInfo = {
  name: string;
  utilization_percent: number | null;
  memory_used_mb: number;
  memory_total_mb: number;
  memory_free_mb: number;
  memory_percent: number | null;
  temperature_c: number | null;
  power_w: number | null;
  driver?: string | null;
};

export type GovernorSnapshot = {
  captured_at: string;
  cpu_percent: number;
  ram_total_gb: number;
  ram_available_gb: number;
  ram_percent: number;
  gpu: GpuInfo | null;
  loaded_models: { tag: string; size_gb: number; vram_gb: number; vram_percent: number }[];
  active_generations: number;
  max_concurrent: number;
  queue_depth: number;
  limits: Limits;
  pressure: 'normal' | 'saturated' | 'critical';
  reasons: string[];
  telemetry_errors: string[];
};

export type Limits = {
  max_concurrent_generations: number;
  max_ram_percent: number;
  min_free_ram_gb: number;
  max_vram_percent: number;
  max_queue_depth: number;
  generation_timeout_s: number;
  default_profile: string;
  allow_concurrent_models: boolean;
};

export type AiHealth = {
  ollama: { online: boolean; version?: string; base_url: string; latency_ms?: number; error: ApiError | null };
  gateway: { base_url: string; read_timeout_s: number; requests: number };
  models: { installed: number; loaded: { tag: string; vram_gb: number }[]; total_size_gb: number };
  resources: GovernorSnapshot;
  queue: { depth: number; active_generations: number };
  capabilities: Record<string, string>;
};

export type ModelCard = {
  tag: string;
  size_bytes: number;
  size_gb: number;
  digest: string;
  modified_at: string | null;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  capabilities: string[];
  profile: string | null;
  role: string | null;
  loaded: boolean;
  vram_gb: number;
  vram_percent: number;
  context_length: number | null;
  context_source: 'loaded' | 'profile' | null;
  benchmark_tps: number | null;
  benchmark_at: string | null;
  benchmark_source: 'measured' | 'reference' | null;
};

export type ModelList = {
  items: ModelCard[];
  total: number;
  total_size_gb: number;
  loaded_count: number;
  ollama_online: boolean;
};

export type ModelDetail = {
  tag: string;
  family: string | null;
  families: string[];
  parameter_size: string | null;
  parameter_count: number | null;
  quantization: string | null;
  format: string | null;
  architecture: string | null;
  context_length: number | null;
  embedding_length: number | null;
  block_count: number | null;
  capabilities: string[];
  modified_at: string | null;
  license_present: boolean;
  profile: string | null;
  benchmark_tps: number | null;
  benchmark_source: string | null;
  loaded: boolean;
  vram_gb: number;
  vram_percent: number;
};

export type PullStatus = {
  pull_id: string;
  model: string;
  state: 'running' | 'succeeded' | 'failed' | 'cancelled';
  status: string;
  completed: number;
  total: number;
  percent: number;
  error: string | null;
  cancel_requested: boolean;
};

export type BenchmarkResult = {
  model: string;
  profile: string | null;
  tokens_per_sec: number;
  completion_tokens: number;
  prompt_tokens: number;
  ttft_ms: number | null;
  eval_duration_ms: number;
  total_duration_ms: number;
  context_len: number;
  sample: string;
};

export type GenerateResponse = {
  execution_id: string;
  model: string;
  profile: string;
  reason: string;
  response: string;
  thinking?: string;
  metrics: TokenMetrics;
  warnings: string[];
  incomplete?: boolean;
};

export type TokenMetrics = {
  prompt_tokens: number;
  completion_tokens: number;
  total_duration_ms: number;
  load_duration_ms: number;
  prompt_eval_duration_ms: number;
  eval_duration_ms: number;
  ttft_ms: number | null;
  tokens_per_sec: number;
  wall_ms: number;
};

export type Profile = {
  name: string;
  ollama_tag: string;
  context_len: number;
  temperature: number;
  roles_json: string[];
  fallback_profile: string | null;
  enabled: boolean;
};

export type ExecutionRow = {
  id: string;
  kind: string;
  model_tag: string;
  profile_name: string | null;
  state: 'running' | 'succeeded' | 'failed' | 'cancelled' | 'refused' | 'interrupted';
  origin: string;
  prompt_text: string | null;
  response_text: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  ttft_ms: number | null;
  duration_ms: number | null;
  tokens_per_sec: number | null;
  context_len: number | null;
  error_code: string | null;
  error_detail: string | null;
  queue_job_id: string | null;
  created_at: string;
  finished_at: string | null;
};

export type StreamEvent =
  | { type: 'meta'; execution_id: string; model: string; profile: string; reason: string; context_len: number }
  | { type: 'token'; text: string }
  | { type: 'thinking'; text: string }
  | { type: 'done'; metrics: TokenMetrics; done_reason: string | null }
  | { type: 'cancelled'; partial: boolean }
  | { type: 'error'; code: string; message: string };

export type GenerateRequest = {
  prompt: string;
  system?: string | null;
  profile?: string | null;
  complexity?: 'low' | 'medium' | 'high';
  options?: Record<string, number> | null;
  context_len?: number | null;
  think?: boolean | null;
};

/** Never let a raw object reach the UI as "[object Object]". */
function describeFailure(body: Record<string, unknown>, status: number): string {
  if (typeof body.message === 'string' && body.message) return body.message;
  if (Array.isArray(body.details) && body.details.length) return body.details.join('; ');
  if (Array.isArray(body.detail)) {
    const problems = body.detail
      .map((item) => {
        const entry = item as { loc?: unknown[]; msg?: string };
        const where = (entry.loc ?? []).filter((part) => typeof part === 'string' && part !== 'body').join('.');
        return where ? `${where}: ${entry.msg ?? 'invalid value'}` : (entry.msg ?? 'invalid value');
      })
      .filter(Boolean);
    if (problems.length) return problems.join('; ');
  }
  if (typeof body.detail === 'string' && body.detail) return body.detail;
  return `request failed with HTTP ${status}`;
}

export type SetupCheck = { id: string; state: string; detail: string };

export type SetupModelFact = { name: string; size_bytes: number | null; modified_at: string | null };

export type SetupOllama = {
  installed: boolean;
  install_path: string | null;
  version: string | null;
  endpoint: string;
  reachable: boolean;
  models: SetupModelFact[];
  error: string | null;
};

export type SetupFfmpeg = {
  present: boolean;
  ffprobe_present: boolean;
  path: string | null;
  version: string | null;
  error: string | null;
};

export type SetupDisk = {
  root: string;
  exists: boolean;
  writable: boolean;
  free_bytes: number;
  total_bytes: number;
  error: string | null;
};

export type SetupFirstRun = {
  completed: boolean;
  completed_at: string | null;
  version: string | null;
  skipped_ai: boolean;
};

export type PrerequisiteState = {
  id: string;
  kind: string;
  display_name: string;
  version: string;
  required: boolean;
  state: string;
  verified: boolean;
  detail: string;
  local_path: string | null;
  redistribution_approved: boolean;
  redistribution_detail: string;
  download_url: string | null;
  release_page: string | null;
  consent_required: boolean;
};

export type SetupStatus = {
  version: string;
  data_root: string;
  setup_complete: boolean;
  first_run: SetupFirstRun;
  system: Record<string, unknown>;
  disk: SetupDisk;
  ollama: SetupOllama;
  ffmpeg: SetupFfmpeg;
  prerequisites: PrerequisiteState[];
  checks: SetupCheck[];
  manifest_updated: string | null;
  manifest_error: string | null;
};

export type PrerequisiteCatalog = {
  manifest_schema: string;
  updated: string;
  source: string;
  components: PrerequisiteState[];
  manifest_error: string | null;
};

export type VerifyCheck = { id: string; state: string; detail: string };

export type VerifyResponse = {
  component_id: string;
  path: string;
  ok: boolean;
  sha256: string | null;
  size_bytes: number | null;
  signer_subject: string | null;
  timestamped: boolean | null;
  checks: VerifyCheck[];
};

export type CompleteResponse = {
  setup_complete: boolean;
  first_run: SetupFirstRun;
  already_completed: boolean;
};

export class CreatorApi {
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
        payload = { detail: text };
      }
    }
    if (!response.ok) {
      const body = (payload ?? {}) as {
        code?: string;
        message?: string;
        detail?: unknown;
        correlation_id?: string;
        details?: string[];
      };
      throw new ApiRequestError({
        code: body.code ?? `HTTP_${response.status}`,
        message: describeFailure(body, response.status),
        status: response.status,
        correlationId: body.correlation_id,
      });
    }
    return payload as T;
  }

  health() {
    return this.request<AiHealth>('/ai/health');
  }

  models() {
    return this.request<ModelList>('/ai/models');
  }

  model(tag: string) {
    return this.request<ModelDetail>(`/ai/models/${encodeURIComponent(tag)}`);
  }

  deleteModel(tag: string, force = false) {
    const query = new URLSearchParams({ confirm: 'true' });
    if (force) query.set('force', 'true');
    return this.request<{ deleted: string; forced: boolean }>(`/ai/models/${encodeURIComponent(tag)}?${query}`, {
      method: 'DELETE',
    });
  }

  unloadModel(tag: string) {
    return this.request<{ tag: string; unloaded: boolean }>(`/ai/models/${encodeURIComponent(tag)}/unload`, {
      method: 'POST',
    });
  }

  pullModel(model: string) {
    return this.request<PullStatus>('/ai/pulls', { method: 'POST', body: JSON.stringify({ model }) });
  }

  pulls() {
    return this.request<PullStatus[]>('/ai/pulls');
  }

  cancelPull(pullId: string) {
    return this.request<PullStatus>(`/ai/pulls/${encodeURIComponent(pullId)}`, { method: 'DELETE' });
  }

  benchmark(tag: string, maxTokens = 48) {
    return this.request<BenchmarkResult>(`/ai/models/${encodeURIComponent(tag)}/benchmark`, {
      method: 'POST',
      body: JSON.stringify({ max_tokens: maxTokens }),
    });
  }

  generate(body: GenerateRequest) {
    return this.request<GenerateResponse>('/ai/generate', { method: 'POST', body: JSON.stringify(body) });
  }

  cancelExecution(executionId: string) {
    return this.request<{ execution_id: string; cancelled_in_flight: boolean }>(
      `/ai/executions/${encodeURIComponent(executionId)}/cancel`,
      { method: 'POST' },
    );
  }

  executions(limit = 25, state?: string, model?: string) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (state) query.set('state', state);
    if (model) query.set('model', model);
    return this.request<ExecutionRow[]>(`/ai/executions?${query}`);
  }

  resources() {
    return this.request<GovernorSnapshot & { stats: Record<string, number>; history: unknown[] }>('/ai/resources');
  }

  limits() {
    return this.request<Limits>('/ai/limits');
  }

  setLimits(body: Partial<Limits>) {
    return this.request<Limits>('/ai/limits', { method: 'PUT', body: JSON.stringify(body) });
  }

  profiles() {
    return this.request<Profile[]>('/ai/profiles');
  }

  updateProfile(name: string, body: { ollama_tag?: string; context_len?: number; temperature?: number }) {
    return this.request<Profile>(`/ai/profiles/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  /** Stream tokens as NDJSON. Returns a handle so the caller can abort. */
  async stream(
    body: GenerateRequest,
    handlers: {
      onEvent: (event: StreamEvent) => void;
      onError: (error: Error) => void;
      onClose: () => void;
    },
  ): Promise<() => void> {
    const controller = new AbortController();
    const run = async () => {
      try {
        const response = await fetch(`${this.baseUrl}/ai/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}` },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          throw new Error(`stream failed with HTTP ${response.status}`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() ?? '';
          for (const line of lines) {
            if (!line.trim()) continue;
            handlers.onEvent(JSON.parse(line) as StreamEvent);
          }
        }
        if (buffer.trim()) handlers.onEvent(JSON.parse(buffer) as StreamEvent);
      } catch (err) {
        if ((err as Error).name === 'AbortError') return;
        handlers.onError(err instanceof Error ? err : new Error(String(err)));
      } finally {
        handlers.onClose();
      }
    };
    void run();
    return () => controller.abort();
  }
  setupStatus(signal?: AbortSignal) {
    return this.request<SetupStatus>('/setup/status', { signal });
  }

  prerequisites(signal?: AbortSignal) {
    return this.request<PrerequisiteCatalog>('/setup/prerequisites', { signal });
  }

  verifyPrerequisite(componentId: string, path?: string) {
    return this.request<VerifyResponse>(
      `/setup/prerequisites/${encodeURIComponent(componentId)}/verify`,
      { method: 'POST', body: JSON.stringify(path ? { path } : {}) },
    );
  }

  completeSetup(body: { skipped_ai: boolean; notes?: string }) {
    return this.request<CompleteResponse>('/setup/complete', { method: 'POST', body: JSON.stringify(body) });
  }
}
