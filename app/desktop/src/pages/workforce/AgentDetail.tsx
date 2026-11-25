import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, Row, formatAge } from '../../components/ui';
import type { AgentDefinition, AgentTaskRow, WorkforceApi } from '../../apiWorkforce';

export default function AgentDetail({
  wf,
  agentId,
  onBack,
}: {
  wf: WorkforceApi;
  agentId: string;
  onBack: () => void;
}) {
  const [agent, setAgent] = useState<AgentDefinition | null>(null);
  const [tasks, setTasks] = useState<AgentTaskRow[]>([]);
  const [prompt, setPrompt] = useState('');
  const [temperature, setTemperature] = useState(0.4);
  const [maxTokens, setMaxTokens] = useState(700);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<null | 'saving' | 'saved'>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [definition, taskRows] = await Promise.all([wf.agent(agentId), wf.tasks({ agent_id: agentId, limit: 20 })]);
      setAgent(definition);
      setTasks(taskRows);
      setPrompt(definition.prompt_active ?? definition.system_prompt);
      setTemperature(definition.temperature);
      setMaxTokens(definition.max_tokens);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [wf, agentId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    if (!agent) return;
    setSaveState('saving');
    setError(null);
    try {
      const updated = await wf.updateAgent(agent.agent_id, {
        system_prompt: prompt,
        temperature,
        max_tokens: maxTokens,
      });
      setAgent(updated);
      setSaveState('saved');
      window.setTimeout(() => setSaveState(null), 2000);
    } catch (err) {
      setSaveState(null);
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function toggleEnabled() {
    if (!agent) return;
    setBusy(true);
    try {
      const updated = await wf.setAgentState(agent.agent_id, !agent.enabled);
      setAgent(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (!agent) {
    return (
      <div className="space-y-4">
        <Button variant="ghost" onClick={onBack}>
          ← directory
        </Button>
        <ErrorNote error={error} onRetry={() => void load()} />
        {!error ? <Empty>Loading agent…</Empty> : null}
      </div>
    );
  }

  const dirty =
    prompt !== (agent.prompt_active ?? agent.system_prompt) ||
    temperature !== agent.temperature ||
    maxTokens !== agent.max_tokens;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button variant="ghost" onClick={onBack}>
          ← directory
        </Button>
        <div className="flex items-center gap-2">
          <Badge tone={agent.enabled ? 'good' : 'bad'}>{agent.enabled ? 'enabled' : 'disabled'}</Badge>
          <Badge tone="muted">config v{agent.config_version}</Badge>
          <Badge tone="info">level {agent.hierarchy_level}</Badge>
          <Button variant={agent.enabled ? 'danger' : 'primary'} busy={busy} onClick={() => void toggleEnabled()}>
            {agent.enabled ? 'disable agent' : 'enable agent'}
          </Button>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={agent.name} subtitle={agent.description}>
          <Row label="Agent id" value={agent.agent_id} />
          <Row label="Department" value={agent.department} />
          <Row label="Model profile" value={agent.model_profile} />
          <Row label="Fallbacks" value={agent.fallback_profiles.join(', ') || 'none'} />
          <Row label="Output contract" value={agent.output_contract ?? 'none'} />
          <Row label="Memory scope" value={agent.memory_scope} />
          <Row label="Can delegate" value={agent.can_delegate ? 'yes' : 'no'} />
          <Row label="Risk level" value={agent.risk_level} />
          <Row label="Approval" value={agent.approval_requirement} />
          <Row label="Catalog version" value={agent.catalog_version} />
          <p className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-xs leading-relaxed text-slate-400">
            {agent.mission}
          </p>
        </Card>

        <Card title="Tools and task types">
          <div className="space-y-3">
            <div>
              <p className="text-[11px] uppercase tracking-wide text-slate-500">Granted</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {agent.allowed_tools.map((tool) => (
                  <Badge key={tool} tone="good">
                    {tool}
                  </Badge>
                ))}
              </div>
            </div>
            <div>
              <p className="text-[11px] uppercase tracking-wide text-slate-500">Explicitly forbidden</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {agent.restricted_tools.map((tool) => (
                  <Badge key={tool} tone="bad">
                    {tool}
                  </Badge>
                ))}
              </div>
            </div>
            <div>
              <p className="text-[11px] uppercase tracking-wide text-slate-500">Task types</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {agent.task_types.map((type) => (
                  <Badge key={type} tone="muted">
                    {type}
                  </Badge>
                ))}
              </div>
            </div>
            <Row label="Timeout" value={`${agent.timeout_s}s`} />
            <Row label="Max retries" value={agent.max_retries} />
            <Row label="Prompt versions" value={(agent.prompt_versions ?? []).join(', ') || '1'} />
          </div>
        </Card>
      </div>

      <Card
        title="System prompt"
        subtitle="Stored per agent and versioned. Saving creates a new prompt version and bumps the config version."
        actions={
          <div className="flex items-center gap-2">
            {saveState === 'saved' ? <Badge tone="good">saved</Badge> : null}
            <Button variant="primary" busy={saveState === 'saving'} disabled={!dirty} onClick={() => void save()}>
              save changes
            </Button>
          </div>
        }
      >
        <div className="grid gap-3 md:grid-cols-[1fr_160px_160px]">
          <textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            rows={14}
            spellCheck={false}
            className="w-full rounded-lg border border-slate-700 bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-200 outline-none focus:border-sky-500"
          />
          <div className="space-y-3">
            <label className="block">
              <span className="text-xs uppercase tracking-wide text-slate-500">Temperature</span>
              <input
                type="number"
                step="0.05"
                min={0}
                max={1.2}
                value={temperature}
                onChange={(event) => setTemperature(Number(event.target.value))}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
              />
            </label>
            <label className="block">
              <span className="text-xs uppercase tracking-wide text-slate-500">Max tokens</span>
              <input
                type="number"
                step="50"
                min={64}
                max={4000}
                value={maxTokens}
                onChange={(event) => setMaxTokens(Number(event.target.value))}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
              />
            </label>
            {dirty ? <p className="text-[11px] text-amber-400">unsaved changes</p> : <p className="text-[11px] text-slate-500">in sync</p>}
          </div>
        </div>
      </Card>

      <Card title="Recent tasks" subtitle="Every row is a stored agent_task; a task keeps the config version it started with.">
        {tasks.length === 0 ? (
          <Empty>This agent has not been assigned a task yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-[11px] uppercase tracking-wide text-slate-500">
                  <th className="py-2 pr-3">Task</th>
                  <th className="py-2 pr-3">State</th>
                  <th className="py-2 pr-3">Depth</th>
                  <th className="py-2 pr-3">Model</th>
                  <th className="py-2 pr-3">Tokens</th>
                  <th className="py-2 pr-3">ms</th>
                  <th className="py-2 pr-3">When</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={task.task_id} className="border-b border-slate-800/60">
                    <td className="py-2 pr-3 text-slate-200">
                      {task.title ?? task.task_type}
                      {task.error_code ? <span className="ml-2 font-mono text-[11px] text-rose-400">{task.error_code}</span> : null}
                    </td>
                    <td className="py-2 pr-3">
                      <Badge tone={task.state === 'succeeded' ? 'good' : task.state === 'failed' ? 'bad' : 'muted'}>
                        {task.state}
                      </Badge>
                    </td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-400">{task.depth}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-400">{task.model_tag ?? '-'}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-400">{task.tokens_used}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-400">{task.duration_ms ?? '-'}</td>
                    <td className="py-2 pr-3 text-[11px] text-slate-500">{formatAge(task.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
