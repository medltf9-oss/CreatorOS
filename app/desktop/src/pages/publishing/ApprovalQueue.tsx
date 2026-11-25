import { useCallback, useEffect, useState } from 'react';
import { type ApprovalStatus, type JobRow, type PublishingApi, platformLabel } from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Row, Spinner, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
  onOpenDestination: (variantId: string) => void;
};

/** Approve or reject what is about to be published.
 *
 * The payload shown here is the exact document the backend hash-binds to the consent, so a person
 * can see the caption, the account and the media before anything leaves the machine.
 */
export default function ApprovalQueue({ api, onOpenDestination }: Props) {
  const [rows, setRows] = useState<JobRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [status, setStatus] = useState<ApprovalStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const page = await api.jobs({ state: 'awaiting_approval', limit: 50 });
      setRows(page.items);
      setSelected((current) => current ?? page.items[0]?.variant_id ?? null);
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

  useEffect(() => {
    if (!selected) {
      setStatus(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const next = await api.approval(selected);
        if (!cancelled) setStatus(next);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, selected]);

  const decide = async (approve: boolean) => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.decideApproval(selected, approve);
      setStatus(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const payload = status?.payload as
    | {
        content?: { caption?: string; title?: string | null; hashtags?: string[] };
        platform?: string;
        publish_mode?: string;
        account?: { display_name?: string };
        validation?: { outcome?: string; codes?: string[] };
        media?: { asset_sha256?: string | null; derivative_profile?: string | null };
        schedule?: { scheduled_at_utc?: string | null; display_timezone?: string };
      }
    | null
    | undefined;

  return (
    <div className="space-y-5">
      <div>
        <p className="text-sm text-slate-400">
          An approval is bound to a hash of the payload. If anything changes afterwards, the approval stops being valid and the
          destination goes back through validation.
        </p>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`Waiting for a decision (${rows.length})`}>
          {busy && rows.length === 0 ? (
            <Spinner label="Reading pending approvals…" />
          ) : rows.length === 0 ? (
            <EmptyState title="Nothing is waiting" hint="A destination appears here once a human asks for approval." />
          ) : (
            <ul className="divide-y divide-slate-800">
              {rows.map((row) => (
                <li key={row.id} className="py-2">
                  <button
                    className={`w-full rounded-lg px-2 py-1 text-left ${selected === row.variant_id ? 'bg-slate-800' : 'hover:bg-slate-900'}`}
                    onClick={() => setSelected(row.variant_id)}
                  >
                    <p className="text-sm text-slate-100">
                      {platformLabel(row.platform)} · {row.account_name ?? row.account_id}
                    </p>
                    <p className="truncate text-xs text-slate-500">{row.caption || '(no caption)'}</p>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="What would be published"
          subtitle={status ? `consent text ${status.consent_text_version}` : undefined}
          actions={
            selected ? (
              <Button variant="ghost" onClick={() => onOpenDestination(selected)}>
                Open destination
              </Button>
            ) : undefined
          }
        >
          {!selected ? (
            <EmptyState title="Select a destination" />
          ) : !status || !status.payload ? (
            <Spinner label="Reading the payload…" />
          ) : (
            <div className="space-y-3">
              <div>
                <p className="text-xs uppercase tracking-wide text-slate-500">Caption</p>
                <p className="mt-1 whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm text-slate-200">
                  {payload?.content?.caption || '(empty)'}
                </p>
                {payload?.content?.hashtags?.length ? (
                  <p className="mt-1 text-xs text-slate-500">{payload.content.hashtags.map((tag) => `#${tag}`).join(' ')}</p>
                ) : null}
              </div>
              <Row label="Account" value={payload?.account?.display_name ?? '—'} mono={false} />
              <Row label="Surface" value={`${payload?.platform} / ${payload?.publish_mode}`} />
              <Row label="Validation" value={payload?.validation?.outcome ?? '—'} mono={false} />
              <Row label="Media hash" value={payload?.media?.derivative_profile ?? payload?.media?.asset_sha256?.slice(0, 16) ?? '—'} />
              <Row
                label="Scheduled"
                value={
                  payload?.schedule?.scheduled_at_utc
                    ? formatWhen(payload.schedule.scheduled_at_utc, payload.schedule.display_timezone)
                    : 'as soon as approved'
                }
                mono={false}
              />
              <Row label="Payload hash" value={status.payload_hash?.slice(0, 16) ?? '—'} />
              <div className="flex items-center gap-2 pt-1">
                <Button variant="primary" onClick={() => void decide(true)} busy={busy}>
                  Approve this exactly
                </Button>
                <Button variant="danger" onClick={() => void decide(false)} busy={busy}>
                  Reject
                </Button>
                {!status.can_request ? <Badge tone="muted">past the approval stage</Badge> : null}
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
