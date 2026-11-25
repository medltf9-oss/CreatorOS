import { useCallback, useEffect, useState } from 'react';
import { type DeliveryState, type PublishingApi } from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Row, Spinner, formatBytes, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
};

/** Bucket configuration for the media-delivery provider.
 *
 * Instagram fetches video from a public URL, which means the bytes leave the machine. This page is
 * where that is decided, and it says so. The access key pair is sealed in the Electron vault and
 * only its secret id is sent here: the backend never receives the value.
 */
export default function DeliverySettings({ api }: Props) {
  const [state, setState] = useState<DeliveryState | null>(null);
  const [provider, setProvider] = useState('s3');
  const [bucket, setBucket] = useState('');
  const [region, setRegion] = useState('');
  const [publicBase, setPublicBase] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [prefix, setPrefix] = useState('creatoros');
  const [secretId, setSecretId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [check, setCheck] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const next = await api.delivery();
      setState(next);
      setProvider(next.provider ?? 's3');
      setBucket(next.bucket ?? '');
      setRegion(next.region ?? '');
      setPublicBase(next.public_base_url_present ? '(already set)' : '');
      setPrefix(next.prefix ?? 'creatoros');
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    setBusy(true);
    try {
      const next = await api.configureDelivery({
        provider,
        bucket: bucket.trim(),
        region: region.trim(),
        public_base_url: publicBase.startsWith('http') ? publicBase.trim() : undefined,
        endpoint_url: endpoint.trim() || undefined,
        prefix: prefix.trim(),
        credentials_secret_id: secretId.trim() || undefined,
      });
      setState(next);
      setError(null);
      setCheck('saved');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const runCheck = async () => {
    try {
      const outcome = await api.checkDelivery();
      setCheck(outcome.ok ? 'the bucket answers' : `not usable: ${outcome.reason}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const cleanup = async () => {
    try {
      const outcome = await api.cleanupDelivery();
      setCheck(`released ${outcome.released}, kept ${outcome.kept}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const clear = async () => {
    try {
      await api.clearDelivery();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <p className="text-sm text-slate-400">
          Instagram does not accept an upload: it fetches the video from a public URL. That URL is temporary and goes in the vault,
          never in a log or an audit entry. Facebook and TikTok upload directly and do not need this.
        </p>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />
      {check ? <p className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-300">{check}</p> : null}

      {!state ? (
        <Spinner label="Reading the delivery configuration…" />
      ) : (
        <>
          <Card title="Current configuration">
            <Row
              label="Status"
              value={state.configured ? 'configured' : 'not configured'}
              tone={state.configured ? 'good' : 'warn'}
              mono={false}
            />
            <Row label="Provider" value={state.provider ?? '—'} />
            <Row label="Bucket" value={state.bucket ?? '—'} />
            <Row label="Region" value={state.region ?? '—'} />
            <Row label="Public base URL" value={state.public_base_url_present ? 'set' : 'not set'} mono={false} />
            <Row label="Object prefix" value={state.prefix ?? '—'} />
            <Row label="URL lifetime" value={state.ttl_seconds ? `${state.ttl_seconds}s` : '—'} />
            <Row label="Credentials" value={state.credentials_present ? 'sealed in the vault' : 'missing'} tone={state.credentials_present ? 'good' : 'bad'} mono={false} />
            {!state.configured ? <p className="mt-2 text-xs text-amber-400">{state.reason}</p> : null}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button onClick={() => void runCheck()} busy={busy}>
                Check access
              </Button>
              <Button onClick={() => void cleanup()} busy={busy} title="Delete expired copies from the bucket">
                Clean up old copies
              </Button>
              <Button variant="danger" onClick={() => void clear()} busy={busy} title="Removes the configuration and revokes the sealed credential">
                Remove configuration
              </Button>
            </div>
          </Card>

          <Card title="Configure" subtitle="supported providers: S3-compatible object storage">
            <div className="grid gap-3 md:grid-cols-3">
              <Field label="Provider" hint={state.supported_providers.join(', ')}>
                <select
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
                  value={provider}
                  onChange={(event) => setProvider(event.target.value)}
                >
                  {state.supported_providers.map((entry) => (
                    <option key={entry} value={entry}>
                      {entry}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Bucket">
                <input
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm"
                  value={bucket}
                  onChange={(event) => setBucket(event.target.value)}
                  placeholder="creatoros-media"
                />
              </Field>
              <Field label="Region">
                <input
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm"
                  value={region}
                  onChange={(event) => setRegion(event.target.value)}
                  placeholder="eu-west-3"
                />
              </Field>
              <Field label="Public base URL" hint="Only if the bucket is served through a CDN or custom domain.">
                <input
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm"
                  value={publicBase}
                  onChange={(event) => setPublicBase(event.target.value)}
                  placeholder="https://media.example.com"
                />
              </Field>
              <Field label="Endpoint URL" hint="For MinIO, R2, B2 or Wasabi.">
                <input
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm"
                  value={endpoint}
                  onChange={(event) => setEndpoint(event.target.value)}
                  placeholder="https://s3.eu-west-3.amazonaws.com"
                />
              </Field>
              <Field label="Object prefix">
                <input
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm"
                  value={prefix}
                  onChange={(event) => setPrefix(event.target.value)}
                />
              </Field>
              <Field
                label="Sealed credential id"
                hint="Seal the access key pair in the vault first; the id is what this API receives, never the key."
              >
                <input
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm"
                  value={secretId}
                  onChange={(event) => setSecretId(event.target.value)}
                  placeholder="01M8…"
                />
              </Field>
            </div>
            <div className="mt-3 flex items-center gap-2">
              <Button variant="primary" onClick={() => void save()} busy={busy} disabled={!bucket.trim() || !region.trim()}>
                Save configuration
              </Button>
              <span className="text-xs text-slate-500">
                {state.credentials_present ? 'Leaving the credential id empty keeps the sealed one.' : 'A sealed credential is required.'}
              </span>
            </div>
          </Card>

          <Card title="Objects in the bucket" subtitle="what was handed to the internet, and whether it is gone">
            {state.objects.length === 0 ? (
              <EmptyState title="Nothing has been delivered" hint="Instagram is the only surface that uses this." />
            ) : (
              <ul className="divide-y divide-slate-800">
                {state.objects.map((object) => (
                  <li key={object.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-xs">
                    <div className="min-w-0">
                      <p className="truncate font-mono text-slate-300">{object.object_key}</p>
                      <p className="text-slate-500">
                        {formatBytes(object.bytes)} · uploaded {formatWhen(object.uploaded_at)} · expires {formatWhen(object.expires_at)}
                      </p>
                    </div>
                    <Badge tone={object.deleted_at ? 'muted' : object.expired ? 'warn' : 'info'}>
                      {object.deleted_at ? 'deleted' : object.expired ? 'expired' : object.state}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
