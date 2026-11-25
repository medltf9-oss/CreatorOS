import { useCallback, useEffect, useState } from 'react';
import { type JobRow, type Platform, type PublishingApi, platformLabel } from '../../apiPublishing';
import { Badge, Button, Card, EmptyState, ErrorNote, Spinner, formatWhen } from '../../components/ui';

type Props = {
  api: PublishingApi;
  onOpenDestination: (variantId: string) => void;
};

const PLATFORMS: Platform[] = ['facebook', 'instagram', 'tiktok'];

export default function PublishedHistory({ api, onOpenDestination }: Props) {
  const [rows, setRows] = useState<JobRow[]>([]);
  const [total, setTotal] = useState(0);
  const [platform, setPlatform] = useState<Platform | 'all'>('all');
  const [search, setSearch] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const page = await api.published({
        platform: platform === 'all' ? undefined : platform,
        search: search || undefined,
        limit: 100,
      });
      setRows(page.items);
      setTotal(page.total);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [api, platform, search]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
            <p className="text-sm text-slate-400">
            Only destinations the provider confirmed. Each row links to the post and to the attempt that created it.
          </p>
        </div>
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
            placeholder="search captions"
          />
          <Button variant="ghost" onClick={() => void load()} busy={busy}>
            Refresh
          </Button>
        </div>
      </div>

      <ErrorNote error={error} onRetry={() => void load()} />

      <Card title={`Published (${total})`}>
        {busy && rows.length === 0 ? (
          <Spinner label="Reading published posts…" />
        ) : rows.length === 0 ? (
          <EmptyState title="Nothing published yet" hint="A destination appears here only after the provider confirmed it." />
        ) : (
          <ul className="divide-y divide-slate-800">
            {rows.map((row) => (
              <li key={row.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="text-sm text-slate-100">
                    {platformLabel(row.platform)} · {row.account_name ?? row.account_id}
                  </p>
                  <p className="truncate text-xs text-slate-500">{row.caption || '(no caption)'}</p>
                  <p className="mt-0.5 text-[11px] text-slate-600">
                    {formatWhen(row.provider_published_at ?? row.updated_at, row.display_timezone)}
                    {row.attempts ? ` · ${row.attempts} attempt(s)` : ''}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge tone="good">published</Badge>
                  {row.provider_permalink ? (
                    <a
                      className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-sky-400 hover:bg-slate-800"
                      href={row.provider_permalink}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      open post
                    </a>
                  ) : (
                    <span className="text-xs text-slate-600" title="The provider did not return a permalink for this surface">
                      no permalink
                    </span>
                  )}
                  <Button variant="ghost" onClick={() => onOpenDestination(row.variant_id)}>
                    History
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
