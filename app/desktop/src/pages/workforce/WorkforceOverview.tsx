import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, Progress, Stat, formatAge } from '../../components/ui';
import type { AgentSummary, Department, Objective, WorkforceApi } from '../../apiWorkforce';

const STATE_TONES: Record<string, 'good' | 'warn' | 'bad' | 'muted' | 'info'> = {
  succeeded: 'good',
  partial: 'warn',
  failed: 'bad',
  cancelled: 'muted',
  blocked: 'bad',
  running: 'info',
  planning: 'info',
  compiling: 'info',
  pending: 'muted',
};

function stateTone(state: string) {
  return STATE_TONES[state] ?? 'muted';
}

export default function WorkforceOverview({
  wf,
  onOpenObjective,
}: {
  wf: WorkforceApi;
  onOpenObjective: (id: string) => void;
}) {
  const [summary, setSummary] = useState<AgentSummary | null>(null);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [objectives, setObjectives] = useState<Objective[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [nextSummary, nextDepartments, nextObjectives] = await Promise.all([
        wf.summary(),
        wf.departments(),
        wf.objectives(),
      ]);
      setSummary(nextSummary);
      setDepartments(nextDepartments);
      setObjectives(nextObjectives);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [wf]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [load]);

  const limits = summary?.delegation_limits ?? {};

  return (
    <div className="space-y-5">
      <ErrorNote error={error} onRetry={() => void load()} />

      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Agents" value={summary?.agents_total ?? (loading ? '…' : 0)} tone={summary ? 'good' : undefined} />
        <Stat label="Chiefs" value={summary?.chiefs ?? (loading ? '…' : 0)} />
        <Stat label="Leaders" value={summary?.leaders ?? (loading ? '…' : 0)} />
        <Stat label="Specialists" value={summary?.specialists ?? (loading ? '…' : 0)} />
        <Stat label="Departments" value={summary?.departments ?? (loading ? '…' : 0)} />
        <Stat
          label="Enabled"
          value={summary ? `${summary.agents_enabled}/${summary.agents_total}` : '…'}
          tone={summary && summary.agents_enabled < summary.agents_total ? 'warn' : undefined}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card
          title="Delegation limits in force"
          subtitle="Stored with each objective at creation time; never read from the environment during a run."
          actions={<Badge tone="info">concurrency {limits.concurrency ?? 1}</Badge>}
        >
          <dl className="grid grid-cols-2 gap-3 text-sm">
            {[
              ['Max depth', limits.max_depth],
              ['Max model calls', limits.max_calls],
              ['Max revisions', limits.max_revisions],
              ['Wall clock (s)', limits.wall_clock_s],
              ['Departments / objective', limits.max_departments],
              ['Specialists / department', limits.specialists_per_department],
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                <dt className="text-[11px] uppercase tracking-wide text-slate-500">{label}</dt>
                <dd className="font-mono text-base text-slate-100">{value ?? '…'}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-slate-500">
            One generation at a time is a hardware limit (GTX 1050 Ti, ~3.3 GiB usable VRAM), not a preference. The
            governor rejects a second concurrent request rather than degrading both.
          </p>
        </Card>

        <Card
          title="Recent objectives"
          subtitle="Everything below is read from agent_objective; nothing is estimated."
          actions={
            <Button variant="ghost" onClick={() => void load()}>
              refresh
            </Button>
          }
        >
          {objectives.length === 0 && !loading ? (
            <Empty>No objectives yet. Open the Playground to submit one.</Empty>
          ) : (
            <ul className="space-y-2">
              {objectives.slice(0, 6).map((objective) => (
                <li key={objective.objective_id}>
                  <button
                    onClick={() => onOpenObjective(objective.objective_id)}
                    className="w-full rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-left transition hover:border-slate-700"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="truncate text-sm text-slate-200" title={objective.objective}>{objective.objective}</span>
                      <Badge tone={stateTone(objective.state)}>{objective.state}</Badge>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px] text-slate-500">
                      <span>{objective.progress.task_count} tasks</span>
                      <span>{objective.calls_used} calls</span>
                      <span>{objective.tokens_used} tokens</span>
                      <span>{formatAge(objective.created_at)}</span>
                    </div>
                    <div className="mt-2">
                      <Progress
                        percent={
                          objective.progress.task_count === 0
                            ? 0
                            : (objective.progress.terminal / objective.progress.task_count) * 100
                        }
                        tone={objective.state === 'failed' ? 'bad' : objective.state === 'succeeded' ? 'good' : 'info'}
                      />
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title="Departments"
        subtitle="10 departments, 11 agents each (1 leader + 10 specialists)."
        actions={<Badge tone="muted">{summary?.tools_registered ?? 0} tools registered</Badge>}
      >
        <div className="grid gap-2 md:grid-cols-2">
          {departments.map((department) => (
            <div key={department.id} className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-slate-200">{department.name}</span>
                <Badge tone={department.enabled ? 'good' : 'bad'}>
                  {department.enabled_count}/{department.agent_count}
                </Badge>
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{department.mission}</p>
              <p className="mt-1 font-mono text-[11px] text-slate-600">leader: {department.leader_agent_id}</p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
