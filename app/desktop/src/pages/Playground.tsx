import { useEffect, useRef, useState } from 'react';
import type { CreatorApi, GenerateResponse, Profile, StreamEvent } from '../api';
import { ApiRequestError } from '../api';
import { Badge, Button, Card, Empty, ErrorNote, Field, Row, formatAge } from '../components/ui';

type Props = { api: CreatorApi; disabled: boolean };

function errText(err: unknown): string {
  return err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err);
}

export default function Playground({ api, disabled }: Props) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profile, setProfile] = useState<string>('specialist');
  const [prompt, setPrompt] = useState('Write one hook line for a 15-second video about morning routines.');
  const [system, setSystem] = useState('You are a concise social video copywriter.');
  const [maxTokens, setMaxTokens] = useState(160);
  const [temperature, setTemperature] = useState(0.7);
  const [useThinking, setUseThinking] = useState<boolean | null>(null);
  const [streaming, setStreaming] = useState(true);
  const [answer, setAnswer] = useState('');
  const [thinking, setThinking] = useState('');
  const [meta, setMeta] = useState<{ model: string; profile: string; reason: string; executionId: string } | null>(null);
  const [metrics, setMetrics] = useState<GenerateResponse['metrics'] | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const cancelStream = useRef<(() => void) | null>(null);
  const executionId = useRef<string | null>(null);

  useEffect(() => {
    if (disabled) return;
    api
      .profiles()
      .then((data) => {
        setProfiles(data);
        setProfile((current) => (data.some((item) => item.name === current) ? current : (data[0]?.name ?? 'specialist')));
      })
      .catch(() => setProfiles([]));
  }, [api, disabled]);

  const reset = () => {
    setAnswer('');
    setThinking('');
    setMetrics(null);
    setWarnings([]);
    setMeta(null);
    setError(null);
    setNotice(null);
    executionId.current = null;
  };

  const run = async () => {
    reset();
    setRunning(true);
    const body = {
      prompt,
      system: system || null,
      profile,
      options: { num_predict: maxTokens, temperature },
      think: useThinking,
    };
    try {
      if (streaming) {
        cancelStream.current = await api.stream(body, {
          onEvent: (event: StreamEvent) => {
            if (event.type === 'meta') {
              executionId.current = event.execution_id;
              setMeta({
                model: event.model,
                profile: event.profile,
                reason: event.reason,
                executionId: event.execution_id,
              });
            } else if (event.type === 'token') {
              setAnswer((prev) => prev + event.text);
            } else if (event.type === 'thinking') {
              setThinking((prev) => prev + event.text);
            } else if (event.type === 'done') {
              setMetrics(event.metrics);
            } else if (event.type === 'cancelled') {
              setNotice('Generation cancelled. The partial output above was really produced.');
            } else if (event.type === 'error') {
              setError(`${event.code}: ${event.message}`);
            }
          },
          onError: (err) => setError(String(err)),
          onClose: () => setRunning(false),
        });
      } else {
        const result = await api.generate(body);
        executionId.current = result.execution_id;
        setMeta({ model: result.model, profile: result.profile, reason: result.reason, executionId: result.execution_id });
        setAnswer(result.response);
        setThinking(result.thinking ?? '');
        setMetrics(result.metrics);
        setWarnings(result.warnings ?? []);
        if (result.incomplete) {
          setError('The model spent its whole budget on hidden reasoning and returned no visible answer.');
        }
      }
    } catch (err) {
      setError(errText(err));
    } finally {
      if (!streaming) setRunning(false);
    }
  };

  const cancel = async () => {
    if (cancelStream.current) {
      cancelStream.current();
      cancelStream.current = null;
    }
    if (executionId.current) {
      try {
        await api.cancelExecution(executionId.current);
      } catch (err) {
        setError(errText(err));
      }
    }
    setRunning(false);
  };

  return (
    <div className="space-y-5">
      <ErrorNote error={error} />
      {notice ? (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">{notice}</div>
      ) : null}
      {disabled ? (
        <Card title="Playground unavailable">
          <Empty>The local model runtime is offline, so nothing can be generated. Start Ollama and reload.</Empty>
        </Card>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card title="Prompt" subtitle="Runs against the local model on this machine. Nothing is sent to a cloud provider.">
          <div className="space-y-3">
            <Field label="System instruction">
              <textarea
                value={system}
                onChange={(event) => setSystem(event.target.value)}
                rows={2}
                className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-500"
              />
            </Field>
            <Field label="Prompt">
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                rows={5}
                className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-500"
              />
            </Field>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Profile">
                <select
                  value={profile}
                  onChange={(event) => setProfile(event.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-500"
                >
                  {(profiles.length ? profiles : [{ name: profile, ollama_tag: '', context_len: 0, temperature: 0, roles_json: [], fallback_profile: null, enabled: true }]).map(
                    (item) => (
                      <option key={item.name} value={item.name}>
                        {item.name}
                        {item.ollama_tag ? ` · ${item.ollama_tag}` : ''}
                      </option>
                    ),
                  )}
                </select>
              </Field>
              <Field label="Max tokens">
                <input
                  type="number"
                  min={16}
                  max={2048}
                  value={maxTokens}
                  onChange={(event) => setMaxTokens(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
                />
              </Field>
              <Field label="Temperature">
                <input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={temperature}
                  onChange={(event) => setTemperature(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
                />
              </Field>
            </div>
            <div className="flex flex-wrap items-center gap-4 text-sm text-slate-300">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={streaming} onChange={(event) => setStreaming(event.target.checked)} />
                stream tokens
              </label>
              <label className="flex items-center gap-2" title="Qwen3 reasons before answering; disable to see only the answer">
                <input
                  type="checkbox"
                  checked={useThinking === true}
                  onChange={(event) => setUseThinking(event.target.checked ? true : null)}
                />
                allow hidden reasoning
              </label>
            </div>
            <div className="flex gap-2">
              <Button variant="primary" onClick={() => void run()} busy={running} disabled={!prompt.trim() || disabled}>
                run
              </Button>
              <Button variant="ghost" onClick={() => void cancel()} disabled={!running}>
                cancel
              </Button>
              <Button variant="ghost" onClick={reset} disabled={running}>
                clear
              </Button>
            </div>
          </div>
        </Card>

        <Card title="Result" subtitle={meta ? `execution ${meta.executionId}` : 'Nothing generated yet.'}>
          {meta ? (
            <div className="mb-3 space-y-1">
              <Row label="Model" value={meta.model} />
              <Row label="Profile" value={meta.profile} />
              <Row label="Why" value={meta.reason} mono={false} />
            </div>
          ) : null}
          {metrics ? (
            <div className="mb-3 grid grid-cols-2 gap-2 text-xs">
              <Badge tone="info">{metrics.completion_tokens} tokens</Badge>
              <Badge tone={metrics.tokens_per_sec > 10 ? 'good' : 'warn'}>{metrics.tokens_per_sec} tok/s</Badge>
              <Badge tone="muted">ttft {metrics.ttft_ms ?? '-'} ms</Badge>
              <Badge tone="muted">{metrics.wall_ms} ms wall</Badge>
            </div>
          ) : null}
          {thinking ? (
            <details className="mb-3 rounded-lg border border-slate-800 bg-slate-950/60 p-2">
              <summary className="cursor-pointer text-xs text-slate-400">hidden reasoning ({thinking.length} chars)</summary>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-xs text-slate-500">{thinking}</pre>
            </details>
          ) : null}
          {answer ? (
            <div className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm leading-relaxed text-slate-100">
              {answer}
            </div>
          ) : running ? (
            <Empty>Generating…</Empty>
          ) : (
            <Empty>{meta ? 'The model returned an empty answer.' : 'Run a prompt to see real output.'}</Empty>
          )}
          {warnings.map((warning) => (
            <p key={warning} className="mt-2 rounded-md bg-amber-500/10 px-2 py-1.5 text-xs text-amber-300">
              {warning}
            </p>
          ))}
        </Card>
      </div>

      <RecentRuns api={api} disabled={disabled} />
    </div>
  );
}

function RecentRuns({ api, disabled }: { api: CreatorApi; disabled: boolean }) {
  const [rows, setRows] = useState<Awaited<ReturnType<CreatorApi['executions']>>>([]);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    if (disabled) return;
    api
      .executions(12)
      .then(setRows)
      .catch((err: unknown) => setError(errText(err)));
  };

  useEffect(load, [api, disabled]);

  return (
    <Card title="History" subtitle="Persisted before the run is reported complete." actions={<Button variant="ghost" onClick={load}>refresh</Button>}>
      <ErrorNote error={error} />
      {rows.length === 0 ? (
        <Empty>No executions recorded yet.</Empty>
      ) : (
        <div className="space-y-1.5">
          {rows.map((row) => (
            <div key={row.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 px-3 py-2 text-xs">
              <span className="font-mono text-slate-300">{row.model_tag}</span>
              <span className="max-w-[45%] truncate text-slate-500" title={row.prompt_text ?? ''}>
                {row.prompt_text}
              </span>
              <Badge tone={row.state === 'succeeded' ? 'good' : row.state === 'failed' ? 'bad' : row.state === 'cancelled' ? 'warn' : 'muted'}>
                {row.state}
              </Badge>
              <span className="font-mono text-slate-500">{row.completion_tokens} tok</span>
              <span className="font-mono text-slate-500">{row.tokens_per_sec ?? '-'} tok/s</span>
              <span className="text-slate-600">{formatAge(row.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
