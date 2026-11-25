import { useCallback, useEffect, useState } from 'react';
import {
  type JobRow,
  type PublishingApi,
  STATE_LABELS,
  platformLabel,
  stateTone,
} from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Progress, Row, Spinner, formatBytes, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
  onOpenDestination: (variantId: string) => void;
};

export default function PublishQueue({ api, onOpenDestination }: Props) {
  const [queue, setQueue] = useState<{
    running: JobRow[];
    running_total: number;
    unknown: JobRow[];
    unknown_total: number;
    leases_held: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setQueue(await api.queue());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (variantId: string, what: 'reconcile' | 'retry') => {
    setAction(variantId);
    try {
      const result = what === 'reconcile' ? await api.reconcile(variantId) : await api.retry(variantId);
      setError(`${result.action}: ${result.reason ?? 'done'} (state ${result.state})`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAction(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
            <p className="text-sm text-slate-400">
            Uploads run on their own worker lane. A destination that is on the way to the provider cannot be cancelled: reconcile
            it instead.
          </p>
        </div>
        <Button onClick={() => void load()} busy={busy}>
          Refresh
        </Button>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      {!queue ? (
        <Spinner label="Reading the queue…" />
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            <Card title="In flight">
              <p className="text-2xl font-semibold">{queue.running_total}</p>
              <p className="text-xs text-slate-500">{queue.leases_held} lease(s) held by a worker</p>
            </Card>
            <Card title="Unknown remote state">
              <p className={`text-2xl font-semibold ${queue.unknown_total ? 'text-amber-400' : ''}`}>{queue.unknown_total}</p>
              <p className="text-xs text-slate-500">needs a reconciliation check before anything else happens</p>
            </Card>
            <Card title="How publishing runs">
              <p className="text-xs leading-relaxed text-slate-400">
                A human approval is spent before the first provider call. Provider ids are written as they arrive, so an
                interrupted upload resumes instead of starting again.
              </p>
            </Card>
          </div>

          <Card title="In flight" subtitle="progress comes from the provider's own byte offsets">
            {queue.running.length === 0 ? (
              <EmptyState title="Nothing is uploading" />
            ) : (
              <ul className="space-y-3">
                {queue.running.map((row) => (
                  <li key={row.id}>
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-sm text-slate-100">
                          {platformLabel(row.platform)} · {row.account_name ?? row.account_id}
                        </p>
                        <p className="text-xs text-slate-500">
                          {row.state_reason ?? STATE_LABELS[row.state]} · attempt {row.attempts}
                          {row.lease.held ? ` · worker ${row.lease.owner}` : ''}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <Badge tone={stateTone(row.state)}>{STATE_LABELS[row.state] ?? row.state}</Badge>
                        <Button variant="ghost" onClick={() => onOpenDestination(row.variant_id)}>
                          Details
                        </Button>
                      </div>
                    </div>
                    {row.upload.percent !== null ? (
                      <div className="mt-2">
                        <Progress percent={row.upload.percent} />
                        <p className="mt-1 text-[11px] text-slate-500">
                          {formatBytes(row.upload.bytes_done)} of {formatBytes(row.upload.bytes_total)}
                        </p>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Unknown remote state" subtitle="the only honest exit is to look, then decide">
            {queue.unknown.length === 0 ? (
              <EmptyState title="Every destination has a known state" />
            ) : (
              <ul className="divide-y divide-slate-800">
                {queue.unknown.map((row) => (
                  <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <div className="min-w-0">
                      <p className="text-sm text-slate-100">
                        {platformLabel(row.platform)} · {row.account_name ?? row.account_id}
                      </p>
                      <p className="text-xs text-slate-500">
                        {row.last_error_code ?? row.state_reason ?? 'no detail'} · {formatWhen(row.updated_at)}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        onClick={() => void run(row.variant_id, 'reconcile')}
                        busy={action === row.variant_id}
                        disabled={!row.provider_media_id}
                        title={row.provider_media_id ? 'Ask the provider what exists' : 'No provider id was recorded, so there is nothing to look up'}
                      >
                        Check provider
                      </Button>
                      <Button variant="ghost" onClick={() => onOpenDestination(row.variant_id)}>
                        Details
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Worker lane">
            <Row label="Queue" value="publish" />
            <Row label="Default workers" value="1 (CREATOROS_PUBLISH_WORKERS)" />
            <Row label="Sweeper" value="every 15s (CREATOROS_SCHEDULER_SECONDS)" />
          </Card>
        </>
      )}
    </div>
  );
}
