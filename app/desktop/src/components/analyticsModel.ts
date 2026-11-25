/**
 * Pure page logic for the P8.21 Analytics pages (publication detail, content comparison, campaigns).
 *
 * Same contract as `chartModel.ts`: no React, no fetch, no DOM — so it can be unit-tested with
 * `node --test`. The rule that governs every function here is the project-wide one: a metric that
 * was not `observed` is **absent**, never zero. `scalarMetric` and `publicationValues` only ever
 * surface a number when the availability is `observed` AND the value is finite, so a page cannot
 * turn "the provider did not say" into a `0` by accident.
 *
 * Types are imported with `import type`, so this module has no runtime dependency and can be loaded
 * directly by the Node test runner.
 */
import {
  type ChartSeries,
  type ObservationAvailability,
  toAvailability,
} from './chartModel.ts';
import type {
  Aggregation,
  Comparison,
  ComparisonGroup,
  GrowthPoint,
  MetricDefinition,
  MetricValue,
  PublicationSummary,
  Retention,
  TimelinePoint,
} from '../services/apiAnalytics';

//: The canonical order of the performance windows (mirrors `AnalyticsWindowKind` + offsets).
export const WINDOW_ORDER = ['initial', 'h24', 'h48', 'd7', 'd28', 'lifetime', 'custom'] as const;

export function windowRank(window: string): number {
  const index = (WINDOW_ORDER as readonly string[]).indexOf(window);
  return index === -1 ? WINDOW_ORDER.length : index;
}

export function indexDefinitions(definitions: MetricDefinition[]): Map<string, MetricDefinition> {
  const map = new Map<string, MetricDefinition>();
  for (const definition of definitions) map.set(definition.metric_key, definition);
  return map;
}

/** The platform a metric belongs to, from the registry (falls back to the key's provider prefix). */
export function platformForMetric(metricKey: string, definitions: Map<string, MetricDefinition>): string {
  const definition = definitions.get(metricKey);
  if (definition) return definition.platform;
  const prefix = metricKey.split('.')[0];
  return prefix === 'fb' ? 'facebook' : prefix === 'ig' ? 'instagram' : prefix === 'tt' ? 'tiktok' : 'unknown';
}

/**
 * Resolve a registry **concept** (`preferred_for`, e.g. `views`) to one platform's own metric key.
 * Returns null when the platform does not expose the concept — the caller must show that, not an
 * empty chart.
 */
export function metricKeyForConcept(
  definitions: MetricDefinition[],
  concept: string,
  platform?: string,
): string | null {
  const matches = definitions.filter(
    (definition) => definition.preferred_for === concept && (!platform || definition.platform === platform),
  );
  const chosen = matches.find((definition) => !definition.is_deprecated) ?? matches[0];
  return chosen ? chosen.metric_key : null;
}

export type ExposedConcept = {
  concept: string;
  metric_key: string;
  unit: string;
  metric_class: string;
  definition: string;
  availability_default: ObservationAvailability;
};

/** Which concepts a platform's registry actually declares — the "provider differences" panel. */
export function conceptsForPlatform(definitions: MetricDefinition[], platform: string): ExposedConcept[] {
  const seen = new Map<string, ExposedConcept>();
  for (const definition of definitions) {
    if (definition.platform !== platform || !definition.preferred_for) continue;
    if (definition.is_deprecated && seen.has(definition.preferred_for)) continue;
    seen.set(definition.preferred_for, {
      concept: definition.preferred_for,
      metric_key: definition.metric_key,
      unit: definition.unit,
      metric_class: definition.metric_class,
      definition: definition.definition_text,
      availability_default: definition.availability_default,
    });
  }
  return [...seen.values()].sort((a, b) => a.concept.localeCompare(b.concept));
}

export type ScalarMetric = {
  metric_key: string;
  unit: string;
  metric_class: string | null;
  period_kind: string;
  availability: ObservationAvailability;
  reason: string | null;
  /** A number **only** when the value was observed; otherwise null (never 0). */
  value: number | null;
};

/** A single metric reduced for display, preserving the availability and never coercing to zero. */
export function scalarMetric(metric: MetricValue): ScalarMetric {
  const availability = toAvailability(metric.availability);
  const observed = availability === 'observed' && typeof metric.value === 'number' && Number.isFinite(metric.value);
  return {
    metric_key: metric.metric_key,
    unit: metric.unit,
    metric_class: metric.metric_class,
    period_kind: metric.period_kind,
    availability,
    reason: metric.availability_reason,
    value: observed ? (metric.value as number) : null,
  };
}

export type WindowGroup = { window: string; metrics: ScalarMetric[] };

/** Publication metrics grouped by performance window, in the canonical window order. */
export function metricsByWindow(metrics: MetricValue[]): WindowGroup[] {
  const groups = new Map<string, ScalarMetric[]>();
  for (const metric of metrics) {
    const window = metric.period_kind || 'current';
    const list = groups.get(window) ?? [];
    list.push(scalarMetric(metric));
    groups.set(window, list);
  }
  return [...groups.entries()]
    .map(([window, list]) => ({
      window,
      metrics: list.sort((a, b) => a.metric_key.localeCompare(b.metric_key)),
    }))
    .sort((a, b) => windowRank(a.window) - windowRank(b.window) || a.window.localeCompare(b.window));
}

/** How many metrics in a set are observed vs unavailable, without inventing a total. */
export function availabilityBreakdown(metrics: ScalarMetric[]): Record<string, number> {
  const summary: Record<string, number> = {};
  for (const metric of metrics) summary[metric.availability] = (summary[metric.availability] ?? 0) + 1;
  return summary;
}

/**
 * One ChartSeries per metric key from a publication timeline. A timeline point that is not observed
 * becomes a gap in the chart; the value is passed through untouched.
 */
export function timelineSeries(
  points: TimelinePoint[],
  definitions: Map<string, MetricDefinition>,
): ChartSeries[] {
  const byMetric = new Map<string, TimelinePoint[]>();
  for (const point of points) {
    const list = byMetric.get(point.metric_key) ?? [];
    list.push(point);
    byMetric.set(point.metric_key, list);
  }
  const series: ChartSeries[] = [];
  for (const [metricKey, rows] of byMetric) {
    const definition = definitions.get(metricKey);
    series.push({
      key: metricKey,
      label: definition?.provider_metric_name ?? metricKey,
      unit: definition?.unit ?? rows[0]?.unit ?? undefined,
      points: rows
        .slice()
        .sort((a, b) => Date.parse(a.collected_at) - Date.parse(b.collected_at))
        .map((row) => ({
          x: Date.parse(row.collected_at),
          y: row.value,
          availability: toAvailability(row.availability),
        })),
    });
  }
  return series.sort((a, b) => a.key.localeCompare(b.key));
}

export type RetentionChart = {
  series: ChartSeries;
  bucketCount: number;
  incomplete: boolean;
  unit: string;
};

/**
 * A retention curve as a chart, **only** when a curve was actually observed and carries numeric
 * points. Any other case returns a chart of `null` plus the reason to display — an unavailable or
 * malformed curve can never become a drawn line.
 */
export function retentionChart(retention: Retention | null | undefined): {
  chart: RetentionChart | null;
  reason: string;
} {
  if (!retention) return { chart: null, reason: 'Retention was not loaded.' };
  const curve = retention.curves.find((item) => toAvailability(item.availability) === 'observed');
  if (!curve) {
    return {
      chart: null,
      reason: retention.reason ?? 'No retention curve is stored; the provider may not expose one.',
    };
  }
  const payload = (curve.value_json ?? null) as { points?: unknown; unit?: string; bucket_count?: number; incomplete?: boolean } | null;
  const raw = Array.isArray(payload?.points) ? payload!.points : [];
  const numbers = raw.filter((item): item is number => typeof item === 'number' && Number.isFinite(item));
  if (numbers.length === 0) {
    return {
      chart: null,
      reason: 'The stored retention payload has no numeric points, so nothing can be plotted.',
    };
  }
  const width = String(numbers.length).length;
  const series: ChartSeries = {
    key: curve.metric_key,
    label: 'Play-through',
    unit: payload?.unit ?? curve.unit,
    points: numbers.map((value, index) => ({
      // Categorical, zero-padded so the axis order is the interval order (localeCompare-safe).
      x: String(index + 1).padStart(width, '0'),
      y: value,
      availability: 'observed' as ObservationAvailability,
    })),
  };
  return {
    chart: {
      series,
      bucketCount: numbers.length,
      incomplete: Boolean(payload?.incomplete) || raw.length !== numbers.length,
      unit: series.unit ?? 'ratio',
    },
    reason: '',
  };
}

export type PublicationValue = {
  publication_id: string;
  value: number | null;
  availability: ObservationAvailability;
  unit: string;
  sample_size: number | null;
  method: string | null;
};

/** Publication-scope aggregation values for one metric+window, keyed by publication id. */
export function publicationValues(
  aggregations: Aggregation[],
  metricKey: string,
  windowKind?: string,
): Map<string, PublicationValue> {
  const map = new Map<string, PublicationValue>();
  for (const row of aggregations) {
    if (row.scope !== 'publication' || row.metric_key !== metricKey) continue;
    if (windowKind && row.window_kind !== windowKind) continue;
    if (!row.publication_key) continue;
    const availability = toAvailability(row.availability);
    const observed = availability === 'observed' && typeof row.value === 'number' && Number.isFinite(row.value);
    map.set(row.publication_key, {
      publication_id: row.publication_key,
      value: observed ? (row.value as number) : null,
      availability,
      unit: row.unit,
      sample_size: row.sample_size,
      method: row.method,
    });
  }
  return map;
}

export type RankedPublication = {
  publication: PublicationSummary;
  value: number | null;
  availability: ObservationAvailability;
  unit: string;
  sample_size: number | null;
};

export type RankedResult = {
  /** Observed rows, best first for `desc` (or worst first for `asc`). */
  ranked: RankedPublication[];
  /** Publications in the selection that have no observed value for this metric. */
  unmeasured: PublicationSummary[];
  observedCount: number;
  unavailableCount: number;
};

/** Rank publications by an observed metric. Non-observed values are listed separately, never as 0. */
export function rankPublications(
  publications: PublicationSummary[],
  values: Map<string, PublicationValue>,
  options: { direction?: 'desc' | 'asc'; limit?: number } = {},
): RankedResult {
  const direction = options.direction ?? 'desc';
  const rows: RankedPublication[] = [];
  const unmeasured: PublicationSummary[] = [];
  for (const publication of publications) {
    const entry = values.get(publication.publication_id);
    if (!entry || entry.value === null) {
      unmeasured.push(publication);
      continue;
    }
    rows.push({
      publication,
      value: entry.value,
      availability: entry.availability,
      unit: entry.unit,
      sample_size: entry.sample_size,
    });
  }
  rows.sort((a, b) => {
    const diff = (a.value as number) - (b.value as number);
    return direction === 'desc' ? -diff : diff;
  });
  const limited = typeof options.limit === 'number' ? rows.slice(0, options.limit) : rows;
  return {
    ranked: limited,
    unmeasured,
    observedCount: rows.length,
    unavailableCount: unmeasured.length,
  };
}

/** Campaign / set breakdown by an arbitrary key, sorted by count then key (deterministic). */
export function countBy<T>(rows: T[], keyOf: (row: T) => string | null | undefined): { key: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const row of rows) {
    const key = keyOf(row);
    if (!key) continue;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

export type PlatformMetrics = { platform: string; metrics: ScalarMetric[] };

/**
 * Campaign metrics grouped by platform. Incompatible platforms are never merged into one total; the
 * caller renders a separate table/axis per platform.
 */
export function metricsByPlatform(
  metrics: MetricValue[],
  definitions: Map<string, MetricDefinition>,
): PlatformMetrics[] {
  const groups = new Map<string, ScalarMetric[]>();
  for (const metric of metrics) {
    const platform = platformForMetric(metric.metric_key, definitions);
    const list = groups.get(platform) ?? [];
    list.push(scalarMetric(metric));
    groups.set(platform, list);
  }
  return [...groups.entries()]
    .map(([platform, list]) => ({
      platform,
      metrics: list.sort((a, b) => a.metric_key.localeCompare(b.metric_key)),
    }))
    .sort((a, b) => a.platform.localeCompare(b.platform));
}

/** The most recent timestamp in a set, or null. Used for a "data as of" label. */
export function latestTimestamp(values: (string | null | undefined)[]): string | null {
  let latest: number | null = null;
  let latestIso: string | null = null;
  for (const value of values) {
    if (!value) continue;
    const parsed = Date.parse(value);
    if (Number.isNaN(parsed)) continue;
    if (latest === null || parsed > latest) {
      latest = parsed;
      latestIso = value;
    }
  }
  return latestIso;
}

/** True when a publication was actually delivered (has a remote id and is not in a failed state). */
export function isDelivered(publication: PublicationSummary): boolean {
  return Boolean(publication.remote_post_id) && publication.state.toLowerCase() !== 'failed';
}

// ============================================================================================
// P8.22 — video performance, audience growth, hashtag analysis
// ============================================================================================

// ---------------------------------------------------------------- video: leaderboard columns

//: The video concepts the leaderboard tries to show, in reading order. A platform that does not
//: declare one simply has no column for it — nothing is estimated.
export const VIDEO_CONCEPT_ORDER = [
  'views',
  'reach',
  'reach_unique',
  'saves',
  'watch_time',
  'avg_watch_time',
  'replays',
  'retention',
] as const;

//: Engagement counters have no `preferred_for`; they are matched on our own stable key suffix.
const ENGAGEMENT_SUFFIXES = ['.likes', '.comments', '.shares', '.reactions', '.impressions'];

export type MetricColumn = { key: string; label: string; unit: string; concept: string | null };

/** The columns a video leaderboard may show for a platform: declared concepts plus our counters. */
export function videoMetricColumns(definitions: MetricDefinition[], platform?: string): MetricColumn[] {
  const columns: MetricColumn[] = [];
  const seen = new Set<string>();
  for (const concept of VIDEO_CONCEPT_ORDER) {
    const key = metricKeyForConcept(definitions, concept, platform);
    if (!key || seen.has(key)) continue;
    const definition = definitions.find((item) => item.metric_key === key);
    if (!definition) continue;
    seen.add(key);
    columns.push({ key, label: concept, unit: definition.unit, concept });
  }
  for (const definition of definitions) {
    if (platform && definition.platform !== platform) continue;
    if (seen.has(definition.metric_key)) continue;
    if (definition.object_type === 'account') continue;
    if (ENGAGEMENT_SUFFIXES.some((suffix) => definition.metric_key.endsWith(suffix))) {
      seen.add(definition.metric_key);
      columns.push({
        key: definition.metric_key,
        label: definition.metric_key.split('.').pop() ?? definition.metric_key,
        unit: definition.unit,
        concept: null,
      });
    }
  }
  return columns;
}

/** Engagement counters carry no `preferred_for`; they are addressed by our stable key suffix. */
export const COUNTER_CONCEPTS = ['likes', 'comments', 'shares', 'reactions', 'impressions'] as const;

/** The metric key that measures one concept *on one platform*, or null when it has none. */
export function metricKeyForCell(
  definitions: MetricDefinition[],
  concept: string,
  platform: string | null | undefined,
): string | null {
  if ((COUNTER_CONCEPTS as readonly string[]).includes(concept)) {
    const suffix = `.${concept}`;
    const matches = definitions.filter(
      (definition) =>
        definition.metric_key.endsWith(suffix) &&
        (!platform || definition.platform === platform) &&
        definition.object_type !== 'account',
    );
    const chosen = matches.find((definition) => !definition.is_deprecated) ?? matches[0];
    return chosen ? chosen.metric_key : null;
  }
  return metricKeyForConcept(definitions, concept, platform ?? undefined);
}

/**
 * The leaderboard's columns, in reading order, for the registry — never for one platform only.
 * A concept appears when *some* platform declares it; each row then resolves it against its own
 * platform, so a TikTok row is never shown a Facebook metric's value (or a zero for its absence).
 */
export function videoConcepts(definitions: MetricDefinition[], platform?: string): string[] {
  const available = new Set<string>();
  for (const definition of definitions) {
    if (platform && definition.platform !== platform) continue;
    if (definition.object_type === 'account') continue;
    if (definition.preferred_for) available.add(definition.preferred_for);
  }
  const ordered: string[] = VIDEO_CONCEPT_ORDER.filter((concept) => available.has(concept));
  for (const concept of COUNTER_CONCEPTS) {
    if (available.has(concept)) continue;
    const suffix = `.${concept}`;
    if (
      definitions.some(
        (definition) =>
          definition.metric_key.endsWith(suffix) &&
          (!platform || definition.platform === platform) &&
          definition.object_type !== 'account',
      )
    ) {
      ordered.push(concept);
    }
  }
  for (const concept of [...available].sort()) {
    if (!ordered.includes(concept)) ordered.push(concept);
  }
  return ordered;
}

export type ConceptCell = {
  publication: PublicationSummary;
  /** The metric that measures this concept on this row's own platform (null: not exposed). */
  metricKey: string | null;
  value: number | null;
  unit: string;
  availability: ObservationAvailability;
  sampleSize: number | null;
};

/**
 * Resolve one concept into per-publication cells. A platform that does not expose the concept gets
 * `not_supported` with a null value; an uncollected metric gets its own availability. Neither is 0.
 */
export function resolveConceptCells(
  publications: PublicationSummary[],
  matrix: Map<string, Map<string, PublicationValue>>,
  definitions: MetricDefinition[],
  concept: string,
): ConceptCell[] {
  return publications.map((publication) => {
    const metricKey = metricKeyForCell(definitions, concept, publication.platform);
    if (!metricKey) {
      return { publication, metricKey: null, value: null, unit: '', availability: 'not_supported' as ObservationAvailability, sampleSize: null };
    }
    const entry = matrix.get(metricKey)?.get(publication.publication_id);
    const unit = entry?.unit ?? definitions.find((item) => item.metric_key === metricKey)?.unit ?? '';
    if (!entry) {
      return { publication, metricKey, value: null, unit, availability: 'not_collected' as ObservationAvailability, sampleSize: null };
    }
    return {
      publication,
      metricKey,
      value: entry.value,
      unit,
      availability: entry.availability,
      sampleSize: entry.sample_size,
    };
  });
}

export type VideoCellBoard = {
  ranked: ConceptCell[];
  /** Rows with no observed value for this concept on their own platform — kept, never zeroed. */
  unmeasured: ConceptCell[];
};

/** Rank resolved cells. Only an observed value enters the ranking; an observed 0 ranks normally. */
export function rankVideoCells(cells: ConceptCell[], options: { direction?: 'desc' | 'asc' } = {}): VideoCellBoard {
  const direction = options.direction ?? 'desc';
  const ranked: ConceptCell[] = [];
  const unmeasured: ConceptCell[] = [];
  for (const cell of cells) {
    if (cell.value === null || cell.availability !== 'observed') unmeasured.push(cell);
    else ranked.push(cell);
  }
  ranked.sort((a, b) => {
    const diff = (a.value as number) - (b.value as number);
    return direction === 'desc' ? -diff : diff;
  });
  return { ranked, unmeasured };
}

export type VideoComparison = {
  /** One block per metric key. Rows inside a block share the metric definition, unit and window. */
  groups: { metricKey: string; unit: string; cells: ConceptCell[] }[];
  excluded: { publication: PublicationSummary; reason: string }[];
};

/**
 * A comparison may only rank rows measured by the *same* metric key at the same window. A TikTok view
 * and a Facebook view are different measurements, so they are separated into their own blocks and the
 * page says which block each video is in — nothing is compared across blocks.
 */
export function videoComparison(cells: ConceptCell[]): VideoComparison {
  const groups = new Map<string, ConceptCell[]>();
  const excluded: { publication: PublicationSummary; reason: string }[] = [];
  for (const cell of cells) {
    if (!cell.metricKey) {
      excluded.push({ publication: cell.publication, reason: `no ${cell.publication.platform} metric measures this concept` });
      continue;
    }
    if (cell.value === null) {
      excluded.push({ publication: cell.publication, reason: `no observed value for ${cell.metricKey}` });
      continue;
    }
    const list = groups.get(cell.metricKey) ?? [];
    list.push(cell);
    groups.set(cell.metricKey, list);
  }
  return {
    groups: [...groups.entries()]
      .map(([metricKey, rows]) => ({ metricKey, unit: rows[0].unit, cells: rows.slice().sort((a, b) => (b.value as number) - (a.value as number)) }))
      .sort((a, b) => a.metricKey.localeCompare(b.metricKey)),
    excluded,
  };
}

/** Publication-scope values for every metric at once, keyed by metric then publication. */
export function valuesByMetric(
  aggregations: Aggregation[],
  windowKind?: string,
): Map<string, Map<string, PublicationValue>> {
  const out = new Map<string, Map<string, PublicationValue>>();
  for (const row of aggregations) {
    if (row.scope !== 'publication' || !row.publication_key) continue;
    if (windowKind && row.window_kind !== windowKind) continue;
    const availability = toAvailability(row.availability);
    const observed = availability === 'observed' && typeof row.value === 'number' && Number.isFinite(row.value);
    const byPublication = out.get(row.metric_key) ?? new Map<string, PublicationValue>();
    byPublication.set(row.publication_key, {
      publication_id: row.publication_key,
      value: observed ? (row.value as number) : null,
      availability,
      unit: row.unit,
      sample_size: row.sample_size,
      method: row.method,
    });
    out.set(row.metric_key, byPublication);
  }
  return out;
}

export type RankedVideo = {
  publication: PublicationSummary;
  value: number | null;
  availability: ObservationAvailability;
  unit: string;
  sample_size: number | null;
};

export type VideoLeaderboard = {
  ranked: RankedVideo[];
  /** Publications with no observed value for the sort metric — kept, never given a zero. */
  unmeasured: PublicationSummary[];
  observedCount: number;
  unavailableCount: number;
};

/**
 * Rank a video leaderboard by one metric. An unobserved value never enters the ranking; an observed
 * zero is a real value and sorts normally.
 */
export function rankVideos(
  publications: PublicationSummary[],
  values: Map<string, PublicationValue>,
  options: { direction?: 'desc' | 'asc'; limit?: number } = {},
): VideoLeaderboard {
  const direction = options.direction ?? 'desc';
  const rows: RankedVideo[] = [];
  const unmeasured: PublicationSummary[] = [];
  for (const publication of publications) {
    const entry = values.get(publication.publication_id);
    if (!entry || entry.value === null) {
      unmeasured.push(publication);
      continue;
    }
    rows.push({ publication, value: entry.value, availability: entry.availability, unit: entry.unit, sample_size: entry.sample_size });
  }
  rows.sort((a, b) => {
    const diff = (a.value as number) - (b.value as number);
    return direction === 'desc' ? -diff : diff;
  });
  const ranked = typeof options.limit === 'number' ? rows.slice(0, options.limit) : rows;
  return { ranked, unmeasured, observedCount: rows.length, unavailableCount: unmeasured.length };
}

export type CompatiblePublications = {
  comparable: PublicationSummary[];
  excluded: { publication: PublicationSummary; reason: string }[];
};

/**
 * Publications that may be compared for one metric: only those on the metric's own platform. A
 * platform-specific key has no meaning elsewhere, so anything else is excluded with the reason.
 */
export function compatiblePublications(
  publications: PublicationSummary[],
  definitions: Map<string, MetricDefinition>,
  metricKey: string,
): CompatiblePublications {
  const platform = definitions.get(metricKey)?.platform;
  const comparable: PublicationSummary[] = [];
  const excluded: { publication: PublicationSummary; reason: string }[] = [];
  for (const publication of publications) {
    if (!platform) {
      excluded.push({ publication, reason: 'the metric is not in the registry' });
      continue;
    }
    if (publication.platform !== platform) {
      excluded.push({ publication, reason: `defined for ${platform}, not ${publication.platform}` });
      continue;
    }
    comparable.push(publication);
  }
  return { comparable, excluded };
}

// ---------------------------------------------------------------- audience growth

export type GrowthBucket = 'day' | 'week' | 'month';

/** The UTC start of the bucket an instant falls in (week starts Monday, month starts the 1st). */
export function bucketStart(iso: string, bucket: GrowthBucket): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const year = date.getUTCFullYear();
  const month = date.getUTCMonth();
  const day = date.getUTCDate();
  if (bucket === 'month') return new Date(Date.UTC(year, month, 1)).toISOString();
  if (bucket === 'day') return new Date(Date.UTC(year, month, day)).toISOString();
  // Week: back up to Monday (UTC day 1).
  const weekday = (date.getUTCDay() + 6) % 7;
  return new Date(Date.UTC(year, month, day - weekday)).toISOString();
}

/**
 * A growth point that can be placed on a time axis. A row with no `collected_at` cannot be dated, so
 * it is left out of the chart rather than plotted at "now" or at zero.
 */
export type DatedGrowthPoint = GrowthPoint & { collected_at: string };

export function datedGrowthPoints(points: GrowthPoint[]): DatedGrowthPoint[] {
  return points.filter((point): point is DatedGrowthPoint => typeof point.collected_at === 'string' && point.collected_at !== '');
}

/** One series per metric from a growth response, gaps preserved. */
export function growthSeries(points: GrowthPoint[], definitions: Map<string, MetricDefinition>): ChartSeries[] {
  const byMetric = new Map<string, DatedGrowthPoint[]>();
  for (const point of datedGrowthPoints(points)) {
    const list = byMetric.get(point.metric_key) ?? [];
    list.push(point);
    byMetric.set(point.metric_key, list);
  }
  const series: ChartSeries[] = [];
  for (const [metricKey, rows] of byMetric) {
    const definition = definitions.get(metricKey);
    series.push({
      key: metricKey,
      label: definition?.provider_metric_name ?? metricKey,
      unit: definition?.unit,
      points: rows
        .slice()
        .sort((a, b) => Date.parse(a.collected_at) - Date.parse(b.collected_at))
        .map((row) => ({ x: Date.parse(row.collected_at), y: row.value, availability: toAvailability(row.availability) })),
    });
  }
  return series.sort((a, b) => a.key.localeCompare(b.key));
}

/**
 * Coarsen a point-in-time series to a bucket, keeping the **latest observed value in each bucket**.
 * A bucket with no observation is simply absent — the chart draws a gap, never a carried-forward line.
 */
export function bucketGrowthSeries(
  points: GrowthPoint[],
  bucket: GrowthBucket,
  definitions: Map<string, MetricDefinition>,
): ChartSeries[] {
  const byMetric = new Map<string, Map<string, DatedGrowthPoint>>();
  for (const point of datedGrowthPoints(points)) {
    if (toAvailability(point.availability) !== 'observed') continue;
    const key = bucketStart(point.collected_at, bucket);
    const buckets = byMetric.get(point.metric_key) ?? new Map<string, DatedGrowthPoint>();
    const current = buckets.get(key);
    if (!current || Date.parse(point.collected_at) > Date.parse(current.collected_at)) buckets.set(key, point);
    byMetric.set(point.metric_key, buckets);
  }
  const series: ChartSeries[] = [];
  for (const [metricKey, buckets] of byMetric) {
    const definition = definitions.get(metricKey);
    series.push({
      key: metricKey,
      label: definition?.provider_metric_name ?? metricKey,
      unit: definition?.unit,
      points: [...buckets.entries()]
        .sort((a, b) => Date.parse(a[0]) - Date.parse(b[0]))
        .map(([key, point]) => ({ x: Date.parse(key), y: point.value, availability: 'observed' as ObservationAvailability })),
    });
  }
  return series.sort((a, b) => a.key.localeCompare(b.key));
}

export type GrowthDelta = {
  metricKey: string;
  first: number;
  last: number;
  net: number;
  direction: 'up' | 'down' | 'flat';
  observations: number;
  firstAt: string;
  lastAt: string;
};

/**
 * The net change between the first and last **observed** growth points. Returns null with fewer than
 * two observations: a single reading is a level, and a level cannot show a change.
 */
export function observedDelta(points: GrowthPoint[]): GrowthDelta | null {
  const observed = datedGrowthPoints(points)
    .filter((point) => toAvailability(point.availability) === 'observed' && typeof point.value === 'number' && Number.isFinite(point.value))
    .sort((a, b) => Date.parse(a.collected_at) - Date.parse(b.collected_at));
  if (observed.length < 2) return null;
  const first = observed[0];
  const last = observed[observed.length - 1];
  const net = (last.value as number) - (first.value as number);
  return {
    metricKey: last.metric_key,
    first: first.value as number,
    last: last.value as number,
    net,
    direction: net > 0 ? 'up' : net < 0 ? 'down' : 'flat',
    observations: observed.length,
    firstAt: first.collected_at,
    lastAt: last.collected_at,
  };
}

// ---------------------------------------------------------------- hashtags

export type HashtagEntry = {
  hashtag: string;
  /** Display form from the first publication that used it. */
  label: string;
  publications: number;
  accounts: number;
  platforms: string[];
  firstPublished: string | null;
  lastPublished: string | null;
  publicationIds: string[];
};

/**
 * Build a hashtag catalogue from published captions. Hashtags are grouped case-insensitively (so
 * `#Reels` and `#reels` are one entry) but the display form is never rewritten. Counts come from the
 * publication rows themselves, not from any performance number.
 */
export function hashtagCatalogue(publications: PublicationSummary[]): HashtagEntry[] {
  const byTag = new Map<string, { label: string; accounts: Set<string>; platforms: Set<string>; ids: string[]; first: string | null; last: string | null }>();
  for (const publication of publications) {
    for (const raw of publication.hashtags) {
      const tag = raw.replace(/^#/, '').trim();
      if (!tag) continue;
      const key = tag.toLowerCase();
      const entry = byTag.get(key) ?? { label: tag, accounts: new Set<string>(), platforms: new Set<string>(), ids: [], first: null, last: null };
      entry.accounts.add(publication.account_id);
      entry.platforms.add(publication.platform);
      entry.ids.push(publication.publication_id);
      const published = publication.provider_published_at;
      if (published) {
        if (!entry.first || Date.parse(published) < Date.parse(entry.first)) entry.first = published;
        if (!entry.last || Date.parse(published) > Date.parse(entry.last)) entry.last = published;
      }
      byTag.set(key, entry);
    }
  }
  return [...byTag.entries()]
    .map(([key, entry]) => ({
      hashtag: key,
      label: entry.label,
      publications: entry.ids.length,
      accounts: entry.accounts.size,
      platforms: [...entry.platforms].sort(),
      firstPublished: entry.first,
      lastPublished: entry.last,
      publicationIds: entry.ids,
    }))
    .sort((a, b) => b.publications - a.publications || a.label.localeCompare(b.label));
}

export type ExcludedGroup = { group: ComparisonGroup; rankable: false; reason: string };

/**
 * Split comparison groups into the ones that may be ranked and the ones that may not. A group is
 * rankable only when it was observed, has a median, and meets the minimum sample size; anything else
 * is returned with the reason so the page can disclose it instead of silently dropping it.
 */
export function comparableGroups(
  comparison: Comparison | null | undefined,
  minSamples = 1,
): { ranked: ComparisonGroup[]; excluded: ExcludedGroup[] } {
  if (!comparison) return { ranked: [], excluded: [] };
  const ranked: ComparisonGroup[] = [];
  const excluded: ExcludedGroup[] = [];
  for (const group of comparison.groups) {
    if (toAvailability(group.availability) !== 'observed') {
      excluded.push({ group, rankable: false, reason: 'not observed' });
      continue;
    }
    if (group.median === null) {
      excluded.push({ group, rankable: false, reason: 'no median' });
      continue;
    }
    if (group.n < minSamples) {
      excluded.push({ group, rankable: false, reason: `fewer than ${minSamples} observation(s)` });
      continue;
    }
    ranked.push(group);
  }
  ranked.sort((a, b) => (b.median as number) - (a.median as number));
  return { ranked, excluded };
}

