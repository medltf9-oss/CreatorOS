import { useCallback, useEffect, useState } from 'react';
import { type Overview, type PublishingApi, STATE_LABELS, type PublishState, platformLabel } from '../../apiPublishing';
import { Badge, Card, EmptyState, ErrorNote, Spinner } from '../../components/ui';

type Props = {
  api: PublishingApi;
  onOpenComposer: () => void;
  onOpenQueue: () => void;
  onOpenDelivery: () => void;
  onOpenAttention: () => void;
};

const ATTENTION: PublishState[] = ['requires_user_action', 'unknown_remote_state', 'needs_reconciliation', 'blocked'];

export default function PublishingOverview({ api, onOpenComposer, onOpenQueue, onOpenDelivery, onOpenAttention }: Props) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setOverview(await api.overview());
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

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <p className="max-w-2xl text-[13px] leading-relaxed text-slate-400">
          Compose, approve and publish to Facebook Pages, Instagram Reels and TikTok. Publishing only happens after a person
          approves the exact payload shown on the approval page.
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <button
            className="h-8 rounded-[7px] border border-slate-700 px-3 text-[13px] text-slate-300 hover:bg-slate-800 disabled:opacity-60"
            onClick={() => void load()}
            disabled={busy}
          >
            {busy ? 'Refreshing…' : 'Refresh'}
          </button>
          <button
            className="h-8 rounded-[7px] bg-sky-600 px-3 text-[13px] font-medium text-white hover:bg-sky-500"
            onClick={onOpenComposer}
          >
            New intent
          </button>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      {overview ? (
        <>
          <div className="grid gap-4 md:grid-cols-4">
            <Card title="Open destinations">
              <p className="text-2xl font-semibold">{overview.summary.open}</p>
              <p className="text-xs text-slate-500">{overview.summary.scheduled_upcoming} scheduled ahead</p>
            </Card>
            <Card title="Needs you">
              <p className={`text-2xl font-semibold ${overview.summary.needs_attention ? 'text-amber-400' : ''}`}>
                {overview.summary.needs_attention}
              </p>
              <button
                className="mt-1 text-xs text-sky-400 hover:underline disabled:text-slate-600 disabled:no-underline"
                disabled={overview.summary.needs_attention === 0}
                title={overview.summary.needs_attention === 0 ? 'nothing is waiting for a decision' : undefined}
                onClick={onOpenAttention}
              >
                open the attention list
              </button>
            </Card>
            <Card title="Published">
              <p className="text-2xl font-semibold">{overview.summary.published}</p>
              <p className="text-xs text-slate-500">{overview.summary.published_last_7d} in the last 7 days</p>
            </Card>
            <Card title="Media delivery">
              <p className={`text-sm font-medium ${overview.delivery.configured ? 'text-emerald-400' : 'text-amber-400'}`}>
                {overview.delivery.configured ? `${overview.delivery.provider} · ${overview.delivery.bucket}` : 'not configured'}
              </p>
              <p className="text-xs text-slate-500">
                {overview.delivery.configured
                  ? 'Instagram can fetch the media it needs'
                  : `Instagram is blocked: ${overview.delivery.missing.join(', ') || 'no bucket'}`}
              </p>
              <button className="mt-1 text-xs text-sky-400 hover:underline" onClick={onOpenDelivery}>
                delivery settings
              </button>
            </Card>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <Card title="By state">
              {Object.keys(overview.summary.by_state).length === 0 ? (
                <EmptyState title="Nothing has been composed yet" hint="Start with an intent: one piece of content, several destinations." />
              ) : (
                <ul className="space-y-1.5">
                  {Object.entries(overview.summary.by_state)
                    .sort((a, b) => b[1] - a[1])
                    .map(([state, count]) => (
                      <li key={state} className="flex items-center justify-between text-sm">
                        <span className="text-slate-300">{STATE_LABELS[state as PublishState] ?? state}</span>
                        <Badge tone={ATTENTION.includes(state as PublishState) ? 'warn' : 'info'}>{count}</Badge>
                      </li>
                    ))}
                </ul>
              )}
            </Card>

            <Card title="Connected accounts">
              {overview.capabilities.length === 0 ? (
                <EmptyState title="No accounts connected" hint="Connect a Page, an Instagram professional account or TikTok first." />
              ) : (
                <ul className="space-y-2">
                  {overview.capabilities.map((account) => (
                    <li key={account.id} className="flex items-center justify-between gap-2 text-sm">
                      <span className="truncate text-slate-200">
                        {account.display_name} <span className="text-xs text-slate-500">· {platformLabel(account.platform)}</span>
                      </span>
                      <Badge tone={account.token_state === 'valid' ? 'good' : 'warn'}>{account.token_state}</Badge>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          <Card title="Platforms">
            {overview.summary.platforms.length === 0 ? (
              <p className="text-sm text-slate-500">No destinations exist yet.</p>
            ) : (
              <div className="grid gap-3 md:grid-cols-3">
                {overview.summary.platforms.map((row) => (
                  <div key={row.platform} className="rounded-lg border border-slate-800 p-3">
                    <p className="text-sm font-medium">{platformLabel(row.platform)}</p>
                    <p className="mt-1 text-xs text-slate-400">
                      {Object.entries(row.by_state)
                        .map(([state, count]) => `${STATE_LABELS[state as PublishState] ?? state}: ${count}`)
                        .join(' · ')}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="Schedule policy">
            <p className="text-sm text-slate-300">
              When a scheduled time passes while the application is closed:{' '}
              <span className="font-medium text-slate-100">
                {overview.schedule.policy.policy === 'ask'
                  ? 'ask me'
                  : overview.schedule.policy.policy === 'publish_now'
                    ? 'publish late, and record how late'
                    : 'stop and propose a new time'}
              </span>
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Grace window {overview.schedule.policy.grace_seconds}s. Change it on the schedule page.
            </p>
            <button className="mt-2 text-xs text-sky-400 hover:underline" onClick={onOpenQueue}>
              open the publish queue
            </button>
          </Card>
        </>
      ) : busy ? (
        <Spinner label="Reading the publishing state…" />
      ) : null}
    </div>
  );
}
