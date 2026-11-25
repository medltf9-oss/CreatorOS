/**
 * First-run setup.
 *
 * The page observes the machine and asks the user to verify a downloaded file against the pinned
 * manifest. It never installs anything, never downloads on its own and never claims a component is
 * verified: the only `ok` it displays comes from a verification response or from the presence checks
 * the backend actually ran.
 */
import { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  Download,
  ExternalLink,
  Film,
  HardDrive,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  XCircle,
} from 'lucide-react';
import type { CreatorApi, PrerequisiteState, SetupCheck, VerifyResponse } from '../api';
import { Badge, Button, Card, EmptyState, Field, Row, Spinner, formatBytes } from '../components/ui';
import { useAsyncData } from './analytics/shared';
import { toneOf, verificationSummary } from '../setupModel';

type Props = { api: CreatorApi; onDone: () => void };

function StateIcon({ state }: { state: string }) {
  if (state === 'ok' || state === 'pass') return <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-emerald-400" />;
  if (state === 'fail' || state === 'missing') return <XCircle size={15} className="mt-0.5 shrink-0 text-rose-400" />;
  if (state === 'warn' || state === 'unverified') return <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-400" />;
  return <CircleSlash size={15} className="mt-0.5 shrink-0 text-slate-500" />;
}

function CheckList({ checks }: { checks: SetupCheck[] }) {
  if (checks.length === 0) return <EmptyState title="No checks were reported" hint="The backend returned no readiness findings." />;
  return (
    <ul className="space-y-2">
      {checks.map((check) => (
        <li key={check.id} className="flex items-start gap-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
          <StateIcon state={check.state} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11px] text-slate-500">{check.id}</span>
              <Badge tone={toneOf(check.state)}>{check.state}</Badge>
            </div>
            <p className="mt-1 break-words text-[12.5px] text-slate-400">{check.detail}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function PrerequisiteCard({
  component,
  api,
}: {
  component: PrerequisiteState;
  api: CreatorApi;
}) {
  const [draft, setDraft] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState<VerifyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [linkNote, setLinkNote] = useState<string | null>(null);

  async function verify() {
    setVerifying(true);
    setError(null);
    try {
      setResult(await api.verifyPrerequisite(component.id, draft.trim() || undefined));
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setVerifying(false);
    }
  }

  async function openDownload() {
    if (!component.download_url) return;
    const bridge = window.creatoros;
    if (!bridge?.openAuthUrl) {
      setLinkNote(component.download_url);
      return;
    }
    const opened = await bridge.openAuthUrl(component.download_url);
    setLinkNote(opened.ok ? 'Opened in your browser. Verify the file once it lands in Downloads.' : opened.error ?? 'Could not open a browser.');
  }

  return (
    <Card
      title={component.display_name}
      subtitle={`Pinned version ${component.version} · ${component.kind === 'vendor-installer' ? 'vendor installer' : 'local tool'}`}
      actions={<Badge tone={toneOf(component.state)}>{component.state}</Badge>}
    >
      <div className="space-y-1">
        <Row label="Where it lives" value={component.local_path ?? 'not found on this machine'} mono={false} />
        <Row label="Required" value={component.required ? 'yes' : 'optional'} mono={false} />
        <Row
          label="Bundling approved"
          value={component.redistribution_approved ? 'yes' : 'no'}
          tone={component.redistribution_approved ? 'good' : 'warn'}
          mono={false}
        />
      </div>

      {component.redistribution_detail ? (
        <p className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-[12px] leading-relaxed text-amber-200/90">
          {component.redistribution_detail}
        </p>
      ) : null}

      <p className="mt-3 text-[12.5px] leading-relaxed text-slate-400">{component.detail}</p>

      {component.download_url ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button variant="default" onClick={() => void openDownload()}>
            <ExternalLink size={13} /> Official download page
          </Button>
          <code className="break-all font-mono text-[10.5px] text-slate-500">{component.download_url}</code>
        </div>
      ) : null}
      {linkNote ? <p className="mt-2 break-all text-[11.5px] text-slate-500">{linkNote}</p> : null}

      <div className="mt-4 border-t border-slate-800 pt-4">
        <Field
          label="Verify a downloaded file"
          hint="Leave empty to use the path the backend found. Verification hashes the file and checks its signature; it never executes it."
          error={error}
        >
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              spellCheck={false}
              placeholder="C:\\Users\\you\\Downloads\\installer.exe"
              className="h-8 min-w-[260px] flex-1 rounded-[7px] border border-slate-700 bg-slate-950 px-2 font-mono text-[12px] text-slate-200 outline-none focus:border-sky-500"
            />
            <Button variant="primary" busy={verifying} onClick={() => void verify()}>
              <ShieldCheck size={13} /> Verify
            </Button>
          </div>
        </Field>

        {result ? (
          <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={result.ok ? 'good' : 'bad'}>{result.ok ? 'verified' : 'not verified'}</Badge>
              <span className="break-all font-mono text-[11px] text-slate-500">{result.path}</span>
            </div>
            <p className="mt-1.5 text-[12px] text-slate-400">{verificationSummary(result)}</p>
            <ul className="mt-2 space-y-1.5">
              {result.checks.map((check) => (
                <li key={check.id} className="flex items-start gap-2">
                  <StateIcon state={check.state} />
                  <div className="min-w-0 text-[12px]">
                    <span className="font-mono text-[11px] text-slate-400">{check.id}</span>
                    <span className="ml-2 break-words text-slate-500">{check.detail}</span>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export default function Setup({ api, onDone }: Props) {
  const status = useAsyncData((signal) => api.setupStatus(signal), [api]);
  const [finishing, setFinishing] = useState<'finish' | 'skip' | null>(null);
  const [finishError, setFinishError] = useState<string | null>(null);

  async function finish(skippedAi: boolean) {
    setFinishing(skippedAi ? 'skip' : 'finish');
    setFinishError(null);
    try {
      await api.completeSetup({ skipped_ai: skippedAi });
      onDone();
    } catch (err) {
      setFinishError(err instanceof Error ? err.message : String(err));
      setFinishing(null);
    }
  }

  const data = status.data;

  return (
    <div className="space-y-4">
      <Card
        title="First-run setup"
        subtitle="What this machine already has, what CreatorOS needs, and what only you can decide."
        actions={
          <Button variant="ghost" onClick={status.reload} busy={status.loading}>
            <RefreshCw size={13} /> Re-check
          </Button>
        }
      >
        {status.loading && !data ? <Spinner label="Reading this machine…" /> : null}
        {status.error ? (
          <p className="rounded-lg border border-rose-400/30 bg-rose-500/5 p-3 text-[12.5px] text-rose-300">
            Setup status failed: {status.error}
          </p>
        ) : null}
        {data ? (
          <div className="space-y-1">
            <Row label="CreatorOS version" value={data.version} />
            <Row label="Data root" value={data.data_root} />
            <Row
              label="First run recorded"
              value={data.first_run.completed ? data.first_run.completed_at ?? 'yes' : 'not yet'}
              tone={data.first_run.completed ? 'good' : 'warn'}
            />
            {data.first_run.completed ? (
              <p className="pt-2 text-[12px] text-slate-500">
                {data.first_run.skipped_ai
                  ? 'You finished setup without AI. You can install a local runtime later from this page.'
                  : 'Setup is complete. Re-running the wizard only refreshes these checks.'}
              </p>
            ) : null}
          </div>
        ) : null}
      </Card>

      {data ? (
        <>
          <Card title="Readiness" subtitle="Every row is something the backend observed just now.">
            <CheckList checks={data.checks} />
          </Card>

          <Card
            title="This machine"
            subtitle="Re-read on every visit; nothing here is cached or assumed."
            actions={
              <Badge tone={data.disk.writable ? 'good' : 'bad'}>
                <HardDrive size={11} className="mr-1" /> disk
              </Badge>
            }
          >
            <div className="space-y-1">
              <Row label="Operating system" value={`${String(data.system.os ?? '')} ${String(data.system.os_release ?? '')}`} mono={false} />
              <Row label="Python" value={String(data.system.python ?? 'unknown')} />
              <Row label="CPU threads" value={String(data.system.cpu_count ?? 'unknown')} />
              <Row
                label="Memory"
                value={
                  data.system.memory_total_bytes
                    ? `${formatBytes(Number(data.system.memory_total_bytes))} total`
                    : 'unreadable'
                }
              />
              <Row label="Free disk" value={`${formatBytes(data.disk.free_bytes)} of ${formatBytes(data.disk.total_bytes)}`} />
              <Row
                label="FFmpeg"
                value={data.ffmpeg.version ?? (data.ffmpeg.present ? data.ffmpeg.path : data.ffmpeg.error ?? 'not present')}
                tone={data.ffmpeg.present ? 'good' : 'bad'}
                mono={false}
              />
            </div>
          </Card>

          <Card
            title="Local AI runtime"
            subtitle="Optional. CreatorOS opens and runs media work without it; AI features stay unavailable until a runtime answers."
            actions={<Badge tone={data.ollama.installed ? 'good' : 'warn'}>{data.ollama.installed ? 'installed' : 'not installed'}</Badge>}
          >
            <div className="space-y-1">
              <Row label="Endpoint" value={data.ollama.endpoint} />
              <Row label="Reachable now" value={data.ollama.reachable ? 'yes' : 'no'} tone={data.ollama.reachable ? 'good' : 'warn'} mono={false} />
              <Row label="Version" value={data.ollama.version ?? 'not reported'} />
              <Row label="Install path" value={data.ollama.install_path ?? 'not found'} mono={false} />
            </div>
            {data.ollama.error ? <p className="mt-2 text-[11.5px] text-slate-500">{data.ollama.error}</p> : null}
            <div className="mt-3">
              {data.ollama.models.length === 0 ? (
                <p className="text-[12px] text-slate-500">No models are installed.</p>
              ) : (
                <ul className="space-y-1">
                  {data.ollama.models.map((model) => (
                    <li key={model.name} className="flex items-center justify-between gap-3 border-b border-slate-800/70 py-1.5 last:border-0">
                      <span className="truncate font-mono text-[12px] text-slate-300">{model.name}</span>
                      <span className="shrink-0 text-[11.5px] text-slate-500">{model.size_bytes ? formatBytes(model.size_bytes) : 'size unknown'}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Card>

          <Card
            title="Prerequisites"
            subtitle="Pinned in the release manifest. The wizard shows what is there and verifies bytes on request; acquiring and installing stays a user action."
            actions={<Film size={14} className="text-slate-500" />}
          >
            {data.prerequisites.length === 0 ? (
              <EmptyState
                title="The prerequisite manifest did not load"
                hint={data.manifest_error ?? 'No components were reported.'}
              />
            ) : (
              <div className="space-y-4">
                {data.prerequisites.map((component) => (
                  <PrerequisiteCard key={component.id} component={component} api={api} />
                ))}
              </div>
            )}
          </Card>

          <Card
            title="Finish"
            subtitle="Finishing records a decision in the database. It does not install, download or enable anything by itself."
            actions={<Sparkles size={14} className="text-slate-500" />}
          >
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" busy={finishing === 'finish'} disabled={finishing !== null} onClick={() => void finish(false)}>
                <Download size={13} /> Finish setup
              </Button>
              <Button variant="default" busy={finishing === 'skip'} disabled={finishing !== null} onClick={() => void finish(true)}>
                Skip AI setup
              </Button>
              <button type="button" className="text-[12px] text-slate-400 underline-offset-2 hover:underline" onClick={onDone}>
                Not now
              </button>
            </div>
            {finishError ? <p className="mt-2 text-[12px] text-rose-400">{finishError}</p> : null}
            <p className="mt-3 text-[11.5px] text-slate-500">
              You can return here at any time; the check results are re-read from this machine, not remembered.
            </p>
          </Card>
        </>
      ) : null}
    </div>
  );
}
