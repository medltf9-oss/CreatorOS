/**
 * Analytics Center — Reports (P8.23).
 *
 * The library, the creation form, the detail record and — the part that matters — verified download.
 *
 * Download discipline (plan §P8.23):
 *
 * 1. read the *metadata* record (`GET /analytics/reports/{id}`) to learn the expected digest;
 * 2. read the file as bytes through the authenticated client;
 * 3. hash exactly those bytes with WebCrypto SHA-256;
 * 4. compare; an empty body is a failure even before hashing, because a 0-byte file is not a report;
 * 5. only a verified payload is offered for saving. A mismatch is shown as an integrity error and the
 *    file is *not* saved, renamed or regenerated — a silent retry would hide a transport or storage bug.
 *
 * Reports are JSON today. The page offers no CSV/PDF: a format list the backend does not implement
 * would be a lie in a dropdown.
 */
import { useEffect, useRef, useState } from 'react';
import { type AnalyticsClient, type Report, type ReportCreate, type ReportFormat, type ReportKind } from '../../services/apiAnalytics';
import {
  REPORT_FORMATS,
  REPORT_KINDS,
  formatBytesDecimal,
  reportAccountId,
  reportFormatLabel,
  reportKindLabel,
  reportTitle,
  sectionSummary,
} from '../../components/analyticsDecisions';
import { type IntegrityReport, shortDigest, verifyReportBytes } from '../../components/reportIntegrity';
import { Badge, Button, Card, EmptyState, formatWhen } from '../../components/ui';
import { ErrorBlock, LoadingBlock, PageHeader, platformLabel, useAsyncData } from './shared';

type DownloadState =
  | { phase: 'idle' }
  | { phase: 'downloading' }
  | { phase: 'verified'; integrity: IntegrityReport; text: string; fileName: string }
  | { phase: 'failed'; integrity: IntegrityReport | null; message: string };

export default function Reports({ api, autoDownloadId = '' }: { api: AnalyticsClient; autoDownloadId?: string }) {
  const [kindFilter, setKindFilter] = useState<ReportKind | ''>('');
  const [selectedId, setSelectedId] = useState('');
  const [download, setDownload] = useState<DownloadState>({ phase: 'idle' });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [form, setForm] = useState<ReportCreate>({
    kind: 'overview',
    format: 'json',
    account_id: null,
    period_start: null,
    period_end: null,
    title: '',
  });
  const [deleteFor, setDeleteFor] = useState<Report | null>(null);
  const [deleteConfirmText, setDeleteConfirmText] = useState('');
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const list = useAsyncData(
    (signal) => api.reports({ kind: kindFilter || undefined, limit: 100 }, signal),
    [kindFilter],
  );

  const rows = list.data?.items ?? [];
  const selected = rows.find((row) => row.id === selectedId) ?? null;

  // The harness opens one download so the integrity gate itself can be screenshotted; the page never
  // downloads on its own without this prop.
  const autoRan = useRef(false);
  useEffect(() => {
    if (!autoDownloadId || autoRan.current || !rows.length) return;
    const target = rows.find((row) => row.id === autoDownloadId);
    if (!target) return;
    autoRan.current = true;
    setSelectedId(target.id);
    void startDownload(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoDownloadId, rows.length]);

  async function startDownload(report: Report) {
    setDownload({ phase: 'downloading' });
    try {
      // The trusted digest comes from the metadata record, never from inside the file itself.
      const metadata = await api.report(report.id);
      const payload = await api.downloadReport(report.id);
      const integrity = await verifyReportBytes(metadata.sha256, payload.bytes);
      if (!integrity.ok) {
        setDownload({ phase: 'failed', integrity, message: integrity.message });
        return;
      }
      setDownload({
        phase: 'verified',
        integrity,
        text: payload.text,
        fileName: `${report.id}-analytics-${metadata.kind}.${metadata.format}`,
      });
    } catch (error) {
      setDownload({ phase: 'failed', integrity: null, message: error instanceof Error ? error.message : String(error) });
    }
  }

  function saveVerified(fileName: string, text: string) {
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    // Revoke on the next tick so the click has consumed the URL.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  async function submitCreate() {
    setCreating(true);
    setCreateError(null);
    try {
      const created = await api.createReport({
        kind: form.kind,
        format: form.format,
        account_id: form.account_id || null,
        period_start: form.period_start || null,
        period_end: form.period_end || null,
        title: form.title?.trim() ? form.title.trim() : null,
      });
      setSelectedId(created.id);
      setDownload({ phase: 'idle' });
      list.reload();
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreating(false);
    }
  }

  async function submitDelete() {
    if (!deleteFor) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await api.deleteReport(deleteFor.id);
      if (selectedId === deleteFor.id) {
        setSelectedId('');
        setDownload({ phase: 'idle' });
      }
      setDeleteFor(null);
      setDeleteConfirmText('');
      list.reload();
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error));
    } finally {
      setDeleteBusy(false);
    }
  }

  const accountLabel = (report: Report) => {
    const id = reportAccountId(report);
    if (!id) return 'all accounts';
    const account = (accounts.data?.items ?? []).find((item) => item.id === id);
    return account ? `${platformLabel(account.platform)} · ${account.display_name}` : id;
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Reports"
        subtitle="Stored report records with a verified download. Generation is synchronous: the record you see is already final."
        actions={<Button onClick={list.reload} busy={list.loading}>Refresh</Button>}
      />

      <ErrorBlock error={accounts.error ?? list.error} onRetry={list.reload} />

      <Card title="Create a report" subtitle="Built from stored aggregations and stored recommendations — no provider call is made">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Kind</span>
            <select
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={form.kind}
              onChange={(event) => setForm({ ...form, kind: event.target.value as ReportKind })}
            >
              {REPORT_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {reportKindLabel(kind)}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Format</span>
            <select
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={form.format}
              onChange={(event) => setForm({ ...form, format: event.target.value as ReportFormat })}
            >
              {REPORT_FORMATS.map((format) => (
                <option key={format} value={format}>
                  {reportFormatLabel(format)}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Account</span>
            <select
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={form.account_id ?? ''}
              onChange={(event) => setForm({ ...form, account_id: event.target.value || null })}
            >
              <option value="">all accounts</option>
              {(accounts.data?.items ?? []).map((account) => (
                <option key={account.id} value={account.id}>
                  {platformLabel(account.platform)} · {account.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Title (optional)</span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={form.title ?? ''}
              onChange={(event) => setForm({ ...form, title: event.target.value })}
              placeholder="Quarterly performance review"
            />
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Period start (optional)</span>
            <input
              type="date"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={(form.period_start ?? '').slice(0, 10)}
              onChange={(event) => setForm({ ...form, period_start: event.target.value || null })}
            />
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Period end (optional)</span>
            <input
              type="date"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={(form.period_end ?? '').slice(0, 10)}
              onChange={(event) => setForm({ ...form, period_end: event.target.value || null })}
            />
          </label>
          <div className="flex items-end">
            <Button variant="primary" busy={creating} onClick={submitCreate}>
              Generate report
            </Button>
          </div>
        </div>
        <p className="mt-3 text-[11px] text-slate-500">
          JSON, CSV and PDF are generated from the same content model, in the same directory, each with a
          digest taken from the bytes written. Nothing else is offered because nothing else is implemented.
        </p>
        {createError ? (
          <div className="mt-2">
            <ErrorBlock error={createError} onRetry={() => setCreateError(null)} />
          </div>
        ) : null}
      </Card>

      <Card
        title="Library"
        subtitle="Newest first"
        actions={
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Kind
            <select
              className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
              value={kindFilter}
              onChange={(event) => setKindFilter(event.target.value as ReportKind | '')}
            >
              <option value="">any</option>
              {REPORT_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {reportKindLabel(kind)}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {list.loading && !list.data ? <LoadingBlock label="Loading reports..." /> : null}
        {list.data && rows.length === 0 ? (
          <EmptyState title="No reports yet" hint="Generate one above; it is built from data already stored, so it needs no provider call." />
        ) : null}
        {rows.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-1 pr-3">Report</th>
                  <th className="py-1 pr-3">Kind</th>
                  <th className="py-1 pr-3">Created</th>
                  <th className="py-1 pr-3">Range</th>
                  <th className="py-1 pr-3">Scope</th>
                  <th className="py-1 pr-3">Status</th>
                  <th className="py-1 pr-3">Format</th>
                  <th className="py-1 pr-3">Size</th>
                  <th className="py-1 pr-3">SHA-256</th>
                  <th className="py-1">&nbsp;</th>
                </tr>
              </thead>
              <tbody className="text-slate-300">
                {rows.map((report) => (
                  <tr key={report.id} className={`border-t border-slate-800/70 ${selectedId === report.id ? 'bg-sky-500/5' : ''}`}>
                    <td className="py-1 pr-3">
                      <button className="text-left text-slate-200 underline-offset-2 hover:underline" onClick={() => { setSelectedId(report.id); setDownload({ phase: 'idle' }); }}>
                        {reportTitle(report)}
                      </button>
                      <span className="block font-mono text-[10px] text-slate-500">{report.id}</span>
                    </td>
                    <td className="py-1 pr-3">{reportKindLabel(report.kind)}</td>
                    <td className="whitespace-nowrap py-1 pr-3 text-slate-500">{formatWhen(report.created_at)}</td>
                    <td className="whitespace-nowrap py-1 pr-3 text-slate-500">
                      {report.period_start || report.period_end
                        ? `${(report.period_start ?? '…').slice(0, 10)} → ${(report.period_end ?? '…').slice(0, 10)}`
                        : 'whole history'}
                    </td>
                    <td className="py-1 pr-3 text-slate-500">{accountLabel(report)}</td>
                    <td className="py-1 pr-3">
                      <Badge tone={report.status === 'final' ? 'good' : report.status === 'archived' ? 'muted' : 'warn'}>{report.status}</Badge>
                    </td>
                    <td className="py-1 pr-3 uppercase text-slate-500">{report.format}</td>
                    <td className="whitespace-nowrap py-1 pr-3 text-slate-500">{formatBytesDecimal(report.bytes)}</td>
                    <td
                      className="whitespace-nowrap py-1 pr-3 font-mono text-[10px] text-slate-500"
                      title={report.sha256 ? `SHA-256 ${report.sha256}` : 'no digest stored — the download will refuse'}
                    >
                      {shortDigest(report.sha256)}
                    </td>
                    <td className="py-1">
                      <div className="flex flex-nowrap justify-end gap-1">
                        <Button
                          variant="ghost"
                          onClick={() => startDownload(report)}
                          busy={download.phase === 'downloading' && selectedId === report.id}
                          title="Fetch the file, verify its SHA-256 against the record, then offer it"
                        >
                          Download
                        </Button>
                        <Button variant="ghost" onClick={() => { setDeleteFor(report); setDeleteConfirmText(''); setDeleteError(null); }}>
                          Delete
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Card>

      {download.phase === 'verified' ? (
        <Card title="Download verified" subtitle="The bytes that arrived hash to the digest the record stored">
          <div className="grid gap-1 text-xs sm:grid-cols-2">
            <Line label="Expected (record)" value={download.integrity.expected ?? '—'} mono />
            <Line label="Computed (received bytes)" value={download.integrity.actual ?? '—'} mono tone="good" />
            <Line label="Size" value={formatBytesDecimal(download.integrity.bytes)} />
            <Line label="File name" value={download.fileName} mono />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={() => saveVerified(download.fileName, download.text)}>
              Save verified file
            </Button>
            <Button variant="ghost" onClick={() => setDownload({ phase: 'idle' })}>
              Dismiss
            </Button>
            <span className="text-[11px] text-slate-500">{download.integrity.message}</span>
          </div>
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-slate-400">Payload preview ({formatBytesDecimal(download.integrity.bytes)})</summary>
            <pre className="mt-2 max-h-64 overflow-auto rounded-lg border border-slate-800 bg-slate-950 p-2 text-[10px] text-slate-400">
              {download.text.slice(0, 4000)}
            </pre>
          </details>
        </Card>
      ) : null}

      {download.phase === 'failed' ? (
        <Card title="Download not accepted" subtitle="Integrity gate — nothing was saved">
          <div className="rounded-lg border border-rose-500/40 bg-rose-500/5 px-3 py-2 text-sm text-rose-200">
            {download.message}
          </div>
          {download.integrity ? (
            <div className="mt-2 grid gap-1 text-xs sm:grid-cols-2">
              <Line label="Expected (record)" value={download.integrity.expected ?? 'none stored'} mono />
              <Line label="Computed (received)" value={download.integrity.actual ?? 'not computed'} mono tone="bad" />
              <Line label="Received size" value={formatBytesDecimal(download.integrity.bytes)} />
              <Line label="Reason" value={download.integrity.failure ?? 'unknown'} mono />
            </div>
          ) : null}
          <p className="mt-2 text-[11px] text-slate-500">
            The file is not offered for saving and was not renamed or regenerated. Re-read the report record and try
            again; a repeated mismatch points at storage or transport, not at a retry.
          </p>
          <div className="mt-2">
            <Button variant="ghost" onClick={() => setDownload({ phase: 'idle' })}>Dismiss</Button>
          </div>
        </Card>
      ) : null}

      {selected ? <ReportDetail report={selected} accountLabel={accountLabel(selected)} /> : null}

      {deleteFor ? (
        <Card title="Delete report" subtitle="Separate confirmation — the stored file is removed with the record">
          <p className="text-sm text-slate-300">
            Deleting <span className="font-mono text-slate-200">{deleteFor.id}</span> ({reportTitle(deleteFor)}) removes
            the report record and its stored file. This cannot be undone, and the analytics measurements the report was
            built from are not affected.
          </p>
          <label className="mt-2 block text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Type the report id to confirm</span>
            <input
              className="mt-1 w-full max-w-md rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 font-mono text-xs text-slate-200"
              value={deleteConfirmText}
              onChange={(event) => setDeleteConfirmText(event.target.value)}
              placeholder={deleteFor.id}
            />
          </label>
          {deleteError ? (
            <div className="mt-2">
              <ErrorBlock error={deleteError} onRetry={() => setDeleteError(null)} />
            </div>
          ) : null}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              variant="danger"
              busy={deleteBusy}
              disabled={deleteConfirmText.trim() !== deleteFor.id}
              onClick={submitDelete}
            >
              Delete permanently
            </Button>
            <Button variant="ghost" disabled={deleteBusy} onClick={() => { setDeleteFor(null); setDeleteConfirmText(''); }}>
              Cancel
            </Button>
            {deleteConfirmText.trim() !== deleteFor.id ? (
              <span className="text-[11px] text-amber-400">The typed id does not match.</span>
            ) : null}
          </div>
        </Card>
      ) : null}
    </div>
  );
}

function ReportDetail({ report, accountLabel }: { report: Report; accountLabel: string }) {
  return (
    <Card title={`Report ${report.id}`} subtitle="Configuration, source, sections and file information">
      <div className="grid gap-1 text-xs sm:grid-cols-2">
        <Line label="Title" value={reportTitle(report)} />
        <Line label="Kind" value={reportKindLabel(report.kind)} />
        <Line label="Status" value={report.status} />
        <Line label="Created" value={formatWhen(report.created_at)} />
        <Line label="Generated by" value={report.generated_by} />
        <Line label="Scope" value={accountLabel} />
        <Line
          label="Period"
          value={
            report.period_start || report.period_end
              ? `${(report.period_start ?? '…').slice(0, 10)} → ${(report.period_end ?? '…').slice(0, 10)}`
              : 'whole stored history'
          }
        />
        <Line label="Format" value={`${report.format} (implemented: ${REPORT_FORMATS.join(', ')})`} />
        <Line label="File size" value={`${formatBytesDecimal(report.bytes)} (${report.bytes} bytes)`} />
        <Line label="SHA-256" value={report.sha256 ?? 'no digest stored — download will refuse'} mono />
        <Line label="Sections" value={`${report.sections.length}`} />
        <Line label="Filters" value={JSON.stringify(report.filters)} mono />
      </div>

      {report.summary ? (
        <div className="mt-3">
          <p className="text-[10px] uppercase tracking-wide text-slate-500">Summary</p>
          <p className="mt-1 whitespace-pre-wrap text-sm text-slate-200">{report.summary}</p>
        </div>
      ) : (
        <p className="mt-3 text-xs text-amber-400">The record carries no summary.</p>
      )}

      <div className="mt-3">
        <p className="text-[10px] uppercase tracking-wide text-slate-500">Sections (source measurements)</p>
        {report.sections.length === 0 ? (
          <p className="mt-1 text-xs text-amber-400">No sections: the report was generated without stored data.</p>
        ) : (
          <div className="mt-1 divide-y divide-slate-800/70">
            {report.sections.map((section, index) => (
              <details key={`${section.heading}-${index}`} className="py-1">
                <summary className="cursor-pointer text-xs text-slate-300">
                  <span className="text-slate-200">{section.heading}</span>
                  <span className="ml-2 text-slate-500">{section.type}</span>
                  <span className="ml-2 text-slate-600">{sectionSummary(section)}</span>
                </summary>
                <pre className="mt-1 max-h-64 overflow-auto rounded-lg border border-slate-800 bg-slate-950 p-2 text-[10px] text-slate-400">
                  {JSON.stringify(section.body, null, 2)}
                </pre>
              </details>
            ))}
          </div>
        )}
      </div>

      <div className="mt-3 grid gap-1 text-xs sm:grid-cols-2">
        <Line label="Missing data" value="carried inside the sections: a metric the provider did not return stays absent, never zero" />
        <Line
          label="Warnings"
          value="the report schema has no warnings array; the generator records limits inside the section bodies"
        />
      </div>
    </Card>
  );
}

function Line({ label, value, mono = false, tone }: { label: string; value: string; mono?: boolean; tone?: 'good' | 'bad' }) {
  const toneClass = tone === 'good' ? 'text-emerald-300' : tone === 'bad' ? 'text-rose-300' : 'text-slate-200';
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-slate-800/60 py-1">
      <span className="text-[10px] uppercase tracking-wide text-slate-500">{label}</span>
      <span className={`min-w-0 break-all text-right ${toneClass} ${mono ? 'font-mono text-[11px]' : ''}`}>{value}</span>
    </div>
  );
}
