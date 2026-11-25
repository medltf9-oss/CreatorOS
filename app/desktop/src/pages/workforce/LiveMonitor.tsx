import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, Progress, Stat, formatAge } from '../../components/ui';
import type { AgentMetrics, MonitorPayload, WorkforceApi } from '../../apiWorkforce';

const ACTIVE_STATES = new Set(['pending', 'planning', 'running', 'compiling']);

export default function LiveMonitor({
  wf,
  onOpenObjective,
}: {
  wf: WorkforceApi;
  onOpenObjective: (id: string) => void;
}) {
  const [monitor, setMonitor] = useState<MonitorPayload | null>(null);
  const [metrics, setMetrics] = useState<AgentMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [nextMonitor, nextMetrics] = await Promise.all([wf.monitor(), wf.metrics()]);
      setMonitor(nextMonitor);
      setMetrics(nextMetrics);
      setLastUpdated(new Date().toISOString());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [wf]);

  useEffect(() => {
    void load();
    if (paused) return;
    const timer = window.setInterval(() => void load(), 2000);
    return () => window.clearInterval(timer);
  }, [load, paused]);

  const queueDepth = Object.entries(monitor?.queue_states ?? {}).reduce(
    (total, [state, count]) => (state === 'queued' || state === 'claimed' || state === 'running' ? total + count : total),
    0,
  );

  return (
    <div className="space-y-4">
      <ErrorNote error={error} onRetry={() => void load()} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Badge tone={paused ? 'warn' : 'good'}>{paused ? 'paused' : 'polling every 2s'}</Badge>
          <span className="text-[11px] text-slate-500">snapshot {lastUpdated ? formatAge(lastUpdated) : '-'}</span>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => setPaused((value) => !value)}>
            {paused ? 'resume' : 'pause'}
          </Button>
          <Button variant="ghost" onClick={() => void load()}>
            refresh now
          </Button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Active objectives" value={monitor?.active_objectives.length ?? 0} />
        <Stat label="Running tasks" value={monitor?.active_tasks.length ?? 0} />
        <Stat label="Queue depth" value={queueDepth} tone={queueDepth > 0 ? 'warn' : undefined} />
        <Stat label="Tasks total" value={Object.values(monitor?.task_states ?? {}).reduce((a, b) => a + b, 0)} />
        <Stat label="Tokens used" value={metrics?.tokens_total ?? 0} />
        <Stat label="Concurrency cap" value={metrics?.concurrency_limit ?? 1} />
      </div>

      <Card title="Active objectives" subtitle="Only objectives in a non-terminal state appear here.">
        {!monitor || monitor.active_objectives.length === 0 ? (
          <Empty>Nothing is running. The workforce is idle, and this view says so rather than inventing progress.</Empty>
        ) : (
          <ul className="space-y-3">
            {monitor.active_objectives.map((objective) => (
              <li key={objective.objective_id} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="truncate text-sm text-slate-100" title={objective.objective}>{objective.objective}</span>
                  <div className="flex items-center gap-2">
                    <Badge tone="info">{objective.state}</Badge>
                    <Button variant="ghost" onClick={() => onOpenObjective(objective.objective_id)}>
                      graph
                    </Button>
                  </div>
                </div>
                <div className="mt-2 grid gap-2 text-[11px] text-slate-500 sm:grid-cols-4">
                  <span>{objective.progress.task_count} tasks</span>
                  <span>{objective.progress.terminal} terminal</span>
                  <span>{objective.calls_used} calls</span>
                  <span>{objective.tokens_used} tokens</span>
                </div>
                <div className="mt-2">
                  <Progress
                    percent={
                      objective.progress.task_count === 0
                        ? 0
                        : (objective.progress.terminal / objective.progress.task_count) * 100
                    }
                  />
                </div>
                {objective.running_task ? (
                  <p className="mt-2 font-mono text-[11px] text-sky-300">
                    running: {objective.running_task.agent_id} · {objective.running_task.title ?? objective.running_task.task_type}
                  </p>
                ) : (
                  <p className="mt-2 font-mono text-[11px] text-slate-500">no task currently executing</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Task states" subtitle="Across every objective ever run.">
          {!monitor || Object.keys(monitor.task_states).length === 0 ? (
            <Empty>No tasks recorded yet.</Empty>
          ) : (
            <ul className="space-y-2">
              {Object.entries(monitor.task_states)
                .sort((a, b) => b[1] - a[1])
                .map(([state, count]) => {
                  const total = Object.values(monitor.task_states).reduce((a, b) => a + b, 0);
                  return (
                    <li key={state}>
                      <div className="flex items-center justify-between text-xs text-slate-400">
                        <span>{state}</span>
                        <span className="font-mono">{count}</span>
                      </div>
                      <Progress percent={(count / total) * 100} />
                    </li>
                  );
                })}
            </ul>
          )}
        </Card>

        <Card title="Running tasks detail">
          {!monitor || monitor.active_tasks.length === 0 ? (
            <Empty>No task is RUNNING or WAITING right now.</Empty>
          ) : (
            <ul className="space-y-2">
              {monitor.active_tasks.map((task) => (
                <li key={task.task_id} className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm text-slate-200">{task.agent_id}</span>
                    <Badge tone="warn">{task.state}</Badge>
                  </div>
                  <p className="mt-1 text-[11px] text-slate-500">{task.title ?? task.task_type}</p>
                  <p className="font-mono text-[11px] text-slate-600">
                    depth {task.depth} · started {formatAge(task.created_at)}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {metrics ? (
        <Card title="Throughput by department" subtitle="Completed and failed tasks counted from agent_task.">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries(metrics.tasks_by_department)
              .sort((a, b) => b[1] - a[1])
              .map(([department, count]) => (
                <div key={department} className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                  <span className="text-xs text-slate-400">{department}</span>
                  <span className="font-mono text-sm text-slate-200">{count}</span>
                </div>
              ))}
          </div>
          <p className="mt-3 text-[11px] text-slate-500">
            objectives: {Object.entries(metrics.objectives_by_state).map(([state, count]) => `${state} ${count}`).join(' · ') || 'none'}
          </p>
        </Card>
      ) : null}

      {monitor && monitor.active_objectives.length === 0 ? (
        <p className="text-center text-[11px] text-slate-600">
          {ACTIVE_STATES.size} active-state kinds are polled; terminal objectives move to the Task Graph view.
        </p>
      ) : null}
    </div>
  );
}
