import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiRequestError } from '../../api';
import {
  formatBytes,
  formatDuration,
  formatRelative,
  LibraryApi,
  type AssetDetail,
  type DerivativeRow,
  type ProfileRow,
} from '../../apiLibrary';
import { Badge, Button, Card, EmptyState, ErrorNote, Progress, Row, Spinner } from '../../components/ui';

const RIGHTS = ['owned', 'licensed', 'uncertain', 'restricted'] as const;
const ARCHIVE = ['active', 'archived', 'trashed'] as const;

function probeSummary(detail: AssetDetail): { label: string; value: string }[] {
  const probe = (detail.probe ?? {}) as Record<string, unknown>;
  const video = (probe.video ?? {}) as Record<string, unknown>;
  const audio = (probe.audio ?? {}) as Record<string, unknown>;
  const container = (probe.container ?? {}) as Record<string, unknown>;
  return [
    { label: 'Container', value: String(container.format_long_name ?? detail.format_name ?? '—') },
    { label: 'Video codec', value: String(video.codec ?? detail.video_codec ?? '—') },
    { label: 'Profile / level', value: `${String(video.profile ?? '—')} ${String(video.level ?? '')}`.trim() },
    { label: 'Pixel format', value: String(video.pix_fmt ?? '—') },
    { label: 'Bit depth', value: video.bit_depth ? `${video.bit_depth} bit` : '—' },
    { label: 'Colour range', value: String(video.color_range ?? '—') },
    { label: 'HDR', value: video.hdr === true ? `yes (${String(video.color_transfer ?? 'unknown')})` : 'no' },
    { label: 'Rotation', value: `${detail.rotation}°` },
    { label: 'Audio codec', value: String(audio.codec ?? detail.audio_codec ?? '—') },
    { label: 'Channels', value: audio.channels ? `${audio.channels} @ ${String(audio.sample_rate ?? '?')} Hz` : '—' },
    { label: 'Probe version', value: detail.probe_version ?? '—' },
  ];
}

const STATE_TONE: Record<string, 'good' | 'warn' | 'bad' | 'muted' | 'info'> = {
  ready: 'good',
  queued: 'muted',
  running: 'info',
  failed: 'bad',
  deleted: 'muted',
};

export default function MediaDetail({
  library,
  assetId,
  onBack,
  onOpenJob,
}: {
  library: LibraryApi;
  assetId: string;
  onBack: () => void;
  onOpenJob: (jobId: string) => void;
}) {
  const [detail, setDetail] = useState<AssetDetail | null>(null);
  const [profiles, setProfiles] = useState<ProfileRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tags, setTags] = useState('');
  const [tab, setTab] = useState<'overview' | 'derivatives' | 'technical' | 'metadata'>('overview');

  const load = useCallback(async () => {
    try {
      setError(null);
      const [asset, profileList] = await Promise.all([library.asset(assetId), library.profiles()]);
      setDetail(asset);
      setProfiles(profileList.profiles);
      setTags(asset.tags_json.join(', '));
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    }
  }, [library, assetId]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      const response = (await action()) as { job_id?: string; changes?: unknown } | undefined;
      if (response?.job_id) setNotice(`Queued job ${response.job_id.slice(-8)} — watch the processing centre.`);
      else setNotice(`${label} completed.`);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(null);
    }
  };

  const videoProfiles = useMemo(() => profiles.filter((profile) => profile.kind === 'video'), [profiles]);

  if (!detail) {
    return error ? <ErrorNote error={error} onRetry={() => void load()} /> : <Spinner label="Loading asset…" />;
  }

  const thumbnails = detail.derivatives.filter((row) => row.kind === 'thumbnail' || row.kind === 'timeline');
  const timeline = detail.derivatives.filter((row) => row.kind === 'timeline');

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" onClick={onBack}>
          ← Library
        </Button>
        <h1 className="min-w-0 flex-1 truncate text-lg font-medium text-slate-100">{detail.original_filename}</h1>
        <Badge tone={detail.intake_state === 'ready' ? 'good' : detail.intake_state === 'quarantined' ? 'bad' : 'warn'}>
          {detail.intake_state}
        </Badge>
        <Badge tone="muted">{detail.kind}</Badge>
      </div>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {notice ? (
        <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-200">{notice}</div>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-3">
        <Card className="lg:col-span-2" title="Preview" subtitle="Served through a signed URL; Range requests supported">
          {detail.kind === 'video' ? (
            <video
              key={detail.id}
              controls
              preload="metadata"
              poster={library.mediaUrl(detail.thumbnail_url) ?? undefined}
              src={library.mediaUrl(detail.stream_url) ?? undefined}
              className="max-h-[420px] w-full rounded-lg bg-black"
            />
          ) : detail.kind === 'audio' ? (
            <div className="space-y-3">
              {detail.poster_url ? (
                <img
                  src={library.mediaUrl(detail.poster_url) ?? undefined}
                  alt="waveform"
                  className="h-32 w-full rounded-lg bg-slate-950 object-contain"
                />
              ) : null}
              <audio controls src={library.mediaUrl(detail.stream_url) ?? undefined} className="w-full" />
            </div>
          ) : detail.thumbnail_url ? (
            <img
              src={library.mediaUrl(detail.thumbnail_url) ?? undefined}
              alt=""
              className="max-h-[420px] w-full rounded-lg bg-slate-950 object-contain"
            />
          ) : (
            <EmptyState title="No preview available" hint="Generate a thumbnail or open the file from disk." />
          )}
        </Card>

        <Card title="Facts">
          <Row label="Size" value={formatBytes(detail.size_bytes)} />
          <Row label="Duration" value={formatDuration(detail.duration_ms)} />
          <Row label="Resolution" value={detail.width && detail.height ? `${detail.width}×${detail.height}` : '—'} />
          <Row label="FPS" value={detail.fps ? detail.fps.toFixed(2) : '—'} />
          <Row label="Audio" value={detail.has_audio ? detail.audio_codec ?? 'yes' : 'none'} />
          <Row label="Bitrate" value={detail.bitrate ? `${Math.round(detail.bitrate / 1000)} kbps` : '—'} />
          <Row label="MIME" value={detail.mime_type ?? '—'} />
          <Row label="Imported" value={formatRelative(detail.created_at)} />
          <Row label="Processed" value={formatRelative(detail.last_processed_at)} />
          <Row label="In projects" value={detail.project_count} />
          <button
            type="button"
            onClick={() => void act('Re-probe', () => library.inspect(detail.id))}
            className="mt-3 text-xs text-sky-400 hover:text-sky-300"
          >
            Re-probe file with ffprobe →
          </button>
        </Card>
      </div>

      <Card title="Predictable storage" subtitle="Hash-derived path, no user filename anywhere">
        <p className="break-all font-mono text-xs text-slate-400">{detail.source_path}</p>
        <p className="mt-2 break-all font-mono text-xs text-slate-500">sha256 {detail.sha256}</p>
        {detail.quarantine_reason ? (
          <p className="mt-2 text-xs text-rose-400">quarantined: {detail.quarantine_reason}</p>
        ) : null}
      </Card>

      <div className="flex gap-1 rounded-lg border border-slate-800 p-1">
        {(['overview', 'derivatives', 'technical', 'metadata'] as const).map((entry) => (
          <button
            key={entry}
            type="button"
            onClick={() => setTab(entry)}
            className={`rounded-md px-3 py-1 text-sm capitalize ${
              tab === entry ? 'bg-slate-800 text-slate-100' : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {entry}
          </button>
        ))}
      </div>

      {tab === 'overview' ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card title="Thumbnail">
            {thumbnails.length === 0 ? (
              <EmptyState title="No thumbnail yet" hint="Generate one; black frames are scored and skipped." />
            ) : (
              <div className="grid grid-cols-2 gap-2">
                {thumbnails.map((row) => (
                  <figure key={row.id} className="overflow-hidden rounded-lg border border-slate-800">
                    <img
                      src={library.mediaUrl(row.urls.file) ?? undefined}
                      alt=""
                      className="aspect-video w-full bg-slate-950 object-cover"
                    />
                    <figcaption className="flex items-center justify-between px-2 py-1 text-[11px] text-slate-500">
                      <span>{row.profile}</span>
                      <span>{row.width}×{row.height}</span>
                    </figcaption>
                  </figure>
                ))}
              </div>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                busy={busy === 'Thumbnail'}
                onClick={() => void act('Thumbnail', () => library.requestThumbnail(detail.id))}
              >
                Generate thumbnail
              </Button>
              {detail.kind === 'video' ? (
                <Button
                  busy={busy === 'Timeline'}
                  onClick={() => void act('Timeline', () => library.requestThumbnail(detail.id, { timeline: 8 }))}
                >
                  Timeline (8 frames)
                </Button>
              ) : null}
            </div>
          </Card>

          <Card title="Processing actions" subtitle="Each action queues a real FFmpeg job on the media queue">
            <div className="flex flex-wrap gap-2">
              {detail.kind === 'video' ? (
                <>
                  <Button busy={busy === 'Preview'} onClick={() => void act('Preview', () => library.requestPreview(detail.id))}>
                    Fast preview (≤720p)
                  </Button>
                  <Button busy={busy === 'Proxy'} onClick={() => void act('Proxy', () => library.requestProxy(detail.id))}>
                    Editing proxy (480p)
                  </Button>
                </>
              ) : null}
              {detail.kind !== 'image' ? (
                <>
                  <Button busy={busy === 'Audio WAV'} onClick={() => void act('Audio WAV', () => library.requestAudio(detail.id, 'wav'))}>
                    Extract audio (WAV)
                  </Button>
                  <Button busy={busy === 'Audio AAC'} onClick={() => void act('Audio AAC', () => library.requestAudio(detail.id, 'aac'))}>
                    Extract audio (AAC)
                  </Button>
                  <Button busy={busy === 'Waveform'} onClick={() => void act('Waveform', () => library.requestWaveform(detail.id))}>
                    Waveform peaks
                  </Button>
                </>
              ) : null}
            </div>
            {detail.kind === 'image' ? (
              <p className="mt-2 text-xs text-slate-500">
                Image assets support thumbnails only: there is no video stream to transcode.
              </p>
            ) : null}
          </Card>
        </div>
      ) : null}

      {tab === 'derivatives' ? (
        <Card title={`Derivatives (${detail.derivatives.length})`} subtitle="Every row is backed by a file that exists on disk">
          {detail.derivatives.length === 0 ? (
            <EmptyState title="No derivatives" hint="Generate a thumbnail or preview above." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="py-2">Kind</th>
                    <th className="py-2">Profile</th>
                    <th className="py-2">State</th>
                    <th className="py-2">Size</th>
                    <th className="py-2">Dimensions</th>
                    <th className="py-2">Version</th>
                    <th className="py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {detail.derivatives.map((row: DerivativeRow) => (
                    <tr key={row.id}>
                      <td className="py-2 text-slate-200">{row.kind}</td>
                      <td className="py-2 font-mono text-xs text-slate-400">{row.profile}</td>
                      <td className="py-2">
                        <Badge tone={STATE_TONE[row.state] ?? 'muted'}>{row.state}</Badge>
                      </td>
                      <td className="py-2 text-slate-400">{formatBytes(row.size_bytes)}</td>
                      <td className="py-2 text-slate-400">
                        {row.width && row.height ? `${row.width}×${row.height}` : formatDuration(row.duration_ms)}
                      </td>
                      <td className="py-2 font-mono text-xs text-slate-500">v{row.version}</td>
                      <td className="py-2 text-right">
                        <div className="flex justify-end gap-2">
                          {row.urls.file ? (
                            <a
                              href={library.mediaUrl(row.urls.file) ?? '#'}
                              target="_blank"
                              rel="noreferrer"
                              className="text-xs text-sky-400"
                            >
                              open
                            </a>
                          ) : null}
                          {row.job_id ? (
                            <button type="button" onClick={() => onOpenJob(row.job_id!)} className="text-xs text-slate-400">
                              job
                            </button>
                          ) : null}
                          {row.state === 'ready' ? (
                            <button
                              type="button"
                              onClick={() => void act('Delete derivative', () => library.deleteDerivative(row.id))}
                              className="text-xs text-rose-400"
                            >
                              delete
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {detail.jobs.length > 0 ? (
            <div className="mt-4">
              <p className="mb-1 text-xs uppercase tracking-widest text-slate-500">Recent jobs</p>
              <ul className="divide-y divide-slate-800">
                {detail.jobs.map((job) => (
                  <li key={job.job_id} className="flex items-center gap-3 py-1.5 text-xs">
                    <span className="w-44 font-mono text-slate-400">{job.job_type}</span>
                    <Badge tone={job.state === 'succeeded' ? 'good' : job.state === 'failed' ? 'bad' : 'muted'}>
                      {job.state}
                    </Badge>
                    <span className="flex-1 text-slate-500">{formatRelative(job.created_at)}</span>
                    <button type="button" onClick={() => onOpenJob(job.job_id)} className="text-sky-400">
                      inspect
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </Card>
      ) : null}

      {tab === 'technical' ? (
        <Card title="Probe result" subtitle={`Measured by ffprobe, stored as ${detail.probe_version ?? 'unknown'}`}>
          <div className="grid gap-x-8 gap-y-0 md:grid-cols-2">
            {probeSummary(detail).map((entry) => (
              <Row key={entry.label} label={entry.label} value={entry.value} />
            ))}
          </div>
          <details className="mt-4">
            <summary className="cursor-pointer text-xs text-slate-400">raw probe JSON</summary>
            <pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-400">
              {JSON.stringify(detail.probe, null, 2)}
            </pre>
          </details>
        </Card>
      ) : null}

      {tab === 'metadata' ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card title="Tags and rights" subtitle="Stored on the asset row; used by the readiness checklist">
            <label className="block text-xs uppercase tracking-wide text-slate-500">Tags (comma separated)</label>
            <input
              value={tags}
              onChange={(event) => setTags(event.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-200"
            />
            <div className="mt-3 flex gap-2">
              {RIGHTS.map((rights) => (
                <button
                  key={rights}
                  type="button"
                  onClick={() => void act('Rights', () => library.updateAsset(detail.id, { rights_status: rights }))}
                  className={`rounded-lg border px-2 py-1 text-xs ${
                    detail.rights_status === rights
                      ? 'border-sky-500 bg-sky-500/10 text-sky-200'
                      : 'border-slate-700 text-slate-400 hover:bg-slate-800'
                  }`}
                >
                  {rights}
                </button>
              ))}
            </div>
            <div className="mt-3 flex gap-2">
              <Button
                busy={busy === 'Save tags'}
                onClick={() =>
                  void act('Save tags', () =>
                    library.updateAsset(detail.id, {
                      tags_json: tags
                        .split(',')
                        .map((tag) => tag.trim())
                        .filter(Boolean),
                    }),
                  )
                }
              >
                Save tags
              </Button>
              {ARCHIVE.map((state) => (
                <Button
                  key={state}
                  variant="ghost"
                  busy={busy === state}
                  onClick={() => void act(state, () => library.updateAsset(detail.id, { archive_state: state }))}
                >
                  mark {state}
                </Button>
              ))}
            </div>
          </Card>

          <Card title="Source quality" subtitle="Rule-based recommendation, no model involved">
            <QualityPanel library={library} assetId={detail.id} />
            <div className="mt-4 border-t border-slate-800 pt-3">
              <p className="text-xs uppercase tracking-widest text-slate-500">Available video profiles</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {videoProfiles.map((profile) => (
                  <Button
                    key={profile.id}
                    busy={busy === profile.id}
                    onClick={() => void act(profile.label, () => library.requestTranscode(detail.id, profile.id))}
                    title={profile.purpose}
                  >
                    {profile.label}
                  </Button>
                ))}
              </div>
            </div>
          </Card>
        </div>
      ) : null}

      <Card title="Danger zone" subtitle="Deleting an asset removes its original file and every derivative">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="danger"
            busy={busy === 'Delete asset'}
            onClick={() => {
              if (!window.confirm(`Delete "${detail.original_filename}" permanently? The original file is removed.`)) return;
              void act('Delete asset', async () => {
                await library.deleteAsset(detail.id);
                onBack();
              });
            }}
          >
            Delete asset
          </Button>
          <span className="text-xs text-slate-500">
            Projects that reference this asset block the delete; use force only if you accept a broken reference.
          </span>
          <Button
            variant="ghost"
            onClick={() => {
              if (!window.confirm('Force delete even if projects reference this asset?')) return;
              void act('Delete asset', async () => {
                await library.deleteAsset(detail.id, true);
                onBack();
              });
            }}
          >
            Force delete
          </Button>
        </div>
        {timeline.length > 0 ? (
          <div className="mt-4">
            <p className="mb-2 text-xs uppercase tracking-widest text-slate-500">Timeline frames</p>
            <div className="flex gap-2 overflow-x-auto">
              {timeline.map((row) => (
                <img
                  key={row.id}
                  src={library.mediaUrl(row.urls.file) ?? undefined}
                  alt=""
                  className="h-16 rounded border border-slate-800"
                />
              ))}
            </div>
          </div>
        ) : null}
        <Progress percent={100} tone="bad" />
      </Card>
    </div>
  );
}

function QualityPanel({ library, assetId }: { library: LibraryApi; assetId: string }) {
  const [data, setData] = useState<{ stored: Record<string, unknown>; recommendation: { profile: string; reason: string } } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    library
      .probe(assetId)
      .then((payload) => {
        if (!cancelled) setData(payload);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiRequestError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [library, assetId]);

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Spinner label="Reading stored probe…" />;
  return (
    <div className="space-y-2">
      <Row label="Recommended profile" value={data.recommendation.profile} tone="good" />
      <p className="text-xs text-slate-400">{data.recommendation.reason}</p>
      <details className="pt-2">
        <summary className="cursor-pointer text-xs text-slate-500">stored probe</summary>
        <pre className="mt-1 max-h-48 overflow-auto rounded bg-slate-950 p-2 text-[11px] text-slate-400">
          {JSON.stringify(data.stored, null, 2)}
        </pre>
      </details>
    </div>
  );
}
