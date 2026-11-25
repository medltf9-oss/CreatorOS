import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError } from '../../api';
import { formatBytes, formatDuration, formatRelative, LibraryApi, type LibraryStats } from '../../apiLibrary';
import { Badge, Card, EmptyState, ErrorNote, Row, Spinner } from '../../components/ui';

export default function LibraryOverview({
  library,
  onNavigate,
  onOpenAsset,
}: {
  library: LibraryApi;
  onNavigate: (page: string) => void;
  onOpenAsset: (assetId: string) => void;
}) {
  const [stats, setStats] = useState<LibraryStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setError(null);
      setStats(await library.stats());
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [library]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  if (loading && !stats) return <Spinner label="Reading library facts…" />;
  if (error && !stats) return <ErrorNote error={error} onRetry={() => void load()} />;
  if (!stats) return null;

  const encoder = stats.capabilities.h264_encoder;
  const kinds = Object.entries(stats.by_kind).filter(([, count]) => count > 0);
  const derivatives = Object.entries(stats.derivatives_by_kind).filter(([, count]) => count > 0);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => onNavigate('lib-import')}
          className="rounded-lg bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500"
        >
          Import media
        </button>
        <button
          type="button"
          onClick={() => onNavigate('lib-grid')}
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
        >
          Browse library
        </button>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
        >
          Refresh
        </button>
        {error ? <span className="text-xs text-rose-400">{error}</span> : null}
      </div>

      <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-4">
        <Card title="Assets">
          <p className="font-mono text-3xl text-slate-100">{stats.assets_total}</p>
          <p className="mt-1 text-xs text-slate-500">{formatBytes(stats.bytes_total)} of originals on disk</p>
          <div className="mt-3 flex flex-wrap gap-1">
            {kinds.length === 0 ? <span className="text-xs text-slate-500">nothing imported yet</span> : null}
            {kinds.map(([kind, count]) => (
              <Badge key={kind} tone="info">
                {kind} {count}
              </Badge>
            ))}
          </div>
        </Card>

        <Card title="Derivatives">
          <p className="font-mono text-3xl text-slate-100">{stats.derivatives_total}</p>
          <p className="mt-1 text-xs text-slate-500">thumbnails, previews, proxies, audio, waveforms</p>
          <div className="mt-3 flex flex-wrap gap-1">
            {derivatives.map(([kind, count]) => (
              <Badge key={kind}>
                {kind} {count}
              </Badge>
            ))}
          </div>
        </Card>

        <Card title="Jobs">
          <Row label="Active" value={stats.jobs_active} tone={stats.jobs_active > 0 ? 'warn' : 'normal'} />
          <Row label="Completed" value={stats.jobs_completed} tone="good" />
          <Row label="Failed" value={stats.jobs_failed} tone={stats.jobs_failed > 0 ? 'bad' : 'normal'} />
          <button
            type="button"
            onClick={() => onNavigate('lib-processing')}
            className="mt-3 text-xs text-sky-400 hover:text-sky-300"
          >
            Open processing centre →
          </button>
        </Card>

        <Card title="Disk">
          <Row label="Free" value={`${stats.disk.free_gb ?? 0} GB`} tone={(stats.disk.free_gb ?? 0) > 5 ? 'good' : 'bad'} />
          <Row label="Total" value={`${stats.disk.total_gb} GB`} />
          <Row label="Used" value={`${stats.disk.percent_used ?? 0}%`} />
          <button
            type="button"
            onClick={() => onNavigate('lib-storage')}
            className="mt-3 text-xs text-sky-400 hover:text-sky-300"
          >
            Storage management →
          </button>
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card
          title="Recent imports"
          subtitle="Newest first, straight from the database"
          actions={
            <button type="button" onClick={() => onNavigate('lib-grid')} className="text-xs text-sky-400">
              view all
            </button>
          }
        >
          {stats.recent.length === 0 ? (
            <EmptyState title="No media yet" hint="Import a video, audio track or image to start the library." />
          ) : (
            <ul className="divide-y divide-slate-800">
              {stats.recent.map((asset) => (
                <li key={asset.id}>
                  <button
                    type="button"
                    onClick={() => onOpenAsset(asset.id)}
                    className="flex w-full items-center gap-3 py-2 text-left hover:bg-slate-800/40"
                  >
                    <span className="flex h-10 w-14 shrink-0 items-center justify-center overflow-hidden rounded bg-slate-950">
                      {asset.thumbnail_url ? (
                        <img src={library.mediaUrl(asset.thumbnail_url) ?? undefined} alt="" className="h-full w-full object-cover" />
                      ) : (
                        <span className="font-mono text-[10px] uppercase text-slate-500">{asset.kind}</span>
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm text-slate-200">{asset.original_filename}</span>
                      <span className="block text-xs text-slate-500">
                        {formatBytes(asset.size_bytes)} · {formatDuration(asset.duration_ms)} ·{' '}
                        {formatRelative(asset.created_at)}
                      </span>
                    </span>
                    <Badge tone={asset.intake_state === 'ready' ? 'good' : 'warn'}>{asset.intake_state}</Badge>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Measured capabilities" subtitle="What this machine can actually do, checked at runtime">
          <Row
            label="H.264 encoder"
            value={`${encoder?.name ?? 'unknown'}${encoder?.hardware ? ' (GPU)' : ' (CPU)'}`}
            tone={encoder?.verified ? 'good' : 'bad'}
          />
          <Row label="Hardware encoding" value={encoder?.hardware ? 'available' : 'unavailable'} tone={encoder?.hardware ? 'good' : 'warn'} />
          <Row label="ffmpeg" value={stats.capabilities.ffmpeg?.version ?? 'missing'} tone={stats.capabilities.ffmpeg?.available ? 'good' : 'bad'} />
          <Row label="ffprobe" value={stats.capabilities.probe?.version ?? 'missing'} tone={stats.capabilities.probe?.available ? 'good' : 'bad'} />
          {encoder?.note ? <p className="mt-2 text-xs text-slate-500">{encoder.note}</p> : null}
        </Card>
      </div>

      <Card title="Storage areas" subtitle="Every byte the library owns, by purpose">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(stats.storage).map(([area, value]) => (
            <div key={area} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
              <p className="text-xs uppercase tracking-widest text-slate-500">{area}</p>
              <p className="mt-1 font-mono text-sm text-slate-200">{formatBytes(value.bytes)}</p>
              <p className="text-xs text-slate-500">{value.files} files</p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
