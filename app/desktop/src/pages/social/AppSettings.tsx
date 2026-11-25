import { useCallback, useEffect, useState } from 'react';
import {
  type LoginRoute,
  type Platform,
  type ProviderApp,
  type ProviderRoute,
  type SocialApi,
  platformLabel,
} from '../../apiSocial';
import { Badge, Button, Card, ErrorNote, Field, Row, Spinner } from '../../components/ui';

type Props = {
  api: SocialApi;
  onConnect: () => void;
};

type Draft = {
  client_id: string;
  client_secret: string;
  redirect_uri: string;
  app_mode: string;
  api_version: string;
  notes: string;
};

const EMPTY: Draft = { client_id: '', client_secret: '', redirect_uri: '', app_mode: 'development', api_version: '', notes: '' };

export default function AppSettings({ api, onConnect }: Props) {
  const [apps, setApps] = useState<ProviderApp[]>([]);
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [selected, setSelected] = useState<{ platform: Platform; route: LoginRoute } | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [verify, setVerify] = useState<Awaited<ReturnType<SocialApi['verifyProviderApp']>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.providerApps();
      setApps(response.items);
      setRoutes(response.routes);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  function pick(platform: Platform, route: LoginRoute) {
    const existing = apps.find((entry) => entry.provider === platform && entry.route === route);
    setSelected({ platform, route });
    setVerify(null);
    setNotice(null);
    const routeSpec = routes.find((entry) => entry.platform === platform && entry.route === route);
    setDraft({
      client_id: existing?.client_id ?? '',
      client_secret: '',
      redirect_uri: existing?.redirect_uri ?? routeSpec?.redirect_uri ?? '',
      app_mode: existing?.app_mode ?? 'development',
      api_version: existing?.api_version ?? '',
      notes: existing?.notes ?? '',
    });
  }

  async function save() {
    if (!selected) return;
    setBusy('save');
    setNotice(null);
    setError(null);
    try {
      const body: Parameters<SocialApi['saveProviderApp']>[2] = {
        client_id: draft.client_id.trim(),
        redirect_uri: draft.redirect_uri.trim() || undefined,
        api_version: draft.api_version.trim() || undefined,
        app_mode: draft.app_mode,
        notes: draft.notes.trim() || undefined,
      };
      // The client secret goes to the vault, never to this JSON body.
      await api.saveProviderApp(selected.platform, selected.route, body);
      if (draft.client_secret.trim()) {
        const stored = await api.storeProviderSecret(selected.platform, selected.route, draft.client_secret.trim());
        if (!stored.ok) {
          setNotice(`The app was saved but the secret was not: ${stored.error ?? stored.code ?? 'unknown reason'}.`);
        } else {
          setNotice(`Saved. The client secret is sealed in the DPAPI vault and linked to this app.`);
        }
      } else {
        setNotice('Saved the app settings.');
      }
      setDraft((current) => ({ ...current, client_secret: '' }));
      const refreshed = await api.providerApps();
      setApps(refreshed.items);
      const saved = refreshed.items.find((entry) => entry.provider === selected.platform && entry.route === selected.route);
      if (saved) {
        // Show what the backend actually stored: it fills in the default redirect when none was typed.
        setDraft({
          client_id: saved.client_id,
          client_secret: '',
          redirect_uri: saved.redirect_uri,
          app_mode: saved.app_mode,
          api_version: saved.api_version ?? '',
          notes: saved.notes ?? '',
        });
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function runVerify() {
    if (!selected) return;
    setBusy('verify');
    try {
      setVerify(await api.verifyProviderApp(selected.platform, selected.route));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    if (!selected) return;
    setBusy('delete');
    try {
      await api.deleteProviderApp(selected.platform, selected.route);
      setDraft(EMPTY);
      setVerify(null);
      setNotice('The app registration was deleted and its secret removed from the vault.');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const configured = (platform: Platform, route: LoginRoute) => apps.find((entry) => entry.provider === platform && entry.route === route);

  return (
    <div className="space-y-5">
      <div>
        <p className="text-sm text-slate-400">
          CreatorOS publishes through official APIs, so each platform needs an app you created in that platform's developer console. The
          client secret is stored in the DPAPI vault; the client id and redirect are ordinary settings.
        </p>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />
      {notice ? <p className="rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-sm text-slate-300">{notice}</p> : null}

      <Card title="Login paths">
        <div className="space-y-2">
          {routes.map((route) => {
            const app = configured(route.platform, route.route);
            const active = selected?.platform === route.platform && selected?.route === route.route;
            return (
              <button
                key={`${route.platform}-${route.route}`}
                onClick={() => pick(route.platform, route.route)}
                className={`flex w-full items-center justify-between gap-4 rounded-lg border px-3 py-2 text-left ${
                  active ? 'border-sky-600 bg-sky-950/30' : 'border-slate-800 bg-slate-950/40 hover:border-slate-700'
                }`}
              >
                <div>
                  <p className="text-sm text-slate-100">{route.label}</p>
                  <p className="text-xs text-slate-500">
                    {route.route} · {route.scopes.length} scopes{route.extra_scopes?.length ? ` + ${route.extra_scopes.length} optional` : ''}
                    {route.pkce_required ? ' · PKCE' : ''}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {app ? <Badge tone={app.app_mode === 'live' ? 'good' : 'warn'}>{app.app_mode}</Badge> : <Badge tone="info">not configured</Badge>}
                  {app?.has_client_secret ? <Badge tone="good">secret stored</Badge> : <Badge tone="muted">no secret</Badge>}
                </div>
              </button>
            );
          })}
        </div>
      </Card>

      {selected ? (
        <Card
          title={`${platformLabel(selected.platform)} · ${selected.route}`}
          subtitle="The redirect must match a URI registered in the platform console, character for character."
        >
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Client ID" hint="Public identifier from the developer console.">
              <input
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100"
                value={draft.client_id}
                onChange={(event) => setDraft({ ...draft, client_id: event.target.value })}
                placeholder="1234567890"
              />
            </Field>
            <Field label="Client secret" hint="Handed to the DPAPI vault by the desktop shell; it is never sent to the backend.">
              <input
                type="password"
                autoComplete="off"
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100"
                value={draft.client_secret}
                onChange={(event) => setDraft({ ...draft, client_secret: event.target.value })}
                placeholder={configured(selected.platform, selected.route)?.has_client_secret ? 'stored — leave empty to keep' : 'paste once'}
              />
            </Field>
            <Field label="Redirect URI" hint="Loopback only. Providers compare this byte for byte.">
              <input
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100"
                value={draft.redirect_uri}
                onChange={(event) => setDraft({ ...draft, redirect_uri: event.target.value })}
              />
            </Field>
            <Field label="App mode" hint="Live publishing usually needs app review first; development mode is stated everywhere it matters.">
              <select
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
                value={draft.app_mode}
                onChange={(event) => setDraft({ ...draft, app_mode: event.target.value })}
              >
                <option value="development">development</option>
                <option value="live">live</option>
              </select>
            </Field>
            <Field label="API version" hint="Leave empty to use the version CreatorOS pins.">
              <input
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100"
                value={draft.api_version}
                onChange={(event) => setDraft({ ...draft, api_version: event.target.value })}
                placeholder="v23.0 / v2"
              />
            </Field>
            <Field label="Notes" hint="Optional: which console app this is.">
              <input
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100"
                value={draft.notes}
                onChange={(event) => setDraft({ ...draft, notes: event.target.value })}
              />
            </Field>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            <Button variant="primary" busy={busy === 'save'} onClick={() => void save()} disabled={!draft.client_id.trim()}>
              Save app
            </Button>
            <Button busy={busy === 'verify'} onClick={() => void runVerify()} disabled={!configured(selected.platform, selected.route)}>
              Verify configuration
            </Button>
            {configured(selected.platform, selected.route) ? (
              <>
                <Button variant="ghost" onClick={onConnect}>
                  Connect an account
                </Button>
                <Button variant="danger" busy={busy === 'delete'} onClick={() => void remove()}>
                  Delete app
                </Button>
              </>
            ) : null}
          </div>

          {verify ? (
            <div className="mt-4 space-y-1 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
              {verify.checks.map((check) => (
                <div key={check.name} className="flex items-start justify-between gap-3 text-xs">
                  <span className="text-slate-400">{check.name}</span>
                  <span className={check.ok === true ? 'text-emerald-400' : check.ok === false ? 'text-rose-400' : 'text-slate-500'}>
                    {check.ok === null ? 'unknown' : check.ok ? 'ok' : 'problem'} · {check.detail}
                  </span>
                </div>
              ))}
              {verify.problems.length ? (
                <ul className="mt-2 space-y-1 text-xs text-amber-400">
                  {verify.problems.map((problem) => (
                    <li key={problem}>· {problem}</li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-xs text-emerald-400">No problem detected. Verification checks configuration only; it does not contact the provider.</p>
              )}
              <a className="mt-2 inline-block text-xs text-sky-400 hover:underline" href={verify.docs_url} target="_blank" rel="noreferrer">
                provider documentation
              </a>
            </div>
          ) : null}

          <div className="mt-4">
            <Row label="Secret in vault" value={configured(selected.platform, selected.route)?.has_client_secret ? 'yes' : 'no'} />
            <Row label="Secret status" value={configured(selected.platform, selected.route)?.client_secret_status ?? '—'} />
            <Row
              label="Last verified"
              value={
                configured(selected.platform, selected.route)?.last_verified_at
                  ? new Date(configured(selected.platform, selected.route)!.last_verified_at!).toLocaleString()
                  : 'never'
              }
            />
            <Row label="Requires app review for live" value={configured(selected.platform, selected.route)?.requires_app_review_for_live ? 'yes' : 'no'} />
          </div>

          <p className="mt-3 text-xs text-slate-500">
            CreatorOS never asks for a social password, never automates a browser session and never scrapes. If a permission is not
            granted, the matching capability is reported unavailable rather than worked around.
          </p>
        </Card>
      ) : (
        <Spinner label="Choose a login path above…" />
      )}
    </div>
  );
}
