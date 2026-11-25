/**
 * Analytics Center — Content comparison (P8.21).
 *
 * Compares published objects **within one account** (one platform), because metric keys and units are
 * platform-specific: `fb.post.media_views` is not `tt.video.views`, and reach is not views. The page
 * resolves a registry **concept** (`preferred_for`) to that platform's own metric, ranks publications
 * by an observed value, and uses the deterministic comparison service for the group medians.
 *
 * Every comparison states the metric definition, the window, the sample size and the limitations the
 * backend returns; nothing here implies that a difference is caused by the grouping attribute.
 */
import { useMemo, useState } from 'react';
import {
  AnalyticsApiError,
  type AnalyticsClient,
  type AccountSummary,
  type Platform,
  type PublicationSummary,
} from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import { seriesFromRows } from '../../components/chartModel';
import {
  indexDefinitions,
  metricKeyForConcept,
  publicationValues,
  rankPublications,
  type RankedPublication,
} from '../../components/analyticsModel';
import { Badge, Button, Card, EmptyState } from '../../components/ui';
import {
  AvailabilityChip,
  DateRangePicker,
  ErrorBlock,
  LoadingBlock,
  PageHeader,
  defaultRange,
  formatRangeLabel,
  platformLabel,
  useAsyncData,
  type RangeValue,
} from './shared';

const PLATFORMS: Platform[] = ['facebook', 'instagram', 'tiktok'];
const WINDOWS = ['h24', 'h48', 'd7', 'd28', 'lifetime'] as const;
const ATTRIBUTES = ['format', 'topic', 'hashtag', 'platform'] as const;

export default function ContentComparison({ api }: { api: AnalyticsClient }) {
  const [range, setRange] = useState<RangeValue>(() => defaultRange('28d'));
  const [platform, setPlatform] = useState<Platform | ''>('');
  const [accountId, setAccountId] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [concept, setConcept] = useState('');
  const [attribute, setAttribute] = useState<(typeof ATTRIBUTES)[number]>('format');
  const [windowKind, setWindowKind] = useState<(typeof WINDOWS)[number]>('h24');

  const metrics = useAsyncData((signal) => api.metrics({ limit: 200 }, signal), []);
  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const campaigns = useAsyncData((signal) => api.campaigns({ limit: 100 }, signal), []);

  const definitions = metrics.data?.items ?? [];
  const definitionMap = useMemo(() => indexDefinitions(definitions), [definitions]);
  const accountList: AccountSummary[] = accounts.data?.items ?? [];

  const concepts = useMemo(() => {
    const seen: string[] = [];
    for (const definition of definitions) if (definition.preferred_for && !seen.includes(definition.preferred_for)) seen.push(definition.preferred_for);
    return seen.sort((a, b) => a.localeCompare(b));
  }, [definitions]);

  const visibleAccounts = platform ? accountList.filter((account) => account.platform === platform) : accountList;
  const account = visibleAccounts.find((item) => item.id === accountId) ?? visibleAccounts[0] ?? null;
  const activePlatform = platform || account?.platform || '';
  const effectiveConcept = concept && concepts.includes(concept) ? concept : concepts.includes('views') ? 'views' : concepts[0] ?? '';
  const metricKey = useMemo(
    () => (effectiveConcept ? metricKeyForConcept(definitions, effectiveConcept, activePlatform || undefined) : null),
    [definitions, effectiveConcept, activePlatform],
  );
  const definition = metricKey ? definitionMap.get(metricKey) ?? null : null;

  const publications = useAsyncData(
    (signal) =>
      account
        ? api.publications({ account_id: account.id, start: range.start, end: range.end, limit: 100 }, signal)
        : Promise.resolve(null),
    [account?.id ?? '', range.start, range.end],
  );

  const aggregations = useAsyncData(
    (signal) => (metricKey ? api.aggregations({ scope: 'publication', metric_key: metricKey, window_kind: windowKind, limit: 200 }, signal) : Promise.resolve(null)),
    [metricKey, windowKind],
  );

  const trend = useAsyncData(
    (signal) =>
      account && metricKey
        ? api.series({ account_id: account.id, metric_key: metricKey, bucket: 'day', start: range.start, end: range.end }, signal)
        : Promise.resolve(null),
    [account?.id ?? '', metricKey, range.start, range.end],
  );

  const comparison = useAsyncData(async (signal) => {
    if (!account || !metricKey) return { value: null, note: 'choose an account and a concept' };
    try {
      const result = await api.compare(
        { account_id: account.id, metric_key: metricKey, attribute, window_kind: windowKind, max_groups: 3 },
        signal,
      );
      return { value: result, note: '' };
    } catch (error) {
      const note =
        error instanceof AnalyticsApiError && error.code === 'NO_PUBLICATIONS'
          ? 'no published objects with a provider id in this selection'
          : error instanceof Error
            ? error.message
            : String(error);
      return { value: null, note };
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account?.id ?? '', metricKey, attribute, windowKind]);

  const campaignFiltered = useMemo(() => {
    const list = publications.data?.items ?? [];
    return campaignId ? list.filter((item) => item.campaign_id === campaignId) : list;
  }, [publications.data, campaignId]);

  const values = useMemo(
    () => publicationValues(aggregations.data?.items ?? [], metricKey ?? '', windowKind),
    [aggregations.data, metricKey, windowKind],
  );
  const top = useMemo(() => rankPublications(campaignFiltered, values, { direction: 'desc', limit: 5 }), [campaignFiltered, values]);
  const bottom = useMemo(() => rankPublications(campaignFiltered, values, { direction: 'asc', limit: 5 }), [campaignFiltered, values]);

  const trendSeries = useMemo(() => {
    if (!trend.data || !definition) return [];
    return [
      seriesFromRows(
        definition.metric_key,
        definition.provider_metric_name,
        definition.unit,
        trend.data.points.map((point) => ({ at: point.bucket_start, value: point.value, availability: point.availability })),
      ),
    ];
  }, [trend.data, definition]);

  function reloadAll() {
    metrics.reload();
    accounts.reload();
    campaigns.reload();
    publications.reload();
    aggregations.reload();
    trend.reload();
    comparison.reload();
  }

  const comparisonResult = comparison.data?.value ?? null;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Content comparison"
        subtitle="Rank and compare published content within one account — same platform, same metric, disclosed limitations."
        actions={
          <>
            <DateRangePicker value={range} onChange={setRange} />
            <Button onClick={reloadAll} busy={publications.loading || aggregations.loading || comparison.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock
        error={metrics.error ?? accounts.error ?? publications.error ?? aggregations.error ?? trend.error}
        onRetry={reloadAll}
      />

      <Card title="Filters" subtitle="Every control here changes the queries below">
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Platform</span>
            <select
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={platform}
              onChange={(event) => { setPlatform(event.target.value as Platform | ''); setAccountId(''); }}
            >
              <option value="">all connected platforms</option>
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
              className="mt-1 max-w-[20rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={account?.id ?? ''}
              onChange={(event) => setAccountId(event.target.value)}
              disabled={visibleAccounts.length === 0}
            >
              {visibleAccounts.length === 0 ? <option value="">no connected account</option> : null}
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
              className="mt-1 max-w-[16rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={campaignId}
              onChange={(event) => setCampaignId(event.target.value)}
            >
              <option value="">any campaign</option>
              {(campaigns.data?.items ?? []).map((campaign) => (
                <option key={campaign.campaign_id} value={campaign.campaign_id}>
                  {campaign.name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Metric concept</span>
            <select
              className="mt-1 max-w-[16rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={effectiveConcept}
              onChange={(event) => setConcept(event.target.value)}
            >
              {concepts.length === 0 ? <option value="">no concepts in the registry</option> : null}
              {concepts.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Compare by</span>
            <select
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={attribute}
              onChange={(event) => setAttribute(event.target.value as (typeof ATTRIBUTES)[number])}
            >
              {ATTRIBUTES.map((value) => (
                <option key={value} value={value}>
                  {value}
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
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Resolved metric — {definition ? `${definition.metric_key} (${definition.unit}, ${definition.metric_class})` : 'no metric for this concept on this platform'}
          {activePlatform ? ` · ${platformLabel(activePlatform)}` : ''}
        </p>
      </Card>

      {definition ? (
        <div className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs text-slate-400">
          <span className="font-semibold text-slate-300">Metric definition:</span> {definition.definition_text}
          {definition.is_estimate ? <Badge tone="warn">estimate</Badge> : null}
          <span className="ml-2">
            window {windowKind} · measured by {definition.provider_metric_name} · {definition.provider_object}
          </span>
        </div>
      ) : (
        <EmptyState title="Concept not exposed here" hint="This platform's registry has no metric for the chosen concept, so nothing is compared." />
      )}

      {publications.loading && !publications.data ? <LoadingBlock label="Loading publications..." /> : null}

      {account ? (
        <section className="grid gap-5 lg:grid-cols-2">
          <Card
            title="Top performers"
            subtitle={`Highest observed ${definition?.metric_key ?? effectiveConcept} · window ${windowKind}`}
          >
            <RankTable result={top} unit={definition?.unit} emptyHint="No publication in range has an observed value for this metric." />
          </Card>
          <Card
            title="Lowest performers"
            subtitle={`Lowest observed ${definition?.metric_key ?? effectiveConcept} · window ${windowKind}`}
          >
            <RankTable result={bottom} unit={definition?.unit} emptyHint="No publication in range has an observed value for this metric." />
          </Card>
        </section>
      ) : (
        <EmptyState title="No connected account" hint="Connect an account to compare its published content." />
      )}

      <section className="grid gap-5 lg:grid-cols-2">
        <Card
          title={`Comparison by ${attribute}`}
          subtitle="Deterministic group medians from the comparison service; each group carries its own sample size"
        >
          {comparison.loading && !comparison.data ? (
            <LoadingBlock label="Comparing..." />
          ) : comparisonResult ? (
            <>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Badge tone={comparisonResult.sufficient === false ? 'warn' : 'muted'}>
                  {comparisonResult.sufficient === false ? 'inconclusive sample' : comparisonResult.sufficient ? 'sufficient sample' : 'sample size unknown'}
                </Badge>
                <span className="text-slate-500">
                  {comparisonResult.metric_key} · by {comparisonResult.attribute} · window {comparisonResult.window}
                </span>
              </div>
              {comparisonResult.groups.length === 0 ? (
                <EmptyState title="No groups" hint="No published object in this window had the chosen attribute to group by." />
              ) : (
                <div className="mt-3 space-y-2">
                  {comparisonResult.groups.map((group) => (
                    <div key={group.label} className="flex items-center justify-between gap-3 text-xs">
                      <span className="min-w-0 truncate text-slate-300" title={group.label}>
                        {group.label}
                      </span>
                      <span className="flex shrink-0 items-center gap-2 text-slate-300">
                        <span className="font-mono">
                          {group.median === null ? '—' : `${group.median} ${definition?.unit ?? ''}`}
                        </span>
                        <Badge tone="muted">n={group.n}</Badge>
                        <AvailabilityChip availability={group.availability} />
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {comparisonResult.limitations.length ? (
                <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-amber-400">
                  {comparisonResult.limitations.map((limitation) => (
                    <li key={limitation}>{limitation}</li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-xs text-slate-500">{comparisonResult.note}</p>
              )}
              <p className="mt-3 text-[11px] text-slate-500">
                A difference between groups is an observation about this sample, not evidence that the group caused it.
              </p>
            </>
          ) : (
            <div className="rounded-lg border border-dashed border-slate-700/70 px-4 py-6 text-center text-xs text-slate-500">
              {comparison.data?.note ?? 'Comparison unavailable.'}
            </div>
          )}
        </Card>

        <Card title="Content trend" subtitle={`Observed ${definition?.metric_key ?? effectiveConcept} over ${formatRangeLabel(range)}`}>
          {trend.loading && !trend.data ? (
            <LoadingBlock label="Loading trend..." />
          ) : definition ? (
            <MetricWidget
              title={definition.metric_key}
              subtitle={`${formatRangeLabel(range)} · ${account?.display_name ?? ''}`}
              series={trendSeries}
              bucket={trend.data?.bucket ?? 'day'}
              height={220}
              emptyHint="No stored observation for this metric in the selected range."
            />
          ) : (
            <EmptyState title="No metric" hint="Pick a concept this platform exposes." />
          )}
        </Card>
      </section>

      <p className="text-xs text-slate-500">
        Top and lowest lists cover {campaignFiltered.length} publication(s) in range
        {campaignId ? ' for the selected campaign' : ''}: {top.observedCount} with an observed value,{' '}
        {top.unavailableCount} without one (shown separately, never as zero).
      </p>
    </div>
  );
}

function RankTable({ result, unit, emptyHint }: { result: ReturnType<typeof rankPublications>; unit?: string; emptyHint: string }) {
  if (result.ranked.length === 0 && result.unmeasured.length === 0) {
    return <EmptyState title="Nothing to rank" hint={emptyHint} />;
  }
  return (
    <div className="space-y-2">
      {result.ranked.length === 0 ? <p className="text-xs text-slate-500">{emptyHint}</p> : null}
      {result.ranked.map((row: RankedPublication, index) => (
        <div key={row.publication.publication_id} className="flex items-center justify-between gap-3 text-xs">
          <span className="min-w-0 truncate text-slate-300" title={row.publication.title ?? row.publication.remote_post_id}>
            <span className="mr-2 text-slate-600">{index + 1}.</span>
            {row.publication.title ?? row.publication.remote_post_id}
            <span className="ml-2 text-slate-600">{row.publication.provider_published_at?.slice(0, 10) ?? ''}</span>
          </span>
          <span className="flex shrink-0 items-center gap-2">
            <span className="font-mono text-slate-100">{row.value === null ? '—' : `${row.value} ${unit ?? row.unit}`}</span>
            {row.sample_size !== null ? <Badge tone="muted">n={row.sample_size}</Badge> : null}
            <AvailabilityChip availability={row.availability} />
          </span>
        </div>
      ))}
      {result.unmeasured.length ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-slate-500">
            {result.unmeasured.length} publication(s) without an observed value
          </summary>
          <ul className="mt-1 space-y-1">
            {result.unmeasured.map((publication: PublicationSummary) => (
              <li key={publication.publication_id} className="flex items-center justify-between gap-2 text-xs text-slate-500">
                <span className="min-w-0 truncate">{publication.title ?? publication.remote_post_id}</span>
                <span className="shrink-0">no observed value in this window</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
