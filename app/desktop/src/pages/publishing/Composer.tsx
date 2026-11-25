import { useCallback, useEffect, useState } from 'react';
import {
  type IntentSummary,
  type Platform,
  type PublishingApi,
  STATE_LABELS,
  platformLabel,
} from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Spinner, formatAge } from '../../components/ui';

type Props = {
  api: PublishingApi;
  onOpenIntent: (id: string) => void;
};

const PLATFORMS: Platform[] = ['facebook', 'instagram', 'tiktok'];

export default function Composer({ api, onOpenIntent }: Props) {
  const [intents, setIntents] = useState<IntentSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [platform, setPlatform] = useState<Platform | 'all'>('all');
  const [title, setTitle] = useState('');
  const [assetId, setAssetId] = useState('');
  const [notes, setNotes] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const page = await api.intents({
        search: search || undefined,
        platform: platform === 'all' ? undefined : platform,
      });
      setIntents(page.items);
      setTotal(page.total);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [api, search, platform]);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    setCreating(true);
    try {
      const created = await api.createIntent({
        title: title.trim(),
        source_asset_id: assetId.trim() || null,
        notes: notes.trim() || null,
      });
      setTitle('');
      setNotes('');
      setAssetId('');
      await load();
      onOpenIntent(created.project.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <p className="text-sm text-slate-400">
          One intent is one piece of content. Each destination on it is a separate publication, with its own approval.
        </p>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      <Card title="New intent" subtitle="Nothing is published from here: a destination starts as a draft.">
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Title">
            <input
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Morning routine, 3 hooks"
            />
          </Field>
          <Field label="Media asset id" hint="From the content library. You can attach it later too.">
            <input
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm"
              value={assetId}
              onChange={(event) => setAssetId(event.target.value)}
              placeholder="01M8…"
            />
          </Field>
          <Field label="Notes">
            <input
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="What this is, for the person reviewing it"
            />
          </Field>
          <div className="flex items-end">
            <Button variant="primary" onClick={() => void create()} disabled={!title.trim()} busy={creating}>
              Create draft intent
            </Button>
          </div>
        </div>
      </Card>

      <Card
        title={`Intents (${total})`}
        actions={
          <div className="flex items-center gap-2">
            {(['all', ...PLATFORMS] as const).map((entry) => (
              <button
                key={entry}
                onClick={() => setPlatform(entry as Platform | 'all')}
                className={`rounded-full px-3 py-1 text-xs ${
                  platform === entry ? 'bg-slate-700 text-slate-100' : 'bg-slate-900 text-slate-400 hover:bg-slate-800'
                }`}
              >
                {entry === 'all' ? 'All' : platformLabel(entry as Platform)}
              </button>
            ))}
            <input
              className="w-40 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-xs"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="search titles"
            />
          </div>
        }
      >
        {loading && intents.length === 0 ? (
          <Spinner label="Reading intents…" />
        ) : intents.length === 0 ? (
          <EmptyState title="No intents yet" hint="Create one above, then add a destination for each platform." />
        ) : (
          <ul className="divide-y divide-slate-800">
            {intents.map((intent) => (
              <li key={intent.id} className="flex items-center justify-between gap-4 py-2">
                <div className="min-w-0">
                  <button className="truncate text-left text-sm text-slate-100 hover:underline" onClick={() => onOpenIntent(intent.id)}>
                    {intent.title}
                  </button>
                  <p className="text-xs text-slate-500">
                    {intent.destinations} destination(s) · {formatAge(intent.updated_at)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap justify-end gap-1">
                  {intent.states.length === 0 ? (
                    <Badge tone="muted">no destination</Badge>
                  ) : (
                    intent.states.map((state, index) => (
                      <Badge key={`${state}-${index}`} tone={state === 'published' ? 'good' : 'info'}>
                        {STATE_LABELS[state] ?? state}
                      </Badge>
                    ))
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
