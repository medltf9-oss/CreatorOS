import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError } from '../../api';
import {
  formatBytes,
  formatDuration,
  LibraryApi,
  type AssetSummary,
  type LibraryCapabilities,
  type ProfileRow,
} from '../../apiLibrary';
import { Badge, Button, Card, EmptyState, ErrorNote, Row, Spinner } from '../../components/ui';

/**
 * A deterministic size estimate from the profile's own bitrate budget. It is labelled as an
 * estimate everywhere because only a real encode can produce a real number.
 */
function estimateBytes(durationMs: number | null, profile: ProfileRow): number | null {
  if (!durationMs) return null;
  const seconds = durationMs / 1000;
  const videoKbps = profile.video_codec === 'none' ? 0 : Math.max(180, 2600 * (profile.crf <= 20 ? 1.6 : profile.crf >= 26 ? 0.6 : 1));
  const audioKbps = profile.audio_codec ? profile.audio_bitrate_k : 0;
  return Math.round(((videoKbps + audioKbps) * 1000 * seconds) / 8);
}

export default function TranscodeStudio({
  library,
  onOpenAsset,
}: {
  library: LibraryApi;
  onOpenAsset: (assetId: string) => void;
}) {
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [profiles, setProfiles] = useState<ProfileRow[]>([]);
  const [capabilities, setCapabilities] = useState<LibraryCapabilities | null>(null);
  const [selectedAsset, setSelectedAsset] = useState<string>('');
  const [selectedProfile, setSelectedProfile] = useState<string>('fast_preview');
  const [height, setHeight] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setError(null);
      const [page, profileList, caps] = await Promise.all([
        library.assets({ limit: 100, sort: 'created_at', order: 'desc' }),
        library.profiles(),
        library.capabilities(),
      ]);
      setAssets(page.items);
      setProfiles(profileList.profiles);
      setCapabilities(caps);
      setSelectedAsset((current) => current || page.items[0]?.id || '');
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [library]);

  useEffect(() => {
    void load();
  }, [load]);

  const asset = assets.find((entry) => entry.id === selectedAsset) ?? null;
  const profile = profiles.find((entry) => entry.id === selectedProfile) ?? null;
  const estimatedBytes = asset && profile ? estimateBytes(asset.duration_ms, profile) : null;
  const encoder = capabilities?.h264_encoder;
  const mismatch =
    asset && profile
      ? (profile.kind === 'video' && asset.kind !== 'video') || (profile.kind === 'audio' && !asset.has_audio)
      : false;

  const run = async () => {
    if (!asset || !profile) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const parsedHeight = height ? Number(height) : undefined;
      const response =
        profile.kind === 'audio'
          ? await library.requestAudio(asset.id, profile.container === 'wav' ? 'wav' : 'aac')
          : await library.requestTranscode(asset.id, profile.id, parsedHeight);
      setNotice(`Queued ${profile.id} for ${asset.original_filename} — job ${response.job_id.slice(-8)}`);
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Spinner label="Reading profiles and capabilities…" />;

  return (
    <div className="space-y-5">
      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {notice ? (
        <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-200">{notice}</div>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-3">
        <Card title="Source" subtitle="Only assets already in the library can be transcoded">
          {assets.length === 0 ? (
            <EmptyState title="Library is empty" hint="Import media first." />
          ) : (
            <>
              <select
                value={selectedAsset}
                onChange={(event) => setSelectedAsset(event.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-200"
              >
                {assets.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.original_filename} · {entry.kind}
                  </option>
                ))}
              </select>
              {asset ? (
                <div className="mt-3">
                  <Row label="Kind" value={asset.kind} />
                  <Row label="Size" value={formatBytes(asset.size_bytes)} />
                  <Row label="Duration" value={formatDuration(asset.duration_ms)} />
                  <Row
                    label="Resolution"
                    value={asset.width && asset.height ? `${asset.width}×${asset.height}` : '—'}
                  />
                  <Row label="FPS" value={asset.fps ? asset.fps.toFixed(2) : '—'} />
                  <Row label="Audio" value={asset.has_audio ? asset.audio_codec ?? 'yes' : 'none'} />
                  <Button variant="ghost" onClick={() => onOpenAsset(asset.id)}>
                    Open asset detail →
                  </Button>
                </div>
              ) : null}
            </>
          )}
        </Card>

        <Card title="Profile" subtitle="Five profiles; the encoder is chosen at run time">
          <div className="space-y-1">
            {profiles.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setSelectedProfile(entry.id)}
                className={`w-full rounded-lg border px-3 py-2 text-left text-sm ${
                  entry.id === selectedProfile
                    ? 'border-sky-500 bg-sky-500/10 text-slate-50'
                    : 'border-slate-800 text-slate-300 hover:bg-slate-800/60'
                }`}
              >
                <span className="flex items-center justify-between gap-2">
                  <span>{entry.label}</span>
                  <Badge tone={entry.kind === 'video' ? 'info' : 'warn'}>{entry.kind}</Badge>
                </span>
                <span className="mt-0.5 block text-xs text-slate-500">{entry.purpose}</span>
              </button>
            ))}
          </div>
        </Card>

        <Card title="Options">
          <label className="block text-xs uppercase tracking-wide text-slate-500">Height override (px)</label>
          <input
            value={height}
            onChange={(event) => setHeight(event.target.value.replace(/[^0-9]/g, ''))}
            placeholder={profile?.max_height ? `default ${profile.max_height}` : 'not applicable'}
            disabled={profile?.kind === 'audio'}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-200 disabled:opacity-40"
          />
          <p className="mt-1 text-xs text-slate-500">
            Only ever downscales. A 360p source stays 360p even if 1080 is requested.
          </p>

          <div className="mt-3 space-y-1">
            <Row label="Container" value={profile?.extension ?? '—'} />
            <Row label="Video codec" value={profile?.video_codec ?? '—'} />
            <Row label="Audio codec" value={profile?.audio_codec ?? 'none'} />
            <Row label="CRF" value={profile?.crf ?? '—'} />
            <Row label="Preset" value={profile?.preset ?? '—'} />
            <Row label="FPS cap" value={profile?.fps_cap ?? 'source'} />
          </div>

          <div className="mt-3">
            <Button variant="primary" busy={busy} disabled={!asset || !profile || Boolean(mismatch)} onClick={() => void run()}>
              Queue transcode
            </Button>
            {mismatch ? (
              <p className="mt-2 text-xs text-amber-400">
                This profile does not apply to {asset?.kind} assets with
                {profile?.kind === 'video' ? ' no video stream' : ' no audio stream'}.
              </p>
            ) : null}
          </div>

          {estimatedBytes && profile?.kind === 'video' ? (
            <p className="mt-3 text-xs text-slate-500">
              Rough size estimate: ~{formatBytes(estimatedBytes)} — an estimate from the profile's bitrate budget,
              not a measurement.
            </p>
          ) : null}
        </Card>
      </div>

      <Card title="Encoder capability" subtitle="Verified with a real 0.5 s test encode, not read from a list">
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <Row
              label="Selected encoder"
              value={`${encoder?.name ?? 'unknown'}${encoder?.hardware ? ' (hardware)' : ' (software)'}`}
              tone={encoder?.verified ? 'good' : 'bad'}
            />
            <Row label="Hardware encoding" value={encoder?.hardware ? 'available' : 'unavailable'} tone={encoder?.hardware ? 'good' : 'warn'} />
            <Row label="ffmpeg" value={capabilities?.ffmpeg.version ?? 'missing'} />
            <Row label="ffprobe" value={capabilities?.probe.version ?? 'missing'} />
            <Row label="Concurrent FFmpeg jobs" value={capabilities?.policy.max_concurrent_ffmpeg ?? 1} />
            <Row label="Minimum free disk" value={`${capabilities?.policy.min_free_gb ?? 0} GB`} />
            <Row label="Thumbnail format" value={capabilities?.thumbnail_format ?? '—'} />
            {encoder?.note ? <p className="mt-2 text-xs text-slate-500">{encoder.note}</p> : null}
          </div>
          <div className="space-y-1">
            {Object.entries(capabilities?.encoder_verification ?? {}).map(([name, info]) => (
              <div key={name} className="flex items-center justify-between rounded border border-slate-800 px-2 py-1 text-xs">
                <span className="font-mono text-slate-300">{name}</span>
                <span className="flex gap-1">
                  <Badge tone={info.available ? 'good' : 'bad'}>{info.available ? 'works' : 'rejected'}</Badge>
                  <Badge tone={info.hardware ? 'info' : 'muted'}>{info.hardware ? 'GPU' : 'CPU'}</Badge>
                </span>
              </div>
            ))}
          </div>
        </div>
      </Card>
    </div>
  );
}
