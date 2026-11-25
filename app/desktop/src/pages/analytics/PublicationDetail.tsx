/**
 * Analytics Center — Publication detail (P8.21).
 *
 * One published object at a time. Everything on this page comes from the analytics API for a real
 * publication row (`/analytics/publications/{id}` and its `metrics`, `retention`, `timeline`); when a
 * metric was not `observed` it is shown with its availability chip and an em dash, never a zero.
 *
 * Two honest limits are stated on the page rather than papered over:
 * * the analytics API exposes no media asset reference, so there is **no thumbnail** — the placeholder
 *   says so instead of rendering a fabricated image;
 * * a retention curve is drawn only when the provider actually returned one; TikTok never does, and
 *   the reason is shown verbatim.
 */
import { useMemo, useState } from 'react';
import {
  type AnalyticsClient,
  type PublicationSummary,
} from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import { formatMetricValue } from '../../components/chartModel';
import {
  conceptsForPlatform,
  indexDefinitions,
  metricsByWindow,
  retentionChart,
  timelineSeries,
} from '../../components/analyticsModel';
import { Badge, Button, Card, EmptyState, formatWhen } from '../../components/ui';
import {
  AvailabilityChip,
  ErrorBlock,
  LoadingBlock,
  MetricStatePanel,
  PageHeader,
  StaleBadge,
  platformLabel,
  useAsyncData,
} from './shared';

export default function PublicationDetail({
  api,
  initialPublicationId = '',
}: {
  api: AnalyticsClient;
  initialPublicationId?: string;
}) {
  const [publicationId, setPublicationId] = useState(initialPublicationId);
  const [search, setSearch] = useState('');

  const publications = useAsyncData((signal) => api.publications({ limit: 100 }, signal), []);
  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const freshness = useAsyncData((signal) => api.freshness(signal), []);

  const list: PublicationSummary[] = publications.data?.items ?? [];
  const term = search.trim().toLowerCase();
  const filtered = term
    ? list.filter(
        (item) =>
          (item.title ?? '').toLowerCase().includes(term) ||
          item.remote_post_id.toLowerCase().includes(term) ||
          item.publication_id.toLowerCase().includes(term),
      )
    : list;
  const selected = list.find((item) => item.publication_id === publicationId) ?? list[0] ?? null;
  const id = selected?.publication_id ?? '';
  const account = accounts.data?.items.find((item) => item.id === selected?.account_id) ?? null;

  const detail = useAsyncData((signal) => (id ? api.publication(id, signal) : Promise.resolve(null)), [id]);
  const retention = useAsyncData((signal) => (id ? api.retention(id, signal) : Promise.resolve(null)), [id]);
  const timeline = useAsyncData((signal) => (id ? api.timeline(id, signal) : Promise.resolve(null)), [id]);
  const definitions = useAsyncData(
    (signal) => (selected ? api.metrics({ platform: selected.platform, limit: 200 }, signal) : Promise.resolve(null)),
    [selected?.platform ?? ''],
  );
  const runs = useAsyncData(
    (signal) => (selected ? api.runs({ account_id: selected.account_id, limit: 10 }, signal) : Promise.resolve(null)),
    [selected?.account_id ?? ''],
  );

  const definitionMap = useMemo(() => indexDefinitions(definitions.data?.items ?? []), [definitions.data]);
  const windowGroups = useMemo(() => metricsByWindow(detail.data?.metrics ?? []), [detail.data]);
  const retentionResult = useMemo(() => retentionChart(retention.data), [retention.data]);
  const historySeries = useMemo(
    () => timelineSeries(timeline.data?.points ?? [], definitionMap),
    [timeline.data, definitionMap],
  );
  const exposed = useMemo(
    () => (selected ? conceptsForPlatform(definitions.data?.items ?? [], selected.platform) : []),
    [definitions.data, selected?.platform],
  );
  const measuredKeys = useMemo(
    () => new Set((detail.data?.metrics ?? []).map((metric) => metric.metric_key)),
    [detail.data],
  );

  const freshnessRow = freshness.data?.accounts.find((row) => row.account_id === selected?.account_id) ?? null;

  function reloadAll() {
    publications.reload();
    accounts.reload();
    freshness.reload();
    detail.reload();
    retention.reload();
    timeline.reload();
    definitions.reload();
    runs.reload();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Publication detail"
        subtitle="Per-published-object metrics, retention and collection history — from stored observations only."
        actions={
          <>
            <label className="text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Search</span>
              <input
                type="search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="title or remote id"
                className="mt-1 w-48 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              />
            </label>
            <label className="text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Publication</span>
              <select
                className="mt-1 max-w-[22rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
                value={id}
                onChange={(event) => setPublicationId(event.target.value)}
                disabled={filtered.length === 0}
              >
                {filtered.length === 0 ? <option value="">no published object</option> : null}
                {filtered.map((item) => (
                  <option key={item.publication_id} value={item.publication_id}>
                    {platformLabel(item.platform)} · {item.title ?? item.remote_post_id}
                  </option>
                ))}
              </select>
            </label>
            <Button onClick={reloadAll} busy={detail.loading || retention.loading || timeline.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock error={publications.error ?? detail.error ?? retention.error ?? timeline.error} onRetry={reloadAll} />

      {publications.loading && !publications.data ? <LoadingBlock label="Loading publications..." /> : null}

      {!selected ? (
        <EmptyState
          title="No published object"
          hint="Analytics record a publication only after a publish job has a provider post id. Publish something first, or connect an account."
        />
      ) : (
        <>
          <section className="grid gap-5 lg:grid-cols-3">
            <Card title="Identity" subtitle={platformLabel(selected.platform)}>
              <div className="flex flex-wrap gap-4">
                <div className="flex h-28 w-40 shrink-0 items-center justify-center rounded-lg border border-dashed border-slate-700 bg-slate-950/60 px-3 text-center text-[11px] leading-tight text-slate-500">
                  No thumbnail — the analytics API exposes no media asset, so none is shown.
                </div>
                <div className="min-w-[12rem] flex-1">
                  <MetricStatePanel
                    title="Publication"
                    rows={[
                      { label: 'Title', value: selected.title ?? '—' },
                      { label: 'Account', value: account?.display_name ?? selected.account_id },
                      { label: 'Published', value: selected.provider_published_at ? formatWhen(selected.provider_published_at) : '—' },
                      { label: 'Status', value: selected.state },
                      { label: 'Remote id', value: selected.remote_post_id },
                      { label: 'Campaign', value: selected.campaign_id ?? '—' },
                    ]}
                  />
                </div>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Badge tone={selected.has_metrics ? 'good' : 'muted'}>{selected.has_metrics ? 'metrics collected' : 'no metrics yet'}</Badge>
                {selected.hashtags.map((tag) => (
                  <Badge key={tag} tone="muted">#{tag}</Badge>
                ))}
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
                  { label: 'Last collected', value: freshnessRow?.last_collected_at ? formatWhen(freshnessRow.last_collected_at) : 'never' },
                  { label: 'Next due', value: freshnessRow?.next_due_at ? formatWhen(freshnessRow.next_due_at) : '—' },
                  { label: 'Observation windows', value: `${windowGroups.length}` },
                ]}
              />
              {freshnessRow?.blocked_reason ? <p className="mt-2 text-xs text-amber-400">{freshnessRow.blocked_reason}</p> : null}
            </Card>

            <Card title="Provider exposure" subtitle={`What ${platformLabel(selected.platform)} actually declares in the registry`}>
              {exposed.length === 0 ? (
                <EmptyState title="No concept metadata" hint="The metric registry has no concepts for this platform." />
              ) : (
                <div className="max-h-60 space-y-1 overflow-auto">
                  {exposed.map((concept) => (
                    <div key={concept.concept} className="flex items-center justify-between gap-2 text-xs">
                      <span className="min-w-0 truncate text-slate-400" title={concept.definition}>
                        {concept.concept} · <span className="font-mono text-slate-500">{concept.metric_key}</span>
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        <span className="text-slate-500">{concept.unit}</span>
                        <Badge tone={measuredKeys.has(concept.metric_key) ? 'good' : 'muted'}>
                          {measuredKeys.has(concept.metric_key) ? 'collected' : 'not collected'}
                        </Badge>
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <p className="mt-2 text-[11px] text-slate-500">
                A concept a platform does not list here is not exposed by that provider at all — it is never estimated.
              </p>
            </Card>
          </section>

          <Card
            title="Performance metrics"
            subtitle="Observed values by performance window. An unavailable metric keeps its reason; it is never a zero."
          >
            {detail.loading && !detail.data ? (
              <LoadingBlock label="Loading metrics..." />
            ) : windowGroups.length === 0 ? (
              <EmptyState
                title="No metrics stored"
                hint="No aggregation exists for this publication yet. Collection may be blocked by a missing permission (see Account performance)."
              />
            ) : (
              <div className="space-y-4">
                {windowGroups.map((group) => (
                  <div key={group.window}>
                    <p className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">Window {group.window}</p>
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-xs">
                        <thead className="text-slate-500">
                          <tr>
                            <th className="py-1 pr-3">Metric</th>
                            <th className="py-1 pr-3">Provider name</th>
                            <th className="py-1 pr-3">Value</th>
                            <th className="py-1">Availability</th>
                          </tr>
                        </thead>
                        <tbody className="text-slate-300">
                          {group.metrics.map((metric) => {
                            const definition = definitionMap.get(metric.metric_key);
                            return (
                              <tr key={`${group.window}-${metric.metric_key}`} className="border-t border-slate-800/70">
                                <td className="py-1 pr-3 font-mono">{metric.metric_key}</td>
                                <td className="py-1 pr-3 text-slate-500">{definition?.provider_metric_name ?? '—'}</td>
                                <td className="py-1 pr-3 font-mono">
                                  {metric.value === null ? '—' : formatMetricValue(metric.value, metric.unit)}
                                </td>
                                <td className="py-1">
                                  <AvailabilityChip availability={metric.availability} reason={metric.reason} />
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <section className="grid gap-5 lg:grid-cols-2">
            <Card
              title="Video retention"
              subtitle="Only a curve the provider actually returned; nothing is interpolated or invented"
            >
              {retention.loading && !retention.data ? (
                <LoadingBlock label="Checking retention..." />
              ) : retentionResult.chart ? (
                <>
                  <MetricWidget
                    title={retentionResult.chart.series.key}
                    subtitle={`${retentionResult.chart.bucketCount} intervals · ${retentionResult.chart.series.unit} · value is the share of plays still watching`}
                    series={[retentionResult.chart.series]}
                    bucket="day"
                    height={220}
                    emptyHint="No retention observation for this publication."
                  />
                  {retentionResult.chart.incomplete ? (
                    <p className="mt-2 text-xs text-amber-400">
                      The provider returned an incomplete curve ({retentionResult.chart.bucketCount} of the documented intervals); it is shown as returned, not padded.
                    </p>
                  ) : null}
                  <p className="mt-1 text-[11px] text-slate-500">
                    Play-through interval (01 = start, {String(retentionResult.chart.bucketCount).padStart(String(retentionResult.chart.bucketCount).length, '0')} = end).
                  </p>
                </>
              ) : (
                <div className="rounded-lg border border-dashed border-slate-700/70 px-4 py-6 text-center">
                  <p className="text-sm text-slate-300">Retention not available</p>
                  <p className="mt-1 text-xs text-slate-500">{retentionResult.reason}</p>
                </div>
              )}
            </Card>

            <Card title="Performance history" subtitle="Stored observations over time; a missing collection is a gap, not a zero">
              {timeline.loading && !timeline.data ? (
                <LoadingBlock label="Loading history..." />
              ) : historySeries.length === 0 ? (
                <EmptyState title="No history stored" hint="No observation has been recorded for this publication yet." />
              ) : (
                <div className="space-y-4">
                  {historySeries.map((series) => (
                    <MetricWidget
                      key={series.key}
                      title={series.key}
                      subtitle={`${series.label ?? ''} · gaps mean missing, not zero`}
                      series={[series]}
                      bucket="day"
                      height={180}
                      emptyHint="No observation in the collected history."
                    />
                  ))}
                </div>
              )}
            </Card>
          </section>

          <Card title="Collection history" subtitle="Real runs on the analytics queue lane for this account">
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
