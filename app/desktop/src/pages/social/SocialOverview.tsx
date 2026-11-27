import { useCallback, useEffect, useState } from 'react';
import { type AccountPage, type Overview, type Platform, type SocialApi, platformLabel, routeLabel, tokenTone } from '../../apiSocial';
import { Badge, Card, EmptyState, ErrorNote, Row, Spinner } from '../../components/ui';

type Props = {
  api: SocialApi;
  onOpenAccount: (id: string) => void;
  onConnect: () => void;
};

const PLATFORMS: { id: Platform; label: string }[] = [
  { id: 'facebook', label: 'Facebook' },
  { id: 'instagram', label: 'Instagram' },
  { id: 'tiktok', label: 'TikTok' },
];

export default function SocialOverview({ api, onOpenAccount, onConnect }: Props) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [accounts, setAccounts] = useState<AccountPage | null>(null);
  const [platform, setPlatform] = useState<Platform | 'all'>('all');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [nextOverview, nextAccounts] = await Promise.all([
        api.overview(),
        api.accounts({ platform: platform === 'all' ? undefined : platform }),
      ]);
      setOverview(nextOverview);
      setAccounts(nextAccounts);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [api, platform]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <p className="max-w-2xl text-[13px] leading-relaxed text-slate-400">
          Every account here was authorised by a person in a browser and holds a credential in the local DPAPI vault.
          Connecting does not publish anything; publishing needs its own approval.
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
            onClick={onConnect}
          >
            Connect an account
          </button>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      {overview ? (
        <div className="grid gap-4 md:grid-cols-4">
          <Card title="Accounts">
            <p className="text-2xl font-semibold">{overview.accounts.total}</p>
            <p className="text-xs text-slate-500">
              {overview.accounts.disconnected} disconnected · {overview.accounts.needs_attention} need attention
            </p>
          </Card>
          <Card title="Credential vault">
            <p className={`text-sm font-medium ${overview.vault.available ? 'text-emerald-400' : 'text-rose-400'}`}>
              {overview.vault.available ? `${overview.vault.backend} · ${overview.vault.entries ?? 0} records` : 'unavailable'}
            </p>
            <p className="text-xs text-slate-500">{overview.vault.protection}</p>
          </Card>
          <Card title="Callback listener">
            <p className={`text-sm font-medium ${overview.callbacks.running ? 'text-emerald-400' : 'text-amber-400'}`}>
              {overview.callbacks.running ? `listening on :${overview.callbacks.port}` : 'not running'}
            </p>
            <p className="text-xs text-slate-500">
              {overview.callbacks.running ? overview.callbacks.url_base : 'OAuth cannot complete without it'}
            </p>
          </Card>
          <Card title="Publishing readiness">
            <p className="text-sm">{overview.phase7.publish_capable_accounts} account(s) publish-capable</p>
            {overview.phase7.blockers.length ? (
              <ul className="mt-1 space-y-1 text-xs text-amber-400">
                {overview.phase7.blockers.map((blocker) => (
                  <li key={blocker}>· {blocker}</li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-emerald-400">no blockers recorded</p>
            )}
          </Card>
        </div>
      ) : busy ? (
        <Spinner label="Reading accounts…" />
      ) : null}

      <div className="flex items-center gap-2">
        {(['all', ...PLATFORMS.map((entry) => entry.id)] as const).map((entry) => (
          <button
            key={entry}
            onClick={() => setPlatform(entry as Platform | 'all')}
            className={`rounded-full px-3 py-1 text-xs ${
              platform === entry ? 'bg-slate-700 text-slate-100' : 'bg-slate-900 text-slate-400 hover:bg-slate-800'
            }`}
          >
            {entry === 'all' ? 'All platforms' : platformLabel(entry as Platform)}
          </button>
        ))}
      </div>

      <Card title="Accounts">
        {accounts && accounts.items.length === 0 ? (
          <EmptyState
            title="No account is connected yet"
            hint="Register a developer app under App settings, then use Connect an account. Nothing can be connected without one."
          />
        ) : null}
        <div className="space-y-2">
          {accounts?.items.map((account) => (
            <button
              key={account.id}
              onClick={() => onOpenAccount(account.id)}
              className="flex w-full items-center justify-between gap-4 rounded-lg border border-slate-800 bg-slate-950/40 px-4 py-3 text-left hover:border-slate-700"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-slate-100">{account.label}</p>
                <p className="truncate text-xs text-slate-500">
                  {platformLabel(account.platform)} · {routeLabel(account.route)} · {account.account_type.replace(/_/g, ' ')}
                  {account.username ? ` · @${account.username}` : ''}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Badge tone={account.connection_status === 'connected' ? 'good' : account.connection_status === 'disconnected' ? 'info' : 'warn'}>
                  {account.connection_status.replace(/_/g, ' ')}
                </Badge>
                <Badge tone={tokenTone(account.token_state)}>{account.token_state.replace(/_/g, ' ')}</Badge>
              </div>
            </button>
          ))}
        </div>
      </Card>

      {overview ? (
        <div className="grid gap-4 md:grid-cols-2">
          <Card title="Provider login paths">
            <div className="space-y-2">
              {overview.providers.map((provider) => (
                <Row
                  key={`${provider.platform}-${provider.route}`}
                  label={provider.label}
                  value={
                    provider.configured ? (
                      <Badge tone={provider.app_mode === 'live' ? 'good' : 'warn'}>{provider.app_mode ?? 'development'}</Badge>
                    ) : (
                      <Badge tone="info">not configured</Badge>
                    )
                  }
                />
              ))}
            </div>
          </Card>
          <Card title="Recent connection attempts">
            {overview.oauth_recent.length === 0 ? (
              <p className="text-sm text-slate-500">No attempt has been made yet.</p>
            ) : (
              <div className="space-y-1">
                {overview.oauth_recent.map((attempt) => (
                  <div key={attempt.session_id} className="flex items-center justify-between text-xs">
                    <span className="text-slate-400">
                      {platformLabel(attempt.platform)} · {attempt.route ? routeLabel(attempt.route as never) : '—'}
                    </span>
                    <span className={attempt.error_code ? 'text-rose-400' : 'text-slate-500'}>
                      {attempt.error_code ?? attempt.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      ) : null}
    </div>
  );
}
