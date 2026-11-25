import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote } from '../../components/ui';
import type { AgentDefinition, Department, WorkforceApi } from '../../apiWorkforce';

export default function Organization({
  wf,
  onOpenAgent,
}: {
  wf: WorkforceApi;
  onOpenAgent: (id: string) => void;
}) {
  const [chief, setChief] = useState<AgentDefinition | null>(null);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [chiefRow, departmentRows] = await Promise.all([
        wf.agent('chief_orchestrator'),
        wf.departments(),
      ]);
      setChief(chiefRow);
      const detailed = await Promise.all(departmentRows.map((department) => wf.department(department.id)));
      setDetailed(departmentRows, detailed);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }

    function setDetailed(_rows: Department[], detailedRows: Department[]) {
      setDepartments(detailedRows);
    }
  }, [wf]);

  useEffect(() => {
    void load();
  }, [load]);

  async function toggleDepartment(id: string, enabled: boolean) {
    setBusy(id);
    try {
      await wf.setDepartmentState(id, enabled);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <ErrorNote error={error} onRetry={() => void load()} />

      {chief ? (
        <Card
          title="Executive"
          subtitle="Receives the objective, plans it, delegates to departments and compiles the verified result."
          actions={<Badge tone={chief.enabled ? 'good' : 'bad'}>{chief.enabled ? 'enabled' : 'disabled'}</Badge>}
        >
          <button
            onClick={() => onOpenAgent(chief.agent_id)}
            className="w-full rounded-lg border border-sky-500/30 bg-sky-500/5 px-4 py-3 text-left transition hover:border-sky-400/60"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-semibold text-slate-100">{chief.name}</span>
              <span className="font-mono text-[11px] text-slate-500">{chief.agent_id}</span>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">{chief.mission}</p>
            <div className="mt-2 flex flex-wrap gap-1">
              <Badge tone="info">{chief.model_profile}</Badge>
              <Badge tone="muted">depth 0</Badge>
              <Badge tone="muted">v{chief.config_version}</Badge>
              {chief.allowed_tools.slice(0, 4).map((tool) => (
                <Badge key={tool} tone="muted">
                  {tool}
                </Badge>
              ))}
            </div>
          </button>
        </Card>
      ) : null}

      <Card title="Departments → leaders → specialists" subtitle="Click any agent to open its full configuration.">
        {departments.length === 0 ? (
          <Empty>Loading the org chart…</Empty>
        ) : (
          <ul className="space-y-3">
            {departments.map((department) => {
              const leader = department.agents?.find((agent) => agent.agent_id === department.leader_agent_id);
              const specialists = (department.agents ?? []).filter(
                (agent) => agent.agent_id !== department.leader_agent_id,
              );
              return (
                <li key={department.id} className="rounded-xl border border-slate-800 bg-slate-950/30 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <span className="text-sm font-semibold text-slate-100">{department.name}</span>
                      <span className="ml-2 font-mono text-[11px] text-slate-600">{department.id}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge tone={department.enabled ? 'good' : 'bad'}>
                        {department.enabled_count}/{department.agent_count} enabled
                      </Badge>
                      <Button
                        variant={department.enabled ? 'danger' : 'default'}
                        busy={busy === department.id}
                        onClick={() => void toggleDepartment(department.id, !department.enabled)}
                        title="Disabling a department stops its agents from being assigned work."
                      >
                        {department.enabled ? 'disable' : 'enable'}
                      </Button>
                    </div>
                  </div>
                  <p className="mt-1 text-[11px] text-slate-500" title={department.mission}>{department.mission}</p>

                  <div className="mt-3 flex flex-col gap-2 md:flex-row md:items-start">
                    {leader ? (
                      <button
                        onClick={() => onOpenAgent(leader.agent_id)}
                        className="w-full rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-left transition hover:border-sky-500/50 md:w-64"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-semibold text-slate-100">{leader.name}</span>
                          <Badge tone="info">leader</Badge>
                        </div>
                        <p className="mt-1 line-clamp-2 text-[11px] text-slate-500" title={leader.mission}>{leader.mission}</p>
                      </button>
                    ) : null}
                    <div className="flex flex-1 flex-wrap gap-1">
                      {specialists.map((agent) => (
                        <button
                          key={agent.agent_id}
                          onClick={() => onOpenAgent(agent.agent_id)}
                          title={agent.mission}
                          className={`rounded-md border px-2 py-1 text-[11px] transition ${
                            agent.enabled
                              ? 'border-slate-800 bg-slate-900/70 text-slate-300 hover:border-slate-600'
                              : 'border-rose-500/30 bg-rose-500/5 text-rose-300'
                          }`}
                        >
                          {agent.name}
                        </button>
                      ))}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}
