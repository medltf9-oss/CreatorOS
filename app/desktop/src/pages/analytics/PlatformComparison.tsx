/**
 * Analytics Center — Platform Comparison (P8.19).
 *
 * Metric keys are platform-specific (`fb.post.media_views` is not `tt.video.views`), so the page
 * compares a **concept** — the registry's `preferred_for`, such as `views` or `reach` — and resolves
 * that concept to each platform's own metric. Each platform is charted on its own axis; when the
 * resolved definitions differ in unit or class the page says so instead of implying comparability.
 * A concept a platform does not expose is a labelled chip, never an empty chart.
 */
import { useMemo, useState } from 'react';
import {
  AnalyticsApiError,
  type AccountSummary,
  type AnalyticsClient,
  type Comparison,
  type MetricDefinition,
  type Platform,
  type Series,
} from '../../services/apiAnalytics';
import { MetricWidget } from '../../components/charts';
import { seriesFromRows } from '../../components/chartModel';
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

type PlatformResult = {
  platform: Platform;
  account: AccountSummary | null;
  definition: MetricDefinition | null;
  series: Series | null;
  comparison: Comparison | null;
  seriesError: string | null;
  comparisonNote: string | null;
};

export default function PlatformComparison({ api }: { api: AnalyticsClient }) {
  const [range, setRange] = useState<RangeValue>(() => defaultRange('28d'));
  const [concept, setConcept] = useState('');
  const [enabled, setEnabled] = useState<Record<Platform, boolean>>({ facebook: true, instagram: true, tiktok: true });
  const [overrides, setOverrides] = useState<Record<string, string>>({});

  const metrics = useAsyncData((signal) => api.metrics({ limit: 200 }, signal), []);
  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);

  const definitions = metrics.data?.items ?? [];
  const accountList = accounts.data?.items ?? [];

  const concepts = useMemo(() => {
    const seen: string[] = [];
    for (const definition of definitions) if (definition.preferred_for && !seen.includes(definition.preferred_for)) seen.push(definition.preferred_for);
    return seen.sort((a, b) => a.localeCompare(b));
  }, [definitions]);

  const effectiveConcept = concept && concepts.includes(concept) ? concept : concepts.includes('views') ? 'views' : concepts[0] ?? '';

  const definitionFor = (platform: Platform): MetricDefinition | null => {
    const matches = definitions.filter((definition) => definition.platform === platform && definition.preferred_for === effectiveConcept);
    return matches.find((definition) => !definition.is_deprecated) ?? matches[0] ?? null;
  };

  const accountFor = (platform: Platform): AccountSummary | null => {
    const forPlatform = accountList.filter((account) => account.platform === platform);
    return forPlatform.find((account) => account.id === overrides[platform]) ?? forPlatform[0] ?? null;
  };

  const results = useAsyncData<PlatformResult[]>(async (signal) => {
    if (!effectiveConcept) return [];
    return Promise.all(
      PLATFORMS.map(async (platform): Promise<PlatformResult> => {
        const account = accountFor(platform);
        const definition = definitionFor(platform);
        const base: PlatformResult = { platform, account, definition, series: null, comparison: null, seriesError: null, comparisonNote: null };
        if (!account || !definition) return base;
        try {
          const series = await api.series(
            { account_id: account.id, metric_key: definition.metric_key, bucket: 'day', start: range.start, end: range.end },
            signal,
          );
          let comparison: Comparison | null = null;
          let comparisonNote: string | null = null;
          try {
            comparison = await api.compare(
              { account_id: account.id, metric_key: definition.metric_key, attribute: 'format', max_groups: 3 },
              signal,
            );
          } catch (err) {
            comparisonNote =
              err instanceof AnalyticsApiError && err.code === 'NO_PUBLICATIONS'
                ? 'no published objects with a provider id to compare yet'
                : err instanceof Error
                  ? err.message
                  : String(err);
          }
          return { ...base, series, comparison, comparisonNote };
        } catch (err) {
          return { ...base, seriesError: err instanceof Error ? err.message : String(err) };
        }
      }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveConcept, range.start, range.end, overrides, accountList.length]);

  const visible = (results.data ?? []).filter((row) => enabled[row.platform]);
  const exposed = visible.filter((row) => row.definition);

  const comparability = useMemo(() => {
    if (exposed.length < 2) {
      return { comparable: null as boolean | null, message: 'Select at least two platforms that expose this concept to compare them.' };
    }
    const units = [...new Set(exposed.map((row) => row.definition?.unit))];
    const classes = [...new Set(exposed.map((row) => row.definition?.metric_class))];
    if (units.length === 1 && classes.length === 1) {
      return { comparable: true, message: `Comparable: every shown platform defines "${effectiveConcept}" as ${units[0]} / ${classes[0]}.` };
    }
    return {
      comparable: false,
      message: `Not directly comparable: "${effectiveConcept}" resolves to different definitions (units: ${units.join(', ')}; classes: ${classes.join(', ')}). Each platform is charted on its own axis on purpose.`,
    };
  }, [exposed, effectiveConcept]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Platform Comparison"
        subtitle="One concept across platforms — only where the providers define it comparably."
        actions={
          <>
            <DateRangePicker value={range} onChange={setRange} />
            <Button onClick={() => { metrics.reload(); accounts.reload(); results.reload(); }} busy={metrics.loading || results.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <ErrorBlock error={metrics.error ?? accounts.error} onRetry={() => { metrics.reload(); accounts.reload(); }} />

      {metrics.loading && !metrics.data ? <LoadingBlock /> : null}

      <Card title="Filters" subtitle="Every control here changes the queries below">
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Concept</span>
            <select
              className="mt-1 max-w-[22rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={effectiveConcept}
              onChange={(event) => setConcept(event.target.value)}
            >
              {concepts.length === 0 ? <option value="">no concepts in the registry</option> : null}
              {concepts.map((value) => (
                <option key={value} value={value}>
                  {value} · {PLATFORMS.filter((platform) => definitionFor(platform)).map(platformLabel).join(', ') || 'no platform'}
                </option>
              ))}
            </select>
          </label>
          <div className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Platforms</span>
            <div className="mt-1 flex gap-3">
              {PLATFORMS.map((platform) => (
                <label key={platform} className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={enabled[platform]}
                    onChange={(event) => setEnabled((current) => ({ ...current, [platform]: event.target.checked }))}
                  />
                  {platformLabel(platform)}
                </label>
              ))}
            </div>
          </div>
          <div className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Account (per platform)</span>
            <span className="mt-1 flex flex-wrap gap-2">
              {PLATFORMS.filter((platform) => enabled[platform]).map((platform) => {
                const forPlatform = accountList.filter((account) => account.platform === platform);
                return (
                  <select
                    key={platform}
                    className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
                    value={accountFor(platform)?.id ?? ''}
                    onChange={(event) => setOverrides((current) => ({ ...current, [platform]: event.target.value }))}
                    disabled={forPlatform.length === 0}
                  >
                    {forPlatform.length === 0 ? <option value="">{platformLabel(platform)}: none</option> : null}
                    {forPlatform.map((account) => (
                      <option key={account.id} value={account.id}>
                        {platformLabel(platform)} · {account.display_name}
                      </option>
                    ))}
                  </select>
                );
              })}
            </span>
          </div>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Resolved keys — {PLATFORMS.filter((platform) => definitionFor(platform)).map((platform) => `${platformLabel(platform)}: ${definitionFor(platform)?.metric_key}`).join(' · ') || 'none'}
        </p>
      </Card>

      {comparability.comparable !== null ? (
        <div
          className={`rounded-lg border px-3 py-2 text-sm ${
            comparability.comparable
              ? 'border-emerald-500/30 bg-emerald-500/5 text-emerald-300'
              : 'border-amber-500/30 bg-amber-500/5 text-amber-300'
          }`}
        >
          {comparability.message}
        </div>
      ) : (
        <p className="text-xs text-slate-500">{comparability.message}</p>
      )}

      {results.loading && !results.data ? <LoadingBlock label="Loading platform series..." /> : null}
      <ErrorBlock error={results.error} onRetry={results.reload} />

      <section className="grid gap-5 xl:grid-cols-2">
        {visible.map((row) => (
          <Card
            key={row.platform}
            title={platformLabel(row.platform)}
            subtitle={row.definition ? `${row.definition.metric_key} · ${row.definition.unit} · ${row.definition.metric_class}` : 'not exposed by this platform'}
            actions={row.definition ? <AvailabilityChip availability={row.definition.availability_default} reason={row.definition.definition_text} /> : undefined}
          >
            {!row.account ? (
              <EmptyState title={`No ${platformLabel(row.platform)} account`} hint="Connect one to include it in the comparison." />
            ) : !row.definition ? (
              <div className="rounded-lg border border-dashed border-slate-700/70 px-4 py-6 text-center">
                <p className="text-sm text-slate-300">Not exposed by {platformLabel(row.platform)}</p>
                <p className="mt-1 text-xs text-slate-500">
                  The metric registry has no "{effectiveConcept}" metric for {platformLabel(row.platform)}; nothing is estimated.
                </p>
              </div>
            ) : row.seriesError ? (
              <ErrorBlock error={row.seriesError} onRetry={results.reload} />
            ) : (
              <>
                <MetricWidget
                  title={row.definition.metric_key}
                  subtitle={`${formatRangeLabel(range)} · ${row.account.display_name}`}
                  series={
                    row.series
                      ? [
                          seriesFromRows(
                            row.definition.metric_key,
                            row.definition.provider_metric_name,
                            row.definition.unit,
                            row.series.points.map((point) => ({ at: point.bucket_start, value: point.value, availability: point.availability })),
                          ),
                        ]
                      : []
                  }
                  bucket={row.series?.bucket ?? 'day'}
                  emptyHint="No stored observation for this account in the selected range."
                  height={200}
                />
                {row.comparison && row.comparison.groups.length > 0 ? (
                  <div className="mt-3">
                    <p className="text-[11px] uppercase tracking-wide text-slate-500">By format (deterministic medians)</p>
                    <div className="mt-1 space-y-1">
                      {row.comparison.groups.map((group) => (
                        <div key={group.label} className="flex items-center justify-between gap-3 text-xs">
                          <span className="min-w-0 truncate text-slate-400">{group.label}</span>
                          <span className="flex items-center gap-2 text-slate-300">
                            <span className="font-mono">{group.median === null ? '—' : `${group.median} ${row.definition?.unit ?? ''}`}</span>
                            <Badge tone="muted">n={group.n}</Badge>
                            <AvailabilityChip availability={group.availability} />
                          </span>
                        </div>
                      ))}
                    </div>
                    {row.comparison.limitations.length ? <p className="mt-1 text-xs text-amber-400">{row.comparison.limitations[0]}</p> : null}
                  </div>
                ) : row.comparisonNote ? (
                  <p className="mt-2 text-xs text-slate-500">Comparison unavailable: {row.comparisonNote}</p>
                ) : null}
              </>
            )}
          </Card>
        ))}
      </section>

      {visible.length === 0 ? <EmptyState title="No platform selected" hint="Enable at least one platform above." /> : null}
    </div>
  );
}
