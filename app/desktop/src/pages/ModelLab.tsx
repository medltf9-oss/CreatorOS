import { useCallback, useEffect, useRef, useState } from 'react';
import type { BenchmarkResult, CreatorApi, ModelCard, ModelDetail, PullStatus } from '../api';
import { ApiRequestError } from '../api';
import { Badge, Button, Card, Empty, ErrorNote, Field, Progress, Row, formatAge, formatBytes } from '../components/ui';

type Props = { api: CreatorApi };

function errText(err: unknown): string {
  return err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err);
}

export default function ModelLab({ api }: Props) {
  const [models, setModels] = useState<ModelCard[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ModelDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [benchmarks, setBenchmarks] = useState<Record<string, BenchmarkResult>>({});
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [pullTag, setPullTag] = useState('');
  const [pull, setPull] = useState<PullStatus | null>(null);
  const pollTimer = useRef<number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await api.models();
      setModels(data.items);
    } catch (err) {
      setError(errText(err));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailError(null);
    api
      .model(selected)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) setDetailError(errText(err));
      });
    return () => {
      cancelled = true;
    };
  }, [api, selected]);

  useEffect(() => {
    return () => {
      if (pollTimer.current) window.clearInterval(pollTimer.current);
    };
  }, []);

  const trackPull = (started: PullStatus) => {
    setPull(started);
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    pollTimer.current = window.setInterval(async () => {
      try {
        const list = await api.pulls();
        const current = list.find((item) => item.pull_id === started.pull_id);
        if (!current) return;
        setPull(current);
        if (current.state !== 'running') {
          if (pollTimer.current) window.clearInterval(pollTimer.current);
          pollTimer.current = null;
          await load();
          if (current.state === 'succeeded') setNotice(`${current.model} pulled and ready to use.`);
          if (current.state === 'failed') setError(`${current.model}: ${current.error ?? 'pull failed'}`);
        }
      } catch (err) {
        setError(errText(err));
      }
    }, 1000);
  };

  const startPull = async () => {
    const tag = pullTag.trim();
    if (!tag) return;
    setError(null);
    setNotice(null);
    setBusy('pull');
    try {
      const started = await api.pullModel(tag);
      trackPull(started);
      setPullTag('');
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(null);
    }
  };

  const cancelPull = async () => {
    if (!pull) return;
    try {
      setPull(await api.cancelPull(pull.pull_id));
      if (pollTimer.current) window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    } catch (err) {
      setError(errText(err));
    }
  };

  const unload = async (tag: string) => {
    setBusy(`unload:${tag}`);
    setError(null);
    try {
      const result = await api.unloadModel(tag);
      setNotice(result.unloaded ? `${tag} unloaded from memory.` : `${tag} is still resident; try again.`);
      await load();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (tag: string, force: boolean) => {
    setBusy(`delete:${tag}`);
    setError(null);
    try {
      await api.deleteModel(tag, force);
      setNotice(`${tag} deleted from disk.`);
      setConfirmDelete(null);
      if (selected === tag) setSelected(null);
      await load();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(null);
    }
  };

  const benchmark = async (tag: string) => {
    setBusy(`bench:${tag}`);
    setError(null);
    setNotice(null);
    try {
      const result = await api.benchmark(tag);
      setBenchmarks((prev) => ({ ...prev, [tag]: result }));
      await load();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-5">
      <ErrorNote error={error} onRetry={() => void load()} />
      {notice ? (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">{notice}</div>
      ) : null}

      <Card
        title="Pull a model"
        subtitle="Progress is streamed from Ollama; the tag must exist in the Ollama library."
        actions={
          pull?.state === 'running' ? (
            <Button variant="ghost" onClick={() => void cancelPull()}>
              cancel pull
            </Button>
          ) : null
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[260px] flex-1">
            <Field label="Model tag" hint="Example: qwen3:1.7b, gemma3:4b, nomic-embed-text">
              <input
                value={pullTag}
                onChange={(event) => setPullTag(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void startPull();
                }}
                placeholder="qwen3:1.7b"
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
              />
            </Field>
          </div>
          <Button variant="primary" onClick={() => void startPull()} busy={busy === 'pull'} disabled={!pullTag.trim()}>
            start download
          </Button>
        </div>
        {pull ? (
          <div className="mt-4 rounded-lg border border-slate-800 p-3">
            <div className="flex items-center justify-between text-sm">
              <span className="font-mono text-slate-200">{pull.model}</span>
              <Badge tone={pull.state === 'succeeded' ? 'good' : pull.state === 'failed' ? 'bad' : 'info'}>{pull.state}</Badge>
            </div>
            <div className="mt-2">
              <Progress
                percent={pull.percent}
                tone={pull.state === 'failed' ? 'bad' : pull.state === 'succeeded' ? 'good' : 'info'}
              />
            </div>
            <div className="mt-1 flex justify-between text-xs text-slate-500">
              <span>{pull.status}</span>
              <span>
                {pull.total > 0
                  ? `${formatBytes(pull.completed)} / ${formatBytes(pull.total)} (${pull.percent}%)`
                  : 'layers already present locally'}
              </span>
            </div>
            {pull.error ? <p className="mt-1 font-mono text-xs text-rose-400">{pull.error}</p> : null}
          </div>
        ) : null}
      </Card>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card
          title={`Installed models (${models.length})`}
          subtitle="Sizes and placement are measured, not estimated."
          actions={
            <Button variant="ghost" onClick={() => void load()} busy={loading}>
              refresh
            </Button>
          }
        >
          {loading && models.length === 0 ? <Empty>Reading the model registry…</Empty> : null}
          {!loading && models.length === 0 ? <Empty>No models installed. Pull one above.</Empty> : null}
          <div className="space-y-2">
            {models.map((model) => {
              const bench = benchmarks[model.tag];
              // Embedding-only models cannot generate text, so benchmarking them is
              // not offered at all rather than failing when clicked.
              const canGenerate = model.capabilities.length === 0 || model.capabilities.includes('completion');
              const tps = bench?.tokens_per_sec ?? model.benchmark_tps;
              const source = bench ? 'measured' : model.benchmark_source;
              return (
                <div
                  key={model.tag}
                  className={`rounded-lg border px-3 py-2.5 transition ${
                    selected === model.tag ? 'border-sky-500/60 bg-sky-500/5' : 'border-slate-800 hover:border-slate-700'
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <button onClick={() => setSelected(model.tag)} className="text-left font-mono text-sm text-slate-100 hover:text-sky-300">
                      {model.tag}
                    </button>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {model.profile ? <Badge tone="info">{model.profile}</Badge> : null}
                      {!canGenerate ? <Badge tone="muted" title="This model produces embeddings, not text">embeddings</Badge> : null}
                      {model.loaded ? (
                        <Badge tone={model.vram_percent >= 99 ? 'good' : 'warn'}>
                          loaded {model.vram_percent}% gpu
                        </Badge>
                      ) : (
                        <Badge>idle</Badge>
                      )}
                      {tps != null ? (
                        <Badge tone="muted" title={source === 'reference' ? 'ADR-09 reference reading' : 'measured on this machine'}>
                          {tps} tok/s {source === 'reference' ? 'ref' : ''}
                        </Badge>
                      ) : null}
                    </div>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                    <span>{formatBytes(model.size_bytes)}</span>
                    <span>{model.parameter_size ?? '?'}</span>
                    <span>{model.quantization ?? '?'}</span>
                    <span title={model.context_source === 'profile' ? 'configured profile context (model not loaded yet)' : 'context allocated by the running model'}>
                      ctx {model.context_length ?? '-'}
                      {model.context_source === 'profile' ? ' (profile)' : ''}
                    </span>
                    <span>{formatAge(model.modified_at)}</span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button
                      onClick={() => void benchmark(model.tag)}
                      busy={busy === `bench:${model.tag}`}
                      disabled={!canGenerate}
                      title={canGenerate ? 'Run a real short generation and record the measurement' : 'Embedding-only model: it cannot generate text'}
                    >
                      benchmark
                    </Button>
                    <Button onClick={() => void unload(model.tag)} busy={busy === `unload:${model.tag}`} disabled={!model.loaded} title={model.loaded ? 'Evict from memory' : 'Not loaded'}>
                      unload
                    </Button>
                    {confirmDelete === model.tag ? (
                      <>
                        <Button variant="danger" onClick={() => void remove(model.tag, model.loaded)} busy={busy === `delete:${model.tag}`}>
                          confirm delete
                        </Button>
                        <Button variant="ghost" onClick={() => setConfirmDelete(null)}>
                          keep
                        </Button>
                      </>
                    ) : (
                      <Button variant="ghost" onClick={() => setConfirmDelete(model.tag)}>
                        delete…
                      </Button>
                    )}
                  </div>
                  {confirmDelete === model.tag ? (
                    <p className="mt-2 rounded-md bg-rose-500/10 px-2 py-1.5 text-xs text-rose-300">
                      Deleting removes {formatBytes(model.size_bytes)} from the local model store.
                      {model.loaded ? ' It is loaded, so it will be unloaded first.' : ''}
                    </p>
                  ) : null}
                  {bench ? (
                    <p className="mt-2 rounded-md bg-slate-950/60 px-2 py-1.5 font-mono text-xs text-slate-400">
                      {bench.completion_tokens} tokens · {bench.tokens_per_sec} tok/s · ttft {bench.ttft_ms ?? '-'} ms · ctx{' '}
                      {bench.context_len}
                      <span className="mt-1 block whitespace-pre-wrap text-slate-500">{bench.sample.slice(0, 160)}</span>
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </Card>

        <Card title="Model detail" subtitle={selected ?? 'Select a model on the left.'}>
          <ErrorNote error={detailError} />
          {!selected ? (
            <Empty>Pick a model to inspect its architecture, context window and placement.</Empty>
          ) : detail ? (
            <div>
              <Row label="Tag" value={detail.tag} />
              <Row label="Architecture" value={detail.architecture ?? '-'} />
              <Row label="Parameters" value={detail.parameter_size ?? '-'} />
              <Row label="Parameter count" value={detail.parameter_count?.toLocaleString() ?? '-'} />
              <Row label="Quantization" value={detail.quantization ?? '-'} />
              <Row label="Format" value={detail.format ?? '-'} />
              <Row label="Context window" value={detail.context_length ?? '-'} />
              <Row label="Embedding dim" value={detail.embedding_length ?? '-'} />
              <Row label="Layers" value={detail.block_count ?? '-'} />
              <Row label="Capabilities" value={detail.capabilities.join(', ') || '-'} />
              <Row label="Profile" value={detail.profile ?? 'not mapped'} />
              <Row
                label="Placement"
                value={detail.loaded ? `${detail.vram_gb} GB in VRAM (${detail.vram_percent}%)` : 'not loaded'}
                tone={detail.loaded ? (detail.vram_percent >= 99 ? 'good' : 'warn') : 'normal'}
              />
              <Row label="License file" value={detail.license_present ? 'bundled by the model' : 'not provided'} />
              <Row label="Modified" value={formatAge(detail.modified_at)} />
            </div>
          ) : (
            <Empty>Loading model metadata…</Empty>
          )}
        </Card>
      </div>
    </div>
  );
}
