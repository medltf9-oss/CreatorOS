/**
 * Shared UI for the Analytics Center pages (P8.19).
 *
 * Data-fetching lives here (`useAsyncData` cancels the previous request when its inputs change, so a
 * slow response for an old filter can never overwrite a newer one). Everything else is presentation
 * scaffolding built from the existing design system.
 */
import { type ReactNode, useCallback, useEffect, useState } from 'react';
import { AVAILABILITY_LABELS, type ObservationAvailability } from '../../components/chartModel';
import { Badge, ErrorNote, Spinner } from '../../components/ui';

export const PLATFORM_LABELS: Record<string, string> = {
  facebook: 'Facebook',
  instagram: 'Instagram',
  tiktok: 'TikTok',
};

export function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform;
}

export function formatRangeLabel(range: RangeValue): string {
  return `${range.start.slice(0, 10)} → ${range.end.slice(0, 10)}`;
}

export type RangePreset = '7d' | '28d' | '90d' | 'custom';
export type RangeValue = { preset: RangePreset; start: string; end: string };

const PRESET_DAYS: Record<Exclude<RangePreset, 'custom'>, number> = { '7d': 7, '28d': 28, '90d': 90 };

function isoDay(moment: Date): string {
  return moment.toISOString().slice(0, 10);
}

export function defaultRange(preset: RangePreset = '28d'): RangeValue {
  const end = new Date();
  const start = new Date(end.getTime());
  if (preset !== 'custom') start.setUTCDate(start.getUTCDate() - PRESET_DAYS[preset]);
  return { preset, start: `${isoDay(start)}T00:00:00Z`, end: `${isoDay(end)}T23:59:59Z` };
}

export function rangeFromPreset(preset: RangePreset): RangeValue {
  return defaultRange(preset);
}

export function DateRangePicker({ value, onChange }: { value: RangeValue; onChange: (next: RangeValue) => void }) {
  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="text-xs text-slate-400">
        <span className="block uppercase tracking-wide text-[10px] text-slate-500">Range</span>
        <select
          className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
          value={value.preset}
          onChange={(event) => {
            const preset = event.target.value as RangePreset;
            onChange(preset === 'custom' ? { ...value, preset } : rangeFromPreset(preset));
          }}
        >
          <option value="7d">Last 7 days</option>
          <option value="28d">Last 28 days</option>
          <option value="90d">Last 90 days</option>
          <option value="custom">Custom</option>
        </select>
      </label>
      {value.preset === 'custom' ? (
        <>
          <label className="text-xs text-slate-400">
            <span className="block uppercase tracking-wide text-[10px] text-slate-500">From</span>
            <input
              type="date"
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={value.start.slice(0, 10)}
              onChange={(event) => onChange({ ...value, start: `${event.target.value}T00:00:00Z` })}
            />
          </label>
          <label className="text-xs text-slate-400">
            <span className="block uppercase tracking-wide text-[10px] text-slate-500">To</span>
            <input
              type="date"
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={value.end.slice(0, 10)}
              onChange={(event) => onChange({ ...value, end: `${event.target.value}T23:59:59Z` })}
            />
          </label>
        </>
      ) : null}
    </div>
  );
}

export function PageHeader({ subtitle, actions }: { title: string; subtitle: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p className="max-w-[78ch] text-[13px] leading-relaxed text-slate-400">{subtitle}</p>
      </div>
      {actions ? <div className="flex flex-wrap items-end gap-2">{actions}</div> : null}
    </div>
  );
}

export function StaleBadge({ stale, ageHours }: { stale: boolean; ageHours: number | null }) {
  const label = ageHours === null ? 'never collected' : `${ageHours}h old`;
  return (
    <Badge tone={stale ? 'warn' : 'good'} title={stale ? 'This data is older than the scheduled interval' : 'Recently collected'}>
      {stale ? 'stale' : 'fresh'} · {label}
    </Badge>
  );
}

export function AvailabilityChip({ availability, reason }: { availability: ObservationAvailability; reason?: string | null }) {
  const tone =
    availability === 'observed'
      ? 'good'
      : availability === 'not_authorized' || availability === 'missing' || availability === 'temporarily_unavailable'
        ? 'warn'
        : availability === 'expired'
          ? 'bad'
          : 'muted';
  return (
    <Badge tone={tone} title={reason ?? undefined}>
      {AVAILABILITY_LABELS[availability]}
    </Badge>
  );
}

export type AsyncState<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
  fetchedAt: string | null;
  reload: () => void;
};

/**
 * Run an async loader and cancel it when the inputs change. The previous response is dropped, so a
 * rapid filter change cannot render stale data.
 */
export function useAsyncData<T>(loader: (signal: AbortSignal) => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchedAt, setFetchedAt] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loader(controller.signal).then(
      (result) => {
        if (controller.signal.aborted) return;
        setData(result);
        setFetchedAt(new Date().toISOString());
        setLoading(false);
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setData(null);
        setLoading(false);
      },
    );
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, loading, error, fetchedAt, reload };
}

export function LoadingBlock({ label = 'Loading analytics...' }: { label?: string }) {
  return <Spinner label={label} />;
}

export function ErrorBlock({ error, onRetry }: { error: string | null; onRetry?: () => void }) {
  return <ErrorNote error={error} onRetry={onRetry} />;
}

export function MetricStatePanel({
  title,
  rows,
}: {
  title: string;
  rows: { label: string; value: ReactNode; tone?: 'normal' | 'good' | 'bad' | 'warn' }[];
}) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</p>
      <div className="space-y-1">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-4 text-sm">
            <span className="min-w-0 truncate text-slate-400" title={row.label}>
              {row.label}
            </span>
            <span
              className={`shrink-0 text-right font-mono text-xs ${
                row.tone === 'good'
                  ? 'text-emerald-400'
                  : row.tone === 'bad'
                    ? 'text-rose-400'
                    : row.tone === 'warn'
                      ? 'text-amber-400'
                      : 'text-slate-200'
              }`}
            >
              {row.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
