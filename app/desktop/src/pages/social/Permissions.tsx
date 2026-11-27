import { useCallback, useEffect, useState } from 'react';
import { type CapabilityMatrix, type CapabilityState, type SocialApi, capabilityTone, platformLabel } from '../../apiSocial';
import { type AnalyticsClient, type ConsentOverview } from '../../services/apiAnalytics';
import { Badge, Button, Card, EmptyState, ErrorNote, Spinner } from '../../components/ui';

type Props = {
  api: SocialApi;
  analytics?: AnalyticsClient;
  onOpenAccount: (id: string) => void;
};

export default function Permissions({ api, analytics, onOpenAccount }: Props) {
  const [matrix, setMatrix] = useState<CapabilityMatrix | null>(null);
  const [consent, setConsent] = useState<ConsentOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [onlyRowsWithData, setOnlyRowsWithData] = useState(true);
  const [grantingId, setGrantingId] = useState<string | null>(null);
  const [grantNotice, setGrantNotice] = useState<string | null>(null);
  const [grantError, setGrantError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setMatrix(await api.matrix());
      if (analytics) setConsent(await analytics.consentOverview());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api, analytics]);

  useEffect(() => {
    void load();
  }, [load]);

  async function grantAnalyticsAccess(accountId: string) {
    if (!analytics) return;
    setGrantingId(accountId);
    setGrantNotice(null);
    setGrantError(null);
    try {
      const started = await analytics.startConsent(accountId);
      if (started.authorization_url) {
        const opened = await window.creatoros?.openAuthUrl?.(started.authorization_url);
        setGrantNotice(
          opened && opened.ok === false
            ? `The shell refused to open the authorization page (${opened.error ?? 'unknown reason'}). Open it from Analytics → Account performance.`
            : 'Approve the analytics permission in your browser, then reload this page.',
        );
      } else {
        setGrantNotice(started.message || 'No further authorization is needed.');
      }
      await load();
    } catch (err) {
      setGrantError(err instanceof Error ? err.message : String(err));
    } finally {
      setGrantingId(null);
    }
  }

  const rows = (matrix?.catalogue ?? []).filter((entry) => (onlyRowsWithData ? matrix?.accounts.some((account) => entry.key in account.states) : true));

  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between">
        <div>
            <p className="text-sm text-slate-400">
            One row per capability in the catalogue, one column per connected account. A cell is a stored verdict with a reason, not a
            guess.
          </p>
        </div>
        <label className="flex items-center gap-2 text-xs text-slate-400">
          <input type="checkbox" checked={onlyRowsWithData} onChange={(event) => setOnlyRowsWithData(event.target.checked)} />
          only capabilities that at least one account has evaluated
        </label>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      {!matrix ? (
        <Spinner label="Reading capability records…" />
      ) : (
        <>
          <div className="flex flex-wrap gap-2">
            {Object.entries(matrix.totals).map(([state, count]) => (
              <Badge key={state} tone={capabilityTone(state as CapabilityState)}>
                {state.replace(/_/g, ' ')}: {count}
              </Badge>
            ))}
          </div>

          {consent && consent.accounts.length > 0 ? (
            <Card
              title="Missing analytics permissions"
              subtitle="Insight permissions the connection flow does not request. Granting one keeps the account connected."
            >
              {grantNotice ? <p className="mb-2 text-xs text-sky-300">{grantNotice}</p> : null}
              {grantError ? <p className="mb-2 text-xs text-rose-400">{grantError}</p> : null}
              <div className="space-y-2">
                {consent.accounts.map((row) => (
                  <div
                    key={row.account_id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
                  >
                    <div className="min-w-0">
                      <button className="text-sm text-slate-200 hover:text-sky-400" onClick={() => onOpenAccount(row.account_id)}>
                        {row.label}
                      </button>
                      <p className="text-xs text-slate-500">
                        <span className="font-mono">{row.capability_key ?? 'analytics'}</span>
                        {row.missing_scopes.length ? ` · needs ${row.missing_scopes.join(', ')}` : ''}
                      </p>
                      <p className="text-xs text-slate-400">{row.detail}</p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <Badge tone={row.status === 'developer_approval_required' || row.status === 'page_task_required' ? 'warn' : 'info'}>
                        {row.status.replace(/_/g, ' ')}
                      </Badge>
                      <Button
                        variant={row.can_start ? 'primary' : 'ghost'}
                        disabled={!row.can_start || grantingId === row.account_id}
                        onClick={() => void grantAnalyticsAccess(row.account_id)}
                      >
                        {grantingId === row.account_id ? 'Starting...' : 'Grant analytics access'}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
              <p className="mt-2 text-[11px] text-slate-500">
                The backend computes the scopes; this page can never request one on its own. Developer-approval or Page-role
                blockers are shown here but cannot be fixed by user consent alone.
              </p>
            </Card>
          ) : null}

          {matrix.accounts.length === 0 ? (
            <EmptyState
              title="No account has been evaluated yet"
              hint="Connect an account and its capabilities are recorded from the real scopes the provider granted."
            />
          ) : (
            <Card title="Permission matrix">
              <div className="overflow-auto">
                <table className="w-full border-collapse text-left text-xs">
                  <thead>
                    <tr className="text-slate-500">
                      <th className="sticky left-0 bg-slate-900/60 py-2 pr-4 font-medium">Capability</th>
                      {matrix.accounts.map((account) => (
                        <th key={account.id} className="px-2 py-2 font-medium">
                          <button className="hover:text-sky-400" onClick={() => onOpenAccount(account.id)}>
                            {account.label}
                          </button>
                          <p className="font-normal text-[10px] text-slate-600">{platformLabel(account.platform)}</p>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((entry) => (
                      <tr key={entry.key} className="border-t border-slate-800/70">
                        <td className="sticky left-0 max-w-[280px] bg-slate-900/60 py-2 pr-4">
                          <p className="text-slate-200">{entry.label}</p>
                          <p className="font-mono text-[10px] text-slate-600">{entry.key}</p>
                        </td>
                        {matrix.accounts.map((account) => {
                          const state = account.states[entry.key];
                          return (
                            <td key={account.id} className="px-2 py-2">
                              {state ? <Badge tone={capabilityTone(state)}>{state.replace(/_/g, ' ')}</Badge> : <span className="text-slate-700">—</span>}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          <Card title="Capability catalogue" subtitle="What each verdict means, which scopes it needs and where it is documented.">
            <div className="space-y-2">
              {matrix.catalogue.map((entry) => (
                <details key={entry.key} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                  <summary className="cursor-pointer text-sm text-slate-200">
                    {entry.label} <span className="ml-2 font-mono text-[10px] text-slate-600">{entry.key}</span>
                    {entry.manual_only ? (
                      <span className="ml-2">
                        <Badge tone="info">manual only</Badge>
                      </span>
                    ) : null}
                  </summary>
                  <div className="mt-2 space-y-1 text-xs text-slate-400">
                    <p>Platform: {platformLabel(entry.platform)} · routes: {entry.routes.join(', ')}</p>
                    <p>Required scopes: {entry.required_scopes.length ? entry.required_scopes.join(', ') : 'none'}</p>
                    {entry.notes ? <p>{entry.notes}</p> : null}
                    <a className="text-sky-400 hover:underline" href={entry.docs_url} target="_blank" rel="noreferrer">
                      provider documentation
                    </a>
                  </div>
                </details>
              ))}
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
