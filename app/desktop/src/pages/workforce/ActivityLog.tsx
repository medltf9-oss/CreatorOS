import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, formatAge } from '../../components/ui';
import type { ActivityRow, WorkforceApi } from '../../apiWorkforce';

const ACTIONS = [
  '',
  'agent.objective.create',
  'agent.objective.request',
  'agent.objective.finish',
  'agent.objective.cancel',
  'agent.task.cancel',
  'agent.tool.call',
  'agent.tool.manual_run',
  'agent.memory.write',
  'agent.memory.rejected',
  'agent.memory.retention',
  'agent.registry.update',
  'agent.registry.state',
  'agent.department.state',
  'approval.request',
  'approval.decide',
  'approval.cancel',
];

export default function ActivityLog({ wf }: { wf: WorkforceApi }) {
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [action, setAction] = useState('');
  const [limit, setLimit] = useState(100);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRows(await wf.activity({ action: action || undefined, limit }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [wf, action, limit]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} onRetry={() => void load()} />

      <Card
        title="Activity log"
        subtitle="Append-only audit events written by the backend. Nothing here is client-generated."
        actions={<Badge tone="muted">{rows.length} events</Badge>}
      >
        <div className="flex flex-wrap items-center gap-3">
          <select
            value={action}
            onChange={(event) => setAction(event.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200 outline-none focus:border-sky-500"
          >
            {ACTIONS.map((value) => (
              <option key={value || 'all'} value={value}>
                {value || 'all agent actions'}
              </option>
            ))}
          </select>
          <select
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value))}
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200 outline-none focus:border-sky-500"
          >
            {[50, 100, 200, 300].map((value) => (
              <option key={value} value={value}>
                last {value}
              </option>
            ))}
          </select>
          <Button variant="ghost" onClick={() => void load()}>
            refresh
          </Button>
        </div>
      </Card>

      <Card title="Events">
        {rows.length === 0 ? (
          <Empty>No audit events match. Submitting an objective will create some.</Empty>
        ) : (
          <ul className="divide-y divide-slate-800/70">
            {rows.map((row) => (
              <li key={row.event_id} className="py-2">
                <button
                  onClick={() => setExpanded(expanded === row.event_id ? null : row.event_id)}
                  className="flex w-full flex-wrap items-center justify-between gap-2 text-left"
                >
                  <div className="flex items-center gap-2">
                    <Badge
                      tone={
                        row.outcome === 'success'
                          ? 'good'
                          : row.outcome === 'denied'
                            ? 'bad'
                            : row.outcome === 'failure'
                              ? 'warn'
                              : 'muted'
                      }
                    >
                      {row.outcome}
                    </Badge>
                    <span className="font-mono text-xs text-slate-200">{row.action}</span>
                  </div>
                  <div className="flex items-center gap-3 text-[11px] text-slate-500">
                    <span className="font-mono">{row.actor}</span>
                    <span className="font-mono">{row.resource_type}</span>
                    {row.resource_id ? <span className="max-w-[16ch] truncate font-mono" title={row.resource_id}>{row.resource_id}</span> : null}
                    <span>{formatAge(row.created_at)}</span>
                  </div>
                </button>
                {expanded === row.event_id ? (
                  <pre className="mt-2 max-h-56 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-400">
                    {JSON.stringify(row.metadata, null, 2)}
                  </pre>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
