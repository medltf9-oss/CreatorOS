/**
 * Analytics Center — Audience Growth (P8.22).
 *
 * One card per account, never a merged total: a Facebook Page like, an Instagram follower and a
 * TikTok follower are three different measurements, and the page says so on every row.
 *
 * The rules:
 * * a level (point-in-time follower count) and a change (derived from the first and last observation)
 *   are labelled differently — the change is always marked **derived**;
 * * history is coarsened by keeping the latest **observed** value in each bucket, so a week with no
 *   collection is a gap, not a carried-forward line;
 * * a change is never shown from a single observation, and followers are never inferred from likes,
 *   views or engagement;
 * * when the follower metric is missing the page shows the reason (permission, provider, threshold,
 *   not collected) from the availability plan instead of a zero.
 */
import { useMemo, useState } from 'react';
import {
  type AccountSummary,
  type AnalyticsClient,
  type Availability,
  type Growth,
  type MetricDefinition,
} from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import {
  bucketGrowthSeries,
  growthSeries,
  indexDefinitions,
  metricKeyForConcept,
  observedDelta,
  type GrowthBucket,
} from '../../components/analyticsModel';
import { Badge, Button, Card, EmptyState, formatWhen } from '../../components/ui';
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

const BUCKETS: { value: GrowthBucket; label: string }[] = [
  { value: 'day', label: 'Daily' },
  { value: 'week', label: 'Weekly' },
  { value: 'month', label: 'Monthly' },
];

type AccountGrowth = {
  account: AccountSummary;
  growth: Growth | null;
  availability: Availability | null;
  error: string | null;
};

export default function AudienceGrowth({
  api,
  initialAccountId = '',
}: {
  api: AnalyticsClient;
  /** Prefill used by the visual harness (and by a deep link); the page works without it. */
  initialAccountId?: string;
}) {
  const [range, setRange] = useState<RangeValue>(() => defaultRange('90d'));
  const [bucket, setBucket] = useState<GrowthBucket>('day');
  const [accountId, setAccountId] = useState(initialAccountId);

  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const metrics = useAsyncData((signal) => api.metrics({ limit: 200 }, signal), []);
  const freshness = useAsyncData((signal) => api.freshness(signal), []);

  const accountList = accounts.data?.items ?? [];
  const definitionsList = metrics.data?.items ?? [];
  const definitionMap = useMemo(() => indexDefinitions(definitionsList), [definitionsList]);

  const perAccount = useAsyncData<AccountGrowth[]>(
    async (signal) => {
      const results = await Promise.all(
        accountList.map(async (account): Promise<AccountGrowth> => {
          try {
            const [growth, availability] = await Promise.all([
              api.growth(account.id, { start: range.start, end: range.end }, signal),
              api.availability(account.id, 'account', signal),
            ]);
            return { account, growth, availability, error: null };
          } catch (error) {
            return { account, growth: null, availability: null, error: error instanceof Error ? error.message : String(error) };
          }
        }),
      );
      return results;
    },
    [accountList.length, range.start, range.end],
  );

  const rows = perAccount.data ?? [];
  const selected = rows.find((row) => row.account.id === accountId) ?? rows[0] ?? null;

  // Why the selected account has no curve: permission, provider, threshold, or nothing collected yet.
  const selectedAbsenceExplanation = useMemo(() => {
    if (!selected) return 'No account selected.';
    const declared = metricKeyForConcept(definitionsList, 'followers', selected.account.platform);
    if (!declared) {
      return `The metric registry declares no follower concept for ${platformLabel(selected.account.platform)}, so no history can exist.`;
    }
    const availabilityRow = selected.availability?.metrics.find((metric) => metric.metric_key === declared) ?? null;
    if (availabilityRow) return availabilityExplanation(availabilityRow);
    return 'The follower metric exists for this platform, but no snapshot for this account falls inside the selected range.';
  }, [selected, definitionsList]);

  const selectedSeries = useMemo(() => {
    if (!selected?.growth) return [];
    return bucket === 'day'
      ? growthSeries(selected.growth.points, definitionMap)
      : bucketGrowthSeries(selected.growth.points, bucket, definitionMap);
  }, [selected, bucket, definitionMap]);

  const changesKey = useMemo(
    () => (selected ? metricKeyForConcept(definitionsList, 'follower_changes', selected.account.platform) : null),
    [definitionsList, selected?.account.platform],
  );
  const changes = useAsyncData(
    (signal) =>
      selected && changesKey
        ? api.series({ account_id: selected.account.id, metric_key: changesKey, bucket: 'day', start: range.start, end: range.end }, signal)
        : Promise.resolve(null),
    [selected?.account.id ?? '', changesKey, range.start, range.end],
  );

  function reloadAll() {
    accounts.reload();
    metrics.reload();
    freshness.reload();
    perAccount.reload();
    changes.reload();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Audience Growth"
        subtitle="Each account measured on its own terms — follower counts are not merged across platforms."
        actions={
          <>
            <label className="text-xs text-slate-400">
              <span className="block text-[10px] uppercase tracking-wide text-slate-500">Coarsen</span>
              <select
                className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
                value={bucket}
                onChange={(event) => setBucket(event.target.value as GrowthBucket)}
              >
                {BUCKETS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <DateRangePicker value={range} onChange={setRange} />
            <Button onClick={reloadAll} busy={perAccount.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock error={accounts.error ?? metrics.error ?? perAccount.error} onRetry={reloadAll} />

      {accounts.loading && !accounts.data ? <LoadingBlock label="Loading accounts..." /> : null}

      {rows.length === 0 && accounts.data ? (
        <EmptyState title="No connected account" hint="Connect a Facebook Page, Instagram professional account or TikTok account to see audience growth." />
      ) : null}

      <section className="grid gap-5 lg:grid-cols-2 xl:grid-cols-3">
        {rows.map((row) => (
          <AccountCard
            key={row.account.id}
            row={row}
            freshnessRow={freshness.data?.accounts.find((item) => item.account_id === row.account.id) ?? null}
            definitions={definitionMap}
            definitionsList={definitionsList}
            selected={selected?.account.id === row.account.id}
            onSelect={() => setAccountId(row.account.id)}
          />
        ))}
      </section>

      {selected ? (
        <>
          <Card
            title={`Growth · ${selected.account.display_name}`}
            subtitle={`${platformLabel(selected.account.platform)} · ${BUCKETS.find((item) => item.value === bucket)?.label} buckets · a bucket keeps the latest observed value, never a carried-forward one`}
          >
            {selectedSeries.length === 0 ? (
              <EmptyState title="No historical observation" hint={selectedAbsenceExplanation} />
            ) : (
              <div className="space-y-4">
                {selectedSeries.map((series) => (
                  <MetricWidget
                    key={series.key}
                    title={series.key}
                    subtitle={`${series.label ?? ''} · gaps mean a bucket with no collection`}
                    series={[series]}
                    bucket={bucket === 'month' ? 'week' : bucket}
                    height={220}
                    emptyHint="No observed snapshot in the selected range."
                  />
                ))}
              </div>
            )}
          </Card>

          <section className="grid gap-5 lg:grid-cols-2">
            <Card title="Period change" subtitle="A period-based change metric is shown only when the provider exposes one">
              {!changesKey ? (
                <EmptyState
                  title="Not exposed by this provider"
                  hint={`${platformLabel(selected.account.platform)} does not publish a follower-changes insight in the metric registry.`}
                />
              ) : changes.loading && !changes.data ? (
                <LoadingBlock label="Loading change series..." />
              ) : (
                <MetricWidget
                  title={changesKey}
                  subtitle={`${platformLabel(selected.account.platform)} · period-based change`}
                  series={
                    changes.data
                      ? [
                          {
                            key: changesKey,
                            label: 'change',
                            unit: definitionMap.get(changesKey)?.unit,
                            points: changes.data.points.map((point) => ({ x: Date.parse(point.bucket_start), y: point.value, availability: point.availability })),
                          },
                        ]
                      : []
                  }
                  bucket="day"
                  height={200}
                  emptyHint="No change observation in the selected range."
                />
              )}
            </Card>

            <Card title="Comparison across accounts" subtitle="Same concept, different measurements — never summed">
              <CompareAccounts rows={rows} definitions={definitionMap} />
            </Card>
          </section>
        </>
      ) : null}
    </div>
  );
}

function AccountCard({
  row,
  freshnessRow,
  definitions,
  definitionsList,
  selected,
  onSelect,
}: {
  row: AccountGrowth;
  freshnessRow: { stale: boolean; age_hours: number | null } | null;
  definitions: ReturnType<typeof indexDefinitions>;
  definitionsList: MetricDefinition[];
  selected: boolean;
  onSelect: () => void;
}) {
  // The concept the registry declares for this platform, independent of whether anything was stored.
  // A missing key means the provider does not expose follower statistics at all; a present key with no
  // points means they exist but were not collected (or not authorized) — two different sentences.
  const declaredKey = metricKeyForConcept(definitionsList, 'followers', row.account.platform);
  const key = row.growth?.metric_keys[0] ?? declaredKey;
  const points = key ? (row.growth?.points ?? []).filter((point) => point.metric_key === key) : [];
  const delta = observedDelta(points);
  const observed = points.filter((point) => point.availability === 'observed' && typeof point.value === 'number');
  const latest = observed.at(-1) ?? null;
  const gaps = points.length - observed.length;
  const definition = key ? definitions.get(key) ?? null : null;
  const availabilityRow = key ? row.availability?.metrics.find((metric) => metric.metric_key === key) ?? null : null;

  return (
    <Card
      title={row.account.display_name}
      subtitle={`${platformLabel(row.account.platform)} · ${row.account.account_type}`}
      actions={
        <Button variant={selected ? 'primary' : 'ghost'} onClick={onSelect}>
          {selected ? 'shown' : 'show'}
        </Button>
      }
    >
      {row.error ? (
        <p className="text-xs text-rose-400">{row.error}</p>
      ) : !declaredKey ? (
        <div className="rounded-lg border border-dashed border-slate-700/70 px-3 py-4 text-center">
          <p className="text-sm text-slate-300">No follower metric for this platform</p>
          <p className="mt-1 text-xs text-slate-500">
            The metric registry declares no follower concept for {platformLabel(row.account.platform)}, so there
            is nothing to collect.
          </p>
        </div>
      ) : (
        <>
          <MetricStatePanel
            title="Latest observed"
            rows={[
              { label: 'Metric', value: definition?.provider_metric_name ?? key },
              {
                label: 'Value',
                value: latest && typeof latest.value === 'number' ? latest.value.toLocaleString() : '—',
                tone: latest ? 'normal' : 'warn',
              },
              { label: 'Observed at', value: latest ? formatWhen(latest.collected_at) : 'never' },
              {
                label: 'Net change',
                value: delta ? `${delta.net >= 0 ? '+' : ''}${delta.net.toLocaleString()} (derived)` : 'insufficient history',
                tone: delta ? (delta.net > 0 ? 'good' : delta.net < 0 ? 'bad' : 'normal') : 'warn',
              },
              { label: 'Observations', value: `${observed.length}${gaps ? ` (${gaps} gap${gaps === 1 ? '' : 's'})` : ''}` },
            ]}
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StaleBadge stale={freshnessRow?.stale ?? true} ageHours={freshnessRow?.age_hours ?? null} />
            {delta ? (
              <Badge tone={delta.direction === 'up' ? 'good' : delta.direction === 'down' ? 'bad' : 'muted'}>{delta.direction}</Badge>
            ) : (
              <Badge tone="warn">level only</Badge>
            )}
            {availabilityRow ? <AvailabilityChip availability={availabilityRowMeasurement(availabilityRow, points)} reason={availabilityRow.detail} /> : null}
          </div>
          {observed.length === 0 ? (
            <p className="mt-2 text-xs text-amber-400">
              {availabilityRow
                ? availabilityExplanation(availabilityRow)
                : 'No historical observation yet: the follower metric exists for this platform, but nothing has been collected for this account.'}
            </p>
          ) : null}
        </>
      )}
    </Card>
  );
}

function availabilityRowMeasurement(
  availabilityRow: { allowed: boolean; reason: string },
  points: { availability: string }[],
): 'observed' | 'not_authorized' | 'not_supported' | 'below_threshold' | 'not_collected' | 'unknown' {
  if (points.some((point) => point.availability === 'observed')) return 'observed';
  const reason = availabilityRow.reason;
  if (reason === 'not_authorized' || reason === 'not_supported' || reason === 'below_threshold') return reason;
  return availabilityRow.allowed ? 'not_collected' : 'unknown';
}

function availabilityExplanation(availabilityRow: { allowed: boolean; reason: string; detail: string }): string {
  if (availabilityRow.reason === 'not_authorized') return `Follower statistics need a permission this account has not granted: ${availabilityRow.detail}`;
  if (availabilityRow.reason === 'not_supported') return `This provider does not expose follower statistics: ${availabilityRow.detail}`;
  if (availabilityRow.reason === 'below_threshold') return `The provider withholds follower statistics below a threshold: ${availabilityRow.detail}`;
  if (!availabilityRow.allowed) return availabilityRow.detail;
  return 'No historical observation yet: collection has not produced a follower snapshot for this account.';
}

function CompareAccounts({
  rows,
  definitions,
}: {
  rows: AccountGrowth[];
  definitions: ReturnType<typeof indexDefinitions>;
}) {
  const comparable = rows.filter((row) => row.growth && row.growth.metric_keys.length > 0);
  if (comparable.length === 0) {
    return (
      <EmptyState
        title="No comparable account"
        hint="No account has an observed follower measurement in this range yet. A metric the account was not authorized for is never shown as a level of 0."
      />
    );
  }
  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1 pr-3">Account</th>
              <th className="py-1 pr-3">Measurement</th>
              <th className="py-1 pr-3">Latest</th>
              <th className="py-1 pr-3">Net change</th>
              <th className="py-1">Window</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {comparable.map((row) => {
              const key = row.growth!.metric_keys[0];
              const points = row.growth!.points.filter((point) => point.metric_key === key);
              const delta = observedDelta(points);
              const observed = points.filter((point) => point.availability === 'observed' && typeof point.value === 'number');
              const latest = observed.at(-1) ?? null;
              return (
                <tr key={row.account.id} className="border-t border-slate-800/70">
                  <td className="py-1 pr-3">
                    {row.account.display_name}
                    <span className="ml-2 text-slate-600">{platformLabel(row.account.platform)}</span>
                  </td>
                  <td className="py-1 pr-3 font-mono text-slate-500">{definitions.get(key)?.provider_metric_name ?? key}</td>
                  <td className="py-1 pr-3 font-mono">{latest && typeof latest.value === 'number' ? latest.value.toLocaleString() : '—'}</td>
                  <td className={`py-1 pr-3 font-mono ${delta && delta.net > 0 ? 'text-emerald-400' : delta && delta.net < 0 ? 'text-rose-400' : 'text-slate-400'}`}>
                    {delta ? `${delta.net >= 0 ? '+' : ''}${delta.net.toLocaleString()}` : '—'}
                  </td>
                  <td className="py-1 text-slate-500">
                    {delta ? `${formatWhen(delta.firstAt).slice(0, 10)} → ${formatWhen(delta.lastAt).slice(0, 10)}` : 'single observation'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] text-slate-500">
        A Facebook Page like is not a follower, and an Instagram follower is not a TikTok follower: the
        measurements are not identical, so the rows are shown side by side and never added together. Net
        change is derived from the first and last observation in the range.
      </p>
    </>
  );
}
