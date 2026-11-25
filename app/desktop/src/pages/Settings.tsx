import { useEffect, useState } from 'react';
import type { CreatorApi, Limits, Profile } from '../api';
import { ApiRequestError } from '../api';
import { Badge, Button, Card, Empty, ErrorNote, Field, Row } from '../components/ui';

type Props = { api: CreatorApi };

function errText(err: unknown): string {
  return err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err);
}

const limitFields: { key: keyof Limits; label: string; hint: string; step?: number; min?: number; max?: number }[] = [
  { key: 'max_concurrent_generations', label: 'Concurrent generations', hint: '1 is the safe default on a 4 GB card', min: 1, max: 4 },
  { key: 'max_ram_percent', label: 'Max RAM percent', hint: 'admission is refused above this', min: 50, max: 98 },
  { key: 'min_free_ram_gb', label: 'Min free RAM (GB)', hint: 'keep the desktop responsive', step: 0.5, min: 0.5, max: 32 },
  { key: 'max_vram_percent', label: 'Max VRAM percent', hint: 'leave headroom for the display', min: 50, max: 100 },
  { key: 'max_queue_depth', label: 'Queue depth cap', hint: 'jobs beyond this are rejected', min: 1, max: 500 },
  { key: 'generation_timeout_s', label: 'Generation timeout (s)', hint: 'cancels runaway generations', min: 10, max: 1800 },
];

export default function Settings({ api }: Props) {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [draft, setDraft] = useState<Limits | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setError(null);
    try {
      const [limitsData, profileData] = await Promise.all([api.limits(), api.profiles()]);
      setLimits(limitsData);
      setDraft(limitsData);
      setProfiles(profileData);
    } catch (err) {
      setError(errText(err));
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  const saveLimits = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.setLimits(draft);
      setLimits(updated);
      setDraft(updated);
      setNotice('Limits saved and applied immediately.');
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  const saveProfile = async (name: string, patch: { ollama_tag?: string; context_len?: number; temperature?: number }) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.updateProfile(name, patch);
      setProfiles((prev) => prev.map((item) => (item.name === name ? updated : item)));
      setNotice(`Profile ${name} updated.`);
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  const dirty = limits && draft ? JSON.stringify(limits) !== JSON.stringify(draft) : false;

  return (
    <div className="space-y-5">
      <ErrorNote error={error} onRetry={() => void load()} />
      {notice ? (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">{notice}</div>
      ) : null}

      <Card
        title="Resource limits"
        subtitle="Validated server-side; out-of-range values are rejected instead of silently clamped."
        actions={
          <div className="flex gap-2">
            <Button variant="ghost" onClick={() => setDraft(limits)} disabled={!dirty || busy}>
              reset
            </Button>
            <Button variant="primary" onClick={() => void saveLimits()} busy={busy} disabled={!dirty}>
              save
            </Button>
          </div>
        }
      >
        {!draft ? (
          <Empty>Loading limits…</Empty>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {limitFields.map((field) => (
              <Field key={field.key} label={field.label} hint={field.hint}>
                <input
                  type="number"
                  min={field.min}
                  max={field.max}
                  step={field.step ?? 1}
                  value={String(draft[field.key])}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      [field.key]: field.key === 'allow_concurrent_models' ? event.target.value === 'true' : Number(event.target.value),
                    })
                  }
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
                />
              </Field>
            ))}
            <Field label="Default profile" hint="used when no profile is requested">
              <select
                value={draft.default_profile}
                onChange={(event) => setDraft({ ...draft, default_profile: event.target.value })}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-500"
              >
                {profiles.map((profile) => (
                  <option key={profile.name} value={profile.name}>
                    {profile.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        )}
        <p className="mt-3 text-xs text-slate-500">
          Concurrency never increases on its own. Raising it above 1 on this GPU is not recommended; the card has 4 GB and the
          models compete for it.
        </p>
      </Card>

      <Card title="Model profiles" subtitle="Each profile maps a role to an installed model tag.">
        {profiles.length === 0 ? (
          <Empty>No profiles configured.</Empty>
        ) : (
          <div className="space-y-2">
            {profiles.map((profile) => (
              <ProfileRow key={profile.name} profile={profile} busy={busy} onSave={saveProfile} />
            ))}
          </div>
        )}
      </Card>

      <Card title="Safety posture" subtitle="These are product decisions, not settings.">
        <Row label="External AI providers" value="disabled — no cloud client is bundled" />
        <Row label="Model downloads" value="explicit, user-initiated only" />
        <Row label="Social platform access" value="official OAuth only, never passwords" />
        <Row label="Fake data" value="none — every number on screen comes from the API" />
      </Card>
    </div>
  );
}

function ProfileRow({
  profile,
  busy,
  onSave,
}: {
  profile: Profile;
  busy: boolean;
  onSave: (name: string, patch: { ollama_tag?: string; context_len?: number; temperature?: number }) => Promise<void>;
}) {
  const [tag, setTag] = useState(profile.ollama_tag);
  const [context, setContext] = useState(String(profile.context_len));
  const [temperature, setTemperature] = useState(String(profile.temperature));

  useEffect(() => {
    setTag(profile.ollama_tag);
    setContext(String(profile.context_len));
    setTemperature(String(profile.temperature));
  }, [profile]);

  const changed =
    tag !== profile.ollama_tag || Number(context) !== profile.context_len || Number(temperature) !== profile.temperature;

  return (
    <div className="rounded-lg border border-slate-800 px-3 py-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm text-slate-100">{profile.name}</span>
          {profile.fallback_profile ? <Badge tone="muted">fallback → {profile.fallback_profile}</Badge> : null}
        </div>
        <div className="flex gap-1.5">
          {profile.roles_json.slice(0, 4).map((role) => (
            <Badge key={role}>{role}</Badge>
          ))}
        </div>
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-[minmax(0,2fr)_repeat(2,minmax(0,1fr))_auto]">
        <input
          value={tag}
          onChange={(event) => setTag(event.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
        />
        <input
          type="number"
          value={context}
          onChange={(event) => setContext(event.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
        />
        <input
          type="number"
          step={0.1}
          value={temperature}
          onChange={(event) => setTemperature(event.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
        />
        <Button
          onClick={() =>
            void onSave(profile.name, {
              ollama_tag: tag,
              context_len: Number(context),
              temperature: Number(temperature),
            })
          }
          disabled={!changed || busy}
        >
          save
        </Button>
      </div>
    </div>
  );
}
