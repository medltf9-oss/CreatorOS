/** Typed client for the Agent Workforce API, kept separate from the AI/Tools client. */

import { ApiRequestError } from './api';

export type AgentSummary = {
  agents_total: number;
  agents_enabled: number;
  agents_disabled: number;
  departments: number;
  chiefs: number;
  leaders: number;
  specialists: number;
  tools_registered: number;
  tools_enabled: number;
  queue_depth: number;
  delegation_limits: Record<string, number>;
  delegation_hard_limits: Record<string, number>;
};

export type Department = {
  id: string;
  name: string;
  mission: string;
  leader_agent_id: string;
  display_order: number;
  enabled: boolean;
  agent_count: number;
  enabled_count: number;
  agents?: AgentDefinition[];
};

export type AgentDefinition = {
  agent_id: string;
  name: string;
  department: string;
  description: string;
  mission: string;
  system_prompt: string;
  prompt_key: string;
  model_profile: string;
  fallback_profiles: string[];
  temperature: number;
  max_tokens: number;
  timeout_s: number;
  max_retries: number;
  allowed_tools: string[];
  restricted_tools: string[];
  task_types: string[];
  memory_scope: string;
  hierarchy_level: number;
  can_delegate: boolean;
  risk_level: string;
  approval_requirement: string;
  output_contract: string | null;
  config_version: number;
  catalog_version: string;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
  prompt_versions?: number[];
  prompt_active?: string;
  recent_tasks?: {
    task_id: string;
    state: string;
    title: string | null;
    duration_ms: number | null;
    tokens: number;
    created_at: string | null;
  }[];
};

export type AgentTaskRow = {
  task_id: string;
  objective_id: string | null;
  agent_id: string;
  department: string;
  role: 'chief' | 'leader' | 'specialist';
  parent_task_id: string | null;
  depth: number;
  task_type: string | null;
  title: string | null;
  state: string;
  priority: number;
  summary: string | null;
  confidence: number | null;
  model_tag: string | null;
  tokens_used: number;
  duration_ms: number | null;
  config_version: number;
  error_code: string | null;
  error: string | null;
  input: Record<string, unknown>;
  output: {
    artifact?: Record<string, unknown>;
    model?: string;
    profile?: string;
    tools?: string[];
    raw_chars?: number;
    warnings?: string[];
  } | null;
  depends_on?: string[];
  execution_ids?: string[];
  created_at: string | null;
};

export type GraphPayload = {
  objective_id: string;
  nodes: {
    task_id: string;
    agent_id: string;
    team_id: string;
    state: string;
    depth: number;
    title: string | null;
    task_type: string | null;
    priority: number;
    parent_task_id: string | null;
    depends_on: string[];
    error_code: string | null;
  }[];
  edges: { from: string; to: string; type: string }[];
  progress?: { task_count: number; states: Record<string, number>; terminal: number; remaining: number };
  tasks?: AgentTaskRow[];
};

export type FinalReport = {
  summary: string;
  deliverable: string;
  quality_verdict: string;
  tasks: {
    task_id: string;
    agent_id: string;
    department: string;
    task_type: string | null;
    summary: string;
    confidence: number;
    duration_ms: number;
    tokens: number;
  }[];
  unfinished: { task_id: string; agent_id: string; code: string | null; error?: string | null }[];
  warnings: string[];
  calls_used: number;
  tokens_used: number;
  generated_at: string;
};

export type Objective = {
  objective_id: string;
  objective: string;
  state: string;
  departments: { department: string; state: string; leader?: string; specialists_done?: number }[];
  calls_used: number;
  tokens_used: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_detail?: string | null;
  summary: string | null;
  plan?: {
    summary: string;
    departments: string[];
    steps: { step: string; owner?: string | null }[];
    plan_task_id: string;
  };
  limits?: Record<string, unknown>;
  progress: {
    task_count: number;
    states: Record<string, number>;
    terminal: number;
    remaining: number;
    ready?: number;
    blocked?: number;
  };
  graph?: GraphPayload;
  final_report?: FinalReport | null;
  run?: { state: string; calls_used: number; tasks_run: number; tasks_failed: number; stopped_reason: string | null; duration_ms: number };
};

export type MonitorPayload = {
  active_objectives: {
    objective_id: string;
    objective: string;
    state: string;
    calls_used: number;
    tokens_used: number;
    started_at: string | null;
    running_task: AgentTaskRow | null;
    progress: { task_count: number; states: Record<string, number>; terminal: number; remaining: number };
  }[];
  active_tasks: AgentTaskRow[];
  task_states: Record<string, number>;
  queue_states: Record<string, number>;
  generated_at: string;
};

export type ToolEntry = {
  tool_id: string;
  name: string;
  description: string;
  category: 'read' | 'compute' | 'write';
  risk: string;
  approval_requirement: string;
  timeout_s: number;
  enabled: boolean;
  registered: boolean;
  input_schema: Record<string, unknown>;
};

export type MemoryRow = {
  memory_id: string;
  scope: string;
  owner_id: string | null;
  project_id: string | null;
  objective_id: string | null;
  source_task_id: string | null;
  title: string | null;
  content: string;
  sensitivity: string;
  expires_at: string | null;
  created_at: string | null;
};

export type ApprovalRow = {
  approval_request_id: string;
  action: string;
  state: string;
  agent_id: string | null;
  objective_id: string | null;
  target_type: string;
  target_id: string;
  summary: string;
  rationale: string | null;
  risks: string[];
  payload_hash: string;
  expires_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  consumed_at: string | null;
  created_at: string | null;
  executed: boolean;
};

export type ActivityRow = {
  event_id: string;
  action: string;
  actor: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

export type AgentMetrics = {
  tasks_by_state: Record<string, number>;
  tasks_by_department: Record<string, number>;
  objectives_by_state: Record<string, number>;
  tokens_total: number;
  duration_ms_total: number;
  duration_ms_avg: number;
  concurrency_limit: number;
};

export type MemoryStats = {
  total: number;
  by_scope: Record<string, number>;
  total_chars: number;
  expired: number;
  budget_chars_per_scope: number;
  retention_days: Record<string, number>;
};

export class WorkforceApi {
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
      throw new ApiRequestError({
        code: body.code ?? `HTTP_${response.status}`,
        message: body.message ?? `request failed with HTTP ${response.status}`,
        status: response.status,
        correlationId: body.correlation_id,
      });
    }
    return payload as T;
  }

  // ---------------------------------------------------------------- registry
  summary() {
    return this.request<AgentSummary>('/agents/registry/summary');
  }

  departments() {
    return this.request<Department[]>('/agents/departments');
  }

  department(id: string) {
    return this.request<Department>(`/agents/departments/${encodeURIComponent(id)}`);
  }

  setDepartmentState(id: string, enabled: boolean) {
    return this.request<Department>(`/agents/departments/${encodeURIComponent(id)}/state`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    });
  }

  agents(params: { department?: string; hierarchy_level?: number; enabled?: boolean; search?: string } = {}) {
    const query = new URLSearchParams();
    if (params.department) query.set('department', params.department);
    if (params.hierarchy_level !== undefined) query.set('hierarchy_level', String(params.hierarchy_level));
    if (params.enabled !== undefined) query.set('enabled', String(params.enabled));
    if (params.search) query.set('search', params.search);
    const suffix = query.toString() ? `?${query}` : '';
    return this.request<AgentDefinition[]>(`/agents/registry${suffix}`);
  }

  agent(id: string) {
    return this.request<AgentDefinition>(`/agents/registry/${encodeURIComponent(id)}`);
  }

  updateAgent(id: string, changes: Record<string, unknown>) {
    return this.request<AgentDefinition>(`/agents/registry/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(changes),
    });
  }

  setAgentState(id: string, enabled: boolean) {
    return this.request<AgentDefinition>(`/agents/registry/${encodeURIComponent(id)}/state`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    });
  }

  // ----------------------------------------------------------- orchestration
  objectives(state?: string) {
    const suffix = state ? `?state=${encodeURIComponent(state)}` : '';
    return this.request<Objective[]>(`/agents/objectives${suffix}`);
  }

  objective(id: string) {
    return this.request<Objective>(`/agents/objectives/${encodeURIComponent(id)}`);
  }

  createObjective(body: {
    objective: string;
    project_id?: string | null;
    priority?: number;
    limits?: Record<string, number>;
    run_now?: boolean;
  }) {
    return this.request<Objective>('/agents/objectives', { method: 'POST', body: JSON.stringify(body) });
  }

  cancelObjective(id: string) {
    return this.request<{ objective_id: string; state: string; cancel_requested?: boolean }>(
      `/agents/objectives/${encodeURIComponent(id)}/cancel`,
      { method: 'POST' },
    );
  }

  resumeObjective(id: string) {
    return this.request<{ objective_id: string; state: string; queued_job_id: string }>(
      `/agents/objectives/${encodeURIComponent(id)}/resume`,
      { method: 'POST' },
    );
  }

  graph(id: string) {
    return this.request<GraphPayload>(`/agents/objectives/${encodeURIComponent(id)}/graph`);
  }

  tasks(params: { state?: string; objective_id?: string; agent_id?: string; limit?: number } = {}) {
    const query = new URLSearchParams();
    if (params.state) query.set('state', params.state);
    if (params.objective_id) query.set('objective_id', params.objective_id);
    if (params.agent_id) query.set('agent_id', params.agent_id);
    if (params.limit) query.set('limit', String(params.limit));
    const suffix = query.toString() ? `?${query}` : '';
    return this.request<AgentTaskRow[]>(`/agents/tasks${suffix}`);
  }

  task(id: string) {
    return this.request<AgentTaskRow>(`/agents/tasks/${encodeURIComponent(id)}`);
  }

  cancelTask(id: string) {
    return this.request<AgentTaskRow>(`/agents/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  }

  monitor() {
    return this.request<MonitorPayload>('/agents/monitor');
  }

  activity(params: { action?: string; limit?: number } = {}) {
    const query = new URLSearchParams();
    if (params.action) query.set('action', params.action);
    if (params.limit) query.set('limit', String(params.limit));
    const suffix = query.toString() ? `?${query}` : '';
    return this.request<ActivityRow[]>(`/agents/activity${suffix}`);
  }

  metrics() {
    return this.request<AgentMetrics>('/agents/metrics/summary');
  }

  // ------------------------------------------------------------------ memory
  memory(scope?: string, limit = 50) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (scope) query.set('scope', scope);
    return this.request<MemoryRow[]>(`/agents/memory?${query}`);
  }

  writeMemory(body: Record<string, unknown>) {
    return this.request<{ memory_id: string; scope: string; owner_id: string | null; expires_at: string | null }>(
      '/agents/memory',
      { method: 'POST', body: JSON.stringify(body) },
    );
  }

  queryMemory(body: Record<string, unknown>) {
    return this.request<MemoryRow[]>('/agents/memory/query', { method: 'POST', body: JSON.stringify(body) });
  }

  memoryStats() {
    return this.request<MemoryStats>('/agents/memory/stats');
  }

  runRetention(dryRun = false) {
    return this.request<{ expired_deleted: number; over_budget_removed: number; dry_run: boolean }>(
      `/agents/memory/retention?dry_run=${dryRun}`,
      { method: 'POST' },
    );
  }

  // ------------------------------------------------------------------- tools
  tools() {
    return this.request<ToolEntry[]>('/agents/tools');
  }

  runTool(toolId: string, agentId: string, payload: Record<string, unknown>) {
    return this.request<{
      tool_id: string;
      ok: boolean;
      output: Record<string, unknown>;
      duration_ms: number;
      error_code: string | null;
      error: string | null;
    }>(`/agents/tools/${encodeURIComponent(toolId)}/run`, {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId, payload }),
    });
  }

  // --------------------------------------------------------------- approvals
  approvals(state?: string) {
    const suffix = state ? `?state=${encodeURIComponent(state)}` : '';
    return this.request<ApprovalRow[]>(`/agents/approvals${suffix}`);
  }

  decideApproval(id: string, approve: boolean, note?: string) {
    return this.request<ApprovalRow>(`/agents/approvals/${encodeURIComponent(id)}/decide`, {
      method: 'POST',
      body: JSON.stringify({ approve, note: note ?? null }),
    });
  }

  cancelApproval(id: string) {
    return this.request<ApprovalRow>(`/agents/approvals/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  }
}
