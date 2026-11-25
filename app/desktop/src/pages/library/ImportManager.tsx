import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError } from '../../api';
import {
  formatBytes,
  formatRelative,
  LibraryApi,
  type ImportPreviewRow,
  type ImportResult,
  type JobRow,
} from '../../apiLibrary';
import { Badge, Button, Card, EmptyState, ErrorNote, Progress, Spinner } from '../../components/ui';

type PickedFile = { path: string; name: string; size_bytes: number };

type ShellBridge = {
  pickMedia?: () => Promise<{ canceled: boolean; files: PickedFile[] }>;
  importMedia?: (paths: string[]) => Promise<{
    ok: boolean;
    status?: number;
    error?: string;
    payload?: ImportResult & { detail?: string; message?: string };
  }>;
};

function shell(): ShellBridge | null {
  if (typeof window === 'undefined') return null;
  return (window as unknown as { creatoros?: ShellBridge }).creatoros ?? null;
}

export default function ImportManager({ library, onOpenAsset }: { library: LibraryApi; onOpenAsset: (id: string) => void }) {
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [preview, setPreview] = useState<ImportPreviewRow[] | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bridgeAvailable, setBridgeAvailable] = useState(true);

  useEffect(() => {
    setBridgeAvailable(Boolean(shell()?.pickMedia));
  }, []);

  const pick = async () => {
    setError(null);
    const picker = shell()?.pickMedia;
    if (!picker) {
      setError('The desktop shell is not available, so the native file picker cannot open.');
      return;
    }
    try {
      const picked = await picker();
      if (picked.canceled) return;
      setFiles(picked.files);
      setResult(null);
      setPreview(null);
      if (picked.files.length > 0) {
        const checked = await library.previewImport(picked.files.map((file) => file.path));
        setPreview(checked.results);
      }
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    }
  };

  const startImport = async () => {
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const importer = shell()?.importMedia;
      if (importer) {
        const response = await importer(files.map((file) => file.path));
        if (!response.ok) {
          setError(response.error ?? response.payload?.detail ?? response.payload?.message ?? 'import rejected');
        } else if (response.payload) {
          setResult({
            jobs: response.payload.jobs ?? [],
            duplicates: response.payload.duplicates ?? [],
            rejected: response.payload.rejected ?? [],
          });
          setFiles([]);
          setPreview(null);
        }
      } else {
        // No shell (browser/headless): the backend still enforces its own signature policy.
        const direct = await library.importPaths(files.map((file) => file.path));
        setResult(direct);
        setFiles([]);
        setPreview(null);
      }
      setJobs((await library.jobs({ limit: 20 })).items);
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(false);
    }
  };

  const refreshJobs = useCallback(async () => {
    try {
      setJobs((await library.jobs({ limit: 20 })).items);
    } catch {
      /* the processing centre surfaces queue errors; this page only mirrors it */
    }
  }, [library]);

  useEffect(() => {
    void refreshJobs();
    const timer = window.setInterval(() => void refreshJobs(), 4000);
    return () => window.clearInterval(timer);
  }, [refreshJobs]);

  const newCount = preview?.filter((row) => row.status === 'new').length ?? files.length;

  return (
    <div className="space-y-5">
      {!bridgeAvailable ? (
        <ErrorNote error="Native file picking needs the Electron shell. Start the app with pnpm dev and use this page inside it." />
      ) : null}
      {error ? <ErrorNote error={error} onRetry={() => setError(null)} /> : null}

      <Card
        title="Import media"
        subtitle="Files are copied in 1 MiB chunks, hashed while copying, then promoted atomically. The original is never modified."
        actions={<Button variant="primary" onClick={() => void pick()} disabled={!bridgeAvailable}>Choose files</Button>}
      >
        {files.length === 0 ? (
          <EmptyState
            title="No files selected"
            hint="Pick video, audio or image files. Duplicates are detected by SHA-256 before anything is copied."
          />
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <Badge tone="info">{files.length} selected</Badge>
              <Badge>{formatBytes(files.reduce((sum, file) => sum + file.size_bytes, 0))}</Badge>
              {preview ? (
                <>
                  <Badge tone="good">{newCount} new</Badge>
                  <Badge tone="warn">{preview.filter((row) => row.status === 'probable_duplicate').length} duplicate</Badge>
                  <Badge tone="bad">{preview.filter((row) => row.status === 'rejected').length} rejected</Badge>
                </>
              ) : null}
            </div>
            <ul className="max-h-72 divide-y divide-slate-800 overflow-auto rounded-lg border border-slate-800">
              {files.map((file) => {
                const check = preview?.find((row) => row.path === file.path);
                return (
                  <li key={file.path} className="flex items-center gap-3 px-3 py-2 text-sm">
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-slate-200">{file.name}</span>
                      <span className="block truncate font-mono text-[11px] text-slate-500">{file.path}</span>
                    </span>
                    <span className="text-xs text-slate-500">{formatBytes(file.size_bytes)}</span>
                    {check ? (
                      <Badge
                        tone={check.status === 'new' ? 'good' : check.status === 'rejected' ? 'bad' : 'warn'}
                        title={check.reason}
                      >
                        {check.status}
                      </Badge>
                    ) : null}
                  </li>
                );
              })}
            </ul>
            <div className="flex gap-2">
              <Button variant="primary" busy={busy} onClick={() => void startImport()} disabled={newCount === 0}>
                Import {newCount} file{newCount === 1 ? '' : 's'}
              </Button>
              <Button
                variant="ghost"
                onClick={() => {
                  setFiles([]);
                  setPreview(null);
                }}
              >
                Clear
              </Button>
            </div>
          </div>
        )}
      </Card>

      {result ? (
        <Card title="Import result" subtitle="The queue accepted these jobs; processing happens in the background.">
          <div className="space-y-2 text-sm">
            {result.jobs.map((job) => (
              <div key={job.job_id} className="flex items-center justify-between gap-3 rounded border border-slate-800 px-3 py-2">
                <span className="min-w-0 flex-1 truncate text-slate-200">{job.original_filename}</span>
                <Badge tone="info">queued</Badge>
                <span className="font-mono text-[11px] text-slate-500">{job.job_id.slice(-6)}</span>
              </div>
            ))}
            {result.duplicates.map((duplicate) => (
              <div key={duplicate.path} className="flex items-center justify-between gap-3 rounded border border-amber-600/30 px-3 py-2">
                <span className="min-w-0 flex-1 truncate text-amber-200">{duplicate.original_filename}</span>
                <Badge tone="warn">already in library</Badge>
                <Button variant="ghost" onClick={() => onOpenAsset(duplicate.existing_asset_id)}>
                  open
                </Button>
              </div>
            ))}
            {result.rejected.map((rejected) => (
              <div key={rejected.path} className="rounded border border-rose-600/30 px-3 py-2 text-rose-200">
                <span className="block truncate font-mono text-xs">{rejected.path}</span>
                <span className="text-xs">{rejected.reason}</span>
              </div>
            ))}
            {result.jobs.length === 0 && result.duplicates.length === 0 && result.rejected.length === 0 ? (
              <EmptyState title="Nothing was accepted" hint="The backend returned an empty result." />
            ) : null}
          </div>
        </Card>
      ) : null}

      <Card title="Recent media jobs" subtitle="Live from the durable queue; refreshed every 4 s">
        {jobs.length === 0 ? <Spinner label="Reading queue…" /> : null}
        <ul className="divide-y divide-slate-800">
          {jobs.map((job) => (
            <li key={job.job_id} className="py-2">
              <div className="flex items-center gap-3 text-sm">
                <span className="w-44 shrink-0 font-mono text-xs text-slate-400">{job.job_type}</span>
                <Badge
                  tone={
                    job.state === 'succeeded'
                      ? 'good'
                      : job.state === 'failed' || job.state === 'dead_letter'
                        ? 'bad'
                        : job.state === 'running'
                          ? 'info'
                          : 'muted'
                  }
                >
                  {job.state}
                </Badge>
                <span className="min-w-0 flex-1 truncate text-xs text-slate-500">
                  {job.stage ? `stage: ${job.stage}` : job.last_error ?? job.job_id}
                </span>
                <span className="text-xs text-slate-500">{formatRelative(job.updated_at)}</span>
              </div>
              <div className="mt-1">
                <Progress
                  percent={Math.round((job.progress ?? 0) * 100)}
                  tone={
                    job.state === 'failed' || job.state === 'dead_letter'
                      ? 'bad'
                      : job.state === 'succeeded'
                        ? 'good'
                        : job.state === 'cancelled'
                          ? 'warn'
                          : 'info'
                  }
                />
              </div>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
