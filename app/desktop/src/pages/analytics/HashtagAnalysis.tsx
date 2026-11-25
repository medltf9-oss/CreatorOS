/**
 * Analytics Center — Hashtag Analysis (P8.22).
 *
 * A catalogue built from the hashtags on published captions, plus the deterministic hashtag
 * comparison the backend already computes (`GET /analytics/hashtags` → group medians with intervals,
 * sample sizes and explicit refusals).
 *
 * The rule the page exists to protect: **performance associated with a hashtag is not performance
 * caused by it.** Every comparison states the metric, the window, the sample size and the backend's
 * limitations, and the page never claims a hashtag is trending — there is no trusted trend dataset.
 */
import { useMemo, useState } from 'react';
import {
  type AnalyticsClient,
  type Comparison,
  type Platform,
  type PublicationSummary,
} from '../../services/apiAnalytics';
import { type ObservationAvailability, formatMetricValue } from '../../components/chartModel';
import {
  comparableGroups,
  hashtagCatalogue,
  indexDefinitions,
  metricKeyForConcept,
  resolveConceptCells,
  valuesByMetric,
} from '../../components/analyticsModel';
import { Badge, Button, Card, EmptyState, formatWhen } from '../../components/ui';
import {
  AvailabilityChip,
  DateRangePicker,
  ErrorBlock,
  LoadingBlock,
  PageHeader,
  platformLabel,
  useAsyncData,
  type RangeValue,
  defaultRange,
} from './shared';

const PLATFORMS: Platform[] = ['facebook', 'instagram', 'tiktok'];
const WINDOWS = ['h24', 'h48', 'd7', 'd28', 'lifetime'] as const;

export default function HashtagAnalysis({
  api,
  onOpenPublication,
  initialTag = '',
}: {
  api: AnalyticsClient;
  onOpenPublication?: (publicationId: string) => void;
  /** Prefill used by the visual harness (and by a deep link); the page works without it. */
  initialTag?: string;
}) {
  const [range, setRange] = useState<RangeValue>(() => defaultRange('90d'));
  const [platform, setPlatform] = useState<Platform | ''>('');
  const [accountId, setAccountId] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [concept, setConcept] = useState('views');
  const [windowKind, setWindowKind] = useState<(typeof WINDOWS)[number]>('h24');
  const [minSamples, setMinSamples] = useState(3);
  const [selectedTag, setSelectedTag] = useState(initialTag);

  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const metrics = useAsyncData((signal) => api.metrics({ limit: 200 }, signal), []);
  const campaigns = useAsyncData((signal) => api.campaigns({ limit: 100 }, signal), []);

  const definitions = metrics.data?.items ?? [];
  const definitionMap = useMemo(() => indexDefinitions(definitions), [definitions]);
  const accountList = accounts.data?.items ?? [];
  const visibleAccounts = platform ? accountList.filter((account) => account.platform === platform) : accountList;
  const account = visibleAccounts.find((item) => item.id === accountId) ?? visibleAccounts[0] ?? null;
  const activePlatform = platform || account?.platform || '';

  const concepts = useMemo(() => {
    const seen: string[] = [];
    for (const definition of definitions) {
      if (definition.platform !== activePlatform) continue;
      if (definition.preferred_for && !seen.includes(definition.preferred_for)) seen.push(definition.preferred_for);
    }
    return seen.sort((a, b) => a.localeCompare(b));
  }, [definitions, activePlatform]);

  const effectiveConcept = concept && concepts.includes(concept) ? concept : concepts.includes('views') ? 'views' : concepts[0] ?? '';
  const metricKey = effectiveConcept ? metricKeyForConcept(definitions, effectiveConcept, activePlatform || undefined) : null;
  const definition = metricKey ? definitionMap.get(metricKey) ?? null : null;

  const publications = useAsyncData(
    (signal) => api.publications({ account_id: account?.id, platform: platform || undefined, start: range.start, end: range.end, limit: 100 }, signal),
    [account?.id ?? '', platform, range.start, range.end],
  );
  const aggregations = useAsyncData(
    (signal) =>
      api.aggregations({ scope: 'publication', account_id: account?.id, window_kind: windowKind, limit: 200 }, signal),
    [windowKind, account?.id ?? ''],
  );
  const hashtags = useAsyncData(async (signal) => {
    if (!account || !metricKey) return { value: null as Comparison | null, note: 'choose an account and a concept' };
    try {
      const result = await api.hashtags({ account_id: account.id, metric_key: metricKey, window_kind: windowKind, max_groups: 5 }, signal);
      return { value: result, note: '' };
    } catch (error) {
      return { value: null, note: error instanceof Error ? error.message : String(error) };
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account?.id ?? '', metricKey, windowKind]);

  const campaignFiltered = useMemo(() => {
    const list = publications.data?.items ?? [];
    return campaignId ? list.filter((item) => item.campaign_id === campaignId) : list;
  }, [publications.data, campaignId]);

  const matrix = useMemo(() => valuesByMetric(aggregations.data?.items ?? [], windowKind), [aggregations.data, windowKind]);

  const catalogue = useMemo(
    () => hashtagCatalogue(campaignFiltered).filter((entry) => entry.publications >= minSamples),
    [campaignFiltered, minSamples],
  );
  const comparison = hashtags.data?.value ?? null;
  const groups = useMemo(() => comparableGroups(comparison, minSamples), [comparison, minSamples]);

  const associated = useMemo(() => {
    if (!selectedTag) return [];
    const key = selectedTag.toLowerCase();
    return campaignFiltered.filter((publication) => publication.hashtags.some((tag) => tag.replace(/^#/, '').trim().toLowerCase() === key));
  }, [campaignFiltered, selectedTag]);

  // Each associated post is measured by *its own* platform's metric for the chosen concept, exactly as
  // the video leaderboard does: a TikTok post is never shown a Facebook metric's absence.
  const associatedCells = useMemo(
    () =>
      effectiveConcept
        ? resolveConceptCells(associated, matrix, definitions, effectiveConcept)
        : associated.map((publication) => ({
            publication,
            metricKey: null,
            value: null,
            unit: '',
            availability: 'not_collected' as const,
            sampleSize: null,
          })),
    [associated, matrix, definitions, effectiveConcept],
  );

  // The aggregation page caps at 200 rows; say so instead of presenting a partial set as complete.
  const truncatedNote = useMemo(() => {
    const page = aggregations.data;
    if (!page || page.total <= page.items.length) return null;
    return `Showing ${page.items.length} of ${page.total} stored aggregations for this window: post values below cover those rows only.`;
  }, [aggregations.data]);

  const inconclusive = useMemo(() => {
    const verdicts = Array.isArray(comparison?.verdicts) ? (comparison?.verdicts as Record<string, unknown>[]) : [];
    return verdicts.filter((verdict) => verdict.verdict === 'inconclusive').length;
  }, [comparison]);

  function reloadAll() {
    accounts.reload();
    metrics.reload();
    campaigns.reload();
    publications.reload();
    aggregations.reload();
    hashtags.reload();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Hashtag Analysis"
        subtitle="Hashtags from published captions, compared only where the observations are comparable."
        actions={
          <>
            <DateRangePicker value={range} onChange={setRange} />
            <Button onClick={reloadAll} busy={publications.loading || hashtags.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-sm text-amber-300">
        A hashtag is associated with the performance of the posts that used it; it does not cause it. Posts
        differ in topic, format, timing and audience, so no comparison here is evidence of causation.
      </div>

      <ErrorBlock error={accounts.error ?? metrics.error ?? publications.error ?? hashtags.error} onRetry={reloadAll} />

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
              className="mt-1 max-w-[14rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={campaignId}
              onChange={(event) => setCampaignId(event.target.value)}
            >
              <option value="">any</option>
              {(campaigns.data?.items ?? []).map((item) => (
                <option key={item.campaign_id} value={item.campaign_id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Metric concept</span>
            <select
              className="mt-1 max-w-[14rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
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
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Min sample</span>
            <input
              type="number"
              min={1}
              max={50}
              value={minSamples}
              onChange={(event) => setMinSamples(Math.max(1, Math.min(50, Number(event.target.value) || 1)))}
              className="mt-1 w-20 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
            />
          </label>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Resolved metric — {definition ? `${definition.metric_key} (${definition.unit}, ${definition.metric_class})` : 'no metric for this concept on this platform'}
        </p>
        {truncatedNote ? <p className="mt-1 text-[11px] text-amber-400">{truncatedNote}</p> : null}
      </Card>

      {publications.loading && !publications.data ? <LoadingBlock label="Loading publications..." /> : null}

      {!account ? (
        <EmptyState title="No connected account" hint="Hashtag comparison needs an account; connect one first." />
      ) : (
        <>
          <section className="grid gap-5 lg:grid-cols-2">
            <Card title="Hashtag catalogue" subtitle="Extracted from published captions; counts only, no performance claim">
              {catalogue.length === 0 ? (
                <EmptyState title="No hashtags" hint={`No published caption in this selection carries a hashtag, or none has ${minSamples}+ publication(s).`} />
              ) : (
                <div className="max-h-80 overflow-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="py-1 pr-3">Hashtag</th>
                        <th className="py-1 pr-3">Platforms</th>
                        <th className="py-1 pr-3">Posts</th>
                        <th className="py-1 pr-3">Accounts</th>
                        <th className="py-1">Period</th>
                      </tr>
                    </thead>
                    <tbody className="text-slate-300">
                      {catalogue.map((entry) => (
                        <tr
                          key={entry.hashtag}
                          className={`cursor-pointer border-t border-slate-800/70 ${selectedTag === entry.hashtag ? 'bg-slate-800/40' : ''}`}
                          onClick={() => setSelectedTag(entry.hashtag)}
                        >
                          <td className="py-1 pr-3 text-slate-200">#{entry.label}</td>
                          <td className="py-1 pr-3">{entry.platforms.map(platformLabel).join(', ')}</td>
                          <td className="py-1 pr-3 font-mono">{entry.publications}</td>
                          <td className="py-1 pr-3 font-mono">{entry.accounts}</td>
                          <td className="py-1 text-slate-500">
                            {entry.firstPublished && entry.lastPublished
                              ? `${formatWhen(entry.firstPublished).slice(0, 10)} → ${formatWhen(entry.lastPublished).slice(0, 10)}`
                              : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <Card
              title="Top hashtags"
              subtitle="Ranked by observation only; a hashtag with too few observations is never ranked"
            >
              {groups.ranked.length === 0 ? (
                <EmptyState
                  title="Nothing rankable"
                  hint={`No hashtag group met the ${minSamples}-observation minimum with an observed median at window ${windowKind}.`}
                />
              ) : (
                <div className="space-y-2">
                  {groups.ranked.map((group, index) => (
                    <div key={group.label} className="flex items-center justify-between gap-3 text-xs">
                      <span className="min-w-0 truncate text-slate-300">
                        <span className="mr-2 text-slate-600">{index + 1}.</span>#{group.label}
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        <span className="font-mono text-slate-100">{group.median === null ? '—' : formatMetricValue(group.median, definition?.unit)}</span>
                        <Badge tone="muted">n={group.n}</Badge>
                        <AvailabilityChip availability={group.availability} />
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {groups.excluded.length ? (
                <details className="mt-3">
                  <summary className="cursor-pointer text-[11px] text-slate-500">
                    {groups.excluded.length} group{groups.excluded.length === 1 ? '' : 's'} not ranked (disclosed, not hidden)
                  </summary>
                  <ul className="mt-1 space-y-1">
                    {groups.excluded.map((item) => (
                      <li key={item.group.label} className="flex items-center justify-between gap-2 text-xs text-slate-500">
                        <span className="min-w-0 truncate">#{item.group.label}</span>
                        <span className="shrink-0">{item.reason} · n={item.group.n}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </Card>
          </section>

          <Card
            title={`Hashtag comparison by ${definition?.metric_key ?? effectiveConcept}`}
            subtitle={`Deterministic group medians from the comparison service · window ${windowKind} · ${definition?.unit ?? ''}`}
          >
            {hashtags.loading && !hashtags.data ? (
              <LoadingBlock label="Comparing hashtags..." />
            ) : comparison ? (
              <>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Badge tone={comparison.sufficient === false ? 'warn' : 'muted'}>
                    {comparison.sufficient === false ? `below the ${minSamples}-observation gate` : 'sufficient sample'}
                  </Badge>
                  {inconclusive > 0 ? (
                    <Badge tone="warn">
                      {inconclusive} inconclusive pair{inconclusive === 1 ? '' : 's'}
                    </Badge>
                  ) : null}
                  <span className="text-slate-500">{comparison.groups.length} group(s) returned (max 5)</span>
                </div>
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="py-1 pr-3">Hashtag group</th>
                        <th className="py-1 pr-3">Median</th>
                        <th className="py-1 pr-3">Sample</th>
                        <th className="py-1">Availability</th>
                      </tr>
                    </thead>
                    <tbody className="text-slate-300">
                      {comparison.groups.map((group) => (
                        <tr key={group.label} className="border-t border-slate-800/70">
                          <td className="py-1 pr-3">#{group.label}</td>
                          <td className="py-1 pr-3 font-mono">{group.median === null ? '—' : formatMetricValue(group.median, definition?.unit)}</td>
                          <td className="py-1 pr-3">
                            n={group.n}
                            {group.n < minSamples ? (
                              <span className="ml-2 text-[10px] uppercase tracking-wide text-amber-400">below gate</span>
                            ) : null}
                          </td>
                          <td className="py-1">
                            <AvailabilityChip availability={group.availability} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {comparison.limitations.length ? (
                  <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-amber-400">
                    {comparison.limitations.map((limitation) => (
                      <li key={limitation}>{limitation}</li>
                    ))}
                  </ul>
                ) : null}
                {definition ? (
                  <p className="mt-3 text-[11px] text-slate-500">
                    Metric definition: {definition.definition_text} (measured by {definition.provider_metric_name}, {definition.unit}).
                  </p>
                ) : null}
              </>
            ) : (
              <div className="rounded-lg border border-dashed border-slate-700/70 px-4 py-6 text-center text-xs text-slate-500">
                {hashtags.data?.note ?? 'Comparison unavailable.'}
              </div>
            )}
          </Card>

          <Card
            title={selectedTag ? `#${selectedTag} — associated posts` : 'Hashtag details'}
            subtitle="Selecting a hashtag shows the posts that used it; the value is the post's own observation, not an effect of the hashtag"
          >
            {!selectedTag ? (
              <EmptyState title="No hashtag selected" hint="Choose a hashtag in the catalogue to see its posts." />
            ) : associated.length === 0 ? (
              <EmptyState title="No associated post in range" hint="The hashtag appears outside this date range or account selection." />
            ) : (
              <AssociatedPosts
                cells={associatedCells}
                accountLabels={accountList}
                onOpenPublication={onOpenPublication}
              />
            )}
          </Card>
        </>
      )}
    </div>
  );
}

function AssociatedPosts({
  cells,
  accountLabels,
  onOpenPublication,
}: {
  cells: {
    publication: PublicationSummary;
    metricKey: string | null;
    value: number | null;
    unit: string;
    availability: ObservationAvailability;
  }[];
  accountLabels: { id: string; display_name: string }[];
  onOpenPublication?: (publicationId: string) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3">Post</th>
            <th className="py-1 pr-3">Platform</th>
            <th className="py-1 pr-3">Account</th>
            <th className="py-1 pr-3">Published</th>
            <th className="py-1 pr-3">Value</th>
            <th className="py-1">Availability</th>
          </tr>
        </thead>
        <tbody className="text-slate-300">
          {cells.map((cell) => {
            const publication = cell.publication;
            return (
              <tr key={publication.publication_id} className="border-t border-slate-800/70">
                <td className="py-1 pr-3">
                  {onOpenPublication ? (
                    <button className="text-sky-400 hover:underline" onClick={() => onOpenPublication(publication.publication_id)}>
                      {publication.title ?? publication.remote_post_id}
                    </button>
                  ) : (
                    publication.title ?? publication.remote_post_id
                  )}
                </td>
                <td className="py-1 pr-3">{platformLabel(publication.platform)}</td>
                <td className="py-1 pr-3 text-slate-500">{accountLabels.find((item) => item.id === publication.account_id)?.display_name ?? publication.account_id}</td>
                <td className="py-1 pr-3">{publication.provider_published_at ? formatWhen(publication.provider_published_at) : '—'}</td>
                <td className="py-1 pr-3 font-mono" title={cell.metricKey ?? undefined}>
                  {cell.value !== null ? formatMetricValue(cell.value, cell.unit) : '—'}
                </td>
                <td className="py-1">
                  <AvailabilityChip availability={cell.availability} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
