import { useCallback, useEffect, useState } from 'react';
import { type JobRow, type PublishingApi, STATE_LABELS, platformLabel, stateTone } from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Spinner, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
  onOpenDestination: (variantId: string) => void;
};

/** Everything that is waiting for a person, with the reason it is waiting.
 *
 * This is the page that keeps the promise "a control works or it is disabled with a stated reason":
 * every row carries the destination's own `state_reason`, and every button here is one the backend
 * will accept in that state.
 */
export default function AttentionList({ api, onOpenDestination }: Props) {
  const [rows, setRows] = useState<JobRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const page = await api.attention();
      setRows(page.items);
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
      const outcome = what === 'reconcile' ? await api.reconcile(variantId) : await api.retry(variantId);
      setError(`${outcome.action}: ${outcome.reason ?? 'done'} → ${outcome.state}`);
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
            Destinations that stopped for a reason: a missed schedule, a revoked approval, a failure, or an unknown remote state.
          </p>
        </div>
        <Button onClick={() => void load()} busy={busy}>
          Refresh
        </Button>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      <Card title={`Waiting (${rows.length})`}>
        {busy && rows.length === 0 ? (
          <Spinner label="Reading the attention list…" />
        ) : rows.length === 0 ? (
          <EmptyState title="Nothing needs a decision" hint="Every destination is either in progress or finished." />
        ) : (
          <ul className="divide-y divide-slate-800">
            {rows.map((row) => (
              <li key={row.id} className="py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm text-slate-100">
                      {platformLabel(row.platform)} · {row.account_name ?? row.account_id}
                    </p>
                    <p className="truncate text-xs text-slate-500">{row.caption || '(no caption)'}</p>
                    <p className="mt-0.5 text-[11px] text-amber-400">{row.state_reason ?? row.last_error_detail ?? 'no reason recorded'}</p>
                    {row.missed_at ? (
                      <p className="text-[11px] text-slate-600">
                        missed {formatWhen(row.missed_at, row.display_timezone)} · was due{' '}
                        {formatWhen(row.scheduled_at_utc, row.display_timezone)}
                      </p>
                    ) : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={stateTone(row.state)}>{STATE_LABELS[row.state] ?? row.state}</Badge>
                    <Button
                      onClick={() => void run(row.variant_id, 'reconcile')}
                      busy={action === row.variant_id}
                      disabled={!row.provider_media_id}
                      title={row.provider_media_id ? 'Ask the provider what exists' : 'No provider id was recorded, so there is nothing to look up'}
                    >
                      Check provider
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => void run(row.variant_id, 'retry')}
                      busy={action === row.variant_id}
                      title="A retry needs a live approval; without one the destination goes back to draft"
                    >
                      Retry
                    </Button>
                    <Button variant="ghost" onClick={() => onOpenDestination(row.variant_id)}>
                      Open
                    </Button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
