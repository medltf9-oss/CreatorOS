/**
 * Reusable chart primitives for the Analytics Center (P8.18).
 *
 * Rendering only. All the rules that decide whether a value may be drawn live in `chartModel.ts`,
 * which is unit-tested without a DOM. These components:
 *
 * * draw one polyline per *segment* of observed points, so a gap is a real hole in the line;
 * * never render a bar or a point for a non-observed state — the datum gets a gap marker instead;
 * * show an explicit labelled state (not an empty chart that looks like zero) when a metric has no
 *   observations at all;
 * * expose an SVG title/description and keyboard-reachable data points.
 *
 * No charting dependency is used: plain React + SVG, styled with the existing Tailwind tokens plus
 * a few `--chart-*` variables defined in `styles.css`.
 */
import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from 'react';
import {
  type Axis,
  type ChartPoint,
  type ChartSeries,
  type ObservationAvailability,
  AVAILABILITY_LABELS,
  bucketStepMs,
  chartEmptyReason,
  computeAxis,
  describeChart,
  formatCompact,
  formatMetricValue,
  formatTimeLabel,
  isObserved,
  observedValues,
  selectTickIndices,
  segmentObserved,
  summariseAvailability,
} from './chartModel';

export type { ChartPoint, ChartSeries } from './chartModel';

const LINE_COLORS = ['var(--chart-line-1)', 'var(--chart-line-2)', 'var(--chart-line-3)', 'var(--chart-line-4)'];
const PAD = { left: 58, right: 18, top: 16, bottom: 30 };

type Bucket = 'hour' | 'day' | 'week';

function colorFor(series: ChartSeries, index: number): string {
  return series.color ?? LINE_COLORS[index % LINE_COLORS.length];
}

/** Measure the container so the SVG renders 1:1 and text stays crisp at any window width. */
function useMeasuredWidth(fallback = 720) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const update = () => setWidth(Math.max(260, Math.round(node.clientWidth || fallback)));
    update();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    observer?.observe(node);
    window.addEventListener('resize', update);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', update);
    };
  }, [fallback]);
  return { ref, width };
}

function xDomain(series: ChartSeries[]): Array<number | string> {
  const seen = new Set<string>();
  const values: Array<number | string> = [];
  for (const line of series) {
    for (const point of line.points) {
      const key = typeof point.x === 'number' ? `n:${point.x}` : `s:${point.x}`;
      if (!seen.has(key)) {
        seen.add(key);
        values.push(point.x);
      }
    }
  }
  const allNumeric = values.every((value) => typeof value === 'number');
  values.sort((a, b) => (allNumeric ? (a as number) - (b as number) : String(a).localeCompare(String(b))));
  return values;
}

/** Position on [0,1]: proportional to time for numeric axes, by order for categorical ones. */
function xRatio(x: number | string, domain: Array<number | string>): number {
  if (domain.length <= 1) return 0;
  if (typeof x === 'number' && typeof domain[0] === 'number') {
    const min = domain[0] as number;
    const max = domain[domain.length - 1] as number;
    return max === min ? 0 : (x - min) / (max - min);
  }
  const index = domain.indexOf(x);
  return index < 0 ? 0 : index / (domain.length - 1);
}

type TooltipState = { label: string; text: string; x: number; y: number } | null;

function Tooltip({ state }: { state: TooltipState }) {
  if (!state) return null;
  return (
    <div role="status" aria-live="polite" className="chart-tooltip" style={{ left: state.x, top: state.y }}>
      <div className="chart-tooltip__label">{state.label}</div>
      <div className="chart-tooltip__value">{state.text}</div>
    </div>
  );
}

export function ChartLegend({ series, bucket }: { series: ChartSeries[]; bucket?: Bucket }) {
  const summary = summariseAvailability(series);
  const gapStates = Object.keys(summary.byState).filter((state) => state !== 'observed') as ObservationAvailability[];
  return (
    <div className="chart-legend">
      {series.map((line, index) => (
        <span key={line.key} className="chart-legend__item">
          <span className="chart-legend__swatch" style={{ background: colorFor(line, index) }} aria-hidden="true" />
          {line.label}
          {line.unit ? <span className="chart-legend__unit">{line.unit}</span> : null}
        </span>
      ))}
      {gapStates.length > 0 ? (
        <span className="chart-legend__item" title="Unavailable observations are shown as gaps, never as zero">
          <span className="chart-legend__swatch chart-legend__swatch--gap" aria-hidden="true" />
          {summary.gaps} gap{summary.gaps === 1 ? '' : 's'}
          {bucket ? ` (no observation for the ${bucket})` : ''}
        </span>
      ) : null}
    </div>
  );
}

/** The honest "nothing to plot" state. Never draws a chart that could be mistaken for zero. */
export function ChartEmptyState({ reason, hint, height = 200 }: { reason: string; hint?: string; height?: number }) {
  return (
    <div className="chart-empty" style={{ minHeight: height }}>
      <p className="chart-empty__title">No data</p>
      <p className="chart-empty__reason">{reason}</p>
      {hint ? <p className="chart-empty__hint">{hint}</p> : null}
    </div>
  );
}

function Grid({
  width,
  height,
  axis,
  domain,
  bucket,
  unit,
}: {
  width: number;
  height: number;
  axis: Axis;
  domain: Array<number | string>;
  bucket: Bucket;
  unit?: string;
}) {
  const plotHeight = height - PAD.top - PAD.bottom;
  const plotWidth = width - PAD.left - PAD.right;
  const xTicks = selectTickIndices(domain.length, Math.max(2, Math.floor(plotWidth / 120)));
  const yFor = (value: number) => PAD.top + (1 - (value - axis.min) / (axis.max - axis.min || 1)) * plotHeight;
  return (
    <g aria-hidden="true">
      {axis.ticks.map((tick) => {
        const y = yFor(tick);
        return (
          <g key={`y-${tick}`}>
            <line x1={PAD.left} x2={PAD.left + plotWidth} y1={y} y2={y} className="chart-grid" />
            <text x={PAD.left - 8} y={y + 3} textAnchor="end" className="chart-axis-label">
              {formatCompact(tick)}
            </text>
          </g>
        );
      })}
      {unit ? (
        // Top-right of the plot: the topmost y tick already owns the top-left gutter, and the two
        // used to collide there. The label stays inside the padding so it can never be clipped.
        <text x={PAD.left + plotWidth} y={PAD.top - 4} textAnchor="end" className="chart-unit-label">
          {unit}
        </text>
      ) : null}
      {xTicks.map((index) => {
        const last = index === xTicks[xTicks.length - 1];
        return (
          <text
            key={`x-${index}`}
            x={PAD.left + xRatio(domain[index], domain) * plotWidth}
            y={height - 10}
            textAnchor={index === 0 ? 'start' : last ? 'end' : 'middle'}
            className="chart-axis-label"
          >
            {formatTimeLabel(domain[index], bucket)}
          </text>
        );
      })}
    </g>
  );
}

export function LineChart({
  series,
  height = 220,
  bucket = 'day',
  title = 'Chart',
}: {
  series: ChartSeries[];
  height?: number;
  bucket?: Bucket;
  title?: string;
}) {
  const { ref, width } = useMeasuredWidth();
  const [tooltip, setTooltip] = useState<TooltipState>(null);
  const id = useId();
  const domain = useMemo(() => xDomain(series), [series]);
  const axis = useMemo(() => computeAxis(series.flatMap((line) => observedValues(line.points))), [series]);
  const plotWidth = width - PAD.left - PAD.right;
  const plotHeight = height - PAD.top - PAD.bottom;
  const threshold = bucketStepMs(bucket) * 1.5;
  const description = describeChart(title, series);

  const yFor = (value: number) => PAD.top + (1 - (value - axis.min) / (axis.max - axis.min || 1)) * plotHeight;
  const xFor = (x: number | string) => PAD.left + xRatio(x, domain) * plotWidth;
  const showGap = (label: string, point: ChartPoint) => {
    setTooltip({
      label: `${label} · ${formatTimeLabel(point.x, bucket)}`,
      text: AVAILABILITY_LABELS[point.availability],
      x: xFor(point.x),
      y: PAD.top,
    });
  };

  return (
    <div ref={ref} className="chart-root">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${id}-title ${id}-desc`}>
        <title id={`${id}-title`}>{title}</title>
        <desc id={`${id}-desc`}>{description}</desc>
        <Grid width={width} height={height} axis={axis} domain={domain} bucket={bucket} unit={series[0]?.unit} />
        {series.map((line, index) => {
          const color = colorFor(line, index);
          return (
            <g key={line.key}>
              {segmentObserved(line.points, { gapThresholdMs: threshold }).map((segment, segmentIndex) => (
                <polyline
                  key={`${line.key}-seg-${segmentIndex}`}
                  className="chart-line"
                  style={{ stroke: color }}
                  points={segment.map((point) => `${xFor(point.x)},${yFor(point.y as number)}`).join(' ')}
                />
              ))}
              {line.points.map((point) => {
                if (!isObserved(point)) {
                  const label = `${line.label}, ${formatTimeLabel(point.x, bucket)}: ${AVAILABILITY_LABELS[point.availability]}`;
                  return (
                    <line
                      key={`${line.key}-gap-${String(point.x)}`}
                      x1={xFor(point.x)}
                      x2={xFor(point.x)}
                      y1={PAD.top + plotHeight - 8}
                      y2={PAD.top + plotHeight}
                      className="chart-gap-marker"
                      tabIndex={0}
                      role="img"
                      aria-label={label}
                      onMouseEnter={() => showGap(line.label, point)}
                      onFocus={() => showGap(line.label, point)}
                      onMouseLeave={() => setTooltip(null)}
                      onBlur={() => setTooltip(null)}
                    />
                  );
                }
                const y = yFor(point.y as number);
                const label = `${line.label}, ${formatTimeLabel(point.x, bucket)}, ${formatMetricValue(point.y, line.unit)}`;
                return (
                  <circle
                    key={`${line.key}-pt-${String(point.x)}`}
                    className="chart-point"
                    style={{ stroke: color }}
                    cx={xFor(point.x)}
                    cy={y}
                    r={3}
                    tabIndex={0}
                    role="img"
                    aria-label={label}
                    onMouseEnter={() =>
                      setTooltip({
                        label: `${line.label} · ${formatTimeLabel(point.x, bucket)}`,
                        text: formatMetricValue(point.y, line.unit),
                        x: xFor(point.x),
                        y,
                      })
                    }
                    onFocus={() =>
                      setTooltip({
                        label: `${line.label} · ${formatTimeLabel(point.x, bucket)}`,
                        text: formatMetricValue(point.y, line.unit),
                        x: xFor(point.x),
                        y,
                      })
                    }
                    onMouseLeave={() => setTooltip(null)}
                    onBlur={() => setTooltip(null)}
                  />
                );
              })}
            </g>
          );
        })}
      </svg>
      <Tooltip state={tooltip} />
    </div>
  );
}

export function BarChart({
  series,
  height = 220,
  bucket = 'day',
  title = 'Chart',
}: {
  series: ChartSeries[];
  height?: number;
  bucket?: Bucket;
  title?: string;
}) {
  const { ref, width } = useMeasuredWidth();
  const [tooltip, setTooltip] = useState<TooltipState>(null);
  const id = useId();
  const domain = useMemo(() => xDomain(series), [series]);
  const axis = useMemo(() => computeAxis(series.flatMap((line) => observedValues(line.points))), [series]);
  const plotWidth = width - PAD.left - PAD.right;
  const plotHeight = height - PAD.top - PAD.bottom;
  const description = describeChart(title, series);
  const slot = domain.length > 0 ? plotWidth / domain.length : plotWidth;
  const groupWidth = Math.max(2, Math.min(slot * 0.7, 26));
  const barWidth = groupWidth / Math.max(1, series.length);
  const yFor = (value: number) => PAD.top + (1 - (value - axis.min) / (axis.max - axis.min || 1)) * plotHeight;
  const baseline = yFor(Math.max(axis.min, 0));

  return (
    <div ref={ref} className="chart-root">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${id}-title ${id}-desc`}>
        <title id={`${id}-title`}>{title}</title>
        <desc id={`${id}-desc`}>{description}</desc>
        <Grid width={width} height={height} axis={axis} domain={domain} bucket={bucket} unit={series[0]?.unit} />
        {series.map((line, seriesIndex) => (
          <g key={line.key}>
            {line.points.map((point) => {
              const slotIndex = Math.max(0, domain.indexOf(point.x));
              const x = PAD.left + slotIndex * slot + (slot - groupWidth) / 2 + seriesIndex * barWidth;
              if (!isObserved(point)) {
                const text = `${AVAILABILITY_LABELS[point.availability]}`;
                const label = `${line.label}, ${formatTimeLabel(point.x, bucket)}: ${text}`;
                return (
                  <rect
                    key={`${line.key}-gap-${String(point.x)}`}
                    x={x}
                    y={baseline - 6}
                    width={Math.max(2, barWidth - 1)}
                    height={6}
                    className="chart-gap-marker chart-gap-marker--bar"
                    tabIndex={0}
                    role="img"
                    aria-label={label}
                    onMouseEnter={() => setTooltip({ label: `${line.label} · ${formatTimeLabel(point.x, bucket)}`, text, x, y: baseline - 12 })}
                    onFocus={() => setTooltip({ label: `${line.label} · ${formatTimeLabel(point.x, bucket)}`, text, x, y: baseline - 12 })}
                    onMouseLeave={() => setTooltip(null)}
                    onBlur={() => setTooltip(null)}
                  />
                );
              }
              const value = point.y as number;
              const y = yFor(value);
              const top = Math.min(y, baseline);
              const barHeight = Math.max(1, Math.abs(baseline - y));
              return (
                <rect
                  key={`${line.key}-bar-${String(point.x)}`}
                  className="chart-bar"
                  style={{ fill: colorFor(line, seriesIndex) }}
                  x={x}
                  y={top}
                  width={Math.max(2, barWidth - 1)}
                  height={barHeight}
                  tabIndex={0}
                  role="img"
                  aria-label={`${line.label}, ${formatTimeLabel(point.x, bucket)}, ${formatMetricValue(value, line.unit)}`}
                  onMouseEnter={() => setTooltip({ label: `${line.label} · ${formatTimeLabel(point.x, bucket)}`, text: formatMetricValue(value, line.unit), x, y: top })}
                  onFocus={() => setTooltip({ label: `${line.label} · ${formatTimeLabel(point.x, bucket)}`, text: formatMetricValue(value, line.unit), x, y: top })}
                  onMouseLeave={() => setTooltip(null)}
                  onBlur={() => setTooltip(null)}
                />
              );
            })}
          </g>
        ))}
      </svg>
      <Tooltip state={tooltip} />
    </div>
  );
}

/**
 * The widget the Analytics pages will use: it decides between a real chart and an explicit
 * "no data" state, and it always shows the legend and the gap count.
 */
export function MetricWidget({
  title,
  subtitle,
  series,
  kind = 'line',
  bucket = 'day',
  height = 220,
  emptyHint,
  actions,
}: {
  title: string;
  subtitle?: string;
  series: ChartSeries[];
  kind?: 'line' | 'bar';
  bucket?: Bucket;
  height?: number;
  emptyHint?: string;
  actions?: ReactNode;
}) {
  const reason = chartEmptyReason(series);
  return (
    <section className="chart-card">
      <header className="chart-card__head">
        <div>
          <h3 className="chart-card__title">{title}</h3>
          {subtitle ? <p className="chart-card__subtitle">{subtitle}</p> : null}
        </div>
        {actions}
      </header>
      {reason ? (
        <ChartEmptyState reason={reason} hint={emptyHint} height={height} />
      ) : (
        <>
          {kind === 'bar' ? (
            <BarChart series={series} height={height} bucket={bucket} title={title} />
          ) : (
            <LineChart series={series} height={height} bucket={bucket} title={title} />
          )}
          <ChartLegend series={series} bucket={bucket} />
        </>
      )}
    </section>
  );
}
