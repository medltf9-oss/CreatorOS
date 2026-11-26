import { useCallback, useEffect, useState } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  ArrowRight, BarChart3, CheckCircle2, LibraryBig, PenLine, Send,
  ShieldCheck, Sparkles, TriangleAlert,
} from 'lucide-react';
import type { AiHealth, CreatorApi, ExecutionRow, GovernorSnapshot } from '../api';
import { ApiRequestError } from '../api';
import type { LibraryApi, LibraryStats } from '../apiLibrary';
import type { Overview as PublishingOverview, PublishingApi } from '../apiPublishing';
import type { Overview as SocialOverview, SocialApi } from '../apiSocial';
import { Badge, Card, Empty, ErrorNote, Progress, Row, formatAge, formatBytes } from '../components/ui';

type Props = {
  api: CreatorApi;
  version: { version: string; api: string; capabilities: Record<string, string> } | null;
  library?: LibraryApi | null;
  publishing?: PublishingApi | null;
  social?: SocialApi | null;
  onNavigate?: (page: string) => void;
};

const pressureTone = { normal: 'good', saturated: 'warn', critical: 'bad' } as const;

function MetricCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = 'mint',
}: {
  label: string;
  value: string | number;
  hint: string;
  icon: LucideIcon;
  tone?: 'mint' | 'lavender' | 'rose';
}) {
  const tones = {
    mint: 'bg-sky-400/10 text-sky-400',
    lavender: 'bg-[var(--app-lavender)]/15 text-[var(--app-lavender)]',
    rose: 'bg-rose-400/10 text-rose-400',
  } as const;
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
      <div className="flex items-start justify-between gap-3">
        <span className="metric__label">{label}</span>
        <span className={`grid h-7 w-7 place-items-center rounded-md ${tones[tone]}`}>
          <Icon size={14} strokeWidth={1.9} aria-hidden="true" />
        </span>
      </div>
      <span className="metric__value">{value}</span>
      <span className="metric__hint">{hint}</span>
    </section>
  );
}

export default function Dashboard({ api, version, library, publishing, social, onNavigate }: Props) {
  const [health, setHealth] = useState<AiHealth | null>(null);
  const [executions, setExecutions] = useState<ExecutionRow[]>([]);
  const [libraryStats, setLibraryStats] = useState<LibraryStats | null>(null);
  const [pubOverview, setPubOverview] = useState<PublishingOverview | null>(null);
  const [socialOverview, setSocialOverview] = useState<SocialOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dataNote, setDataNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setError(null);
    const [healthResult, executionsResult, libraryResult, publishingResult, socialResult] =
      await Promise.allSettled([
        api.health(),
        api.executions(8),
        library ? library.stats() : Promise.resolve(null),
        publishing ? publishing.overview() : Promise.resolve(null),
        social ? social.overview() : Promise.resolve(null),
      ]);

    if (healthResult.status === 'fulfilled') setHealth(healthResult.value);
    else setError(healthResult.reason instanceof ApiRequestError
      ? `${healthResult.reason.code}: ${healthResult.reason.message}`
      : String(healthResult.reason));

    if (executionsResult.status === 'fulfilled') setExecutions(executionsResult.value);
    if (libraryResult.status === 'fulfilled') setLibraryStats(libraryResult.value);
    if (publishingResult.status === 'fulfilled') setPubOverview(publishingResult.value);
    if (socialResult.status === 'fulfilled') setSocialOverview(socialResult.value);

    const failed = [
      libraryResult.status === 'rejected' ? 'library' : null,
      publishingResult.status === 'rejected' ? 'publishing' : null,
      socialResult.status === 'rejected' ? 'social accounts' : null,
    ].filter(Boolean);
    setDataNote(failed.length ? `Could not reach ${failed.join(', ')}. The sections below show only what loaded.` : null);
    setLoading(false);
  }, [api, library, publishing, social]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  const snapshot: GovernorSnapshot | null = health?.resources ?? null;
  const gpu = snapshot?.gpu ?? null;
  const online = health?.ollama.online ?? false;
  const pub = pubOverview?.summary;
  const accounts = socialOverview?.phase7.accounts_connected ?? null;
  const needsAttention = (pub?.needs_attention ?? 0) + (socialOverview?.phase7.accounts_needing_attention ?? 0);
  const delivery = pubOverview?.delivery;

  const pipeline = [
    { label: 'Open work', value: pub?.open, hint: 'drafts and active intents' },
    { label: 'Needs you', value: pub?.needs_attention, hint: 'approvals or attention' },
    { label: 'Scheduled', value: pub?.scheduled_upcoming, hint: 'upcoming destinations' },
    { label: 'Published · 7 days', value: pub?.published_last_7d, hint: 'confirmed deliveries' },
  ];

  return (
    <div className="space-y-5">
      <ErrorNote error={error} onRetry={() => void load()} />
      {dataNote ? (
        <p className="flex items-start gap-2 rounded-lg border border-amber-400/25 bg-amber-400/10 px-3 py-2 text-xs text-amber-300">
          <TriangleAlert size={14} className="mt-px shrink-0" aria-hidden="true" />
          {dataNote}
        </p>
      ) : null}

      {loading && !health ? <Card title="Studio overview">Loading live workspace data…</Card> : null}

      <section className="feature-panel" aria-labelledby="studio-overview-title">
        <div className="feature-panel__grid">
          <div>
            <span className="feature-panel__eyebrow">
              <Sparkles size={13} aria-hidden="true" /> Local-first content studio
            </span>
            <h2 id="studio-overview-title" className="feature-panel__title">
              Everything you make, schedule, and learn from — in one calm workspace.
            </h2>
            <p className="feature-panel__text">
              Import footage, prepare it with local AI, shape each post for its destination, and publish
              when you decide. Your media stays on this machine.
            </p>
            <div className="feature-panel__actions">
              <button
                type="button"
                onClick={() => onNavigate?.('lib-grid')}
                className="inline-flex h-9 items-center gap-2 rounded-[7px] bg-[#15242d] px-3.5 text-[13px] font-medium text-white transition hover:bg-[#243b46]"
              >
                <LibraryBig size={15} aria-hidden="true" /> Open media library <ArrowRight size={14} aria-hidden="true" />
              </button>
              <button
                type="button"
                onClick={() => onNavigate?.('pub-compose')}
                className="inline-flex h-9 items-center gap-2 rounded-[7px] border border-[#cfdad6] bg-white/70 px-3.5 text-[13px] font-medium text-[#15242d] transition hover:bg-white"
              >
                <PenLine size={14} aria-hidden="true" /> Compose a draft
              </button>
            </div>
          </div>
          <div className="workflow" aria-label="The CreatorOS production loop">
            <p className="workflow__title">One production loop</p>
            <div className="workflow__step">
              <span className="workflow__icon"><LibraryBig size={13} aria-hidden="true" /></span>
              <span><span className="workflow__label">Bring in your media</span><span className="workflow__hint">Import, tag, and keep originals safe.</span></span>
            </div>
            <div className="workflow__step">
              <span className="workflow__icon"><Sparkles size={13} aria-hidden="true" /></span>
              <span><span className="workflow__label">Prepare with local AI</span><span className="workflow__hint">Transcode, caption, and draft on-device.</span></span>
            </div>
            <div className="workflow__step">
              <span className="workflow__icon"><PenLine size={13} aria-hidden="true" /></span>
              <span><span className="workflow__label">Shape every destination</span><span className="workflow__hint">Review the variant before it goes out.</span></span>
            </div>
            <div className="workflow__step">
              <span className="workflow__icon"><Send size={13} aria-hidden="true" /></span>
              <span><span className="workflow__label">Publish and learn</span><span className="workflow__hint">Approve, schedule, then read the results.</span></span>
            </div>
          </div>
        </div>
        <div className="feature-panel__meta">
          <span><b>{online ? 'Ready' : 'Offline'}</b> · local AI</span>
          <span><b>{accounts ?? '—'}</b> connected {accounts === 1 ? 'account' : 'accounts'}</span>
          <span><b>{libraryStats ? formatBytes(libraryStats.bytes_total) : '—'}</b> media stored</span>
          <span className="inline-flex items-center gap-1.5">
            <CheckCircle2 size={12} aria-hidden="true" /> {version ? `v${version.version}` : 'version pending'}
          </span>
        </div>
      </section>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Media assets"
          value={libraryStats?.assets_total ?? '—'}
          hint={libraryStats ? `${libraryStats.derivatives_total} derivatives · ${formatBytes(libraryStats.bytes_total)}` : 'library not loaded'}
          icon={LibraryBig}
        />
        <MetricCard
          label="Connected accounts"
          value={accounts ?? '—'}
          hint={socialOverview ? `${socialOverview.phase7.publish_capable_accounts} ready to publish` : 'accounts not loaded'}
          icon={ShieldCheck}
          tone="lavender"
        />
        <MetricCard
          label="Local AI models"
          value={health?.models.installed ?? '—'}
          hint={health ? `${health.models.total_size_gb} GB on disk` : 'AI infrastructure not loaded'}
          icon={Sparkles}
        />
        <MetricCard
          label="Needs attention"
          value={needsAttention}
          hint="approvals, accounts, retries"
          icon={TriangleAlert}
          tone={needsAttention > 0 ? 'rose' : 'mint'}
        />
      </div>

      <div className="dash-grid">
        <div className="dash-stack">
          <Card
            title="Content pipeline"
            subtitle="A live count from publishing and social accounts — no estimate, no sample data."
            actions={
              <button type="button" className="quiet-link text-xs" onClick={() => onNavigate?.('pub-overview')}>
                Open publishing
              </button>
            }
          >
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {pipeline.map((stage) => (
                <div key={stage.label} className="rounded-[9px] border border-slate-800 bg-slate-950/25 px-3.5 py-3">
                  <span className="block text-[11px] text-slate-500">{stage.label}</span>
                  <span className="mt-1 block font-display text-[26px] font-semibold leading-none tracking-tight text-slate-100 [font-variant-numeric:tabular-nums]">
                    {stage.value ?? '—'}
                  </span>
                  <span className="mt-1.5 block text-[11px] text-slate-500">{stage.hint}</span>
                </div>
              ))}
            </div>
            <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 border-t border-slate-800 pt-3 text-xs text-slate-500">
              <span><span className="text-slate-300">{libraryStats?.jobs_active ?? '—'}</span> media jobs active</span>
              <span><span className="text-slate-300">{libraryStats?.jobs_failed ?? '—'}</span> failed</span>
              <span><span className="text-slate-300">{libraryStats?.jobs_completed ?? '—'}</span> completed</span>
            </div>
          </Card>

          <Card
            title="Recent AI work"
            subtitle="Generations are written to the local database before they are reported complete."
            actions={
              <button type="button" className="quiet-link text-xs" onClick={() => onNavigate?.('playground')}>
                Open playground
              </button>
            }
          >
            {executions.length === 0 ? (
              <Empty>No generations yet. Anything you run in the Playground appears here.</Empty>
            ) : (
              <div>
                {executions.slice(0, 5).map((row) => (
                  <div key={row.id} className="activity-row">
                    <span className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-slate-800 text-slate-400">
                      <Sparkles size={13} aria-hidden="true" />
                    </span>
                    <div className="activity-row__main">
                      <div className="activity-row__title">{row.model_tag}</div>
                      <div className="activity-row__meta">
                        {row.profile_name ?? 'default profile'} · {formatAge(row.created_at)} · {row.completion_tokens} tokens
                        {row.tokens_per_sec != null ? ` · ${row.tokens_per_sec} tok/s` : ''}
                      </div>
                    </div>
                    <Badge
                      tone={
                        row.state === 'succeeded'
                          ? 'good'
                          : row.state === 'failed'
                            ? 'bad'
                            : row.state === 'cancelled'
                              ? 'warn'
                              : 'muted'
                      }
                    >
                      {row.state}
                    </Badge>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>

        <div className="dash-stack">
          <Card title="Studio status" subtitle="What is available right now, reported by the desktop backend.">
            <Row
              label="Backend"
              value={health ? 'responding' : 'offline'}
              tone={health ? 'good' : 'bad'}
              mono={false}
            />
            <Row
              label="Local AI"
              value={online ? `Ollama ${health?.ollama.version ?? ''}`.trim() : 'offline'}
              tone={online ? 'good' : 'warn'}
              mono={false}
            />
            <Row
              label="Accounts"
              value={accounts == null ? 'not loaded' : `${accounts} connected`}
              tone={accounts && accounts > 0 ? 'good' : 'warn'}
              mono={false}
            />
            <Row
              label="Media delivery"
              value={delivery?.configured ? (delivery.provider ?? 'configured') : 'local only'}
              tone={delivery?.configured ? 'good' : 'warn'}
              mono={false}
            />
            <Row
              label="Vault"
              value={socialOverview?.vault.available ? 'available' : 'unavailable'}
              tone={socialOverview?.vault.available ? 'good' : 'warn'}
              mono={false}
            />
            <div className="mt-3 flex items-center justify-between gap-3">
              <span className="text-xs text-slate-500">
                {snapshot ? `${snapshot.cpu_percent}% CPU · ${snapshot.ram_available_gb} GB free` : 'resource telemetry unavailable'}
              </span>
              <button type="button" className="quiet-link text-xs" onClick={() => onNavigate?.('ai')}>
                Details
              </button>
            </div>
          </Card>

          <Card
            title="Performance analytics"
            subtitle={accounts && accounts > 0 ? 'Account summaries and content comparisons are ready to explore.' : 'Charts need a connected account with synced analytics.'}
            actions={<Badge tone={accounts && accounts > 0 ? 'good' : 'muted'}>{accounts && accounts > 0 ? 'available' : 'awaiting data'}</Badge>}
          >
            <div className="rounded-[9px] border border-dashed border-slate-700/70 px-4 py-6 text-center">
              <BarChart3 size={20} className="mx-auto text-slate-500" aria-hidden="true" />
              <p className="mt-2 text-sm text-slate-300">
                {accounts && accounts > 0 ? 'Analytics pages are available for your connected accounts.' : 'No performance data to chart yet.'}
              </p>
              <p className="mx-auto mt-1 max-w-[36ch] text-xs leading-relaxed text-slate-500">
                {accounts && accounts > 0
                  ? 'Open the analytics workspace to compare platforms, publications, and audience growth.'
                  : 'Connect a social account and sync its metrics. CreatorOS will not invent numbers before then.'}
              </p>
              <button
                type="button"
                onClick={() => onNavigate?.(accounts && accounts > 0 ? 'an-overview' : 'soc-connect')}
                className="mt-3 inline-flex h-8 items-center gap-1.5 rounded-[7px] border border-slate-700 bg-slate-800 px-3 text-xs font-medium text-slate-200 transition hover:bg-slate-700"
              >
                {accounts && accounts > 0 ? <BarChart3 size={13} aria-hidden="true" /> : <ShieldCheck size={13} aria-hidden="true" />}
                {accounts && accounts > 0 ? 'Open analytics' : 'Connect an account'}
              </button>
            </div>
          </Card>
        </div>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card
          title="Local AI infrastructure"
          subtitle="The model runtime, concurrency, and hardware pressure this app is actually using."
          actions={<Badge tone={online ? 'good' : 'bad'}>{online ? 'online' : 'offline'}</Badge>}
        >
          <div className="grid grid-cols-2 gap-2.5">
            <div className="rounded-[9px] border border-slate-800 px-3 py-2.5">
              <span className="block text-[11px] text-slate-500">Models installed</span>
              <span className="mt-0.5 block font-display text-lg font-semibold text-slate-100">{health?.models.installed ?? '—'}</span>
            </div>
            <div className="rounded-[9px] border border-slate-800 px-3 py-2.5">
              <span className="block text-[11px] text-slate-500">GPU load</span>
              <span className="mt-0.5 block font-display text-lg font-semibold text-slate-100">
                {gpu?.utilization_percent != null ? `${Math.round(gpu.utilization_percent)}%` : 'n/a'}
              </span>
            </div>
            <div className="rounded-[9px] border border-slate-800 px-3 py-2.5">
              <span className="block text-[11px] text-slate-500">Inference slots</span>
              <span className="mt-0.5 block font-display text-lg font-semibold text-slate-100">
                {snapshot ? `${snapshot.active_generations}/${snapshot.max_concurrent}` : '—'}
              </span>
            </div>
            <div className="rounded-[9px] border border-slate-800 px-3 py-2.5">
              <span className="block text-[11px] text-slate-500">Requests this session</span>
              <span className="mt-0.5 block font-display text-lg font-semibold text-slate-100">{health?.gateway.requests ?? '—'}</span>
            </div>
          </div>
          <div className="mt-3">
            <div className="mb-1.5 flex items-center justify-between text-[11px] text-slate-500">
              <span>Memory pressure</span>
              <span>{snapshot?.pressure ?? 'unknown'}</span>
            </div>
            <Progress
              percent={snapshot?.ram_percent ?? 0}
              tone={pressureTone[snapshot?.pressure ?? 'normal'] === 'good' ? 'good' : pressureTone[snapshot?.pressure ?? 'normal'] === 'bad' ? 'bad' : 'warn'}
            />
          </div>
          <button type="button" className="quiet-link mt-3 text-xs" onClick={() => onNavigate?.('ai')}>
            Open AI infrastructure <ArrowRight size={12} className="inline" aria-hidden="true" />
          </button>
        </Card>

        <Card title="Workspace health" subtitle="Storage, vault, and the local service footprint.">
          <Row label="Free disk" value={libraryStats ? `${libraryStats.disk.free_gb} GB of ${libraryStats.disk.total_gb} GB` : 'library not loaded'} mono={false} />
          <Row
            label="Vault backend"
            value={socialOverview?.vault.backend ?? 'not loaded'}
            tone={socialOverview?.vault.available ? 'good' : 'warn'}
            mono={false}
          />
          <Row
            label="Delivery provider"
            value={delivery?.provider ?? (delivery?.configured ? 'configured' : 'not configured')}
            mono={false}
          />
          <Row label="API" value={version?.api ?? '-'} />
          <Row label="Runtime" value={snapshot ? `${snapshot.cpu_percent}% CPU · ${snapshot.ram_percent}% RAM` : 'telemetry unavailable'} mono={false} />
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" className="quiet-link text-xs" onClick={() => onNavigate?.('lib-storage')}>
              Storage
            </button>
            <span className="text-slate-700">·</span>
            <button type="button" className="quiet-link text-xs" onClick={() => onNavigate?.('soc-vault')}>
              Vault status
            </button>
            <span className="text-slate-700">·</span>
            <button type="button" className="quiet-link text-xs" onClick={() => onNavigate?.('pub-delivery')}>
              Media delivery
            </button>
          </div>
        </Card>
      </div>
    </div>
  );
}
