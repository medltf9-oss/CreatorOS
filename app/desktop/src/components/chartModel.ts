/**
 * Pure chart logic for the Analytics Center.
 *
 * This module has no React and no SVG: it is the part that must be *provably* correct, so it can be
 * unit-tested with `node --test`. `charts.tsx` renders what these functions return.
 *
 * The one rule the whole module exists to enforce: an observation that was not `observed` is a
 * **gap**, never a zero. `isObserved` requires both the availability AND a finite value, so even a
 * malformed payload that carries `y: 0` with a non-observed state is treated as missing.
 *
 * The availability values are exactly the backend's `MetricAvailability` (db/enums.py); P8.19's
 * `apiAnalytics.ts` reuses this union rather than inventing a second vocabulary.
 */

export type ObservationAvailability =
  | 'observed'
  | 'missing'
  | 'below_threshold'
  | 'not_supported'
  | 'not_authorized'
  | 'not_collected'
  | 'temporarily_unavailable'
  | 'expired'
  | 'redacted_by_policy'
  | 'unknown';

export const AVAILABILITY_STATES: ObservationAvailability[] = [
  'observed',
  'missing',
  'below_threshold',
  'not_supported',
  'not_authorized',
  'not_collected',
  'temporarily_unavailable',
  'expired',
  'redacted_by_policy',
  'unknown',
];

/** Human wording for a gap. Used by the legend, the tooltip and the empty state. */
export const AVAILABILITY_LABELS: Record<ObservationAvailability, string> = {
  observed: 'Observed',
  missing: 'Missing',
  below_threshold: 'Below the provider threshold',
  not_supported: 'Not exposed by the provider',
  not_authorized: 'Permission not granted',
  not_collected: 'Not collected yet',
  temporarily_unavailable: 'Temporarily unavailable',
  expired: 'Expired (outside the provider retention window)',
  redacted_by_policy: 'Redacted by policy',
  unknown: 'Unknown',
};

/** Normalise any string to a known availability, mirroring the backend's total mapping. */
export function toAvailability(value: string | null | undefined): ObservationAvailability {
  if (!value) return 'unknown';
  const text = String(value).trim().toLowerCase();
  return (AVAILABILITY_STATES as string[]).includes(text) ? (text as ObservationAvailability) : 'unknown';
}

export type ChartPoint = {
  /** Epoch milliseconds for a time series, or a category label. */
  x: number | string;
  /** The measured value, or `null` when the metric was not observed. */
  y: number | null;
  availability: ObservationAvailability;
};

export type ChartSeries = {
  key: string;
  label: string;
  unit?: string;
  points: ChartPoint[];
  kind?: 'line' | 'bar';
  color?: string;
};

/** True only when there is a real, finite measurement. */
export function isObserved(point: ChartPoint): boolean {
  return point.availability === 'observed' && typeof point.y === 'number' && Number.isFinite(point.y);
}

export function observedValues(points: ChartPoint[]): number[] {
  return points.filter(isObserved).map((point) => point.y as number);
}

export function observedCount(points: ChartPoint[]): number {
  return observedValues(points).length;
}

export type SegmentOptions = {
  /**
   * For a time series: break the line when two observed points are further apart than this many
   * milliseconds. Absent buckets are not listed by the API, so without this a line would silently
   * bridge a week of missing data. When omitted, only explicit non-observed points break a line.
   */
  gapThresholdMs?: number;
};

/**
 * Split a series into the runs of points a line may be drawn through.
 *
 * A non-observed point ends the current run (so no segment is drawn across the gap), and an
 * over-long time distance starts a new one. Nothing is ever added between points.
 */
export function segmentObserved(points: ChartPoint[], options: SegmentOptions = {}): ChartPoint[][] {
  const threshold = options.gapThresholdMs;
  const segments: ChartPoint[][] = [];
  let current: ChartPoint[] = [];
  let previousX: number | null = null;

  const flush = () => {
    if (current.length) segments.push(current);
    current = [];
  };

  for (const point of points) {
    if (!isObserved(point)) {
      flush();
      previousX = null;
      continue;
    }
    if (
      current.length > 0 &&
      typeof threshold === 'number' &&
      threshold > 0 &&
      typeof point.x === 'number' &&
      previousX !== null &&
      point.x - previousX > threshold
    ) {
      flush();
    }
    current.push(point);
    previousX = typeof point.x === 'number' ? point.x : null;
  }
  flush();
  return segments;
}

/** The bucket step in milliseconds, so a chart can pass it as `gapThresholdMs`. */
export function bucketStepMs(bucket: 'hour' | 'day' | 'week'): number {
  const hour = 60 * 60 * 1000;
  return bucket === 'hour' ? hour : bucket === 'day' ? 24 * hour : 7 * 24 * hour;
}

export type Axis = {
  min: number;
  max: number;
  step: number;
  ticks: number[];
  allZero: boolean;
  empty: boolean;
};

/** The 1-2-2.5-5-10 step used for all axes. Deterministic for a given input. */
export function niceStep(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalized = raw / magnitude;
  const multiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10;
  return multiplier * magnitude;
}

export function decimalsFor(step: number): number {
  if (!Number.isFinite(step) || step <= 0) return 0;
  for (let decimals = 0; decimals <= 6; decimals += 1) {
    if (Math.abs(Number(step.toFixed(decimals)) - step) < step * 1e-9) return decimals;
  }
  return 6;
}

/**
 * A predictable axis.
 *
 * * empty input → 0..1 with quarter ticks (never a collapse to nothing);
 * * all observed values zero → 0..1, so a real flat line at zero is visible and cannot be confused
 *   with a missing series;
 * * `includeZero` (default true) anchors the baseline at zero for count/views metrics;
 * * decimals follow the step, so a ratio axis reads 0 / 0.25 / 0.5 and a count axis reads 0 / 1k / 2k.
 */
export function computeAxis(values: number[], options: { tickCount?: number; includeZero?: boolean } = {}): Axis {
  const tickCount = Math.min(10, Math.max(2, Math.round(options.tickCount ?? 5)));
  const includeZero = options.includeZero !== false;
  const finite = values.filter((value) => Number.isFinite(value));
  if (finite.length === 0) {
    return { min: 0, max: 1, step: 0.25, ticks: [0, 0.25, 0.5, 0.75, 1], allZero: false, empty: true };
  }

  let min = Math.min(...finite);
  let max = Math.max(...finite);
  const allZero = finite.every((value) => value === 0);
  if (includeZero) {
    min = Math.min(min, 0);
    max = Math.max(max, 0);
  }
  if (allZero) {
    min = 0;
    max = 1;
  } else if (min === max) {
    const pad = Math.abs(min) || 1;
    min = includeZero && min > 0 ? 0 : min - pad;
    max = max + pad;
  }

  const step = niceStep((max - min) / (tickCount - 1));
  const niceMin = Math.floor(min / step) * step;
  const niceMax = Math.ceil(max / step) * step;
  const decimals = decimalsFor(step);
  const ticks: number[] = [];
  const count = Math.round((niceMax - niceMin) / step);
  for (let index = 0; index <= count; index += 1) {
    ticks.push(Number((niceMin + index * step).toFixed(decimals)));
  }
  if (ticks.length === 0) ticks.push(niceMin);
  if (ticks[ticks.length - 1] < niceMax) ticks.push(Number(niceMax.toFixed(decimals)));
  return { min: niceMin, max: niceMax, step, ticks, allZero, empty: false };
}

/** Map a value onto a pixel coordinate. `invert` puts larger values higher (SVG y grows downward). */
export function scaleValue(value: number, axis: Axis, size: number, invert = true): number {
  const span = axis.max - axis.min || 1;
  const ratio = (value - axis.min) / span;
  const clamped = Math.min(1, Math.max(0, ratio));
  return invert ? size - clamped * size : clamped * size;
}

/** Evenly spaced indices, always including the first and last when there is more than one point. */
export function selectTickIndices(pointCount: number, maxTicks: number): number[] {
  if (pointCount <= 0) return [];
  if (pointCount === 1) return [0];
  const count = Math.max(2, Math.min(pointCount, Math.floor(maxTicks)));
  const indices: number[] = [];
  for (let index = 0; index < count; index += 1) {
    indices.push(Math.round((index * (pointCount - 1)) / (count - 1)));
  }
  return [...new Set(indices)];
}

function trimOne(n: number): string {
  return String(Number(n.toFixed(1)));
}

/** Compact axis/label formatting: 0, 42, 1.5k, 3.4M, 1.2B, -18.5. */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const sign = value < 0 ? '-' : '';
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${sign}${trimOne(abs / 1e9)}B`;
  if (abs >= 1e6) return `${sign}${trimOne(abs / 1e6)}M`;
  if (abs >= 1e3) return `${sign}${trimOne(abs / 1e3)}k`;
  if (Number.isInteger(value)) return String(value);
  return String(Number(value.toFixed(2)));
}

/** Format a measured value for a tooltip or a label. A missing value is worded, never shown as 0. */
export function formatMetricValue(
  value: number | null | undefined,
  unit?: string,
  availability: ObservationAvailability = 'observed',
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return availability === 'observed' ? '—' : `unavailable (${AVAILABILITY_LABELS[availability]})`;
  }
  if (unit === '%' || unit === 'percent') return `${Number(value.toFixed(1))}%`;
  if (unit === 'seconds' || unit === 's') return `${Number(value.toFixed(1))}s`;
  const formatted = formatCompact(value);
  return unit ? `${formatted} ${unit}` : formatted;
}

export type AvailabilitySummary = {
  observed: number;
  gaps: number;
  byState: Partial<Record<ObservationAvailability, number>>;
};

export function summariseAvailability(series: ChartSeries[]): AvailabilitySummary {
  const byState: Partial<Record<ObservationAvailability, number>> = {};
  let observed = 0;
  let gaps = 0;
  for (const line of series) {
    for (const point of line.points) {
      byState[point.availability] = (byState[point.availability] ?? 0) + 1;
      if (isObserved(point)) observed += 1;
      else gaps += 1;
    }
  }
  return { observed, gaps, byState };
}

/**
 * Why a chart has nothing to draw, or `null` when it has at least one observation.
 *
 * This is what stops an all-unavailable metric from being rendered as a flat zero line: the widget
 * shows this sentence instead of a chart.
 */
export function chartEmptyReason(series: ChartSeries[]): string | null {
  const points = series.flatMap((line) => line.points);
  if (points.length === 0) return 'No data collected for this range.';
  if (points.some(isObserved)) return null;
  const states = [...new Set(points.map((point) => point.availability))];
  if (states.length === 1) {
    return `${AVAILABILITY_LABELS[states[0]]} — there is no observation to plot.`;
  }
  return 'No observations are available for this selection.';
}

/** A screen-reader description of the chart and what is missing from it. */
export function describeChart(title: string, series: ChartSeries[]): string {
  const parts = series.map((line) => {
    const values = observedValues(line.points);
    const missing = line.points.length - values.length;
    if (!values.length) {
      return `${line.label}: no observations (${chartEmptyReason([line]) ?? 'unavailable'})`;
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const unit = line.unit ? ` ${line.unit}` : '';
    const gap = missing ? `, ${missing} unavailable` : '';
    return `${line.label}: ${values.length} observation(s)${gap}, range ${formatCompact(min)} to ${formatCompact(max)}${unit}`;
  });
  return `${title}. ${parts.join('. ')}.`;
}

/** ISO date label for a time axis. Deterministic (UTC) so tests and screenshots match. */
export function formatTimeLabel(x: number | string, bucket: 'hour' | 'day' | 'week' = 'day'): string {
  if (typeof x !== 'number') return String(x);
  const date = new Date(x);
  const iso = date.toISOString();
  if (bucket === 'hour') return `${iso.slice(0, 10)} ${iso.slice(11, 13)}:00`;
  return iso.slice(0, 10);
}

/** A row as the P8.17 API returns it (series point, growth point or snapshot). */
export type ApiChartRow = { at: string | number; value: number | null; availability: string };

/**
 * Map API rows to chart points.
 *
 * `value` is passed through untouched — a null stays null, and the availability string decides
 * whether the point may be drawn. This is the only place a page turns API rows into chart data, so
 * the "never zero" rule cannot be bypassed by a page.
 */
export function pointsFromRows(rows: ApiChartRow[]): ChartPoint[] {
  return rows.map((row) => ({
    x: typeof row.at === 'number' ? row.at : Date.parse(row.at),
    y: row.value,
    availability: toAvailability(row.availability),
  }));
}

export function seriesFromRows(
  key: string,
  label: string,
  unit: string | undefined,
  rows: ApiChartRow[],
): ChartSeries {
  return { key, label, unit, points: pointsFromRows(rows) };
}
