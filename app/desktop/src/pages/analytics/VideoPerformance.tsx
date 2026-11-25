/**
 * Analytics Center — Video Performance (P8.22).
 *
 * One table of published videos ranked by an observed metric, plus the retention curve and the
 * collected history for one selected video, plus a compatible-metric comparison of selected videos.
 *
 * The rules the page will not break:
 * * a column is a *concept* (views, reach, likes...), resolved against each row's own platform, so a
 *   TikTok row is never shown a Facebook metric's value — an unsupported concept is `—` with a reason,
 *   and an unobserved one is `—` with its availability (never `0`);
 * * retention is drawn only from a curve the provider actually returned (TikTok never does);
 * * a comparison only ever runs on one metric key at one window, and it says so — a single-window
 *   value per video is not a statistical test;
 * * the analytics API exposes no media asset, so there is **no thumbnail**; the column says so.
 */
import { useMemo, useState } from 'react';
import {
  type AnalyticsClient,
  type MetricDefinition,
  type Platform,
} from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import { AVAILABILITY_LABELS, formatMetricValue } from '../../components/chartModel';
import {
  conceptsForPlatform,
  indexDefinitions,
  rankVideoCells,
  resolveConceptCells,
  retentionChart,
  timelineSeries,
  valuesByMetric,
  videoComparison,
  videoConcepts,
} from '../../components/analyticsModel';
import { Badge, Button, Card, EmptyState, formatWhen } from '../../components/ui';
import {
  AvailabilityChip,
  DateRangePicker,
  ErrorBlock,
  LoadingBlock,
  PageHeader,
  StaleBadge,
  platformLabel,
  useAsyncData,
  type RangeValue,
  defaultRange,
} from './shared';

const PLATFORMS: Platform[] = ['facebook', 'instagram', 'tiktok'];
const WINDOWS = ['h24', 'h48', 'd7', 'd28', 'lifetime'] as const;

export default function VideoPerformance({
  api,
  onOpenPublication,
  initialSelectedId = '',
  initialCompareIds = [],
}: {
  api: AnalyticsClient;
  onOpenPublication?: (publicationId: string) => void;
  /** Prefill used by the visual harness (and by a deep link); the page works without it. */
  initialSelectedId?: string;
  initialCompareIds?: string[];
}) {
  const [range, setRange] = useState<RangeValue>(() => defaultRange('28d'));
  const [platform, setPlatform] = useState<Platform | ''>('');
  const [accountId, setAccountId] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [concept, setConcept] = useState('');
  const [windowKind, setWindowKind] = useState<(typeof WINDOWS)[number]>('h24');
  const [direction, setDirection] = useState<'desc' | 'asc'>('desc');
  const [selectedId, setSelectedId] = useState(initialSelectedId);
  const [compareIds, setCompareIds] = useState<string[]>(initialCompareIds);

  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const metrics = useAsyncData((signal) => api.metrics({ limit: 200 }, signal), []);
  const campaigns = useAsyncData((signal) => api.campaigns({ limit: 100 }, signal), []);
  const freshness = useAsyncData((signal) => api.freshness(signal), []);

  const definitions = metrics.data?.items ?? [];
  const definitionMap = useMemo(() => indexDefinitions(definitions), [definitions]);
  const accountList = accounts.data?.items ?? [];
  const visibleAccounts = platform ? accountList.filter((account) => account.platform === platform) : accountList;
  const account = visibleAccounts.find((item) => item.id === accountId) ?? null;
  const activePlatform = platform || account?.platform || '';

  // Concepts, not metric keys: each row resolves the concept against its own platform below.
  const columns = useMemo(() => videoConcepts(definitions, activePlatform || undefined), [definitions, activePlatform]);
  const exposed = useMemo(
    () => (activePlatform ? conceptsForPlatform(definitions, activePlatform) : []),
    [definitions, activePlatform],
  );
  const effectiveSort = columns.includes(concept) ? concept : columns.includes('views') ? 'views' : columns[0] ?? '';
  const sortPlatforms = useMemo(() => {
    const keys = new Set<string>();
    for (const definition of definitions) {
      if (definition.preferred_for !== effectiveSort && !definition.metric_key.endsWith(`.${effectiveSort}`)) continue;
      keys.add(`${definition.platform}:${definition.metric_key}`);
    }
    return [...keys].sort();
  }, [definitions, effectiveSort]);

  const publications = useAsyncData(
    (signal) => api.publications({ account_id: account?.id, platform: platform || undefined, start: range.start, end: range.end, limit: 100 }, signal),
    [account?.id ?? '', platform, range.start, range.end],
  );
  const aggregations = useAsyncData(
    (signal) =>
      api.aggregations({ scope: 'publication', account_id: account?.id, window_kind: windowKind, limit: 200 }, signal),
    [windowKind, account?.id ?? ''],
  );

  const campaignsList = campaigns.data?.items ?? [];
  const campaignFiltered = useMemo(() => {
    const list = publications.data?.items ?? [];
    return campaignId ? list.filter((item) => item.campaign_id === campaignId) : list;
  }, [publications.data, campaignId]);

  const matrix = useMemo(() => valuesByMetric(aggregations.data?.items ?? [], windowKind), [aggregations.data, windowKind]);
  const cellsByConcept = useMemo(() => {
    const out = new Map<string, Map<string, ReturnType<typeof resolveConceptCells>[number]>>();
    for (const columnConcept of columns) {
      const rows = resolveConceptCells(campaignFiltered, matrix, definitions, columnConcept);
      out.set(columnConcept, new Map(rows.map((cell) => [cell.publication.publication_id, cell])));
    }
    return out;
  }, [campaignFiltered, matrix, definitions, columns]);
  const sortCells = useMemo(
    () => [...(cellsByConcept.get(effectiveSort)?.values() ?? [])],
    [cellsByConcept, effectiveSort],
  );
  const leaderboard = useMemo(() => rankVideoCells(sortCells, { direction }), [sortCells, direction]);

  const selected = campaignFiltered.find((item) => item.publication_id === selectedId) ?? null;
  const timeline = useAsyncData((signal) => (selected ? api.timeline(selected.publication_id, signal) : Promise.resolve(null)), [selected?.publication_id ?? '']);
  const retention = useAsyncData((signal) => (selected ? api.retention(selected.publication_id, signal) : Promise.resolve(null)), [selected?.publication_id ?? '']);
  const retentionResult = useMemo(() => retentionChart(retention.data), [retention.data]);
  const historySeries = useMemo(() => timelineSeries(timeline.data?.points ?? [], definitionMap), [timeline.data, definitionMap]);

  const comparison = useMemo(() => {
    const chosen = sortCells.filter((cell) => compareIds.includes(cell.publication.publication_id));
    return videoComparison(chosen);
  }, [sortCells, compareIds]);

  // An aggregation page holds at most 200 rows; if there are more, say so rather than quietly ranking a subset.
  const truncatedNote = useMemo(() => {
    const page = aggregations.data;
    if (!page) return null;
    if (page.total <= page.items.length) return null;
    return `Showing ${page.items.length} of ${page.total} stored aggregations for this window: the ranking covers those rows only and does not claim to cover every publication.`;
  }, [aggregations.data]);

  const dataAsOf = useMemo(
    () => (aggregations.data?.items ?? []).map((row) => row.computed_at).sort().at(-1) ?? null,
    [aggregations.data],
  );
  const freshnessRow = freshness.data?.accounts.find((row) => row.account_id === account?.id) ?? null;

  function toggleCompare(id: string) {
    setCompareIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id].slice(-4)));
  }

  function reloadAll() {
    accounts.reload();
    metrics.reload();
    campaigns.reload();
    freshness.reload();
    publications.reload();
    aggregations.reload();
    timeline.reload();
    retention.reload();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Video Performance"
        subtitle="Published videos ranked by an observed metric, with retention and history for one video."
        actions={
          <>
            <DateRangePicker value={range} onChange={setRange} />
            <Button onClick={reloadAll} busy={publications.loading || aggregations.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock error={accounts.error ?? metrics.error ?? publications.error ?? aggregations.error} onRetry={reloadAll} />

      <Card title="Filters" subtitle="Every control here changes the queries below">
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Platform</span>
            <select
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={platform}
              onChange={(event) => { setPlatform(event.target.value as Platform | ''); setAccountId(''); }}
            >
              <option value="">all</option>
              {PLATFORMS.map((value) => (
                <option key={value} value={value}>
                  {platformLabel(value)}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Account</span>
            <select
              className="mt-1 max-w-[18rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={account?.id ?? ''}
              onChange={(event) => setAccountId(event.target.value)}
              disabled={visibleAccounts.length === 0}
            >
              <option value="">all accounts</option>
              {visibleAccounts.map((item) => (
                <option key={item.id} value={item.id}>
                  {platformLabel(item.platform)} · {item.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Campaign</span>
            <select
              className="mt-1 max-w-[14rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={campaignId}
              onChange={(event) => setCampaignId(event.target.value)}
            >
              <option value="">any</option>
              {campaignsList.map((item) => (
                <option key={item.campaign_id} value={item.campaign_id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Sort by concept</span>
            <select
              className="mt-1 max-w-[18rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={effectiveSort}
              onChange={(event) => setConcept(event.target.value)}
            >
              {columns.length === 0 ? <option value="">no video concept in the registry</option> : null}
              {columns.map((columnConcept) => (
                <option key={columnConcept} value={columnConcept}>
                  {columnConcept}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Window</span>
            <select
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={windowKind}
              onChange={(event) => setWindowKind(event.target.value as (typeof WINDOWS)[number])}
            >
              {WINDOWS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input type="checkbox" checked={direction === 'asc'} onChange={(event) => setDirection(event.target.checked ? 'asc' : 'desc')} />
            lowest first
          </label>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span>
            {columns.length} concept column(s) for {activePlatform ? platformLabel(activePlatform) : 'each row’s own platform'}
          </span>
          {freshnessRow ? <StaleBadge stale={freshnessRow.stale} ageHours={freshnessRow.age_hours} /> : null}
          {dataAsOf ? <span>data as of {formatWhen(dataAsOf)}</span> : null}
        </div>
        {sortPlatforms.length ? (
          <p className="mt-1 text-[11px] text-slate-500">
            {effectiveSort} resolves to {sortPlatforms.join(' · ')} — one metric per platform, never pooled
          </p>
        ) : null}
        {exposed.length ? (
          <p className="mt-1 text-[11px] text-slate-500">
            Provider concepts: {exposed.map((item) => `${item.concept} → ${item.metric_key}`).join(' · ')}
          </p>
        ) : null}
      </Card>

      {publications.loading && !publications.data ? <LoadingBlock label="Loading videos..." /> : null}

      <Card
        title="Video leaderboard"
        subtitle={`Ranked by ${effectiveSort || 'an observed concept'} measured on each row's own platform · window ${windowKind}. Nothing a provider omits is estimated.`}
      >
        {leaderboard.ranked.length === 0 && leaderboard.unmeasured.length === 0 ? (
          <EmptyState title="No published video" hint="Nothing in this selection has a provider post id yet, or the date range is empty." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-1 pr-3">Video</th>
                  <th className="py-1 pr-3">Platform</th>
                  <th className="py-1 pr-3">Account</th>
                  <th className="py-1 pr-3">Published</th>
                  {columns.map((columnConcept) => (
                    <th
                      key={columnConcept}
                      className={`cursor-pointer py-1 pr-3 ${columnConcept === effectiveSort ? 'text-slate-300' : ''}`}
                      title={`sort by ${columnConcept}`}
                      onClick={() => setConcept(columnConcept)}
                    >
                      {columnConcept}
                    </th>
                  ))}
                  <th className="py-1">Details</th>
                </tr>
              </thead>
              <tbody className="text-slate-300">
                {[...leaderboard.ranked, ...leaderboard.unmeasured].map((cell) => {
                  const publication = cell.publication;
                  return (
                  <tr key={publication.publication_id} className="border-t border-slate-800/70">
                    <td className="py-1 pr-3">
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={compareIds.includes(publication.publication_id)}
                          onChange={() => toggleCompare(publication.publication_id)}
                          title="add to comparison"
                        />
                        <span className="min-w-0 truncate" title="Thumbnail unavailable: the analytics API exposes no media asset">
                          {publication.title ?? publication.remote_post_id}
                        </span>
                      </label>
                    </td>
                    <td className="py-1 pr-3">{platformLabel(publication.platform)}</td>
                    <td className="py-1 pr-3 text-slate-500">
                      {accountList.find((item) => item.id === publication.account_id)?.display_name ?? publication.account_id}
                    </td>
                    <td className="py-1 pr-3">{publication.provider_published_at ? formatWhen(publication.provider_published_at) : '—'}</td>
                    {columns.map((columnConcept) => (
                      <td key={columnConcept} className="py-1 pr-3">
                        {renderCell(cellsByConcept.get(columnConcept)?.get(publication.publication_id), columnConcept === effectiveSort)}
                      </td>
                    ))}
                    <td className="py-1">
                      <div className="flex items-center gap-2">
                        <button className="text-sky-400 hover:underline" onClick={() => setSelectedId(publication.publication_id)}>
                          select
                        </button>
                        {onOpenPublication ? (
                          <button className="text-slate-400 hover:underline" onClick={() => onOpenPublication(publication.publication_id)}>
                            open
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="mt-2 text-[11px] text-slate-500">
              {leaderboard.ranked.length} video(s) with an observed {effectiveSort || 'value'} on their own platform; {leaderboard.unmeasured.length} without one (listed after the ranked rows, never shown as 0).
              No thumbnail is shown: the analytics API exposes no media asset. A value that the provider did not return is an em dash.
            </p>
            {truncatedNote ? <p className="mt-1 text-[11px] text-amber-400">{truncatedNote}</p> : null}
          </div>
        )}
      </Card>

      {compareIds.length >= 2 ? (
        <Card
          title="Video comparison"
          subtitle={`Window ${windowKind} · only videos measured by the same metric key are ranked together — a TikTok view and a Facebook view are different measurements`}
        >
          {comparison.groups.length === 0 ? (
            <EmptyState
              title="Nothing compatible to compare"
              hint={`The selected videos do not share one ${effectiveSort || 'metric'} definition. Compatibility is decided by metric key, unit and window, not by the concept name.`}
            />
          ) : (
            <div className="space-y-4">
              {comparison.groups.map((group) => (
                <div key={group.metricKey} className="space-y-1">
                  <p className="text-[11px] uppercase tracking-wide text-slate-500">
                    {group.metricKey} · {group.unit} · n={group.cells.length} video(s)
                  </p>
                  {group.cells.map((cell) => (
                    <div key={cell.publication.publication_id} className="flex items-center justify-between gap-3 text-xs">
                      <span className="min-w-0 truncate text-slate-300">
                        {cell.publication.title ?? cell.publication.remote_post_id}
                        <span className="ml-2 text-slate-600">{platformLabel(cell.publication.platform)}</span>
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        <span className="font-mono text-slate-100">{cell.value === null ? '—' : formatMetricValue(cell.value, cell.unit)}</span>
                        <AvailabilityChip availability={cell.availability} />
                      </span>
                    </div>
                  ))}
                </div>
              ))}
              {comparison.excluded.length ? (
                <ul className="list-disc pl-4 text-[11px] text-amber-400">
                  {comparison.excluded.map((item) => (
                    <li key={item.publication.publication_id}>
                      {item.publication.title ?? item.publication.remote_post_id}: excluded — {item.reason}
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className="text-[11px] text-slate-500">
                n=1 observation per video at this window; values are provider readings, not a modelled estimate.
                Correlation between videos is not causation, and two videos are never compared across metric keys.
              </p>
            </div>
          )}
        </Card>
      ) : (
        <p className="text-xs text-slate-500">Tick two or more videos to compare them on one compatible metric.</p>
      )}

      <section className="grid gap-5 lg:grid-cols-2">
        <Card title="Video retention" subtitle="Only a curve the provider actually returned; nothing is interpolated">
          {!selected ? (
            <EmptyState title="No video selected" hint="Use 'select' on a leaderboard row to load its retention and history." />
          ) : retention.loading && !retention.data ? (
            <LoadingBlock label="Checking retention..." />
          ) : retentionResult.chart ? (
            <>
              <MetricWidget
                title={retentionResult.chart.series.key}
                subtitle={`${retentionResult.chart.bucketCount} intervals · ${retentionResult.chart.series.unit} · share of plays still watching`}
                series={[retentionResult.chart.series]}
                height={220}
                emptyHint="No retention observation."
              />
              {retentionResult.chart.incomplete ? (
                <p className="mt-2 text-xs text-amber-400">
                  Incomplete curve ({retentionResult.chart.bucketCount} of the documented intervals); shown as returned.
                </p>
              ) : null}
            </>
          ) : (
            <div className="rounded-lg border border-dashed border-slate-700/70 px-4 py-6 text-center">
              <p className="text-sm text-slate-300">Retention not available</p>
              <p className="mt-1 text-xs text-slate-500">{retentionResult.reason}</p>
            </div>
          )}
        </Card>

        <Card title="Collected history" subtitle="Stored observations for the selected video; a missing collection is a gap">
          {!selected ? (
            <EmptyState title="No video selected" hint="Use 'select' on a leaderboard row to load its history." />
          ) : timeline.loading && !timeline.data ? (
            <LoadingBlock label="Loading history..." />
          ) : historySeries.length === 0 ? (
            <EmptyState title="No history stored" hint="No observation has been recorded for this video yet." />
          ) : (
            <div className="space-y-4">
              {historySeries.map((series) => (
                <MetricWidget
                  key={series.key}
                  title={series.key}
                  subtitle={`${series.label ?? ''} · gaps mean missing, not zero`}
                  series={[series]}
                  bucket="day"
                  height={170}
                  emptyHint="No observation in the collected history."
                />
              ))}
            </div>
          )}
        </Card>
      </section>

      {selected ? (
        <Card title="Selected video" subtitle="Retention and watch-time metrics are provider-specific; a metric this provider omits is not estimated">
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
            <span className="text-slate-200">{selected.title ?? selected.remote_post_id}</span>
            <Badge tone="muted">{platformLabel(selected.platform)}</Badge>
            <Badge tone={selected.has_metrics ? 'good' : 'muted'}>{selected.has_metrics ? 'metrics collected' : 'no metrics yet'}</Badge>
            {selected.provider_published_at ? <span>published {formatWhen(selected.provider_published_at)}</span> : null}
            {onOpenPublication ? (
              <Button variant="ghost" onClick={() => onOpenPublication(selected.publication_id)}>
                Open publication detail
              </Button>
            ) : null}
          </div>
          <WatchTimeSummary metrics={matrixMetrics(matrix, selected.publication_id)} definitions={definitionMap} />
        </Card>
      ) : null}
    </div>
  );
}

function renderCell(
  cell: { value: number | null; unit: string; availability: keyof typeof AVAILABILITY_LABELS; metricKey: string | null } | undefined,
  highlight: boolean,
) {
  if (!cell || cell.value === null) {
    const availability = cell?.availability ?? 'not_collected';
    return (
      <span className="flex items-center gap-1">
        <span
          className="font-mono text-slate-600"
          title={cell?.metricKey ? `${cell.metricKey}: ${AVAILABILITY_LABELS[availability]}` : 'this platform exposes no metric for the concept'}
        >
          —
        </span>
        {highlight ? <AvailabilityChip availability={availability} reason={cell?.metricKey ? undefined : 'not exposed by this platform'} /> : null}
      </span>
    );
  }
  return <span className="font-mono text-slate-200">{formatMetricValue(cell.value, cell.unit)}</span>;
}

/** The metrics stored for one publication across the columns that were fetched. */
function matrixMetrics(matrix: Map<string, Map<string, { value: number | null; unit: string; availability: keyof typeof AVAILABILITY_LABELS; sample_size: number | null }>>, publicationId: string) {
  const rows: { metricKey: string; value: number | null; unit: string; availability: keyof typeof AVAILABILITY_LABELS }[] = [];
  for (const [metricKey, byPublication] of matrix) {
    const entry = byPublication.get(publicationId);
    if (!entry) continue;
    rows.push({ metricKey, value: entry.value, unit: entry.unit, availability: entry.availability });
  }
  return rows.sort((a, b) => a.metricKey.localeCompare(b.metricKey));
}

function WatchTimeSummary({
  metrics,
  definitions,
}: {
  metrics: { metricKey: string; value: number | null; unit: string; availability: keyof typeof AVAILABILITY_LABELS }[];
  definitions: Map<string, MetricDefinition>;
}) {
  if (metrics.length === 0) {
    return <p className="mt-2 text-xs text-slate-500">No stored metric for this video at the chosen window.</p>;
  }
  return (
    <div className="mt-2 grid gap-1 sm:grid-cols-2">
      {metrics.map((metric) => (
        <div key={metric.metricKey} className="flex items-center justify-between gap-3 text-xs">
          <span className="min-w-0 truncate text-slate-400" title={definitions.get(metric.metricKey)?.definition_text}>
            {metric.metricKey}
          </span>
          <span className="flex shrink-0 items-center gap-2">
            <span className="font-mono text-slate-200">{metric.value === null ? '—' : formatMetricValue(metric.value, metric.unit)}</span>
            <AvailabilityChip availability={metric.availability} />
          </span>
        </div>
      ))}
    </div>
  );
}
