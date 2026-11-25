export function Card({
  title,
  subtitle,
  actions,
  children,
  className = '',
}: {
  title: string;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-xl border border-slate-800 bg-slate-900 p-5 shadow-sm ${className}`}>
      <header className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-[14px] font-semibold tracking-[-0.01em] text-slate-100">{title}</h2>
          {subtitle ? <p className="mt-1 text-[12.5px] leading-relaxed text-slate-500">{subtitle}</p> : null}
        </div>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function Row({
  label,
  value,
  tone = 'normal',
  mono = true,
}: {
  label: string;
  value: React.ReactNode;
  tone?: 'normal' | 'good' | 'bad' | 'warn';
  mono?: boolean;
}) {
  const color =
    tone === 'good'
      ? 'text-emerald-400'
      : tone === 'bad'
        ? 'text-rose-400'
        : tone === 'warn'
          ? 'text-amber-400'
          : 'text-slate-200';
  return (
    <div className="flex items-baseline justify-between gap-6 border-b border-slate-800/70 py-2 last:border-0">
      <span className="text-sm text-slate-400">{label}</span>
      <span className={`text-right text-sm ${mono ? 'font-mono' : ''} ${color}`}>{value}</span>
    </div>
  );
}

const badgeTones: Record<string, string> = {
  good: 'bg-emerald-500/10 text-emerald-400 ring-emerald-500/20',
  bad: 'bg-rose-500/10 text-rose-400 ring-rose-500/20',
  warn: 'bg-amber-500/10 text-amber-400 ring-amber-500/20',
  info: 'bg-sky-500/10 text-sky-400 ring-sky-500/20',
  muted: 'bg-slate-700/30 text-slate-400 ring-slate-700/40',
};

export function Badge({
  children,
  tone = 'muted',
  title,
}: {
  children: React.ReactNode;
  tone?: keyof typeof badgeTones;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-md px-2 py-0.5 font-mono text-[11px] ring-1 ${badgeTones[tone]}`}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = 'default',
  disabled = false,
  busy = false,
  title,
  type = 'button',
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: 'default' | 'primary' | 'danger' | 'ghost';
  disabled?: boolean;
  busy?: boolean;
  title?: string;
  type?: 'button' | 'submit';
}) {
  const styles: Record<string, string> = {
    default: 'bg-slate-800 text-slate-100 hover:bg-slate-700 border-slate-700',
    primary: 'bg-sky-600 text-white hover:bg-sky-500 border-sky-500',
    danger: 'bg-rose-600 text-white hover:bg-rose-500 border-rose-500',
    ghost: 'bg-transparent text-slate-300 hover:bg-slate-800 border-transparent',
  };
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled || busy}
      className={`inline-flex h-8 items-center gap-2 rounded-[7px] border px-3 text-[13px] font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${styles[variant]}`}
    >
      {busy ? <span className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" /> : null}
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-slate-400">{label}</span>
      <div className="mt-1">{children}</div>
      {hint && !error ? <span className="mt-1 block text-xs text-slate-500">{hint}</span> : null}
      {error ? <span className="mt-1 block text-xs text-rose-400">{error}</span> : null}
    </label>
  );
}

export function Progress({ percent, tone = 'info' }: { percent: number; tone?: 'info' | 'good' | 'bad' | 'warn' }) {
  const colors = { info: 'bg-sky-500', good: 'bg-emerald-500', bad: 'bg-rose-500', warn: 'bg-amber-500' };
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
      <div className={`h-full ${colors[tone]} transition-all`} style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
    </div>
  );
}

export function ErrorNote({ error, onRetry }: { error: string | null; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="flex items-start justify-between gap-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
      <span className="font-mono text-xs leading-relaxed">{error}</span>
      {onRetry ? (
        <button onClick={onRetry} className="shrink-0 rounded border border-rose-400/40 px-2 py-0.5 text-xs hover:bg-rose-500/20">
          retry
        </button>
      ) : null}
    </div>
  );
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-6 text-sm text-slate-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-500 border-t-transparent" />
      {label}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-700/70 px-4 py-8 text-center">
      <p className="text-sm text-slate-300">{title}</p>
      {hint ? <p className="mt-1 text-xs text-slate-500">{hint}</p> : null}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="rounded-lg border border-dashed border-slate-700/70 px-3 py-6 text-center text-sm text-slate-500">{children}</p>;
}

export function Stat({ label, value, unit, tone }: { label: string; value: React.ReactNode; unit?: string; tone?: 'good' | 'warn' | 'bad' }) {
  const color = tone === 'good' ? 'text-emerald-400' : tone === 'warn' ? 'text-amber-400' : tone === 'bad' ? 'text-rose-400' : 'text-slate-100';
  return (
    <div className="rounded-[9px] border border-slate-800 bg-slate-950/35 px-3.5 py-3">
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className={`mt-1 font-display text-[22px] font-semibold leading-none tracking-tight [font-variant-numeric:tabular-nums] ${color}`}>
        {value}
        {unit ? <span className="ml-1 text-xs text-slate-500">{unit}</span> : null}
      </div>
    </div>
  );
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

export function formatAge(iso: string | null): string {
  if (!iso) return '-';
  const then = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`).getTime();
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function formatWhen(iso: string | null, zone?: string): string {
  if (!iso) return '-';
  const date = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`);
  if (Number.isNaN(date.getTime())) return iso;
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: zone || undefined,
    }).format(date);
  } catch {
    return date.toLocaleString();
  }
}
