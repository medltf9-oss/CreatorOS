/**
 * Dev-only visual harness for the chart primitives (P8.18).
 *
 * Every number here is a **synthetic fixture**, clearly labelled, used only to render the components
 * for screenshots and manual review. It is a separate Vite entry (`charts-preview.html`) and is not
 * imported by the application shell, so no fixture can reach the product.
 *
 * `?shot=<key>` renders a single scenario (used by scripts/charts-screenshot.cjs); with no query the
 * page shows every scenario for manual inspection.
 */
import { type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { MetricWidget } from './components/charts';
import { type ChartPoint, type ChartSeries, observedCount, summariseAvailability } from './components/chartModel';
import './styles.css';

const DAY = 24 * 60 * 60 * 1000;
const START = Date.UTC(2026, 10, 10); // 2026-11-10

function observed(index: number, value: number): ChartPoint {
  return { x: START + index * DAY, y: value, availability: 'observed' };
}

function gap(index: number, availability: ChartPoint['availability']): ChartPoint {
  return { x: START + index * DAY, y: null, availability };
}

// 1. A normally populated time series.
const populated: ChartSeries[] = [
  {
    key: 'reach',
    label: 'Estimated reach',
    unit: 'people',
    points: [820, 910, 1180, 1320, 1275, 1600, 1720, 1680, 1950, 2140, 2080, 2360, 2510, 2700].map((v, i) =>
      observed(i, v),
    ),
  },
];

// 2. Missing observations: a real gap in the line, never a zero, and never bridged.
const withGaps: ChartSeries[] = [
  {
    key: 'watch_time',
    label: 'Watch time',
    unit: 'seconds',
    points: [
      observed(0, 41.2),
      observed(1, 44.8),
      gap(2, 'not_collected'),
      gap(3, 'not_collected'),
      observed(4, 52.1),
      observed(5, 55.6),
      gap(6, 'not_authorized'),
      observed(7, 49.3),
      observed(8, 47.7),
    ],
  },
];

// 3. A metric the provider does not expose at all.
const unavailable: ChartSeries[] = [
  {
    key: 'tt_retention',
    label: 'Retention curve',
    unit: 'percent',
    points: [gap(0, 'not_supported'), gap(1, 'not_supported'), gap(2, 'not_supported')],
  },
];

// 4. A genuinely observed zero.
const zeroes: ChartSeries[] = [
  { key: 'saves', label: 'Saves', unit: 'count', points: [0, 0, 0, 0, 0, 0, 0].map((v, i) => observed(i, v)) },
];

// 5. Two metric series on one axis.
const multi: ChartSeries[] = [
  {
    key: 'views',
    label: 'Views',
    unit: 'count',
    points: [1200, 1450, 1310, 1720, 1980, 1875, 2210, 2540, 2390, 2680].map((v, i) => observed(i, v)),
  },
  {
    key: 'likes',
    label: 'Likes',
    unit: 'count',
    points: [95, 120, 108, 140, 162, 151, 188, 210, 197, 226].map((v, i) => observed(i, v)),
  },
];

type Scenario = { id: string; title: string; subtitle: string; node: ReactNode };

const scenarios: Record<string, Scenario> = {
  populated: {
    id: 'populated',
    title: '1. Populated time series',
    subtitle: `Synthetic fixture — ${observedCount(populated[0].points)} observed days, 0 gaps.`,
    node: <MetricWidget title="Estimated reach" subtitle="Daily, last 14 days" series={populated} bucket="day" />,
  },
  gaps: {
    id: 'gaps',
    title: '2. Missing observations become gaps',
    subtitle: `Synthetic fixture — ${summariseAvailability(withGaps).gaps} unavailable days become holes, never zeros.`,
    node: (
      <MetricWidget
        title="Watch time"
        subtitle="Days 3-4 not collected, day 7 not authorized"
        series={withGaps}
        bucket="day"
        emptyHint="Unavailable observations are gaps in the line, never zero."
      />
    ),
  },
  unavailable: {
    id: 'unavailable',
    title: '3. Entirely unavailable metric',
    subtitle: 'Synthetic fixture — state not_supported, so no chart is drawn at all.',
    node: (
      <MetricWidget
        title="Retention curve"
        subtitle="TikTok does not expose this through any approved API"
        series={unavailable}
        bucket="day"
        emptyHint="Connect an account that exposes this metric, or read the availability note."
      />
    ),
  },
  zero: {
    id: 'zero',
    title: '4. Observed zero (a real flat line at zero)',
    subtitle: 'Synthetic fixture — seven days with zero saves, every one observed.',
    node: <MetricWidget title="Saves" subtitle="Seven days with zero saves, all observed" series={zeroes} bucket="day" />,
  },
  series: {
    id: 'series',
    title: '5. Multiple metric series',
    subtitle: 'Synthetic fixture — two series share one axis and one legend.',
    node: <MetricWidget title="Views and likes" subtitle="Two series on one axis" series={multi} bucket="day" />,
  },
};

function Section({ scenario }: { scenario: Scenario }) {
  return (
    <section id={`shot-${scenario.id}`} className="space-y-2">
      <div>
        <h2 className="text-sm font-semibold text-slate-200">{scenario.title}</h2>
        <p className="text-xs text-slate-500">{scenario.subtitle}</p>
      </div>
      {scenario.node}
    </section>
  );
}

function App() {
  const shot = new URLSearchParams(window.location.search).get('shot');
  const chosen = shot && scenarios[shot] ? [scenarios[shot]] : Object.values(scenarios);
  return (
    <main className="mx-auto max-w-3xl space-y-8 p-6">
      <header className="space-y-1">
        <h1 className="text-base font-semibold text-slate-100">Chart primitives — visual review</h1>
        <p className="text-xs text-slate-400">
          CreatorOS P8.18 harness. Every value below is a synthetic, labelled fixture: nothing is read from the backend
          and nothing here ships in the application bundle.
        </p>
      </header>
      {chosen.map((scenario) => (
        <Section key={scenario.id} scenario={scenario} />
      ))}
    </main>
  );
}

createRoot(document.getElementById('charts-preview-root')!).render(<App />);
