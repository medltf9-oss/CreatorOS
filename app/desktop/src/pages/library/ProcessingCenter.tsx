import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiRequestError } from '../../api';
import { formatRelative, LibraryApi, type JobRow } from '../../apiLibrary';
import { Badge, Button, Card, EmptyState, ErrorNote, Progress, Spinner } from '../../components/ui';

const STATE_FILTERS = ['all', 'queued', 'running', 'succeeded', 'failed', 'dead_letter', 'cancelled'] as const;

function toneFor(state: string): 'good' | 'bad' | 'info' | 'warn' | 'muted' {
  if (state === 'succeeded') return 'good';
  if (state === 'failed' || state === 'dead_letter') return 'bad';
  if (state === 'running') return 'info';
  if (state === 'cancelled') return 'warn';
  return 'muted';
}

export default function ProcessingCenter({
  library,
  focusJobId,
  onOpenAsset,
}: {
  library: LibraryApi;
  focusJobId: string | null;
  onOpenAsset: (assetId: string) => void;
}) {
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [total, setTotal] = useState(0);
  const [state, setState] = useState<(typeof STATE_FILTERS)[number]>('all');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<JobRow | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const page = await library.jobs({ state: state === 'all' ? undefined : state, limit: 50 });
      setJobs(page.items);
      setTotal(page.total);
      if (focusJobId) {
        const focused = page.items.find((job) => job.job_id === focusJobId);
        if (focused) setSelected(focused);
        else setSelected(await library.job(focusJobId).catch(() => null));
      }
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [library, state, focusJobId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    for (const job of jobs) result[job.state] = (result[job.state] ?? 0) + 1;
    return result;
  }, [jobs]);

  const act = async (jobId: string, action: 'cancel' | 'retry') => {
    setBusy(jobId);
    setError(null);
    try {
      if (action === 'cancel') await library.cancelJob(jobId);
      else await library.retryJob(jobId);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1 rounded-lg border border-slate-800 p-1">
          {STATE_FILTERS.map((entry) => (
            <button
              key={entry}
              type="button"
              onClick={() => setState(entry)}
              className={`rounded-md px-2.5 py-1 text-xs capitalize ${
                state === entry ? 'bg-slate-800 text-slate-100' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {entry.replace('_', ' ')}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-500">{total} job(s)</span>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" onClick={() => void load()}>
            Refresh
          </Button>
        </div>
      </div>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      <div className="grid gap-5 lg:grid-cols-3">
        <Card className="lg:col-span-2" title="Media queue" subtitle="Durable, leased, retried with backoff. Refreshed every 3 s.">
          {loading && jobs.length === 0 ? <Spinner label="Reading media jobs…" /> : null}
          {!loading && jobs.length === 0 ? (
            <EmptyState title="No jobs in this state" hint="Imports and derivative requests appear here." />
          ) : null}
          <ul className="divide-y divide-slate-800">
            {jobs.map((job) => (
              <li key={job.job_id} className="py-2">
                <button type="button" onClick={() => setSelected(job)} className="w-full text-left">
                  <div className="flex items-center gap-2 text-sm">
                    <span className="w-44 shrink-0 font-mono text-xs text-slate-400">{job.job_type}</span>
                    <Badge tone={toneFor(job.state)}>{job.state}</Badge>
                    <span className="min-w-0 flex-1 truncate text-xs text-slate-500">
                      {job.stage ?? job.last_error ?? job.job_id}
                    </span>
                    <span className="text-xs text-slate-500">{formatRelative(job.updated_at)}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <Progress
                      percent={Math.round((job.progress ?? 0) * 100)}
                      tone={
                        job.state === 'failed' || job.state === 'dead_letter'
                          ? 'bad'
                          : job.state === 'succeeded'
                            ? 'good'
                            : job.state === 'cancelled'
                              ? 'warn'
                              : 'info'
                      }
                    />
                    <span className="w-24 shrink-0 text-right font-mono text-[11px] text-slate-500">
                      {job.attempts}/{job.max_attempts}
                    </span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </Card>

        <div className="space-y-5">
          <Card title="By state">
            {Object.keys(counts).length === 0 ? <p className="text-sm text-slate-500">nothing to show</p> : null}
            {Object.entries(counts).map(([key, count]) => (
              <div key={key} className="flex items-center justify-between py-1 text-sm">
                <span className="text-slate-400">{key.replace('_', ' ')}</span>
                <span className="font-mono text-slate-200">{count}</span>
              </div>
            ))}
          </Card>

          <Card title="Job detail">
            {!selected ? (
              <p className="text-sm text-slate-500">Select a job to see its payload and error.</p>
            ) : (
              <div className="space-y-2 text-sm">
                <p className="break-all font-mono text-xs text-slate-400">{selected.job_id}</p>
                <div className="flex flex-wrap gap-1">
                  <Badge tone={toneFor(selected.state)}>{selected.state}</Badge>
                  <Badge>{selected.job_type}</Badge>
                  <Badge tone={selected.cancel_requested ? 'warn' : 'muted'}>
                    cancel {selected.cancel_requested ? 'requested' : 'no'}
                  </Badge>
                </div>
                <p className="text-xs text-slate-400">attempts {selected.attempts} of {selected.max_attempts}</p>
                {selected.last_error ? (
                  <p className="rounded border border-rose-500/30 bg-rose-500/10 p-2 font-mono text-[11px] text-rose-300">
                    {selected.last_error}
                  </p>
                ) : null}
                <pre className="max-h-48 overflow-auto rounded bg-slate-950 p-2 text-[11px] text-slate-400">
                  {JSON.stringify(selected.result, null, 2)}
                </pre>
                <div className="flex flex-wrap gap-2 pt-1">
                  {selected.state === 'queued' || selected.state === 'running' ? (
                    <Button variant="danger" busy={busy === selected.job_id} onClick={() => void act(selected.job_id, 'cancel')}>
                      Cancel
                    </Button>
                  ) : null}
                  {selected.state === 'failed' || selected.state === 'dead_letter' || selected.state === 'cancelled' ? (
                    <Button busy={busy === selected.job_id} onClick={() => void act(selected.job_id, 'retry')}>
                      Retry
                    </Button>
                  ) : null}
                  {selected.asset_id ? (
                    <Button variant="ghost" onClick={() => onOpenAsset(selected.asset_id!)}>
                      Open asset
                    </Button>
                  ) : null}
                </div>
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
