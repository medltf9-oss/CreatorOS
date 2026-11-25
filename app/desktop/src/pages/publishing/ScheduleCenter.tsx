import { useCallback, useEffect, useState } from 'react';
import {
  type JobRow,
  type PublishingApi,
  type SchedulePolicy,
  STATE_LABELS,
  platformLabel,
  stateTone,
} from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Row, Spinner, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
  onOpenDestination: (variantId: string) => void;
};

const POLICIES: { id: SchedulePolicy['policy']; label: string; hint: string }[] = [
  { id: 'ask', label: 'Ask me', hint: 'The destination stops and waits for a decision. Nothing is published late.' },
  { id: 'publish_now', label: 'Publish late', hint: 'Queue it immediately and record how late it was.' },
  { id: 'skip', label: 'Stop and propose a new time', hint: 'Nothing is published; the destination asks to be rescheduled.' },
];

export default function ScheduleCenter({ api, onOpenDestination }: Props) {
  const [scheduled, setScheduled] = useState<JobRow[]>([]);
  const [missed, setMissed] = useState<JobRow[]>([]);
  const [policy, setPolicy] = useState<SchedulePolicy>({ policy: 'ask', grace_seconds: 120 });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sweepResult, setSweepResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const view = await api.scheduleView(14);
      setScheduled(view.scheduled);
      setMissed(view.missed);
      setPolicy(view.policy);
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

  const savePolicy = async (next: SchedulePolicy['policy']) => {
    try {
      setPolicy(await api.setPolicy({ ...policy, policy: next }));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const sweep = async () => {
    try {
      const result = await api.sweep();
      setSweepResult(`scanned ${result.scanned}: ${result.enqueued} queued, ${result.missed} missed, ${result.future} still ahead`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const publishMissed = async (variantId: string) => {
    try {
      const result = await api.publishMissedNow(variantId);
      setSweepResult(`${result.action}: ${result.reason} (state ${result.state})`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
            <p className="text-sm text-slate-400">
            Times are stored as absolute instants plus the zone you chose, so a daylight-saving change never moves a post.
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => void sweep()} busy={busy} title="What the sweeper thread does every 15 seconds">
            Sweep now
          </Button>
          <Button variant="ghost" onClick={() => void load()} busy={busy}>
            Refresh
          </Button>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />
      {sweepResult ? (
        <p className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-300">{sweepResult}</p>
      ) : null}

      <Card title="Missed schedules" subtitle="the time passed while the application was closed">
        {missed.length === 0 ? (
          <EmptyState title="No missed schedule" />
        ) : (
          <ul className="divide-y divide-slate-800">
            {missed.map((row) => (
              <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div className="min-w-0">
                  <p className="text-sm text-slate-100">
                    {platformLabel(row.platform)} · {row.account_name ?? row.account_id}
                  </p>
                  <p className="text-xs text-slate-500">
                    was due {formatWhen(row.scheduled_at_utc, row.display_timezone)} · {row.state_reason ?? 'missed'}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Button onClick={() => void publishMissed(row.variant_id)} busy={busy}>
                    Publish now
                  </Button>
                  <Button variant="ghost" onClick={() => onOpenDestination(row.variant_id)}>
                    Reschedule
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Upcoming" subtitle="the next 14 days">
        {scheduled.length === 0 ? (
          <EmptyState title="Nothing scheduled" hint="Open a destination and set a time; the approval stays separate." />
        ) : (
          <ul className="divide-y divide-slate-800">
            {scheduled.map((row) => (
              <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div className="min-w-0">
                  <p className="text-sm text-slate-100">
                    {platformLabel(row.platform)} · {row.account_name ?? row.account_id}
                  </p>
                  <p className="text-xs text-slate-500">{row.caption || '(no caption)'}</p>
                </div>
                <div className="flex items-center gap-2 text-xs text-slate-400">
                  <span>{formatWhen(row.scheduled_at_utc, row.display_timezone)}</span>
                  <Badge tone={stateTone(row.state)}>{STATE_LABELS[row.state] ?? row.state}</Badge>
                  <Button variant="ghost" onClick={() => onOpenDestination(row.variant_id)}>
                    Open
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Late policy" subtitle="what happens when a scheduled time is missed">
        <div className="grid gap-3 md:grid-cols-3">
          {POLICIES.map((entry) => (
            <button
              key={entry.id}
              onClick={() => void savePolicy(entry.id)}
              className={`rounded-lg border p-3 text-left transition ${
                policy.policy === entry.id
                  ? 'border-sky-500/60 bg-sky-500/10'
                  : 'border-slate-800 bg-slate-950/40 hover:border-slate-700'
              }`}
            >
              <p className="text-sm font-medium text-slate-100">{entry.label}</p>
              <p className="mt-1 text-xs text-slate-400">{entry.hint}</p>
            </button>
          ))}
        </div>
        <div className="mt-3">
          <Row label="Current policy" value={policy.policy} />
          <Row label="Grace window" value={`${policy.grace_seconds}s`} />
        </div>
        <p className="mt-2 text-xs text-slate-500">
          None of these publish silently: `ask` and `skip` stop and wait for a person, and `publish late` records how late it was.
        </p>
      </Card>

      {busy ? <Spinner label="Reading the schedule…" /> : null}
    </div>
  );
}
