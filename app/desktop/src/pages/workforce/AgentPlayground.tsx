import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, Field, Row } from '../../components/ui';
import type { AgentDefinition, MemoryRow, Objective, ToolEntry, WorkforceApi } from '../../apiWorkforce';

export default function AgentPlayground({
  wf,
  onOpenObjective,
}: {
  wf: WorkforceApi;
  onOpenObjective: (id: string) => void;
}) {
  const [objective, setObjective] = useState('Turn one long video into three platform-specific shorts about hooks');
  const [projectId, setProjectId] = useState('');
  const [maxCalls, setMaxCalls] = useState(12);
  const [maxDepartments, setMaxDepartments] = useState(2);
  const [specialists, setSpecialists] = useState(2);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Objective | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [tools, setTools] = useState<ToolEntry[]>([]);
  const [toolId, setToolId] = useState('project.metadata.read');
  const [agentId, setAgentId] = useState('research_synthesis_agent');
  const [toolPayload, setToolPayload] = useState('{\n  "project_id": ""\n}');
  const [toolResult, setToolResult] = useState<string | null>(null);
  const [toolError, setToolError] = useState<string | null>(null);
  const [toolBusy, setToolBusy] = useState(false);

  const [memoryScope, setMemoryScope] = useState('agent');
  const [memoryOwner, setMemoryOwner] = useState('hook_writer_agent');
  const [memoryContent, setMemoryContent] = useState('');
  const [memoryRows, setMemoryRows] = useState<MemoryRow[]>([]);
  const [memoryError, setMemoryError] = useState<string | null>(null);

  const loadBasics = useCallback(async () => {
    try {
      const [agentRows, toolRows] = await Promise.all([wf.agents(), wf.tools()]);
      setAgents(agentRows);
      setTools(toolRows);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [wf]);

  useEffect(() => {
    void loadBasics();
  }, [loadBasics]);

  async function runObjective() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const created = await wf.createObjective({
        objective,
        project_id: projectId.trim() || null,
        limits: {
          max_calls: maxCalls,
          max_departments: maxDepartments,
          specialists_per_department: specialists,
        },
        run_now: true,
      });
      setResult(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  async function runTool() {
    setToolBusy(true);
    setToolError(null);
    setToolResult(null);
    try {
      const payload = JSON.parse(toolPayload) as Record<string, unknown>;
      const outcome = await wf.runTool(toolId, agentId, payload);
      setToolResult(JSON.stringify(outcome, null, 2));
      if (!outcome.ok) setToolError(`${outcome.error_code}: ${outcome.error}`);
    } catch (err) {
      setToolError(err instanceof Error ? err.message : String(err));
    } finally {
      setToolBusy(false);
    }
  }

  async function writeMemory() {
    setMemoryError(null);
    try {
      await wf.writeMemory({
        scope: memoryScope,
        owner_id: memoryOwner || null,
        content: memoryContent,
      });
      setMemoryContent('');
      setMemoryRows(await wf.memory(memoryScope, 20));
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : String(err));
    }
  }

  async function queryMemory() {
    setMemoryError(null);
    try {
      setMemoryRows(
        await wf.queryMemory({
          scope: memoryScope,
          owner_id: memoryOwner || null,
          limit: 20,
        }),
      );
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : String(err));
    }
  }

  const reserved = tools.filter((tool) => tool.category === 'write').map((tool) => tool.tool_id);

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />

      <Card
        title="Submit an objective"
        subtitle="Runs synchronously so you can watch every step. This blocks the request while the local model works."
        actions={
          result ? (
            <Button variant="ghost" onClick={() => onOpenObjective(result.objective_id)}>
              open graph
            </Button>
          ) : null
        }
      >
        <div className="grid gap-3 lg:grid-cols-[1fr_320px]">
          <Field label="Objective" hint="At least 8 characters. The chief decides which departments handle it.">
            <textarea
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              rows={4}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 p-3 text-sm text-slate-100 outline-none focus:border-sky-500"
            />
          </Field>
          <div className="space-y-3">
            <Field label="Project id (optional)" hint="Links the run to a content project so tools can read it.">
              <input
                value={projectId}
                onChange={(event) => setProjectId(event.target.value)}
                placeholder="01H..."
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
              />
            </Field>
            <div className="grid grid-cols-3 gap-2">
              <Field label="calls">
                <input
                  type="number"
                  min={3}
                  max={60}
                  value={maxCalls}
                  onChange={(event) => setMaxCalls(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
                />
              </Field>
              <Field label="depts">
                <input
                  type="number"
                  min={1}
                  max={5}
                  value={maxDepartments}
                  onChange={(event) => setMaxDepartments(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
                />
              </Field>
              <Field label="specialists">
                <input
                  type="number"
                  min={1}
                  max={5}
                  value={specialists}
                  onChange={(event) => setSpecialists(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
                />
              </Field>
            </div>
            <Button variant="primary" busy={running} onClick={() => void runObjective()}>
              run objective
            </Button>
          </div>
        </div>

        {result ? (
          <div className="mt-4 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={result.state === 'succeeded' ? 'good' : result.state === 'partial' ? 'warn' : 'bad'}>
                {result.state}
              </Badge>
              <span className="font-mono text-[11px] text-slate-500">{result.objective_id}</span>
              <span className="text-[11px] text-slate-500">
                {result.progress.task_count} tasks · {result.calls_used} calls · {result.tokens_used} tokens
              </span>
              {result.run ? <span className="text-[11px] text-slate-500">{result.run.duration_ms} ms</span> : null}
            </div>
            {result.plan ? (
              <p className="mt-2 text-xs leading-relaxed text-slate-300">
                <span className="text-slate-500">plan: </span>
                {result.plan.summary}
              </p>
            ) : null}
            {result.final_report ? (
              <>
                <p className="mt-2 text-xs leading-relaxed text-slate-200">{result.final_report.summary}</p>
                <p className="mt-1 font-mono text-[11px] text-slate-500">
                  quality verdict: {result.final_report.quality_verdict}
                </p>
                {result.final_report.deliverable ? (
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300">
                    {result.final_report.deliverable}
                  </pre>
                ) : null}
              </>
            ) : null}
          </div>
        ) : null}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Run a tool directly"
          subtitle="Authorization is enforced exactly as it is for an agent: the chosen agent must hold the grant."
        >
          <div className="space-y-3">
            <Field label="Tool">
              <select
                value={toolId}
                onChange={(event) => setToolId(event.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 outline-none focus:border-sky-500"
              >
                {tools.map((tool) => (
                  <option key={tool.tool_id} value={tool.tool_id}>
                    {tool.tool_id} ({tool.category})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Agent" hint="Denied grants come back as 403 TOOL_DENIED.">
              <select
                value={agentId}
                onChange={(event) => setAgentId(event.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 outline-none focus:border-sky-500"
              >
                {agents.map((agent) => (
                  <option key={agent.agent_id} value={agent.agent_id}>
                    {agent.agent_id}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Payload (JSON)">
              <textarea
                value={toolPayload}
                onChange={(event) => setToolPayload(event.target.value)}
                rows={5}
                spellCheck={false}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 p-2 font-mono text-[11px] text-slate-200 outline-none focus:border-sky-500"
              />
            </Field>
            <Button variant="primary" busy={toolBusy} onClick={() => void runTool()}>
              run tool
            </Button>
            <ErrorNote error={toolError} />
            {toolResult ? (
              <pre className="max-h-56 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300">
                {toolResult}
              </pre>
            ) : null}
            <p className="text-[11px] text-slate-500">
              write tools: {reserved.join(', ')}. They create rows or files and never contact a platform.
            </p>
          </div>
        </Card>

        <Card title="Scoped memory" subtitle="Secrets are rejected, not redacted. Project rows never leak into another project.">
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <Field label="Scope">
                <select
                  value={memoryScope}
                  onChange={(event) => setMemoryScope(event.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 outline-none focus:border-sky-500"
                >
                  {['task', 'agent', 'department', 'project', 'global', 'long_term'].map((scope) => (
                    <option key={scope} value={scope}>
                      {scope}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Owner">
                <input
                  value={memoryOwner}
                  onChange={(event) => setMemoryOwner(event.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-sky-500"
                />
              </Field>
            </div>
            <Field label="Content">
              <textarea
                value={memoryContent}
                onChange={(event) => setMemoryContent(event.target.value)}
                rows={3}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-xs text-slate-100 outline-none focus:border-sky-500"
              />
            </Field>
            <div className="flex gap-2">
              <Button variant="primary" disabled={!memoryContent.trim()} onClick={() => void writeMemory()}>
                write
              </Button>
              <Button onClick={() => void queryMemory()}>query</Button>
            </div>
            <ErrorNote error={memoryError} />
            {memoryRows.length === 0 ? (
              <Empty>No memory rows for this scope and owner.</Empty>
            ) : (
              <ul className="max-h-56 space-y-2 overflow-auto">
                {memoryRows.map((row) => (
                  <li key={row.memory_id} className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <Badge tone="muted">{row.scope}</Badge>
                      <span className="font-mono text-[11px] text-slate-600">{row.owner_id ?? 'shared'}</span>
                    </div>
                    <p className="mt-1 text-[11px] leading-relaxed text-slate-300">{row.content}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>

      <Card title="What a run costs" subtitle="Measured on this machine, not estimated.">
        <Row label="Default worker model" value="qwen3:1.7b (~45 tok/s, 100% GPU)" />
        <Row label="Classifier" value="qwen3:0.6b (~73 tok/s)" />
        <Row label="Planner / reviewer" value="qwen3:4b (~15 tok/s, 69% GPU)" />
        <Row label="Concurrency" value="1 (governor-enforced)" />
        <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
          A 12-call objective on the 1.7B worker typically takes 1–3 minutes. If the objective needs the 4B planner,
          expect materially longer: that is why max_calls is bounded and every call is recorded.
        </p>
      </Card>
    </div>
  );
}
