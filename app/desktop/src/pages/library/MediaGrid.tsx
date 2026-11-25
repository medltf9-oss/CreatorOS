import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError } from '../../api';
import {
  formatBytes,
  formatDuration,
  formatRelative,
  LibraryApi,
  type AssetQuery,
  type AssetSummary,
  type MediaKind,
} from '../../apiLibrary';
import { Badge, Button, Card, EmptyState, ErrorNote, Spinner } from '../../components/ui';

const KINDS: { id: MediaKind | ''; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'video', label: 'Video' },
  { id: 'audio', label: 'Audio' },
  { id: 'image', label: 'Images' },
];

const SORTS: { id: NonNullable<AssetQuery['sort']>; label: string }[] = [
  { id: 'created_at', label: 'Newest' },
  { id: 'name', label: 'Name' },
  { id: 'size', label: 'Size' },
  { id: 'duration', label: 'Duration' },
];

export default function MediaGrid({
  library,
  onOpenAsset,
  onImport,
}: {
  library: LibraryApi;
  onOpenAsset: (assetId: string) => void;
  onImport: () => void;
}) {
  const [items, setItems] = useState<AssetSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(24);
  const [offset, setOffset] = useState(0);
  const [kind, setKind] = useState<MediaKind | ''>('');
  const [sort, setSort] = useState<NonNullable<AssetQuery['sort']>>('created_at');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const page = await library.assets({ kind, sort, order, q: search, limit, offset });
      setItems(page.items);
      setTotal(page.total);
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [library, kind, sort, order, search, limit, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  const pages = Math.max(1, Math.ceil(total / limit));
  const pageIndex = Math.floor(offset / limit);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex rounded-lg border border-slate-700 p-0.5">
          {KINDS.map((entry) => (
            <button
              key={entry.id || 'all'}
              type="button"
              onClick={() => {
                setKind(entry.id);
                setOffset(0);
              }}
              className={`rounded-md px-3 py-1 text-sm ${
                kind === entry.id ? 'bg-slate-800 text-slate-100' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {entry.label}
            </button>
          ))}
        </div>

        <select
          value={sort}
          onChange={(event) => setSort(event.target.value as NonNullable<AssetQuery['sort']>)}
          className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200"
        >
          {SORTS.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label}
            </option>
          ))}
        </select>
        <Button variant="ghost" onClick={() => setOrder(order === 'asc' ? 'desc' : 'asc')}>
          {order === 'asc' ? '↑ ascending' : '↓ descending'}
        </Button>

        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(query.trim());
            setOffset(0);
          }}
        >
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search filename or tag"
            className="w-56 rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-200"
          />
          <Button type="submit">Search</Button>
        </form>

        <div className="ml-auto flex gap-2">
          <Button variant="primary" onClick={onImport}>
            Import
          </Button>
        </div>
      </div>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      <Card
        title={`${total} asset${total === 1 ? '' : 's'}`}
        subtitle="Paths are derived from content hashes; filenames never touch the filesystem layout."
        actions={
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
              prev
            </Button>
            <span className="font-mono">
              {pageIndex + 1}/{pages}
            </span>
            <Button
              variant="ghost"
              disabled={offset + limit >= total}
              onClick={() => setOffset(offset + limit)}
            >
              next
            </Button>
          </div>
        }
      >
        {loading && items.length === 0 ? <Spinner label="Loading media…" /> : null}
        {!loading && items.length === 0 ? (
          <EmptyState title="No media matches this view" hint="Import files or clear the filters." />
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {items.map((asset) => (
            <button
              key={asset.id}
              type="button"
              onClick={() => onOpenAsset(asset.id)}
              className="group overflow-hidden rounded-xl border border-slate-800 bg-slate-950/50 text-left transition hover:border-sky-600/60"
            >
              <div className="relative flex aspect-video items-center justify-center bg-black/60">
                {asset.thumbnail_url ? (
                  <img
                    src={library.mediaUrl(asset.thumbnail_url) ?? undefined}
                    alt=""
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                ) : asset.poster_url ? (
                  <img
                    src={library.mediaUrl(asset.poster_url) ?? undefined}
                    alt=""
                    className="h-full w-full object-contain"
                    loading="lazy"
                  />
                ) : (
                  <span className="font-mono text-xs uppercase tracking-widest text-slate-500">{asset.kind}</span>
                )}
                <span className={`absolute left-2 top-2 inline-flex items-center rounded-md px-2 py-0.5 font-mono text-[11px] ring-1 ring-white/20 ${asset.kind === 'video' ? 'bg-sky-600/90 text-white' : asset.kind === 'audio' ? 'bg-amber-600/90 text-white' : 'bg-black/70 text-white'}`}>
                  {asset.kind}
                </span>
                {asset.duration_ms ? (
                  <span className="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 font-mono text-[11px] text-white">
                    {formatDuration(asset.duration_ms)}
                  </span>
                ) : null}
              </div>
              <div className="space-y-1 p-3">
                <p className="truncate text-sm text-slate-200" title={asset.original_filename}>
                  {asset.original_filename}
                </p>
                <p className="text-xs text-slate-500">
                  {asset.width && asset.height ? `${asset.width}×${asset.height} · ` : ''}
                  {formatBytes(asset.size_bytes)} · {formatRelative(asset.created_at)}
                </p>
                <div className="flex flex-wrap gap-1 pt-1">
                  <Badge tone={asset.intake_state === 'ready' ? 'good' : 'warn'}>{asset.intake_state}</Badge>
                  <Badge tone={asset.rights_status === 'owned' ? 'good' : 'muted'}>{asset.rights_status}</Badge>
                  {asset.tags_json.slice(0, 2).map((tag) => (
                    <Badge key={tag}>{tag}</Badge>
                  ))}
                </div>
              </div>
            </button>
          ))}
        </div>

        <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
          <span>
            showing {items.length} of {total}
          </span>
          <select
            value={limit}
            onChange={(event) => {
              setLimit(Number(event.target.value));
              setOffset(0);
            }}
            className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-300"
          >
            {[12, 24, 48, 96].map((size) => (
              <option key={size} value={size}>
                {size} per page
              </option>
            ))}
          </select>
        </div>
      </Card>
    </div>
  );
}
