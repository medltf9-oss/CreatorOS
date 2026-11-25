import { useCallback, useEffect, useState } from 'react';
import {
  type CatalogueAccount,
  type DestinationRow,
  type IntentSummary,
  type Platform,
  type PublishingApi,
  STATE_LABELS,
  modeLabel,
  platformLabel,
  stateTone,
} from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Row, Spinner, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
  projectId: string;
  onOpenDestination: (variantId: string) => void;
  onOpenComposer: () => void;
};

const PLATFORMS: Platform[] = ['facebook', 'instagram', 'tiktok'];

export default function IntentDetail({ api, projectId, onOpenDestination, onOpenComposer }: Props) {
  const [project, setProject] = useState<IntentSummary | null>(null);
  const [destinations, setDestinations] = useState<DestinationRow[]>([]);
  const [accounts, setAccounts] = useState<CatalogueAccount[]>([]);
  const [platform, setPlatform] = useState<Platform>('facebook');
  const [accountId, setAccountId] = useState('');
  const [caption, setCaption] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [detail, catalogue] = await Promise.all([api.intent(projectId), api.capabilities()]);
      setProject(detail.project);
      setDestinations(detail.destinations);
      setAccounts(catalogue.accounts);
      setAccountId((current) => current || catalogue.accounts[0]?.id || '');
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [api, projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async () => {
    setAdding(true);
    try {
      await api.addDestination(projectId, {
        platform,
        account_id: accountId,
        caption: caption.trim() || undefined,
      });
      setCaption('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAdding(false);
    }
  };

  const duplicate = async () => {
    try {
      const copy = await api.duplicateIntent(projectId);
      onOpenComposer();
      setError(`Created a new intent "${copy.project.title}" from this one.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const remove = async () => {
    try {
      await api.deleteIntent(projectId);
      onOpenComposer();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  if (!project) {
    return busy ? <Spinner label="Reading the intent…" /> : <ErrorNote error={error} onRetry={() => void load()} />;
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{project.title}</h1>
          <p className="text-sm text-slate-400">
            {project.destinations} destination(s) · {project.brief || 'no notes'} · status {project.status}
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => void duplicate()} title="Republish on purpose: creates a new intent and keeps this one as history">
            Duplicate
          </Button>
          <Button variant="danger" onClick={() => void remove()} title="Only an intent with no published destination can be deleted">
            Delete
          </Button>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      <Card title="Destinations">
        {destinations.length === 0 ? (
          <EmptyState title="No destination yet" hint="Add one per platform. Each needs its own approval." />
        ) : (
          <ul className="divide-y divide-slate-800">
            {destinations.map((destination) => (
              <li key={destination.id} className="py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm text-slate-100">
                      {platformLabel(destination.platform)} · {modeLabel(destination.mode)}
                      <span className="ml-2 text-xs text-slate-500">{destination.aspect_ratio}</span>
                    </p>
                    <p className="truncate text-xs text-slate-500">{destination.caption || '(no caption)'}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge tone={stateTone(destination.state)}>{STATE_LABELS[destination.state] ?? destination.state}</Badge>
                    {destination.scheduled_at_utc ? (
                      <span className="text-xs text-slate-500">{formatWhen(destination.scheduled_at_utc, destination.display_timezone)}</span>
                    ) : null}
                    <Button variant="ghost" onClick={() => onOpenDestination(destination.id)}>
                      Open
                    </Button>
                  </div>
                </div>
                {destination.validation?.findings?.length ? (
                  <ul className="mt-2 space-y-1">
                    {destination.validation.findings.slice(0, 4).map((finding, index) => (
                      <li key={`${finding.code}-${index}`} className="text-xs">
                        <Badge tone={finding.severity === 'failed' ? 'bad' : finding.severity === 'warning' ? 'warn' : 'info'}>
                          {finding.code}
                        </Badge>{' '}
                        <span className="text-slate-400">{finding.message}</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Add a destination" subtitle="Creates a draft. Nothing is queued and no approval is requested yet.">
        <div className="grid gap-3 md:grid-cols-4">
          <Field label="Platform">
            <select
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
              value={platform}
              onChange={(event) => setPlatform(event.target.value as Platform)}
            >
              {PLATFORMS.map((entry) => (
                <option key={entry} value={entry}>
                  {platformLabel(entry)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Account" hint={accounts.length === 0 ? 'connect an account first' : undefined}>
            <select
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
              value={accountId}
              onChange={(event) => setAccountId(event.target.value)}
              disabled={accounts.length === 0}
            >
              {accounts.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.display_name} ({platformLabel(entry.platform)})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Caption">
            <input
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
              value={caption}
              onChange={(event) => setCaption(event.target.value)}
              placeholder="Caption for this platform"
            />
          </Field>
          <div className="flex items-end">
            <Button
              variant="primary"
              onClick={() => void add()}
              busy={adding}
              disabled={!accountId}
              title={!accountId ? 'No connected account to publish with' : undefined}
            >
              Add destination
            </Button>
          </div>
        </div>
      </Card>

      <Card title="Facts">
        <Row label="Intent id" value={project.id} />
        <Row label="Source asset" value={project.source_asset_id ?? '—'} />
        <Row label="Campaign" value={project.campaign_id ?? '—'} />
        <Row label="Updated" value={formatWhen(project.updated_at)} />
      </Card>
    </div>
  );
}
