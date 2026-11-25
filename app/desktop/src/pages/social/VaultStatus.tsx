import { useCallback, useEffect, useState } from 'react';
import { type RotationRow, type SocialApi, type VaultHealth, platformLabel } from '../../apiSocial';
import { Badge, Button, Card, EmptyState, ErrorNote, Row, Spinner } from '../../components/ui';

type Props = {
  api: SocialApi;
  onOpenAccount: (id: string) => void;
};

export default function VaultStatus({ api, onOpenAccount }: Props) {
  const [health, setHealth] = useState<VaultHealth | null>(null);
  const [rotations, setRotations] = useState<{ items: RotationRow[]; pending: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextHealth, nextRotations] = await Promise.all([api.vaultHealth(), api.rotations()]);
      setHealth(nextHealth);
      setRotations(nextRotations);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function resolve(accountId: string) {
    setBusy(accountId);
    setNotice(null);
    try {
      const result = await api.resolveRotations(accountId);
      setNotice(
        result.resolved.length === 0
          ? 'No interrupted rotation was found for that account.'
          : result.resolved.map((entry) => entry.outcome).join(' '),
      );
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between">
        <div>
            <p className="text-sm text-slate-400">
            Windows DPAPI, user scope, called only from the Electron main process. The backend asks a loopback broker for one secret at a
            time and never holds the key.
          </p>
        </div>
        <Button onClick={() => void load()} busy={busy === 'load'}>
          Refresh
        </Button>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />
      {notice ? <p className="rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-sm text-slate-300">{notice}</p> : null}

      {!health ? (
        <Spinner label="Asking the vault…" />
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            <Card title="Availability">
              <p className={`text-sm font-medium ${health.available ? 'text-emerald-400' : 'text-rose-400'}`}>
                {health.available ? 'available' : 'unavailable'}
              </p>
              <p className="mt-1 text-xs text-slate-500">{health.reason ?? health.protection}</p>
            </Card>
            <Card title="Backend">
              <Row label="Implementation" value={health.backend} mono />
              <Row label="Algorithm" value={health.alg ?? '—'} mono />
              <Row label="Record schema" value={health.schema_version ?? '—'} />
              <Row label="Records" value={health.entries ?? 0} />
              <Row label="Directory" value={health.directory ?? "not reported"} mono />
              <Row label="Corrupt" value={health.corrupt ?? 0} tone={(health.corrupt ?? 0) > 0 ? 'bad' : 'good'} />
            </Card>
            <Card title="Operations">
              <Row label="Calls served" value={health.operations ?? 0} />
              <Row label="Failures" value={health.failures ?? 0} tone={(health.failures ?? 0) > 0 ? 'warn' : 'good'} />
              <Row label="Re-encrypted" value={health.reencrypted ?? 0} />
              <Row label="Last operation" value={health.last_operation ?? '—'} mono />
            </Card>
          </div>

          <Card title="What DPAPI actually protects" subtitle="Stated plainly, because a vault that overclaims is worse than none.">
            <p className="text-sm text-slate-300">{health.protection}</p>
            <ul className="mt-2 space-y-1 text-xs text-slate-500">
              <li>· Another Windows user on this machine cannot decrypt these records.</li>
              <li>· Copying <code>config/secrets</code> to another machine produces unreadable files.</li>
              <li>· Malware running as you can ask DPAPI to decrypt, exactly as CreatorOS does. This is not isolation from your own session.</li>
              <li>· Losing your Windows profile means losing the credentials: reconnect the accounts.</li>
            </ul>
          </Card>

          <Card title="Secret metadata" subtitle="Counts only: no value is exposed anywhere in the interface.">
            {health.secrets ? (
              <div className="grid gap-2 md:grid-cols-4">
                <Row label="Total" value={health.secrets.total} />
                <Row label="Active" value={health.secrets.active} tone="good" />
                <Row label="Unavailable" value={health.secrets.unavailable} tone={health.secrets.unavailable ? 'bad' : 'normal'} />
                <Row label="Revoked" value={health.secrets.revoked} />
                {Object.entries(health.secrets.by_status).map(([status, count]) => (
                  <Row key={status} label={status} value={count} />
                ))}
              </div>
            ) : (
              <p className="text-sm text-slate-500">The vault did not report secret metadata.</p>
            )}
          </Card>

          <Card title="Refresh journal" subtitle="A refresh writes its intent before it calls the provider, so an interrupted rotation is recoverable.">
            {rotations && rotations.items.length === 0 ? (
              <EmptyState title="No refresh has run yet" hint="Rotations appear here the first time a refresh token is renewed." />
            ) : null}
            {rotations?.pending ? (
              <p className="mb-2 text-xs text-amber-400">{rotations.pending} rotation(s) are still open.</p>
            ) : null}
            <div className="space-y-2">
              {rotations?.items.map((rotation) => (
                <div key={rotation.id} className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                  <div className="min-w-0">
                    <p className="text-sm text-slate-200">
                      {platformLabel(rotation.provider)} · attempt {rotation.attempts}
                    </p>
                    <p className="font-mono text-[11px] text-slate-500">
                      {new Date(rotation.started_at).toLocaleString()}
                      {rotation.error_code ? ` · ${rotation.error_code}` : ''}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge tone={rotation.state === 'completed' ? 'good' : rotation.state === 'failed' ? 'bad' : 'warn'}>{rotation.state}</Badge>
                    {rotation.state === 'in_progress' || rotation.state === 'pending' ? (
                      <Button variant="ghost" busy={busy === rotation.account_id} onClick={() => void resolve(rotation.account_id)}>
                        Resolve
                      </Button>
                    ) : (
                      <Button variant="ghost" onClick={() => onOpenAccount(rotation.account_id)}>
                        Account
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </Card>

          <Card title="Callback listener">
            <Row label="Running" value={health.callbacks?.running ? 'yes' : 'no'} tone={health.callbacks?.running ? 'good' : 'bad'} />
            <Row label="Port" value={health.callbacks?.port ?? '—'} />
            <Row label="Base URL" value={health.callbacks?.url_base ?? '—'} mono />
            <p className="mt-2 text-xs text-slate-500">
              The listener binds 127.0.0.1 only and answers one path. If it is not running, authorization cannot complete: providers
              redirect to this exact address.
            </p>
          </Card>
        </>
      )}
    </div>
  );
}
