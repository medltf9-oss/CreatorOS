import { useCallback, useEffect, useState } from 'react';
import {
  type ActionGate,
  type ApprovalStatus,
  type JobDetail,
  type PublishingApi,
  STATE_LABELS,
  platformLabel,
  stateTone,
} from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Row, Spinner, formatBytes, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
  variantId: string;
  onBack: () => void;
};

type Actions = {
  state: string;
  publish_now: ActionGate;
  retry: ActionGate;
  cancel: ActionGate;
  reconcile: ActionGate;
  history: JobDetail;
};

export default function DestinationDetail({ api, variantId, onBack }: Props) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [actions, setActions] = useState<Actions | null>(null);
  const [approval, setApproval] = useState<ApprovalStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [detail, gates, approvalStatus] = await Promise.all([
        api.destination(variantId),
        api.destinationActions(variantId),
        api.approval(variantId),
      ]);
      setJob(detail.job);
      setActions(gates as unknown as Actions);
      setApproval(approvalStatus);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [api, variantId]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (label: string, call: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await call();
      setResult(`${label}: done`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!job) {
    return busy ? <Spinner label="Reading the destination…" /> : <ErrorNote error={error} onRetry={() => void load()} />;
  }

  const gate = (action: ActionGate | undefined) => ({
    disabled: !action?.enabled,
    title: action?.reason ?? undefined,
  });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <button className="text-xs text-slate-500 hover:underline" onClick={onBack}>
            ← back
          </button>
          <h1 className="text-lg font-semibold">
            {platformLabel(job.platform)} · {job.account_name ?? job.account_id}
          </h1>
          <p className="text-sm text-slate-400">
            {STATE_LABELS[job.state] ?? job.state}
            {job.state_reason ? ` — ${job.state_reason}` : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={stateTone(job.state)}>{STATE_LABELS[job.state] ?? job.state}</Badge>
          <Button
            variant="primary"
            {...gate(actions?.publish_now)}
            onClick={() => void act('publish now', () => api.publishNow(variantId))}
            busy={busy}
          >
            Publish now
          </Button>
          <Button {...gate(actions?.retry)} onClick={() => void act('retry', () => api.retry(variantId))} busy={busy}>
            Retry
          </Button>
          <Button {...gate(actions?.reconcile)} onClick={() => void act('reconcile', () => api.reconcile(variantId))} busy={busy}>
            Check provider
          </Button>
          <Button
            variant="danger"
            {...gate(actions?.cancel)}
            onClick={() => void act('cancel', () => api.cancel(variantId, note || undefined))}
            busy={busy}
          >
            Cancel
          </Button>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />
      {result ? <p className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-300">{result}</p> : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Content">
          <p className="whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm text-slate-200">
            {job.caption || '(no caption)'}
          </p>
          {job.hashtags.length ? <p className="mt-1 text-xs text-slate-500">{job.hashtags.map((tag) => `#${tag}`).join(' ')}</p> : null}
          <div className="mt-3">
            <Row label="Mode" value={job.mode} />
            <Row label="Attempts" value={job.attempts} />
            <Row label="Media bytes" value={formatBytes(Number(job.media?.bytes ?? 0))} />
            <Row label="Validation" value={String((job.variant as { validation?: { outcome?: string } })?.validation?.outcome ?? '—')} />
          </div>
        </Card>

        <Card title="Approval">
          {!approval ? (
            <Spinner label="Reading the approval…" />
          ) : approval.payload_hash ? (
            <>
              <Row label="State" value={approval.state} mono={false} />
              <Row label="Payload hash" value={approval.payload_hash.slice(0, 16)} />
              <Row label="Consent text" value={approval.consent_text_version} />
              <Row label="Approved" value={job.approval.approved ? 'yes' : 'no'} mono={false} />
              <p className="mt-2 text-xs text-slate-500">
                {approval.reason ?? 'The approval is bound to this exact payload; editing content invalidates it.'}
              </p>
            </>
          ) : (
            <>
              <p className="text-sm text-slate-400">
                {approval.can_request ? 'No approval has been requested for this destination yet.' : (approval.reason ?? 'This destination is past the approval stage.')}
              </p>
              <Button
                variant="primary"
                disabled={!approval.can_request}
                title={approval.can_request ? undefined : (approval.reason ?? 'not available in this state')}
                onClick={() => void act('request approval', () => api.requestApproval(variantId, {}))}
                busy={busy}
              >
                Request approval
              </Button>
            </>
          )}
        </Card>
      </div>

      <Card title="Timeline">
        <div className="space-y-4">
          <div>
            <p className="mb-1 text-xs uppercase tracking-wide text-slate-500">Attempts</p>
            {job.attempts_log.length === 0 ? (
              <EmptyState title="No attempt yet" hint="Nothing has been sent to the provider." />
            ) : (
              <ul className="space-y-2">
                {job.attempts_log.map((attempt) => (
                  <li key={attempt.id} className="rounded-lg border border-slate-800 p-2 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="text-slate-300">
                        #{attempt.attempt_no} · {attempt.provider_operation ?? 'publish'} · {attempt.outcome}
                      </span>
                      <span className="text-slate-500">{formatWhen(attempt.started_at)}</span>
                    </div>
                    {attempt.error_category ? <p className="mt-1 text-rose-400">{attempt.error_category}</p> : null}
                    {attempt.response_excerpt ? <p className="mt-1 text-slate-500">{attempt.response_excerpt}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <p className="mb-1 text-xs uppercase tracking-wide text-slate-500">Media delivery</p>
            {job.deliveries.length === 0 ? (
              <EmptyState title="No cloud copy" hint="Only Instagram needs one; Facebook and TikTok are uploaded directly." />
            ) : (
              <ul className="space-y-2">
                {job.deliveries.map((delivery) => (
                  <li key={delivery.id} className="rounded-lg border border-slate-800 p-2 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-slate-300">{delivery.object_key}</span>
                      <Badge tone={delivery.expired ? 'warn' : delivery.deleted_at ? 'muted' : 'info'}>{delivery.state}</Badge>
                    </div>
                    <p className="mt-1 text-slate-500">
                      {formatBytes(delivery.bytes)} · expires {formatWhen(delivery.expires_at)}
                      {delivery.deleted_at ? ` · deleted ${formatWhen(delivery.deleted_at)}` : ''}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <p className="mb-1 text-xs uppercase tracking-wide text-slate-500">Reconciliation</p>
            {job.reconciliations.length === 0 ? (
              <EmptyState title="Never needed" hint="A check is recorded every time the provider is asked what exists." />
            ) : (
              <ul className="space-y-2">
                {job.reconciliations.map((caseRow) => (
                  <li key={caseRow.id} className="rounded-lg border border-slate-800 p-2 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="text-slate-300">
                        {caseRow.decision} · provider said {caseRow.provider_state ?? 'nothing'}
                      </span>
                      <span className="text-slate-500">{formatWhen(caseRow.checked_at)}</span>
                    </div>
                    {caseRow.resolved_by ? <p className="mt-1 text-slate-500">resolved by {caseRow.resolved_by}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </Card>

      <Card title="Resolve by hand" subtitle="when the provider cannot be asked, or the post was deleted in its own app">
        <Field label="Note" hint="This is recorded with your name, and it moves the destination.">
          <input
            className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="I checked the Page and the post is there"
          />
        </Field>
        <div className="mt-3 flex gap-2">
          <Button
            disabled={!note.trim()}
            title={note.trim() ? undefined : 'A manual resolution needs a note explaining how you know'}
            onClick={() =>
              void act('resolve as published', () => api.resolveReconciliation(variantId, 'remote_exists', note.trim()))
            }
            busy={busy}
          >
            It is published
          </Button>
          <Button
            disabled={!note.trim()}
            title={note.trim() ? undefined : 'A manual resolution needs a note explaining how you know'}
            onClick={() =>
              void act('resolve as absent', () => api.resolveReconciliation(variantId, 'no_remote_object', note.trim()))
            }
            busy={busy}
          >
            It is not there
          </Button>
        </div>
      </Card>

      <Card title="Facts">
        <Row label="Destination" value={job.variant_id} />
        <Row label="Job" value={job.id} />
        <Row label="Provider id" value={job.provider_media_id ?? '—'} />
        <Row label="Permalink" value={job.provider_permalink ?? '—'} />
        <Row label="Created" value={formatWhen(job.created_at)} />
        <Row label="Lease" value={job.lease.held ? `${job.lease.owner} until ${formatWhen(job.lease.expires_at)}` : 'free'} />
      </Card>
    </div>
  );
}
