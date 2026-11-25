import { useCallback, useEffect, useState } from 'react';
import {
  type OAuthStart,
  type OAuthTransaction,
  type ProviderRoute,
  type SocialApi,
  platformLabel,
} from '../../apiSocial';
import { Badge, Button, Card, ErrorNote, Field, Row, Spinner } from '../../components/ui';

type Props = {
  api: SocialApi;
  onConnected: (accountId: string | null) => void;
  onOpenAppSettings: () => void;
};

type Step = 'choose' | 'authorising' | 'result';

const EXPLANATIONS: Record<string, string> = {
  facebook:
    'CreatorOS connects Facebook Pages only. A personal profile cannot be published to through the Graph API, so it is reported and skipped rather than pretending to work.',
  instagram:
    'Two login paths exist. Instagram Login needs no Facebook Page; the Facebook Login path finds the professional account linked to a Page. They are stored as separate accounts.',
  tiktok:
    'TikTok Login Kit requires PKCE. Direct Post additionally requires the Direct Post add-on on your developer app; login succeeding never implies you may post.',
};

export default function ConnectWizard({ api, onConnected, onOpenAppSettings }: Props) {
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [callbackRunning, setCallbackRunning] = useState(false);
  const [vaultAvailable, setVaultAvailable] = useState(false);
  const [step, setStep] = useState<Step>('choose');
  const [session, setSession] = useState<OAuthStart | null>(null);
  const [transaction, setTransaction] = useState<OAuthTransaction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await api.routes();
      setRoutes(response.items);
      setCallbackRunning(response.callback.running);
      setVaultAvailable(response.vault_available);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll the transaction while a browser tab is open. The listener completes it; this only watches.
  useEffect(() => {
    if (step !== 'authorising' || !session) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.transaction(session.session_id);
        if (cancelled) return;
        setTransaction(next);
        if (next.status !== 'pending') {
          setStep('result');
          window.clearInterval(timer);
          if (next.status === 'succeeded') {
            onConnected(next.accounts[0]?.id ?? null);
          }
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [api, onConnected, session, step]);

  async function begin(route: ProviderRoute) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const started = await api.startOAuth(route.platform, route.route);
      setSession(started);
      const opened = await window.creatoros?.openAuthUrl?.(started.authorization_url);
      if (!opened?.ok) {
        setNotice(
          `The shell refused to open the authorization page (${opened?.error ?? 'unknown reason'}). Copy the URL below into a browser.`,
        );
      }
      setStep('authorising');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStep('choose');
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!session) return;
    try {
      const next = await api.cancelTransaction(session.session_id);
      setTransaction(next);
      setStep('result');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function reset() {
    setStep('choose');
    setSession(null);
    setTransaction(null);
    setNotice(null);
  }

  return (
    <div className="space-y-5">
      <div>
        <p className="text-sm text-slate-400">
          CreatorOS opens your browser, you approve the permissions there, and the provider redirects back to a loopback address that
          only this machine can reach. No password ever reaches CreatorOS.
        </p>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      {!callbackRunning || !vaultAvailable ? (
        <Card title="Before you start">
          {!vaultAvailable ? (
            <p className="text-sm text-rose-400">
              The credential vault is not available, so nothing can be stored. This normally means the app is not running inside the
              CreatorOS desktop shell.
            </p>
          ) : null}
          {!callbackRunning ? (
            <p className="mt-2 text-sm text-amber-400">
              The loopback callback listener is not running. Authorization cannot complete until it is, because providers redirect to a
              fixed address.
            </p>
          ) : null}
        </Card>
      ) : null}

      {step === 'choose' ? (
        <div className="grid gap-4 md:grid-cols-2">
          {routes.map((route) => {
            const usable = route.configured;
            return (
              <Card key={`${route.platform}-${route.route}`} title={route.label}>
                <p className="text-xs text-slate-400">{EXPLANATIONS[route.platform]}</p>
                <div className="mt-3 space-y-1">
                  <Row label="Login path" value={route.route} />
                  <Row label="App registered" value={usable ? <Badge tone="good">yes</Badge> : <Badge tone="info">no</Badge>} />
                  <Row label="App mode" value={route.app_mode ?? '—'} />
                  <Row label="PKCE" value={route.pkce_required ? 'required (S256)' : 'not used by this provider'} />
                  <Row label="Scopes" value={`${route.scopes.length} requested`} />
                </div>
                <div className="mt-3">
                  <Button
                    variant={usable ? 'primary' : 'ghost'}
                    disabled={!usable || busy}
                    onClick={() => void begin(route)}
                    title={usable ? undefined : 'Register the developer app first (App settings)'}
                  >
                    {usable ? `Connect ${platformLabel(route.platform)}` : 'App not configured'}
                  </Button>
                  {!usable ? (
                    <button className="ml-2 text-xs text-sky-400 hover:underline" onClick={onOpenAppSettings}>
                      Open app settings
                    </button>
                  ) : null}
                </div>
                <p className="mt-3 text-[11px] text-slate-500">{route.notes}</p>
              </Card>
            );
          })}
        </div>
      ) : null}

      {step === 'authorising' && session ? (
        <Card title={`Waiting for ${session.label}`}>
          <div className="flex items-center gap-3">
            <Spinner label="Waiting for the browser to come back…" />
          </div>
          <div className="mt-3 space-y-1">
            <Row label="Requested scopes" value={session.scopes_requested.join(', ')} />
            <Row label="Redirect" value={session.redirect_uri} />
            <Row label="Expires" value={new Date(session.expires_at).toLocaleTimeString()} />
            <Row label="PKCE" value={session.pkce ?? 'not used'} />
            <Row label="App mode" value={session.app_mode} />
          </div>
          {notice ? <p className="mt-3 text-xs text-amber-400">{notice}</p> : null}
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-slate-500">Authorization URL (contains a one-time state value)</summary>
            <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-400">
              {session.authorization_url}
            </pre>
          </details>
          <p className="mt-3 text-xs text-slate-500">
            This attempt expires in 10 minutes and can be used once. If the browser was closed, cancel and start again.
          </p>
          <div className="mt-3">
            <Button variant="ghost" onClick={() => void cancel()}>
              Cancel this attempt
            </Button>
          </div>
        </Card>
      ) : null}

      {step === 'result' && transaction ? (
        <Card title={transaction.status === 'succeeded' ? 'Connected' : 'Not connected'}>
          <p className={`text-sm ${transaction.status === 'succeeded' ? 'text-emerald-400' : 'text-rose-400'}`}>
            {transaction.status === 'succeeded'
              ? `${transaction.accounts.length} account(s) connected.`
              : transaction.error_message ?? `The attempt ended as ${transaction.status}.`}
          </p>
          {transaction.error_code ? <p className="mt-1 font-mono text-xs text-slate-500">{transaction.error_code}</p> : null}

          {transaction.accounts.map((account) => (
            <div key={account.id} className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
              <p className="text-sm font-medium">{account.label}</p>
              <p className="text-xs text-slate-500">
                {platformLabel(account.platform)} · {account.connection_status.replace(/_/g, ' ')}
              </p>
              <div className="mt-2 grid grid-cols-2 gap-1 text-xs">
                {Object.entries(account.capabilities).map(([capability, state]) => (
                  <div key={capability} className="flex items-center justify-between gap-2">
                    <span className="truncate text-slate-400">{capability.split('.').slice(1).join(' ')}</span>
                    <Badge tone={state === 'available' ? 'good' : state === 'manual_only' ? 'info' : 'warn'}>{state.replace(/_/g, ' ')}</Badge>
                  </div>
                ))}
              </div>
            </div>
          ))}

          {transaction.unsupported.length ? (
            <p className="mt-3 text-xs text-amber-400">
              Skipped {transaction.unsupported.length} account(s) that this platform cannot publish to.
            </p>
          ) : null}

          <div className="mt-4 flex gap-2">
            <Button variant="primary" onClick={reset}>
              Connect another
            </Button>
            {transaction.accounts[0] ? (
              <Button variant="ghost" onClick={() => onConnected(transaction.accounts[0].id)}>
                Open the account
              </Button>
            ) : null}
          </div>
        </Card>
      ) : null}

      <Field label="What CreatorOS does with the credential">
        <p className="text-xs text-slate-500">
          The token is encrypted by Windows DPAPI in the Electron main process and stored under <code>config/secrets</code>. The backend
          receives it only through a loopback broker, one value at a time, and the interface you are looking at can never read it back.
        </p>
      </Field>
    </div>
  );
}
