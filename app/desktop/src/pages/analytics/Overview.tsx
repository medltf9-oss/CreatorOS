/**
 * Analytics Center — Overview (P8.19).
 *
 * A global read of what is actually stored: counts from the overview endpoint, a real trend for one
 * chosen account and metric, per-platform counts as separate widgets (never one shared axis for two
 * different metric types), recent publications with the metrics that were collected for them, and a
 * freshness panel. Nothing is summed across platforms and no unavailable value is shown as zero.
 */
import { useMemo, useState } from 'react';
import {
  type AnalyticsClient,
  type AccountSummary,
  type MetricDefinition,
  type MetricValue,
  type PublicationSummary,
} from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import { seriesFromRows, formatCompact, formatMetricValue } from '../../components/chartModel';
import { Badge, Button, Card, EmptyState, Stat, formatAge, formatWhen } from '../../components/ui';
import {
  AvailabilityChip,
  DateRangePicker,
  ErrorBlock,
  LoadingBlock,
  PageHeader,
  StaleBadge,
  formatRangeLabel,
  platformLabel,
  useAsyncData,
  type RangeValue,
  defaultRange,
} from './shared';

type Summary = {
  overview: Awaited<ReturnType<AnalyticsClient['overview']>>;
  platforms: Awaited<ReturnType<AnalyticsClient['platforms']>>;
  freshness: Awaited<ReturnType<AnalyticsClient['freshness']>>;
  capabilities: Awaited<ReturnType<AnalyticsClient['capabilities']>>;
};

function pickDefaultMetric(metrics: MetricDefinition[]): string {
  const views = metrics.find((metric) => metric.preferred_for === 'views' && !metric.is_deprecated);
  if (views) return views.metric_key;
  const preferred = metrics.find((metric) => metric.preferred_for);
  return (preferred ?? metrics[0])?.metric_key ?? '';
}

export default function AnalyticsOverview({ api }: { api: AnalyticsClient }) {
  const [range, setRange] = useState<RangeValue>(() => defaultRange('28d'));
  const [accountId, setAccountId] = useState('');
  const [metricKey, setMetricKey] = useState('');

  const summary = useAsyncData<Summary>(
    (signal) =>
      Promise.all([api.overview(signal), api.platforms(signal), api.freshness(signal), api.capabilities(signal)]).then(
        ([overview, platforms, freshness, capabilities]) => ({ overview, platforms, freshness, capabilities }),
      ),
    [],
  );

  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);

  const accountList: AccountSummary[] = accounts.data?.items ?? [];
  const selectedAccount = accountList.find((account) => account.id === accountId) ?? accountList[0] ?? null;
  const effectiveAccountId = selectedAccount?.id ?? '';

  const metrics = useAsyncData(
    (signal) => (selectedAccount ? api.metrics({ platform: selectedAccount.platform }, signal) : Promise.resolve(null)),
    [selectedAccount?.platform ?? ''],
  );
  const metricList = metrics.data?.items ?? [];
  const effectiveMetricKey = metricKey && metricList.some((metric) => metric.metric_key === metricKey)
    ? metricKey
    : pickDefaultMetric(metricList);
  const selectedMetric = metricList.find((metric) => metric.metric_key === effectiveMetricKey) ?? null;

  const trend = useAsyncData(
    (signal) =>
      effectiveAccountId && effectiveMetricKey
        ? api.series({ account_id: effectiveAccountId, metric_key: effectiveMetricKey, bucket: 'day', start: range.start, end: range.end }, signal)
        : Promise.resolve(null),
    [effectiveAccountId, effectiveMetricKey, range.start, range.end],
  );

  const recent = useAsyncData(async (signal) => {
    const page = await api.publications({ limit: 8 }, signal);
    const withMetrics = page.items.filter((item) => item.has_metrics).slice(0, 6);
    const pairs = await Promise.all(
      withMetrics.map(
        async (item) =>
          [item.publication_id, await api.publicationMetrics(item.publication_id, {}, signal)] as const,
      ),
    );
    return { page, metricsByPublication: Object.fromEntries(pairs) as Record<string, MetricValue[]> };
  }, []);

  const overview = summary.data?.overview ?? null;
  const platforms = summary.data?.platforms.platforms ?? [];
  const freshness = summary.data?.freshness.accounts ?? [];
  const capabilities = summary.data?.capabilities.platforms ?? [];

  const publicationsSeries = useMemo(() => {
    if (!platforms.length) return null;
    return {
      key: 'publications',
      label: 'Publications measured',
      unit: 'posts',
      points: platforms.map((row) => ({ x: platformLabel(row.platform), y: row.publications, availability: 'observed' as const })),
    };
  }, [platforms]);

  const observationsSeries = useMemo(() => {
    if (!platforms.length) return null;
    return {
      key: 'observations',
      label: 'Observations stored',
      unit: 'rows',
      points: platforms.map((row) => ({ x: platformLabel(row.platform), y: row.observations, availability: 'observed' as const })),
    };
  }, [platforms]);

  const trendSeries = useMemo(() => {
    if (!trend.data || !selectedMetric) return [];
    const rows = trend.data.points.map((point) => ({ at: point.bucket_start, value: point.value, availability: point.availability }));
    return [seriesFromRows(selectedMetric.metric_key, selectedMetric.provider_metric_name, selectedMetric.unit, rows)];
  }, [trend.data, selectedMetric]);

  const permissionNotes = capabilities.filter((row) => row.connected_accounts > 0 && Object.keys(row.planned_account_metrics).some((key) => key !== 'ok'));

  return (
    <div className="space-y-5">
      <PageHeader
        title="Analytics Overview"
        subtitle="A unified view of your connected social media performance."
        actions={
          <>
            <DateRangePicker value={range} onChange={setRange} />
            <Button onClick={() => { summary.reload(); accounts.reload(); trend.reload(); recent.reload(); }} busy={summary.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock error={accounts.error ?? summary.error} onRetry={() => { summary.reload(); accounts.reload(); }} />

      {summary.loading && !summary.data ? <LoadingBlock /> : null}

      {overview ? (
        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <Stat label="Connected accounts" value={`${overview.accounts_connected}/${overview.accounts_total}`} />
          <Stat label="Publications measured" value={formatCompact(overview.publications_measured)} unit="posts" />
          <Stat label="Observations stored" value={formatCompact(overview.observations)} unit="rows" />
          <Stat label="Aggregations" value={formatCompact(overview.aggregations)} unit="rollups" />
          <Stat label="Draft recommendations" value={formatCompact(overview.recommendations_by_status?.draft ?? 0)} />
          <Stat label="Last collection" value={formatWhen(overview.latest_collection_at)} unit={formatAge(overview.latest_collection_at)} />
        </section>
      ) : null}

      {overview ? (
        <p className="text-xs text-slate-500">
          Counts are additive because they are ours (publications, rows, rollups). Metric values are never summed across
          platforms: each platform defines reach, views and watch time differently.
        </p>
      ) : null}

      <section className="grid gap-5 lg:grid-cols-2">
        <Card title="Performance trend" subtitle="One account, one metric, real observations only">
          <div className="mb-3 flex flex-wrap items-end gap-3">
            <label className="min-w-0 text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Account</span>
              <select
                className="mt-1 w-full max-w-[14rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
                value={effectiveAccountId}
                onChange={(event) => setAccountId(event.target.value)}
                disabled={!accountList.length}
              >
                {accountList.length === 0 ? <option value="">no accounts</option> : null}
                {accountList.map((account) => (
                  <option key={account.id} value={account.id}>
                    {platformLabel(account.platform)} · {account.display_name}
                  </option>
                ))}
              </select>
            </label>
            <label className="min-w-0 text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Metric</span>
              <select
                className="mt-1 w-full max-w-[14rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
                value={effectiveMetricKey}
                onChange={(event) => setMetricKey(event.target.value)}
                disabled={!metricList.length}
              >
                {metricList.length === 0 ? <option value="">no metrics</option> : null}
                {metricList.map((metric) => (
                  <option key={metric.metric_key} value={metric.metric_key}>
                    {metric.metric_key} ({metric.unit})
                  </option>
                ))}
              </select>
            </label>
          </div>
          {!accountList.length ? (
            <EmptyState
              title="No connected account"
              hint="Analytics reads stored observations, so connect an account on the Social accounts page first."
            />
          ) : trend.loading && !trend.data ? (
            <LoadingBlock label="Loading trend..." />
          ) : (
            <>
              <ErrorBlock error={trend.error} onRetry={trend.reload} />
              {selectedMetric ? (
                <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                  <AvailabilityChip availability={selectedMetric.availability_default} reason={selectedMetric.definition_text} />
                  <span>{selectedMetric.definition_text || selectedMetric.provider_metric_name}</span>
                </div>
              ) : null}
              <MetricWidget
                title={selectedMetric?.metric_key ?? 'Trend'}
                subtitle={`${formatRangeLabel(range)} · gaps mean the observation is missing, not zero`}
                series={trendSeries}
                bucket={trend.data?.bucket ?? 'day'}
                emptyHint="This metric has no stored observation in the selected range."
              />
            </>
          )}
        </Card>

        <Card title="Platform breakdown" subtitle="Counts of our own records — comparable across platforms">
          {!platforms.length ? (
            <EmptyState title="No platform data" hint="Run a collection from the Accounts page to populate this." />
          ) : (
            <div className="space-y-4">
              {publicationsSeries ? (
                <MetricWidget title="Publications measured" series={[publicationsSeries]} kind="bar" bucket="day" height={180} />
              ) : null}
              {observationsSeries ? (
                <MetricWidget title="Observations stored" series={[observationsSeries]} kind="bar" bucket="day" height={180} />
              ) : null}
              <div className="grid gap-2 sm:grid-cols-3">
                {platforms.map((row) => (
                  <div key={row.platform} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                    <p className="text-sm font-medium text-slate-200">{platformLabel(row.platform)}</p>
                    <p className="mt-1 text-xs text-slate-400">{row.connected_accounts} account(s)</p>
                    <p className="text-xs text-slate-400">{row.observed_metrics} metric(s) observed</p>
                    <p className="mt-1 text-xs text-slate-500">last {formatAge(row.last_collected_at)}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </section>

      <section className="grid gap-5 lg:grid-cols-2">
        <Card title="Recent performance" subtitle="Recently published content and the metrics collected for it">
          {recent.loading && !recent.data ? <LoadingBlock label="Loading recent posts..." /> : null}
          <ErrorBlock error={recent.error} onRetry={recent.reload} />
          {recent.data && recent.data.page.items.length === 0 ? (
            <EmptyState title="No published content yet" hint="Publish something on the Publishing pages and it will appear here after the next collection." />
          ) : null}
          <div className="space-y-3">
            {(recent.data?.page.items ?? []).map((item: PublicationSummary) => {
              const rows = (recent.data?.metricsByPublication[item.publication_id] ?? []).slice(0, 4);
              return (
                <div key={item.publication_id} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm text-slate-200">{item.title || item.remote_post_id}</span>
                    <span className="flex items-center gap-2">
                      <Badge tone="info">{platformLabel(item.platform)}</Badge>
                      <Badge tone={item.has_metrics ? 'good' : 'muted'}>{item.has_metrics ? 'has metrics' : 'no metrics collected'}</Badge>
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-500">
                    published {formatWhen(item.provider_published_at)} · {item.hashtags.length} hashtag(s)
                  </p>
                  {item.has_metrics && rows.length === 0 ? (
                    <p className="mt-1 text-xs text-amber-400">metrics exist but carried no observed value</p>
                  ) : null}
                  <div className="mt-2 flex flex-wrap gap-2">
                    {rows.map((metric) => (
                      <span key={metric.metric_key} className="flex items-center gap-1 text-xs text-slate-400">
                        <span className="font-mono">
                          {metric.value !== null && metric.availability === 'observed' ? formatMetricValue(metric.value, metric.unit) : '—'}
                        </span>
                        {metric.metric_key}
                        <AvailabilityChip availability={metric.availability} reason={metric.availability_reason} />
                      </span>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <Card title="Data freshness" subtitle="Stale analytics are never shown as fresh">
          {freshness.length === 0 ? (
            <EmptyState title="No accounts to check" hint="Connect an account to start collecting." />
          ) : (
            <div className="space-y-2">
              {freshness.map((row) => (
                <div key={row.account_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-slate-200">
                      {platformLabel(row.platform)} · {row.display_name}
                    </p>
                    <p className="text-xs text-slate-500">
                      last {row.last_collected_at ? formatWhen(row.last_collected_at) : 'never'}
                      {row.next_due_at ? ` · next due ${formatWhen(row.next_due_at)}` : ''}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {row.state ? <Badge tone="muted">{row.state}</Badge> : null}
                    <StaleBadge stale={row.stale} ageHours={row.age_hours} />
                  </div>
                  {row.blocked_reason ? <p className="w-full text-xs text-amber-400">{row.blocked_reason}</p> : null}
                </div>
              ))}
            </div>
          )}
        </Card>
      </section>

      <Card title="What is missing, and why" subtitle="From the metric registry and the granted scopes — no estimates">
        {permissionNotes.length === 0 ? (
          <p className="text-sm text-slate-400">No connected platform is reporting a skipped metric right now.</p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-3">
            {permissionNotes.map((row) => (
              <div key={row.platform} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                <p className="text-sm text-slate-200">{platformLabel(row.platform)}</p>
                <p className="mt-1 text-xs text-slate-400">{row.note}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {Object.entries(row.planned_account_metrics)
                    .filter(([reason]) => reason !== 'ok')
                    .map(([reason, count]) => (
                      <Badge key={reason} tone="muted">
                        {reason}: {count}
                      </Badge>
                    ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
