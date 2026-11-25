/**
 * Analytics Center — Account Performance (P8.19).
 *
 * One account at a time: identity and connection health, what it is allowed to collect, how fresh the
 * data is, the follower metric the provider actually exposes (kept separate per platform — a Facebook
 * Page like is not a follower), the stored account-scope metrics, and the recent collection runs.
 */
import { useMemo, useState } from 'react';
import {
  type AnalyticsClient,
  type AccountSummary,
  type ConsentPlan,
  type MetricDefinition,
} from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import { seriesFromRows, formatCompact, formatMetricValue } from '../../components/chartModel';
import { Badge, Card, EmptyState, Button, formatAge, formatWhen } from '../../components/ui';
import {
  AvailabilityChip,
  DateRangePicker,
  ErrorBlock,
  LoadingBlock,
  MetricStatePanel,
  PageHeader,
  StaleBadge,
  platformLabel,
  useAsyncData,
  type RangeValue,
  defaultRange,
} from './shared';

export default function AccountPerformance({ api }: { api: AnalyticsClient }) {
  const [range, setRange] = useState<RangeValue>(() => defaultRange('28d'));
  const [accountId, setAccountId] = useState('');
  const [granting, setGranting] = useState(false);
  const [grantNotice, setGrantNotice] = useState<string | null>(null);
  const [grantError, setGrantError] = useState<string | null>(null);
  const [grantUrl, setGrantUrl] = useState<string | null>(null);

  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const accountList: AccountSummary[] = accounts.data?.items ?? [];
  const selected = accountList.find((account) => account.id === accountId) ?? accountList[0] ?? null;
  const id = selected?.id ?? '';

  const detail = useAsyncData(
    (signal) => (id ? api.account(id, signal) : Promise.resolve(null)),
    [id],
  );

  const freshness = useAsyncData((signal) => api.freshness(signal), []);

  const capability = useAsyncData(
    (signal) => (id ? api.availability(id, 'account', signal) : Promise.resolve(null)),
    [id],
  );

  const consent = useAsyncData(
    (signal) => (id ? api.consent(id, signal) : Promise.resolve(null)),
    [id],
  );

  const metrics = useAsyncData(
    (signal) => (selected ? api.metrics({ platform: selected.platform, limit: 200 }, signal) : Promise.resolve(null)),
    [selected?.platform ?? ''],
  );

  const growth = useAsyncData(
    (signal) => (id ? api.growth(id, { start: range.start, end: range.end }, signal) : Promise.resolve(null)),
    [id, range.start, range.end],
  );

  const storedMetrics = useAsyncData(
    (signal) => (id ? api.accountMetrics(id, {}, signal) : Promise.resolve([])),
    [id],
  );

  const runs = useAsyncData(
    (signal) => (id ? api.runs({ account_id: id, limit: 10 }, signal) : Promise.resolve(null)),
    [id],
  );

  const definitionByKey = useMemo(() => {
    const map = new Map<string, MetricDefinition>();
    for (const definition of metrics.data?.items ?? []) map.set(definition.metric_key, definition);
    return map;
  }, [metrics.data]);

  const growthSeries = useMemo(() => {
    if (!growth.data || growth.data.metric_keys.length === 0) return [];
    return growth.data.metric_keys.map((metricKey) => {
      const definition = definitionByKey.get(metricKey);
      const rows = growth.data!.points
        .filter((point) => point.metric_key === metricKey)
        .map((point) => ({ at: point.bucket_start, value: point.value, availability: point.availability }));
      return seriesFromRows(metricKey, definition?.provider_metric_name ?? metricKey, definition?.unit, rows);
    });
  }, [growth.data, definitionByKey]);

  const freshnessRow = freshness.data?.accounts.find((row) => row.account_id === id) ?? null;
  const capabilitySummary = capability.data?.summary ?? {};
  const denied = (capability.data?.metrics ?? []).filter((row) => !row.allowed);
  const consentPlan = consent.data ?? null;

  async function grantAnalyticsAccess() {
    if (!id) return;
    setGranting(true);
    setGrantError(null);
    setGrantNotice(null);
    setGrantUrl(null);
    try {
      const started = await api.startConsent(id);
      if (started.authorization_url) {
        setGrantUrl(started.authorization_url);
        const opened = await window.creatoros?.openAuthUrl?.(started.authorization_url);
        setGrantNotice(
          opened && opened.ok === false
            ? `The shell refused to open the authorization page (${opened.error ?? 'unknown reason'}). Approve it from the URL below.`
            : 'Approve the analytics permission in your browser, then choose "Check again".',
        );
      } else {
        setGrantNotice(started.message || 'No further authorization is needed.');
      }
      consent.reload();
    } catch (err) {
      setGrantError(err instanceof Error ? err.message : String(err));
    } finally {
      setGranting(false);
    }
  }

  function recheckConsent() {
    consent.reload();
    capability.reload();
    detail.reload();
  }

  if (accounts.loading && !accounts.data) return <LoadingBlock label="Loading accounts..." />;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Account Performance"
        subtitle="Per account: identity, what it may collect, freshness, growth and collection history."
        actions={
          <>
            <label className="text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Account</span>
              <select
                className="mt-1 max-w-[18rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
                value={id}
                onChange={(event) => setAccountId(event.target.value)}
                disabled={accountList.length === 0}
              >
                {accountList.length === 0 ? <option value="">no connected accounts</option> : null}
                {accountList.map((account) => (
                  <option key={account.id} value={account.id}>
                    {platformLabel(account.platform)} · {account.display_name}
                  </option>
                ))}
              </select>
            </label>
            <DateRangePicker value={range} onChange={setRange} />
            <Button
              onClick={() => { detail.reload(); freshness.reload(); capability.reload(); growth.reload(); storedMetrics.reload(); runs.reload(); }}
              busy={growth.loading || storedMetrics.loading}
            >
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock error={accounts.error} onRetry={accounts.reload} />

      {!selected ? (
        <EmptyState title="No connected account" hint="Connect a Facebook Page, Instagram professional account or TikTok account first." />
      ) : (
        <>
          <section className="grid gap-5 lg:grid-cols-3">
            <Card title="Identity" subtitle={platformLabel(selected.platform)}>
              <MetricStatePanel
                title="Account"
                rows={[
                  { label: 'Display name', value: selected.display_name },
                  { label: 'Username', value: selected.username ?? '—' },
                  { label: 'Type', value: selected.account_type },
                  { label: 'Provider id', value: selected.provider_account_id },
                ]}
              />
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Badge tone={selected.connection_status === 'connected' ? 'good' : 'warn'}>{selected.connection_status}</Badge>
                <span className="text-xs text-slate-500">verified {formatWhen(selected.last_verified_at)}</span>
                {selected.token_expires_at ? <span className="text-xs text-slate-500">token expires {formatWhen(selected.token_expires_at)}</span> : null}
              </div>
            </Card>

            <Card title="Freshness" subtitle="Stale data is labelled, never presented as current">
              <div className="flex flex-wrap items-center gap-2">
                <StaleBadge stale={freshnessRow?.stale ?? true} ageHours={freshnessRow?.age_hours ?? null} />
                {freshnessRow?.state ? <Badge tone="muted">{freshnessRow.state}</Badge> : null}
              </div>
              <MetricStatePanel
                title="Collection"
                rows={[
                  { label: 'Last collected', value: selected.last_collected_at ? formatWhen(selected.last_collected_at) : 'never' },
                  { label: 'Age', value: formatAge(selected.last_collected_at) },
                  { label: 'Next due', value: freshnessRow?.next_due_at ? formatWhen(freshnessRow.next_due_at) : '—' },
                  { label: 'Observations stored', value: formatCompact(selected.observations) },
                ]}
              />
              {freshnessRow?.blocked_reason ? <p className="mt-2 text-xs text-amber-400">{freshnessRow.blocked_reason}</p> : null}
            </Card>

            <Card title="Analytics capability" subtitle="From the scopes actually granted on this account">
              {capability.loading && !capability.data ? (
                <LoadingBlock label="Checking scopes..." />
              ) : capability.data ? (
                <>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(capabilitySummary).map(([reason, count]) => (
                      <Badge key={reason} tone={reason === 'ok' ? 'good' : reason === 'not_authorized' ? 'warn' : 'muted'}>
                        {reason}: {count}
                      </Badge>
                    ))}
                  </div>
                  <p className="mt-2 text-xs text-slate-500">
                    api version {capability.data.api_version ?? '—'} · object {capability.data.object_type}
                  </p>
                  {denied.length ? (
                    <div className="mt-2 max-h-40 space-y-1 overflow-auto">
                      {denied.slice(0, 12).map((row) => (
                        <div key={row.metric_key} className="flex items-center justify-between gap-2 text-xs">
                          <span className="min-w-0 truncate text-slate-400" title={row.detail}>
                            {row.metric_key}
                          </span>
                          <Badge tone="warn">{row.reason}</Badge>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="mt-2 text-xs text-emerald-400">Every account metric for this platform is collectable.</p>
                  )}
                </>
              ) : (
                <EmptyState title="Capability unknown" hint="No availability data for this account yet." />
              )}
            </Card>
          </section>

          {consentPlan && consentPlan.blocked_by.length > 0 ? (
            <ConsentCard
              plan={consentPlan}
              busy={granting}
              notice={grantNotice}
              error={grantError}
              authorizationUrl={grantUrl}
              onGrant={() => void grantAnalyticsAccess()}
              onRecheck={recheckConsent}
            />
          ) : null}

          <section className="grid gap-5 lg:grid-cols-2">
            <Card
              title="Account growth"
              subtitle="Only the follower metric the provider actually exposes — never inferred"
            >
              {growth.loading && !growth.data ? (
                <LoadingBlock label="Loading growth..." />
              ) : growthSeries.length === 0 ? (
                <EmptyState
                  title="No follower metric available"
                  hint="This platform does not expose a follower series for this account, or nothing has been collected yet."
                />
              ) : (
                <>
                  <p className="mb-2 text-xs text-slate-500">
                    Metric: <span className="font-mono text-slate-300">{growth.data?.metric_keys.join(', ')}</span>. A Facebook Page like
                    is not a follower, and Instagram followers are not the same as the follows/unfollows insight.
                  </p>
                  {growthSeries.map((series) => (
                    <MetricWidget
                      key={series.key}
                      title={series.key}
                      subtitle={`${series.label ?? ''} · gaps mean missing, not zero`}
                      series={[series]}
                      bucket="day"
                      height={200}
                      emptyHint="No follower observation in the selected range."
                    />
                  ))}
                </>
              )}
            </Card>

            <Card title="Stored account metrics" subtitle="Account-scope rollups with their availability">
              {storedMetrics.loading && !storedMetrics.data ? (
                <LoadingBlock label="Loading metrics..." />
              ) : (storedMetrics.data ?? []).length === 0 ? (
                <EmptyState title="No account metrics stored" hint="Run a collection to populate account-scope rollups." />
              ) : (
                <div className="max-h-80 space-y-2 overflow-auto">
                  {(storedMetrics.data ?? []).map((metric) => (
                    <div key={`${metric.metric_key}-${metric.period_kind}`} className="flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                      <div className="min-w-0">
                        <p className="truncate text-sm text-slate-200">{metric.metric_key}</p>
                        <p className="text-xs text-slate-500">
                          {metric.period_kind} · {metric.unit}
                          {metric.sample_size !== null ? ` · n=${metric.sample_size}` : ''}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        <span className="font-mono text-sm text-slate-100">
                          {metric.value !== null && metric.availability === 'observed' ? formatMetricValue(metric.value, metric.unit) : '—'}
                        </span>
                        <AvailabilityChip availability={metric.availability} reason={metric.availability_reason} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </section>

          <Card title="Recent collection history" subtitle="Real runs on the analytics queue lane">
            {runs.loading && !runs.data ? (
              <LoadingBlock label="Loading runs..." />
            ) : (runs.data?.items ?? []).length === 0 ? (
              <EmptyState title="No collection runs yet" hint="Use 'Collect now' on the Accounts page, or wait for the scheduler." />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-slate-500">
                    <tr>
                      <th className="py-1 pr-3">Started</th>
                      <th className="py-1 pr-3">Target</th>
                      <th className="py-1 pr-3">Window</th>
                      <th className="py-1 pr-3">Outcome</th>
                      <th className="py-1 pr-3">HTTP</th>
                      <th className="py-1 pr-3">Observed</th>
                      <th className="py-1">Error</th>
                    </tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {(runs.data?.items ?? []).map((run) => (
                      <tr key={run.id} className="border-t border-slate-800/70">
                        <td className="py-1 pr-3">{formatWhen(run.started_at)}</td>
                        <td className="py-1 pr-3">{run.target_kind}</td>
                        <td className="py-1 pr-3">{run.window_kind ?? '—'}</td>
                        <td className="py-1 pr-3">
                          <Badge tone={run.outcome === 'succeeded' ? 'good' : run.outcome === 'failed' ? 'bad' : 'warn'}>{run.outcome}</Badge>
                        </td>
                        <td className="py-1 pr-3 font-mono">{run.http_calls}</td>
                        <td className="py-1 pr-3 font-mono">
                          {run.metrics_observed}/{run.metrics_attempted}
                        </td>
                        <td className="py-1 font-mono text-rose-300">{run.error_code ?? ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}


/**
 * The consent notice (P8.20): what analytics is missing, why, and whether a user can act on it.
 * It never shows a success state on its own - capability is re-read from the backend afterwards.
 */
function ConsentCard({
  plan,
  busy,
  notice,
  error,
  authorizationUrl,
  onGrant,
  onRecheck,
}: {
  plan: ConsentPlan;
  busy: boolean;
  notice: string | null;
  error: string | null;
  authorizationUrl: string | null;
  onGrant: () => void;
  onRecheck: () => void;
}) {
  const tone = plan.blocked_by.includes('developer_approval') || plan.blocked_by.includes('page_task') ? 'warn' : 'info';
  return (
    <Card title="Analytics permission" subtitle={plan.capability_label ?? 'Read insights'}>
      <div className="space-y-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={plan.status === 'developer_approval_required' ? 'warn' : tone}>{plan.status.replace(/_/g, ' ')}</Badge>
          <span className="text-xs text-slate-400">{plan.detail}</span>
        </div>
        <p className="text-xs text-slate-500">
          Account <span className="text-slate-300">{plan.provider_account_id}</span> · {plan.account_type} · app mode{' '}
          {plan.app_mode ?? '—'}
        </p>
        {plan.missing_scopes.length ? (
          <div>
            <p className="text-xs uppercase tracking-wide text-slate-500">Requested permission</p>
            <p className="font-mono text-xs text-amber-300">{plan.missing_scopes.join(', ')}</p>
            <ul className="mt-1 space-y-0.5">
              {plan.missing_scopes.map((scope) => (
                <li key={scope} className="text-xs text-slate-400">
                  <span className="font-mono text-slate-300">{scope}</span>: {plan.scope_reasons[scope] ?? 'needed for analytics'}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {plan.page_tasks_required.length ? (
          <p className="text-xs text-slate-400">
            Page role: {plan.page_tasks_held.length ? plan.page_tasks_held.join(', ') : 'none reported'}
            {plan.page_tasks_missing.length ? ` · missing ${plan.page_tasks_missing.join(', ')}` : ''}
          </p>
        ) : null}
        {plan.metrics_blocked.length ? (
          <div>
            <p className="text-xs uppercase tracking-wide text-slate-500">Metrics still blocked</p>
            <p className="text-xs text-slate-400">{plan.metrics_blocked.map((row) => row.metric_key).join(', ')}</p>
          </div>
        ) : null}
        {plan.developer_approval_required ? (
          <p className="text-xs text-amber-400">
            The developer app has not been approved for this permission, so user consent alone cannot make it available.
          </p>
        ) : null}
        {notice ? <p className="text-xs text-sky-300">{notice}</p> : null}
        {error ? <p className="text-xs text-rose-400">{error}</p> : null}
        {authorizationUrl ? (
          <details>
            <summary className="cursor-pointer text-xs text-slate-500">Authorization URL (contains a one-time state value)</summary>
            <pre className="mt-2 max-h-32 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-400">
              {authorizationUrl}
            </pre>
          </details>
        ) : null}
        <div className="flex flex-wrap gap-2 pt-1">
          <Button variant="primary" disabled={!plan.can_start || busy} onClick={onGrant}>
            {busy ? 'Starting...' : 'Grant analytics access'}
          </Button>
          <Button variant="ghost" onClick={onRecheck}>
            Check again
          </Button>
        </div>
        <p className="text-[11px] text-slate-500">
          Declining does not disconnect the account or remove existing access: the account keeps working and analytics
          stays blocked for the missing permission. CreatorOS never asks for a scope beyond this one.
        </p>
      </div>
    </Card>
  );
}
