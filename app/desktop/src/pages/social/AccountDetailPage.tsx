import { useCallback, useEffect, useState } from 'react';
import {
  type AccountDetail,
  type CapabilityRow,
  type SocialApi,
  capabilityTone,
  platformLabel,
  routeLabel,
  tokenTone,
} from '../../apiSocial';
import { Badge, Button, Card, ErrorNote, Row, Spinner } from '../../components/ui';

type Props = {
  api: SocialApi;
  accountId: string;
  onBack: () => void;
  onConnect: () => void;
};

type Diagnostics = Awaited<ReturnType<SocialApi['diagnostics']>>;

export default function AccountDetailPage({ api, accountId, onBack, onConnect }: Props) {
  const [detail, setDetail] = useState<AccountDetail | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [label, setLabel] = useState('');
  const [group, setGroup] = useState('');

  const load = useCallback(async () => {
    try {
      const next = await api.account(accountId);
      setDetail(next);
      setLabel(next.label);
      setGroup(next.group ?? '');
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api, accountId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(name: string, action: () => Promise<unknown>, success: string) {
    setBusy(name);
    setNotice(null);
    setError(null);
    try {
      await action();
      setNotice(success);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  if (!detail) {
    return (
      <div className="space-y-4">
        <Button variant="ghost" onClick={onBack}>
          ← Back to accounts
        </Button>
        <ErrorNote error={error} onRetry={() => void load()} />
        {busy === null && !error ? <Spinner label="Reading the account…" /> : null}
      </div>
    );
  }

  const available = Object.entries(detail.capabilities).filter(([, state]) => state === 'available');
  const blocked = Object.entries(detail.capabilities).filter(([, state]) => state !== 'available');

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <button className="text-xs text-slate-500 hover:text-slate-300" onClick={onBack}>
            ← Back to accounts
          </button>
          <h1 className="mt-1 text-lg font-semibold">{detail.label}</h1>
          <p className="text-sm text-slate-400">
            {platformLabel(detail.platform)} · {routeLabel(detail.route)} · {detail.account_type.replace(/_/g, ' ')}
            {detail.username ? ` · @${detail.username}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={detail.connection_status === 'connected' ? 'good' : 'warn'}>
            {detail.connection_status.replace(/_/g, ' ')}
          </Badge>
          <Badge tone={tokenTone(detail.token_state)}>{detail.token_state.replace(/_/g, ' ')}</Badge>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />
      {notice ? <p className="rounded-lg border border-emerald-800 bg-emerald-950/40 px-3 py-2 text-sm text-emerald-300">{notice}</p> : null}

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Connection">
          <Row label="Internal id" value={detail.id} mono />
          <Row label="Provider account id" value={detail.provider_account_id} mono />
          <Row label="Login path" value={routeLabel(detail.route)} />
          <Row label="API version" value={detail.api_version ?? '—'} />
          <Row label="Last verified" value={detail.last_verified_at ? new Date(detail.last_verified_at).toLocaleString() : 'never'} />
          <Row label="Last refresh" value={detail.last_refresh_at ? new Date(detail.last_refresh_at).toLocaleString() : 'never'} />
          {detail.disconnected_at ? (
            <Row label="Disconnected" value={new Date(detail.disconnected_at).toLocaleString()} tone="bad" />
          ) : null}
        </Card>

        <Card title="Token lifecycle">
          <Row label="State" value={<Badge tone={tokenTone(detail.token_state)}>{detail.token_state.replace(/_/g, ' ')}</Badge>} />
          <Row label="Access expires" value={detail.token?.expires_at ? new Date(detail.token.expires_at).toLocaleString() : 'no expiry recorded'} />
          <Row
            label="Refresh expires"
            value={detail.token?.refresh_expires_at ? new Date(detail.token.refresh_expires_at).toLocaleString() : 'no refresh token'}
          />
          <Row label="Can refresh" value={detail.token?.can_refresh ? 'yes' : 'no'} tone={detail.token?.can_refresh ? 'good' : 'warn'} />
          {detail.token?.reason ? <p className="mt-2 text-xs text-amber-400">{detail.token.reason}</p> : null}
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              busy={busy === 'refresh'}
              disabled={!detail.token?.can_refresh}
              onClick={() =>
                void act('refresh', () => api.refreshToken(detail.id), 'The token was refreshed through the provider.')
              }
              title={detail.token?.can_refresh ? undefined : 'this account has no refresh token'}
            >
              Refresh token
            </Button>
            <Button busy={busy === 'verify'} onClick={() => void act('verify', () => api.verify(detail.id), 'The account was re-read from the provider.')}>
              Verify now
            </Button>
            <Button
              busy={busy === 'capabilities'}
              onClick={() =>
                void act('capabilities', () => api.refreshCapabilities(detail.id), 'Capabilities were re-evaluated from stored facts.')
              }
            >
              Re-evaluate capabilities
            </Button>
          </div>
        </Card>
      </div>

      <Card title="Credentials in the vault" subtitle="Only metadata is shown: the value cannot be read back through this interface.">
        {detail.secret_health.length === 0 ? (
          <p className="text-sm text-slate-500">No credential reference is attached to this account.</p>
        ) : (
          <div className="space-y-2">
            {detail.secret_health.map((secret) => (
              <div key={secret.kind} className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                <div>
                  <p className="text-sm text-slate-200">{secret.kind.replace(/_/g, ' ')}</p>
                  <p className="font-mono text-[11px] text-slate-500">
                    fingerprint {secret.fingerprint ?? '—'} · {secret.bytes ?? 0} bytes
                  </p>
                </div>
                <Badge tone={secret.present && secret.status === 'active' ? 'good' : 'bad'}>
                  {secret.present ? secret.status : (secret.code ?? 'missing')}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Capabilities" subtitle="Each state comes from stored evidence: scopes actually granted, app mode and token state.">
        <div className="grid gap-3 md:grid-cols-2">
          {detail.capabilities_detail.map((capability) => (
            <CapabilityCard key={capability.capability} capability={capability} />
          ))}
        </div>
        {available.length === 0 ? (
          <p className="mt-3 text-xs text-amber-400">No capability is available yet. The list above says why for each one.</p>
        ) : null}
        {blocked.length > 0 ? (
          <p className="mt-1 text-xs text-slate-500">{blocked.length} capability(ies) are not available; the reason is on each card.</p>
        ) : null}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Label and group" subtitle="Yours to set; a reconnect never overwrites them.">
          <div className="space-y-3">
            <label className="block text-xs text-slate-400">
              Label
              <input
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                maxLength={200}
              />
            </label>
            <label className="block text-xs text-slate-400">
              Group
              <input
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
                value={group}
                onChange={(event) => setGroup(event.target.value)}
                maxLength={120}
                placeholder="client name, brand, campaign…"
              />
            </label>
            <Button
              busy={busy === 'label'}
              onClick={() => void act('label', () => api.rename(detail.id, label || null, group || null), 'The label was saved.')}
            >
              Save label
            </Button>
          </div>
        </Card>

        <Card title="Permissions granted">
          <Row label="Scope source" value={detail.scope_source ?? 'unknown'} />
          <div className="mt-2 flex flex-wrap gap-1">
            {detail.scopes.length === 0 ? (
              <span className="text-sm text-slate-500">no scope recorded</span>
            ) : (
              detail.scopes.map((scope) => (
                <Badge key={scope} tone="muted">
                  {scope}
                </Badge>
              ))
            )}
          </div>
          <p className="mt-3 text-xs text-slate-500">
            {detail.scope_source === 'consent_request'
              ? 'This provider does not return the granted scope list; the requested scopes are recorded as the evidence, which is stated wherever they are used.'
              : 'Scopes come from the provider token response or debug output.'}
          </p>
        </Card>
      </div>

      <Card
        title="Diagnostics"
        subtitle="A redacted report of what the provider answered, fetched on demand."
        actions={
          <Button
            busy={busy === 'diagnostics'}
            onClick={() =>
              void act(
                'diagnostics',
                async () => {
                  const report = await api.diagnostics(detail.id);
                  setDiagnostics(report);
                },
                'Diagnostics collected.',
              )
            }
          >
            Run diagnostics
          </Button>
        }
      >
        {diagnostics ? (
          <pre className="max-h-72 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-400">
            {JSON.stringify(diagnostics, null, 2)}
          </pre>
        ) : (
          <p className="text-xs text-slate-500">Not run yet. Nothing is cached: it asks the provider each time.</p>
        )}
      </Card>

      <Card title="Disconnect">
        <p className="text-sm text-slate-400">
          Disconnecting revokes the credential at the provider where an endpoint exists, deletes the encrypted record, and marks the
          account disconnected. The row stays so its history remains readable.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            variant="danger"
            busy={busy === 'disconnect'}
            onClick={() => void act('disconnect', () => api.disconnect(detail.id, true), 'The account was disconnected.')}
          >
            Disconnect and revoke
          </Button>
          <Button
            variant="ghost"
            busy={busy === 'disconnect-local'}
            onClick={() => void act('disconnect-local', () => api.disconnect(detail.id, false), 'The local credential was deleted.')}
          >
            Delete local credential only
          </Button>
          {detail.connection_status === 'disconnected' ? (
            <>
              <Button variant="ghost" onClick={onConnect}>
                Reconnect
              </Button>
              <Button
                variant="danger"
                busy={busy === 'delete'}
                onClick={() => void act('delete', () => api.deleteAccount(detail.id), 'The account row was deleted.')}
              >
                Delete the row
              </Button>
            </>
          ) : null}
        </div>
      </Card>
    </div>
  );
}

function CapabilityCard({ capability }: { capability: CapabilityRow }) {
  const tone = capabilityTone(capability.state);
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium text-slate-100">{capability.capability}</p>
        <Badge tone={tone}>{capability.state.replace(/_/g, ' ')}</Badge>
      </div>
      <p className="mt-1 text-xs text-slate-400">{capability.reason ?? 'no reason recorded'}</p>
      {capability.missing_scopes.length ? (
        <p className="mt-1 text-[11px] text-amber-400">missing: {capability.missing_scopes.join(', ')}</p>
      ) : null}
      <p className="mt-1 font-mono text-[10px] text-slate-600">
        {capability.verified_at ? `verified ${new Date(capability.verified_at).toLocaleString()}` : 'never verified'}
        {capability.api_version ? ` · ${capability.api_version}` : ''}
      </p>
    </div>
  );
}
