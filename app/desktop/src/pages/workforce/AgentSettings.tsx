import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, Row, Stat } from '../../components/ui';
import type { AgentSummary, ApprovalRow, MemoryStats, WorkforceApi } from '../../apiWorkforce';

export default function AgentSettings({ wf }: { wf: WorkforceApi }) {
  const [summary, setSummary] = useState<AgentSummary | null>(null);
  const [memory, setMemory] = useState<MemoryStats | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [retention, setRetention] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [nextSummary, nextMemory, nextApprovals] = await Promise.all([
        wf.summary(),
        wf.memoryStats(),
        wf.approvals(),
      ]);
      setSummary(nextSummary);
      setMemory(nextMemory);
      setApprovals(nextApprovals);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [wf]);

  useEffect(() => {
    void load();
  }, [load]);

  async function runRetention(dryRun: boolean) {
    setBusy(true);
    setRetention(null);
    try {
      const result = await wf.runRetention(dryRun);
      setRetention(
        `${dryRun ? 'dry run' : 'applied'}: ${result.expired_deleted} expired removed, ${result.over_budget_removed} over budget`,
      );
      if (!dryRun) await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function decide(id: string, approve: boolean) {
    setBusy(true);
    try {
      await wf.decideApproval(id, approve);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const hard = summary?.delegation_hard_limits ?? {};
  const defaults = summary?.delegation_limits ?? {};

  return (
    <div className="space-y-4">
      <ErrorNote error={error} onRetry={() => void load()} />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Agents enabled" value={summary ? `${summary.agents_enabled}/${summary.agents_total}` : '…'} />
        <Stat label="Tools enabled" value={summary ? `${summary.tools_enabled}/${summary.tools_registered}` : '…'} />
        <Stat label="Memory rows" value={memory?.total ?? '…'} />
        <Stat label="Pending approvals" value={approvals.filter((row) => row.state === 'pending').length} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Delegation policy" subtitle="Hard limits are code; defaults are what a new objective receives.">
          <Row label="Max depth (default / hard)" value={`${defaults.max_depth} / ${hard.max_depth}`} />
          <Row label="Model calls (default / hard)" value={`${defaults.max_calls} / ${hard.max_calls}`} />
          <Row label="Revisions (default / hard)" value={`${defaults.max_revisions} / ${hard.max_revisions}`} />
          <Row label="Concurrency" value={`${defaults.concurrency} (fixed)`} />
          <Row label="Wall clock" value={`${defaults.wall_clock_s}s`} />
          <Row label="Departments per objective" value={defaults.max_departments} />
          <Row label="Specialists per department" value={defaults.specialists_per_department} />
          <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
            A deeper graph or a larger call budget is rejected by the API, not clamped silently. The limit is stored on
            the objective when it is created, so an in-flight run cannot be widened mid-way.
          </p>
        </Card>

        <Card title="Memory retention" subtitle="Retention runs on demand; nothing is deleted in the background.">
          <Row label="Rows" value={memory?.total ?? '-'} />
          <Row label="Characters stored" value={memory?.total_chars ?? '-'} />
          <Row label="Expired (pending sweep)" value={memory?.expired ?? '-'} tone={(memory?.expired ?? 0) > 0 ? 'warn' : 'normal'} />
          <Row label="Budget per scope" value={`${memory?.budget_chars_per_scope ?? 0} chars`} />
          <div className="mt-3 space-y-2">
            {Object.entries(memory?.retention_days ?? {}).map(([scope, days]) => (
              <div key={scope} className="flex items-center justify-between text-xs text-slate-400">
                <span>{scope}</span>
                <span className="font-mono">{days} days</span>
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-2">
            <Button busy={busy} onClick={() => void runRetention(true)}>
              dry run
            </Button>
            <Button variant="danger" busy={busy} onClick={() => void runRetention(false)}>
              apply retention
            </Button>
          </div>
          {retention ? <p className="mt-2 font-mono text-[11px] text-emerald-400">{retention}</p> : null}
        </Card>
      </div>

      <Card
        title="Human approvals"
        subtitle="Approvals record intent only. Nothing here can publish, post or contact a platform."
        actions={<Badge tone="warn">intent only</Badge>}
      >
        {approvals.length === 0 ? (
          <Empty>No approval requests. Agents create them with the human.approval.request tool.</Empty>
        ) : (
          <ul className="space-y-2">
            {approvals.map((row) => (
              <li key={row.approval_request_id} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Badge
                      tone={
                        row.state === 'approved'
                          ? 'good'
                          : row.state === 'pending'
                            ? 'warn'
                            : row.state === 'rejected'
                              ? 'bad'
                              : 'muted'
                      }
                    >
                      {row.state}
                    </Badge>
                    <span className="font-mono text-xs text-slate-200">{row.action}</span>
                    <span className="font-mono text-[11px] text-slate-500">{row.target_type}</span>
                  </div>
                  {row.state === 'pending' ? (
                    <div className="flex gap-2">
                      <Button variant="primary" busy={busy} onClick={() => void decide(row.approval_request_id, true)}>
                        approve
                      </Button>
                      <Button variant="danger" busy={busy} onClick={() => void decide(row.approval_request_id, false)}>
                        reject
                      </Button>
                    </div>
                  ) : null}
                </div>
                <p className="mt-1 text-xs text-slate-300">{row.summary}</p>
                <p className="mt-1 font-mono text-[11px] text-slate-600">
                  hash {row.payload_hash.slice(0, 16)}… · expires {row.expires_at ?? 'never'}
                  {row.consumed_at ? ` · consumed ${row.consumed_at}` : ''}
                </p>
                {row.risks.length ? (
                  <ul className="mt-1 list-disc pl-5 text-[11px] text-amber-300">
                    {row.risks.map((risk) => (
                      <li key={risk}>{risk}</li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Safety posture" subtitle="What this build will and will not do.">
        <ul className="space-y-2 text-xs leading-relaxed text-slate-400">
          <li>
            <Badge tone="good">enforced</Badge> Agents cannot execute shell commands, write arbitrary files, fetch URLs
            or read secrets: those tool ids are refused before any handler runs.
          </li>
          <li>
            <Badge tone="good">enforced</Badge> A model's text is never executed as an instruction; it is parsed as JSON
            and validated against the agent's output contract.
          </li>
          <li>
            <Badge tone="good">enforced</Badge> All inference passes through the resource governor with one generation
            slot, so a large graph cannot load two models at once.
          </li>
          <li>
            <Badge tone="warn">not implemented</Badge> Publishing, OAuth and media transcoding remain 501 until their
            phases land. The UI shows those capabilities as unavailable rather than offering a dead button.
          </li>
        </ul>
      </Card>
    </div>
  );
}
