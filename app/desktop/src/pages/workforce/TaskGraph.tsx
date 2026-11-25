import { useCallback, useEffect, useMemo, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, Progress, Row, formatAge } from '../../components/ui';
import type { AgentTaskRow, GraphPayload, Objective, WorkforceApi } from '../../apiWorkforce';

const STATE_COLORS: Record<string, string> = {
  succeeded: '#10b981',
  failed: '#f43f5e',
  running: '#38bdf8',
  assigned: '#38bdf8',
  ready: '#a78bfa',
  pending: '#64748b',
  blocked: '#f97316',
  cancelled: '#475569',
  waiting: '#eab308',
  review: '#eab308',
};

function shortLabel(task: GraphPayload['nodes'][number]): string {
  const title = task.title ?? task.task_type ?? 'task';
  return title.length > 30 ? `${title.slice(0, 29)}…` : title;
}

export default function TaskGraph({
  wf,
  objectiveId,
  onOpenObjective,
}: {
  wf: WorkforceApi;
  objectiveId: string | null;
  onOpenObjective: (id: string) => void;
}) {
  const [objectives, setObjectives] = useState<Objective[]>([]);
  const [objective, setObjective] = useState<Objective | null>(null);
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadList = useCallback(async () => {
    try {
      const rows = await wf.objectives();
      setObjectives(rows);
      return rows;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return [];
    }
  }, [wf]);

  const loadObjective = useCallback(
    async (id: string) => {
      setError(null);
      try {
        const [detail, graphPayload] = await Promise.all([wf.objective(id), wf.graph(id)]);
        setObjective(detail);
        setGraph(graphPayload);
        setSelected((current) => current ?? graphPayload.nodes[0]?.task_id ?? null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [wf],
  );

  useEffect(() => {
    void (async () => {
      const rows = await loadList();
      const target = objectiveId ?? rows[0]?.objective_id ?? null;
      if (target) await loadObjective(target);
    })();
  }, [loadList, loadObjective, objectiveId]);

  const layout = useMemo<{ nodes: Map<string, { x: number; y: number }>; width: number; height: number }>(() => {
    if (!graph) return { nodes: new Map(), width: 0, height: 0 };
    const byDepth = new Map<number, GraphPayload['nodes']>();
    for (const node of graph.nodes) {
      const bucket = byDepth.get(node.depth) ?? [];
      bucket.push(node);
      byDepth.set(node.depth, bucket);
    }
    const columns = [...byDepth.entries()].sort((a, b) => a[0] - b[0]);
    const positions = new Map<string, { x: number; y: number }>();
    const width = 40 + columns.length * 240;
    const tallest = Math.max(...columns.map(([, nodes]) => nodes.length), 1);
    const height = 40 + tallest * 64;
    for (const [depth, nodes] of columns) {
      nodes.forEach((node, index) => {
        positions.set(node.task_id, { x: 30 + depth * 240, y: 30 + index * 64 });
      });
    }
    return { nodes: positions, width, height };
  }, [graph]);

  const selectedTask: AgentTaskRow | undefined = useMemo(
    () => graph?.tasks?.find((task) => task.task_id === selected),
    [graph, selected],
  );

  async function cancelObjective() {
    if (!objective) return;
    setBusy(true);
    try {
      await wf.cancelObjective(objective.objective_id);
      await loadObjective(objective.objective_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const terminal = objective ? objective.progress.terminal : 0;
  const total = objective ? objective.progress.task_count : 0;

  return (
    <div className="space-y-4">
      <ErrorNote error={error} onRetry={() => objective && void loadObjective(objective.objective_id)} />

      <Card
        title="Task graph"
        subtitle="Persisted DAG: agent_task rows joined by task_edge. Edges are dependencies, not suggestions."
        actions={
          <div className="flex items-center gap-2">
            <select
              value={objective?.objective_id ?? ''}
              onChange={(event) => {
                setSelected(null);
                void loadObjective(event.target.value);
              }}
              className="max-w-xs rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200 outline-none focus:border-sky-500"
            >
              {objectives.map((row) => (
                <option key={row.objective_id} value={row.objective_id}>
                  {row.objective.slice(0, 60)}
                </option>
              ))}
            </select>
            {objective && ['pending', 'planning', 'running', 'compiling'].includes(objective.state) ? (
              <Button variant="danger" busy={busy} onClick={() => void cancelObjective()}>
                cancel objective
              </Button>
            ) : null}
          </div>
        }
      >
        {!objective ? (
          <Empty>No objective selected. Submit one from the Playground.</Empty>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-3">
              <Badge tone={objective.state === 'succeeded' ? 'good' : objective.state === 'failed' ? 'bad' : 'info'}>
                {objective.state}
              </Badge>
              <span className="text-sm text-slate-200">{objective.objective}</span>
            </div>
            <div className="grid gap-3 text-xs text-slate-400 sm:grid-cols-4">
              <span>{total} tasks</span>
              <span>{terminal} terminal</span>
              <span>{objective.calls_used} model calls</span>
              <span>{objective.tokens_used} tokens</span>
            </div>
            <Progress percent={total === 0 ? 0 : (terminal / total) * 100} tone={objective.state === 'failed' ? 'bad' : 'info'} />
          </div>
        )}
      </Card>

      {graph && layout.nodes.size > 0 ? (
        <Card title="Graph" subtitle="Left to right by delegation depth. Click a node to inspect the stored task.">
          <div className="overflow-x-auto">
            <svg width={layout.width} height={layout.height} className="min-w-full">
              {graph.edges.map((edge) => {
                const from = layout.nodes.get(edge.from);
                const to = layout.nodes.get(edge.to);
                if (!from || !to) return null;
                const x1 = from.x + 190;
                const y1 = from.y + 20;
                const x2 = to.x;
                const y2 = to.y + 20;
                const mid = (x1 + x2) / 2;
                return (
                  <path
                    key={`${edge.from}-${edge.to}`}
                    d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
                    stroke="#334155"
                    strokeWidth={1.2}
                    fill="none"
                  />
                );
              })}
              {graph.nodes.map((node) => {
                const position = layout.nodes.get(node.task_id);
                if (!position) return null;
                const color = STATE_COLORS[node.state] ?? '#64748b';
                const isSelected = node.task_id === selected;
                return (
                  <g
                    key={node.task_id}
                    transform={`translate(${position.x},${position.y})`}
                    onClick={() => setSelected(node.task_id)}
                    className="cursor-pointer"
                  >
                    <rect
                      width={190}
                      height={40}
                      rx={6}
                      fill={isSelected ? '#1e293b' : '#0b1220'}
                      stroke={isSelected ? color : '#1e293b'}
                      strokeWidth={isSelected ? 2 : 1}
                    />
                    <rect width={4} height={40} rx={2} fill={color} />
                    <text x={12} y={16} fontSize={10} fill="#94a3b8" fontFamily="monospace">
                      {node.depth === 0 ? 'chief' : `d${node.depth} · ${node.team_id}`}
                    </text>
                    <text x={12} y={30} fontSize={11} fill="#e2e8f0">
                      {shortLabel(node)}
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>
          <div className="mt-3 flex flex-wrap gap-3 text-[11px] text-slate-500">
            {Object.entries(STATE_COLORS).map(([state, color]) => (
              <span key={state} className="inline-flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
                {state}
              </span>
            ))}
          </div>
        </Card>
      ) : null}

      {selectedTask ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title={`Task ${selectedTask.task_id}`} subtitle={`${selectedTask.agent_id} · ${selectedTask.department}`}>
            <Row label="State" value={selectedTask.state} tone={selectedTask.state === 'succeeded' ? 'good' : selectedTask.state === 'failed' ? 'bad' : 'normal'} />
            <Row label="Role" value={selectedTask.role} />
            <Row label="Depth" value={selectedTask.depth} />
            <Row label="Type" value={selectedTask.task_type ?? '-'} />
            <Row label="Config version" value={`v${selectedTask.config_version}`} />
            <Row label="Model" value={selectedTask.model_tag ?? '-'} />
            <Row label="Tokens" value={selectedTask.tokens_used} />
            <Row label="Duration" value={`${selectedTask.duration_ms ?? 0} ms`} />
            <Row label="Confidence" value={selectedTask.confidence ?? '-'} />
            <Row label="Created" value={formatAge(selectedTask.created_at)} />
            {selectedTask.error_code ? <Row label="Error" value={`${selectedTask.error_code}: ${selectedTask.error ?? ''}`} tone="bad" /> : null}
            {selectedTask.summary ? (
              <p className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-xs leading-relaxed text-slate-300">
                {selectedTask.summary}
              </p>
            ) : null}
          </Card>

          <Card title="Model output" subtitle="Validated artifact plus the exact execution reference.">
            {selectedTask.output?.artifact ? (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-1">
                  <Badge tone="muted">{String(selectedTask.output.artifact.contract ?? 'artifact')}</Badge>
                  {selectedTask.output.profile ? <Badge tone="info">{selectedTask.output.profile}</Badge> : null}
                  {(selectedTask.output.tools ?? []).map((tool) => (
                    <Badge key={tool} tone="muted">
                      {tool}
                    </Badge>
                  ))}
                </div>
                <pre className="max-h-72 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300">
                  {JSON.stringify(selectedTask.output.artifact, null, 2)}
                </pre>
                {selectedTask.output.warnings?.length ? (
                  <ul className="list-disc pl-5 text-[11px] text-amber-300">
                    {selectedTask.output.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : (
              <Empty>This task has no stored model output.</Empty>
            )}
          </Card>
        </div>
      ) : null}

      {objective?.final_report ? (
        <Card title="Compiled result" subtitle={`quality verdict: ${objective.final_report.quality_verdict}`}>
          <p className="text-sm text-slate-200">{objective.final_report.summary}</p>
          {objective.final_report.deliverable ? (
            <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-xs leading-relaxed text-slate-300">
              {objective.final_report.deliverable}
            </pre>
          ) : null}
          {objective.final_report.unfinished.length ? (
            <div className="mt-3">
              <p className="text-[11px] uppercase tracking-wide text-rose-400">Unfinished</p>
              <ul className="mt-1 space-y-1 text-[11px] text-rose-300">
                {objective.final_report.unfinished.map((row) => (
                  <li key={row.task_id} className="font-mono">
                    {row.agent_id} · {row.code ?? 'unknown'}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <div className="mt-3 flex justify-end">
            <Button variant="ghost" onClick={() => onOpenObjective(objective.objective_id)}>
              refresh
            </Button>
          </div>
        </Card>
      ) : null}
    </div>
  );
}
