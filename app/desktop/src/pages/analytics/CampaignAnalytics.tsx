/**
 * Analytics Center — Campaign analytics (P8.21).
 *
 * The attribution chain the backend already stores is shown verbatim:
 * `campaign → content_project → platform_variant → publish_job → analytics_observation`.
 *
 * Two rules from the phase plan are enforced here rather than left to the renderer:
 * * **no cross-platform total.** Campaign metrics are grouped by platform and shown in separate
 *   tables; nothing sums a Facebook count with an Instagram count.
 * * **no performance without observations.** A campaign with publish jobs but zero observations shows
 *   an explicit empty state, not a zero.
 */
import { useMemo, useState } from 'react';
import { type AnalyticsClient, type CampaignSummary } from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import {
  countBy,
  indexDefinitions,
  latestTimestamp,
  metricsByPlatform,
  platformForMetric,
} from '../../components/analyticsModel';
import { formatMetricValue } from '../../components/chartModel';
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

export default function CampaignAnalytics({
  api,
  initialCampaignId = '',
}: {
  api: AnalyticsClient;
  initialCampaignId?: string;
}) {
  const [campaignId, setCampaignId] = useState(initialCampaignId);
  const [compareId, setCompareId] = useState('');

  const campaigns = useAsyncData((signal) => api.campaigns({ limit: 100 }, signal), []);
  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const metrics = useAsyncData((signal) => api.metrics({ limit: 200 }, signal), []);
  const freshness = useAsyncData((signal) => api.freshness(signal), []);

  const list: CampaignSummary[] = campaigns.data?.items ?? [];
  const selected = list.find((campaign) => campaign.campaign_id === campaignId) ?? list[0] ?? null;
  const id = selected?.campaign_id ?? '';
  const compare = list.find((campaign) => campaign.campaign_id === compareId) ?? null;

  const detail = useAsyncData((signal) => (id ? api.campaign(id, signal) : Promise.resolve(null)), [id]);
  const compareDetail = useAsyncData(
    (signal) => (compareId ? api.campaign(compareId, signal) : Promise.resolve(null)),
    [compareId],
  );

  const definitionMap = useMemo(() => indexDefinitions(metrics.data?.items ?? []), [metrics.data]);
  const accountMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const account of accounts.data?.items ?? []) map.set(account.id, account.display_name);
    return map;
  }, [accounts.data]);

  const publications = detail.data?.publications ?? [];
  const platformDistribution = useMemo(() => countBy(publications, (row) => row.platform), [publications]);
  const accountDistribution = useMemo(() => countBy(publications, (row) => row.account_id), [publications]);
  const stateCounts = useMemo(() => countBy(publications, (row) => row.state), [publications]);
  const perPlatformMetrics = useMemo(
    () => metricsByPlatform(detail.data?.metrics ?? [], definitionMap),
    [detail.data, definitionMap],
  );

  const campaign = detail.data?.campaign ?? selected;
  const observations = campaign?.observations ?? 0;
  const publishedDestinations = publications.length;
  const partiallyPublished = Boolean(campaign && campaign.projects > publishedDestinations);
  const unmeasured = publications.filter((publication) => !publication.has_metrics).length;
  const lastActivity = latestTimestamp(publications.map((publication) => publication.provider_published_at));
  const freshnessRow = freshness.data?.accounts?.[0] ?? null;

  function reloadAll() {
    campaigns.reload();
    accounts.reload();
    metrics.reload();
    freshness.reload();
    detail.reload();
    compareDetail.reload();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Campaign analytics"
        subtitle="Campaign → project → variant → publish job → observation, kept per platform."
        actions={
          <>
            <label className="text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Campaign</span>
              <select
                className="mt-1 max-w-[18rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
                value={id}
                onChange={(event) => setCampaignId(event.target.value)}
                disabled={list.length === 0}
              >
                {list.length === 0 ? <option value="">no campaign</option> : null}
                {list.map((item) => (
                  <option key={item.campaign_id} value={item.campaign_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Compare with</span>
              <select
                className="mt-1 max-w-[18rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
                value={compareId}
                onChange={(event) => setCompareId(event.target.value)}
                disabled={list.length < 2}
              >
                <option value="">none</option>
                {list
                  .filter((item) => item.campaign_id !== id)
                  .map((item) => (
                    <option key={item.campaign_id} value={item.campaign_id}>
                      {item.name}
                    </option>
                  ))}
              </select>
            </label>
            <Button onClick={reloadAll} busy={campaigns.loading || detail.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock error={campaigns.error ?? detail.error ?? compareDetail.error} onRetry={reloadAll} />

      {campaigns.loading && !campaigns.data ? <LoadingBlock label="Loading campaigns..." /> : null}

      {!selected ? (
        <EmptyState
          title="No campaigns"
          hint="A campaign groups content projects. Create one and publish from it to see performance here."
        />
      ) : (
        <>
          <Card title="Campaign" subtitle={campaign?.goal ?? 'no stated goal'}>
            <MetricStatePanel
              title="Summary"
              rows={[
                { label: 'Name', value: campaign?.name ?? '—' },
                { label: 'Goal', value: campaign?.goal ?? '—' },
                { label: 'Starts', value: campaign?.starts_on ? formatWhen(campaign.starts_on) : '—' },
                { label: 'Ends', value: campaign?.ends_on ? formatWhen(campaign.ends_on) : 'open' },
                { label: 'Content projects', value: `${campaign?.projects ?? 0}` },
                { label: 'Published destinations', value: `${publishedDestinations}` },
                { label: 'Observations', value: `${observations}` },
              ]}
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {partiallyPublished ? (
                <Badge tone="warn">
                  partially published — {campaign?.projects ?? 0} project(s), {publishedDestinations} destination(s)
                </Badge>
              ) : (
                <Badge tone="good">every project published</Badge>
              )}
              {unmeasured ? <Badge tone="muted">{unmeasured} destination(s) without metrics yet</Badge> : null}
              {freshnessRow ? <StaleBadge stale={freshnessRow.stale} ageHours={freshnessRow.age_hours} /> : null}
            </div>
            <p className="mt-2 text-xs text-slate-500">
              Last destination published {lastActivity ? formatWhen(lastActivity) : '—'}.
            </p>
          </Card>

          <Card title="Attribution chain" subtitle="Every level is a stored row; a later level existing does not imply an earlier one has data">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {[
                { label: 'campaign', value: campaign?.name ?? '—' },
                { label: 'content projects', value: `${campaign?.projects ?? 0}` },
                { label: 'published destinations', value: `${publishedDestinations}` },
                { label: 'analytics observations', value: `${observations}` },
              ].map((step, index) => (
                <span key={step.label} className="flex items-center gap-2">
                  {index > 0 ? <span className="text-slate-600">→</span> : null}
                  <span className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-1">
                    <span className="block text-[10px] uppercase tracking-wide text-slate-500">{step.label}</span>
                    <span className="font-mono text-slate-200">{step.value}</span>
                  </span>
                </span>
              ))}
            </div>
            {observations === 0 ? (
              <p className="mt-3 text-xs text-amber-400">
                Publishing jobs exist but no analytics observation has been collected for this campaign yet. Performance
                is not shown as zero — it has simply not been measured.
              </p>
            ) : null}
          </Card>

          <section className="grid gap-5 lg:grid-cols-3">
            <Card title="Platform distribution" subtitle="Published destinations per platform (a count of our own rows, not a provider metric)">
              {platformDistribution.length === 0 ? (
                <EmptyState title="Nothing published" hint="This campaign has no delivered destination yet." />
              ) : (
                <MetricWidget
                  title="Destinations"
                  series={[
                    {
                      key: 'destinations',
                      label: 'Destinations',
                      points: platformDistribution.map((row) => ({ x: platformLabel(row.key), y: row.count, availability: 'observed' as const })),
                    },
                  ]}
                  kind="bar"
                  height={180}
                  emptyHint="No destination to plot."
                />
              )}
            </Card>

            <Card title="Account distribution" subtitle="Which connected account each destination belongs to">
              {accountDistribution.length === 0 ? (
                <EmptyState title="No destinations" hint="Nothing has been published from this campaign yet." />
              ) : (
                <MetricStatePanel
                  title="Accounts"
                  rows={accountDistribution.map((row) => ({ label: accountMap.get(row.key) ?? row.key, value: `${row.count}` }))}
                />
              )}
            </Card>

            <Card title="Destination status" subtitle="Success and failure of the publish jobs themselves">
              {stateCounts.length === 0 ? (
                <EmptyState title="No destinations" hint="No publish job has a provider id yet." />
              ) : (
                <div className="space-y-1">
                  {stateCounts.map((row) => (
                    <div key={row.key} className="flex items-center justify-between gap-3 text-xs">
                      <span className="text-slate-300">{row.key}</span>
                      <Badge tone={row.key === 'published' ? 'good' : row.key.includes('fail') ? 'bad' : 'warn'}>{row.count}</Badge>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </section>

          <Card
            title="Campaign metrics by platform"
            subtitle="Separate tables per platform: a Facebook count and an Instagram count are never added together"
          >
            {detail.loading && !detail.data ? (
              <LoadingBlock label="Loading campaign metrics..." />
            ) : observations === 0 || perPlatformMetrics.length === 0 ? (
              <EmptyState
                title="No campaign metrics"
                hint="No campaign-scope aggregation exists yet. Publishing jobs alone produce no performance data."
              />
            ) : (
              <div className="space-y-4">
                {perPlatformMetrics.map((group) => (
                  <div key={group.platform}>
                    <p className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">{platformLabel(group.platform)}</p>
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-xs">
                        <thead className="text-slate-500">
                          <tr>
                            <th className="py-1 pr-3">Metric</th>
                            <th className="py-1 pr-3">Value</th>
                            <th className="py-1 pr-3">Sample</th>
                            <th className="py-1">Availability</th>
                          </tr>
                        </thead>
                        <tbody className="text-slate-300">
                          {group.metrics.map((metric) => {
                            const prefix = platformForMetric(metric.metric_key, definitionMap);
                            const definition = definitionMap.get(metric.metric_key);
                            return (
                              <tr key={`${group.platform}-${metric.metric_key}-${prefix}`} className="border-t border-slate-800/70">
                                <td className="py-1 pr-3 font-mono">{metric.metric_key}</td>
                                <td className="py-1 pr-3 font-mono">
                                  {metric.value === null ? '—' : formatMetricValue(metric.value, metric.unit)}
                                </td>
                                <td className="py-1 pr-3 text-slate-500">{definition ? definition.metric_class : '—'}</td>
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

          {compare ? (
            <Card title="Campaign comparison" subtitle="Counts and per-platform destination totals — metrics are never summed across platforms">
              <CompareTable
                left={campaign}
                right={compareDetail.data?.campaign ?? compare}
                leftPlatforms={platformDistribution}
                rightPlatforms={countBy(compareDetail.data?.publications ?? [], (row) => row.platform)}
              />
            </Card>
          ) : null}

          <Card title="Destinations" subtitle="Every published destination in this campaign">
            {publications.length === 0 ? (
              <EmptyState title="No destinations" hint="This campaign has no delivered publish job yet." />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-slate-500">
                    <tr>
                      <th className="py-1 pr-3">Title</th>
                      <th className="py-1 pr-3">Platform</th>
                      <th className="py-1 pr-3">Account</th>
                      <th className="py-1 pr-3">Published</th>
                      <th className="py-1 pr-3">State</th>
                      <th className="py-1">Metrics</th>
                    </tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {publications.map((publication) => (
                      <tr key={publication.publication_id} className="border-t border-slate-800/70">
                        <td className="py-1 pr-3">{publication.title ?? publication.remote_post_id}</td>
                        <td className="py-1 pr-3">{platformLabel(publication.platform)}</td>
                        <td className="py-1 pr-3 text-slate-500">{accountMap.get(publication.account_id) ?? publication.account_id}</td>
                        <td className="py-1 pr-3">{publication.provider_published_at ? formatWhen(publication.provider_published_at) : '—'}</td>
                        <td className="py-1 pr-3">
                          <Badge tone={publication.state === 'published' ? 'good' : publication.state.includes('fail') ? 'bad' : 'warn'}>
                            {publication.state}
                          </Badge>
                        </td>
                        <td className="py-1">
                          <Badge tone={publication.has_metrics ? 'good' : 'muted'}>{publication.has_metrics ? 'yes' : 'none'}</Badge>
                        </td>
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

function CompareTable({
  left,
  right,
  leftPlatforms,
  rightPlatforms,
}: {
  left: CampaignSummary | null;
  right: CampaignSummary;
  leftPlatforms: { key: string; count: number }[];
  rightPlatforms: { key: string; count: number }[];
}) {
  const rows: { label: string; left: string; right: string }[] = [
    { label: 'Content projects', left: `${left?.projects ?? 0}`, right: `${right.projects}` },
    { label: 'Published destinations', left: `${left?.publications ?? 0}`, right: `${right.publications}` },
    { label: 'Observations', left: `${left?.observations ?? 0}`, right: `${right.observations}` },
  ];
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1 pr-3">Measure</th>
              <th className="py-1 pr-3">{left?.name ?? 'this campaign'}</th>
              <th className="py-1">{right.name}</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.label} className="border-t border-slate-800/70">
                <td className="py-1 pr-3 text-slate-400">{row.label}</td>
                <td className="py-1 pr-3 font-mono">{row.left}</td>
                <td className="py-1 font-mono">{row.right}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[11px] uppercase tracking-wide text-slate-500">Destinations per platform (not summed)</p>
      <div className="mt-1 grid gap-2 sm:grid-cols-2">
        <div className="space-y-1 text-xs text-slate-400">
          {leftPlatforms.length === 0 ? <span>—</span> : leftPlatforms.map((row) => (
            <div key={row.key} className="flex justify-between gap-2">
              <span>{platformLabel(row.key)}</span>
              <span className="font-mono text-slate-300">{row.count}</span>
            </div>
          ))}
        </div>
        <div className="space-y-1 text-xs text-slate-400">
          {rightPlatforms.length === 0 ? <span>—</span> : rightPlatforms.map((row) => (
            <div key={row.key} className="flex justify-between gap-2">
              <span>{platformLabel(row.key)}</span>
              <span className="font-mono text-slate-300">{row.count}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
