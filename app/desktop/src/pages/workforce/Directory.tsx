import { useCallback, useEffect, useMemo, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote } from '../../components/ui';
import type { AgentDefinition, Department, WorkforceApi } from '../../apiWorkforce';

export default function Directory({
  wf,
  onOpenAgent,
}: {
  wf: WorkforceApi;
  onOpenAgent: (id: string) => void;
}) {
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [search, setSearch] = useState('');
  const [department, setDepartment] = useState('');
  const [level, setLevel] = useState('');
  const [enabledOnly, setEnabledOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await wf.agents({
        search: search.trim() || undefined,
        department: department || undefined,
        hierarchy_level: level === '' ? undefined : Number(level),
        enabled: enabledOnly ? true : undefined,
      });
      setAgents(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [wf, search, department, level, enabledOnly]);

  useEffect(() => {
    wf.departments()
      .then(setDepartments)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [wf]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 200);
    return () => window.clearTimeout(timer);
  }, [load]);

  const departmentName = useMemo(() => {
    const map = new Map(departments.map((item) => [item.id, item.name]));
    return (id: string) => map.get(id) ?? id;
  }, [departments]);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} onRetry={() => void load()} />

      <Card title="Directory" subtitle={`${agents.length} matching agent definitions`}>
        <div className="grid gap-3 md:grid-cols-4">
          <label className="block md:col-span-2">
            <span className="text-xs uppercase tracking-wide text-slate-500">Search</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="name, id or mission"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-sky-500"
            />
          </label>
          <label className="block">
            <span className="text-xs uppercase tracking-wide text-slate-500">Department</span>
            <select
              value={department}
              onChange={(event) => setDepartment(event.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-sky-500"
            >
              <option value="">all</option>
              {departments.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-xs uppercase tracking-wide text-slate-500">Level</span>
            <select
              value={level}
              onChange={(event) => setLevel(event.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-sky-500"
            >
              <option value="">all</option>
              <option value="0">chief</option>
              <option value="1">leader</option>
              <option value="2">specialist</option>
            </select>
          </label>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={enabledOnly}
              onChange={(event) => setEnabledOnly(event.target.checked)}
              className="h-3.5 w-3.5 rounded border-slate-600 bg-slate-950"
            />
            enabled only
          </label>
          <Button variant="ghost" onClick={() => void load()}>
            refresh
          </Button>
        </div>
      </Card>

      <Card title="Agents">
        {loading && agents.length === 0 ? (
          <Empty>Loading agents…</Empty>
        ) : agents.length === 0 ? (
          <Empty>No agent matches those filters.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-[11px] uppercase tracking-wide text-slate-500">
                  <th className="py-2 pr-3">Agent</th>
                  <th className="py-2 pr-3">Department</th>
                  <th className="py-2 pr-3">Profile</th>
                  <th className="py-2 pr-3">Contract</th>
                  <th className="py-2 pr-3">Tools</th>
                  <th className="py-2 pr-3">Ver</th>
                  <th className="py-2 pr-3">State</th>
                </tr>
              </thead>
              <tbody>
                {agents.map((agent) => (
                  <tr
                    key={agent.agent_id}
                    onClick={() => onOpenAgent(agent.agent_id)}
                    className="cursor-pointer border-b border-slate-800/60 transition hover:bg-slate-800/40"
                  >
                    <td className="py-2 pr-3">
                      <div className="text-slate-100">{agent.name}</div>
                      <div className="font-mono text-[11px] text-slate-500">{agent.agent_id}</div>
                    </td>
                    <td className="py-2 pr-3 text-xs text-slate-400">{departmentName(agent.department)}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-400">{agent.model_profile}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-400">{agent.output_contract ?? '-'}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-500">{agent.allowed_tools.length}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-500">v{agent.config_version}</td>
                    <td className="py-2 pr-3">
                      <Badge tone={agent.enabled ? 'good' : 'bad'}>{agent.enabled ? 'enabled' : 'disabled'}</Badge>
                    </td>
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
