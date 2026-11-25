import { useCallback, useEffect, useState } from 'react';
import type { AiHealth, CreatorApi, ExecutionRow, GovernorSnapshot } from '../api';
import { ApiRequestError } from '../api';
import { Badge, Card, Empty, ErrorNote, Progress, Row, Stat, formatAge } from '../components/ui';

type Props = { api: CreatorApi; version: { version: string; api: string; capabilities: Record<string, string> } | null };

const pressureTone = { normal: 'good', saturated: 'warn', critical: 'bad' } as const;

export default function Infrastructure({ api, version }: Props) {
  const [health, setHealth] = useState<AiHealth | null>(null);
  const [executions, setExecutions] = useState<ExecutionRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [healthData, historyData] = await Promise.all([api.health(), api.executions(8)]);
      setHealth(healthData);
      setExecutions(historyData);
    } catch (err) {
      setError(err instanceof ApiRequestError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const snapshot: GovernorSnapshot | null = health?.resources ?? null;
  const gpu = snapshot?.gpu ?? null;
  const online = health?.ollama.online ?? false;

  return (
    <div className="space-y-5">
      <ErrorNote error={error} onRetry={() => void load()} />
      {loading && !health ? <Card title="AI infrastructure">Loading live telemetry…</Card> : null}

      {health && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Ollama" value={online ? `v${health.ollama.version ?? '?'}` : 'offline'} tone={online ? 'good' : 'bad'} />
            <Stat label="Models installed" value={health.models.installed} unit={`${health.models.total_size_gb} GB`} />
            <Stat label="GPU load" value={gpu?.utilization_percent != null ? `${Math.round(gpu.utilization_percent)}%` : 'n/a'} tone={gpu ? 'good' : 'warn'} />
            <Stat
              label="Inference slots"
              value={`${snapshot?.active_generations ?? 0}/${snapshot?.max_concurrent ?? 0}`}
              tone={snapshot && snapshot.active_generations >= snapshot.max_concurrent ? 'warn' : 'good'}
            />
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card
              title="Runtime"
              subtitle="The only model runtime this app talks to. No cloud provider is configured."
              actions={<Badge tone={online ? 'good' : 'bad'} title={health.ollama.error?.message}>{online ? 'online' : (health.ollama.error?.code ?? 'offline')}</Badge>}
            >
              <Row label="Endpoint" value={health.ollama.base_url} />
              <Row label="Round trip" value={health.ollama.latency_ms != null ? `${health.ollama.latency_ms} ms` : '-'} />
              <Row label="Generation timeout" value={`${health.gateway.read_timeout_s} s`} />
              <Row label="Samples taken" value={health.gateway.requests} />
              {health.ollama.error ? (
                <p className="mt-2 rounded-md bg-rose-500/10 px-2 py-1.5 font-mono text-xs text-rose-300">{health.ollama.error.message}</p>
              ) : null}
            </Card>

            <Card
              title="Resource governor"
              subtitle="Admission control keeps the machine usable; concurrency only rises when you change it."
              actions={<Badge tone={pressureTone[snapshot?.pressure ?? 'normal']}>{snapshot?.pressure ?? 'unknown'}</Badge>}
            >
              <Row label="CPU" value={`${snapshot?.cpu_percent ?? 0}%`} />
              <div className="mt-1"><Progress percent={snapshot?.cpu_percent ?? 0} /></div>
              <Row
                label="RAM"
                value={`${snapshot?.ram_available_gb ?? 0} GB free of ${snapshot?.ram_total_gb ?? 0} GB`}
                tone={(snapshot?.ram_percent ?? 0) > (snapshot?.limits.max_ram_percent ?? 85) ? 'bad' : 'normal'}
              />
              <div className="mt-1">
                <Progress percent={snapshot?.ram_percent ?? 0} tone={(snapshot?.ram_percent ?? 0) > (snapshot?.limits.max_ram_percent ?? 85) ? 'bad' : 'info'} />
              </div>
              <Row label="VRAM" value={gpu ? `${gpu.memory_used_mb} / ${gpu.memory_total_mb} MB` : 'telemetry unavailable'} />
              <div className="mt-1"><Progress percent={gpu?.memory_percent ?? 0} tone={(gpu?.memory_percent ?? 0) > 90 ? 'bad' : 'info'} /></div>
              <Row label="GPU temp / power" value={gpu ? `${gpu.temperature_c ?? '-'} °C · ${gpu.power_w ?? '-'} W` : '-'} />
              {(snapshot?.reasons ?? []).length > 0 ? (
                <ul className="mt-2 space-y-1">
                  {snapshot?.reasons.map((reason) => (
                    <li key={reason} className="rounded-md bg-amber-500/10 px-2 py-1 text-xs text-amber-300">{reason}</li>
                  ))}
                </ul>
              ) : null}
              {(snapshot?.telemetry_errors ?? []).map((note) => (
                <p key={note} className="mt-2 text-xs text-slate-500">{note} (measurements above are reported from what is actually available)</p>
              ))}
            </Card>

            <Card title="Loaded models" subtitle="What is resident in memory right now, straight from Ollama.">
              {(snapshot?.loaded_models ?? []).length === 0 ? (
                <Empty>No model is loaded. The first generation will load one and report the cost.</Empty>
              ) : (
                <div className="space-y-2">
                  {snapshot?.loaded_models.map((model) => (
                    <div key={model.tag} className="rounded-lg border border-slate-800 px-3 py-2">
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-sm text-slate-200">{model.tag}</span>
                        <Badge tone={model.vram_percent >= 99 ? 'good' : model.vram_percent > 0 ? 'warn' : 'bad'}>{model.vram_percent}% on GPU</Badge>
                      </div>
                      <div className="mt-1 flex justify-between text-xs text-slate-500">
                        <span>{model.size_gb} GB total</span>
                        <span>{model.vram_gb} GB in VRAM</span>
                      </div>
                      <div className="mt-1.5"><Progress percent={model.vram_percent} tone={model.vram_percent >= 99 ? 'good' : 'info'} /></div>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <Card title="Capabilities" subtitle={`API ${version?.api ?? '-'} · reported by the backend, not the UI.`}>
              <div className="space-y-2">
                {Object.entries(health.capabilities).map(([key, value]) => (
                  <div key={key} className="flex items-center justify-between text-sm">
                    <span className="text-slate-400">{key.replace(/_/g, ' ')}</span>
                    <Badge tone={value === 'available' ? 'good' : 'warn'}>{value.replace(/_/g, ' ')}</Badge>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <Card title="Recent executions" subtitle="Every generation is persisted before it is reported as complete.">
            {executions.length === 0 ? (
              <Empty>No generations yet. Anything you run in the Playground appears here.</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-[11px] text-slate-500">
                    <tr>
                      <th className="py-1 pr-3">When</th>
                      <th className="py-1 pr-3">Model</th>
                      <th className="py-1 pr-3">Profile</th>
                      <th className="py-1 pr-3">State</th>
                      <th className="py-1 pr-3">Tokens</th>
                      <th className="py-1 pr-3">tok/s</th>
                      <th className="py-1">Origin</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono text-xs text-slate-300">
                    {executions.map((row) => (
                      <tr key={row.id} className="border-t border-slate-800/70">
                        <td className="py-1.5 pr-3 text-slate-500">{formatAge(row.created_at)}</td>
                        <td className="py-1.5 pr-3">{row.model_tag}</td>
                        <td className="py-1.5 pr-3 text-slate-400">{row.profile_name ?? '-'}</td>
                        <td className="py-1.5 pr-3">
                          <Badge tone={row.state === 'succeeded' ? 'good' : row.state === 'failed' ? 'bad' : row.state === 'cancelled' ? 'warn' : 'muted'}>{row.state}</Badge>
                        </td>
                        <td className="py-1.5 pr-3">{row.completion_tokens}</td>
                        <td className="py-1.5 pr-3">{row.tokens_per_sec ?? '-'}</td>
                        <td className="py-1.5 text-slate-500">{row.origin}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
