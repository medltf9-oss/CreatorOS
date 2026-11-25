import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError } from '../../api';
import { formatBytes, LibraryApi, type LibraryStats, type RecoveryReport, type StorageReport } from '../../apiLibrary';
import { Badge, Button, Card, ErrorNote, Progress, Row, Spinner } from '../../components/ui';

const AREA_PURPOSE: Record<string, string> = {
  originals: 'The imported source file. Never deleted automatically.',
  thumbnails: 'Poster frames and timeline strips (WEBP/JPEG).',
  previews: 'Fast H.264 previews for scrubbing (≤720p).',
  proxies: '480p editing proxies for long or heavy sources.',
  transcodes: 'Normalised delivery copies.',
  audio: 'Extracted audio (WAV / AAC).',
  waveforms: 'Peak JSON and rendered waveform images.',
  staging: 'In-flight copies. Anything left here after a crash is swept.',
  quarantine: 'Files that failed validation. Kept for inspection, never served.',
};

export default function StorageManagement({
  library,
  onOpenAsset,
}: {
  library: LibraryApi;
  onOpenAsset: (assetId: string) => void;
}) {
  const [storage, setStorage] = useState<StorageReport | null>(null);
  const [stats, setStats] = useState<LibraryStats | null>(null);
  const [report, setReport] = useState<RecoveryReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [olderThan, setOlderThan] = useState(0);

  const load = useCallback(async () => {
    try {
      setError(null);
      const [storageReport, libraryStats] = await Promise.all([library.storage(), library.stats()]);
      setStorage(storageReport);
      setStats(libraryStats);
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [library]);

  useEffect(() => {
    void load();
  }, [load]);

  const scan = async () => {
    setBusy('scan');
    setError(null);
    try {
      setReport(await library.recoveryScan(olderThan));
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(null);
    }
  };

  if (loading && !storage) return <Spinner label="Measuring storage…" />;
  if (!storage || !stats) return <ErrorNote error={error} onRetry={() => void load()} />;

  const totalBytes = Object.values(storage.areas).reduce((sum, area) => sum + area.bytes, 0);
  const originalBytes = storage.areas.originals?.bytes ?? 0;
  const overhead = totalBytes - originalBytes;
  const freePercent = storage.disk.total_gb > 0 ? (storage.disk.free_gb / storage.disk.total_gb) * 100 : 0;

  return (
    <div className="space-y-5">
      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      <div className="grid gap-5 md:grid-cols-3">
        <Card title="Disk">
          <p className="font-mono text-3xl text-slate-100">{storage.disk.free_gb} GB</p>
          <p className="mt-1 text-xs text-slate-500">free of {storage.disk.total_gb} GB ({freePercent.toFixed(1)}%)</p>
          <div className="mt-3">
            <Progress percent={100 - freePercent} tone={storage.disk.free_gb < 10 ? 'bad' : storage.disk.free_gb < 40 ? 'info' : 'good'} />
          </div>
          <p className="mt-2 text-xs text-slate-500">
            New work is refused below {storage.disk.min_free_gb ?? stats.disk.min_free_gb ?? 0} GB free, checked
            before every copy and encode.
          </p>
        </Card>

        <Card title="Library footprint">
          <Row label="Originals" value={formatBytes(originalBytes)} />
          <Row label="Derivatives" value={formatBytes(overhead)} />
          <Row label="Total" value={formatBytes(totalBytes)} />
          <Row label="Assets" value={stats.assets_total} />
          <p className="mt-2 text-xs text-slate-500">{storage.spacing_note}</p>
        </Card>

        <Card title="Originals policy">
          <Badge tone="good">never auto-deleted</Badge>
          <p className="mt-2 text-xs text-slate-500">
            Derivative cleanup only ever touches generated files. Removing an original requires an explicit delete on the
            asset detail page, with the asset id typed as confirmation.
          </p>
          <Button variant="ghost" onClick={() => void load()}>
            Re-measure
          </Button>
        </Card>
      </div>

      <Card title="Areas" subtitle={`${Object.keys(storage.areas).length} purpose-built directories under data/media`}>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="py-2">Area</th>
                <th className="py-2">Files</th>
                <th className="py-2">Size</th>
                <th className="py-2">Purpose</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {Object.entries(storage.areas).map(([area, value]) => (
                <tr key={area}>
                  <td className="py-2 font-mono text-slate-200">{area}</td>
                  <td className="py-2 text-slate-400">{value.files}</td>
                  <td className="py-2 text-slate-400">{formatBytes(value.bytes)}</td>
                  <td className="py-2 text-xs text-slate-500">{AREA_PURPOSE[area] ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card
          title="Recovery scan"
          subtitle="Clears abandoned staging files and flags rows whose files vanished"
          actions={
            <div className="flex items-center gap-2">
              <input
                value={olderThan}
                onChange={(event) => setOlderThan(Number(event.target.value.replace(/[^0-9]/g, '')) || 0)}
                className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
                title="minimum age in minutes"
              />
              <Button variant="primary" busy={busy === 'scan'} onClick={() => void scan()}>
                Run scan
              </Button>
            </div>
          }
        >
          <p className="text-xs text-slate-500">
            Ignores anything younger than {olderThan} minute(s), so an active import is never disturbed.
          </p>
          {report ? (
            <div className="mt-3">
              <Row label="Staging files removed" value={report.staging_removed} tone={report.staging_removed > 0 ? 'warn' : 'normal'} />
              <Row label="Bytes freed" value={formatBytes(report.bytes_freed)} />
              <Row label="Assets quarantined" value={report.assets_quarantined} tone={report.assets_quarantined > 0 ? 'bad' : 'normal'} />
              <Row label="Derivatives marked missing" value={report.derivatives_missing} tone={report.derivatives_missing > 0 ? 'bad' : 'normal'} />
            </div>
          ) : (
            <p className="mt-3 text-xs text-slate-500">No scan has run in this session.</p>
          )}
        </Card>

        <Card title="Largest assets" subtitle="Largest originals first; deleting is always explicit">
          <ul className="divide-y divide-slate-800">
            {[...stats.recent]
              .sort((a, b) => b.size_bytes - a.size_bytes)
              .slice(0, 8)
              .map((asset) => (
                <li key={asset.id} className="flex items-center gap-3 py-2">
                  <button
                    type="button"
                    onClick={() => onOpenAsset(asset.id)}
                    className="min-w-0 flex-1 truncate text-left text-sm text-slate-200 hover:text-sky-300"
                  >
                    {asset.original_filename}
                  </button>
                  <span className="text-xs text-slate-500">{formatBytes(asset.size_bytes)}</span>
                  <Badge>{asset.kind}</Badge>
                </li>
              ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}
