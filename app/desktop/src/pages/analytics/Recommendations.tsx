/**
 * Analytics Center — Recommendations (P8.23).
 *
 * A reading surface for what the deterministic engine detected: every field of `RecommendationOut`
 * plus the evidence from `GET /analytics/recommendations/{id}`, with the mandated fields checked for
 * presence rather than assumed.
 *
 * The decision rules this page exists to protect (plan §13.3, requirement 8):
 *
 * * **accepting is not publishing.** Accepting records a hash-bound approval request *for the
 *   experiment*; it creates no publish job, schedules nothing and approves nothing. There is no
 *   publish control on this page and no call to the publishing client.
 * * **two decisions, two confirmations.** Acceptance and rejection each have their own panel with the
 *   full context, their own wording and their own final button. One generic "are you sure" for both
 *   would erase the difference between endorsing an experiment and refusing one.
 * * **the server decides.** Nothing is marked accepted or rejected locally: the row is replaced by
 *   the response, and a 409 (already decided) is shown as a conflict and re-read from the server.
 */
import { useMemo, useState } from 'react';
import { type AnalyticsClient, type MetricValue, type Recommendation, type RecommendationKind, type RecommendationStatus } from '../../services/apiAnalytics';
import {
  RECOMMENDATION_CATEGORIES,
  RECOMMENDATION_STATUSES,
  benefitIsEstimate,
  benefitKindLabel,
  canDecide,
  categoryLabel,
  decisionBlockedReason,
  evidenceSummary,
  evidenceView,
  filterRecommendations,
  missingFields,
  sampleSizeLabel,
} from '../../components/analyticsDecisions';
import { Badge, Button, Card, EmptyState, formatWhen } from '../../components/ui';
import {
  AvailabilityChip,
  DateRangePicker,
  ErrorBlock,
  LoadingBlock,
  PageHeader,
  platformLabel,
  useAsyncData,
  type RangeValue,
  defaultRange,
} from './shared';

const PLATFORMS = ['facebook', 'instagram', 'tiktok'] as const;
const WINDOWS = ['h24', 'h48', 'd7', 'd28'] as const;

type DecisionMode = null | 'accept' | 'reject';

export default function Recommendations({
  api,
  initialRecommendationId = '',
  initialDecision = null,
}: {
  api: AnalyticsClient;
  /** Prefill used by the visual harness (and by a deep link); the page works without it. */
  initialRecommendationId?: string;
  /** Opens one of the two confirmation panels immediately; used by the harness for screenshots. */
  initialDecision?: DecisionMode;
}) {
  const [statusFilter, setStatusFilter] = useState<RecommendationStatus | ''>('');
  const [kindFilter, setKindFilter] = useState('');
  const [accountId, setAccountId] = useState('');
  const [platform, setPlatform] = useState('');
  const [range, setRange] = useState<RangeValue>(() => defaultRange('90d'));
  const [selectedId, setSelectedId] = useState(initialRecommendationId);
  const [decision, setDecision] = useState<DecisionMode>(initialDecision);
  const [note, setNote] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionInfo, setActionInfo] = useState<string | null>(null);
  const [refreshWindow, setRefreshWindow] = useState<(typeof WINDOWS)[number]>('h24');

  const accounts = useAsyncData((signal) => api.accounts({ limit: 100 }, signal), []);
  const list = useAsyncData(
    (signal) =>
      api.recommendations(
        {
          status: statusFilter || undefined,
          account_id: accountId || undefined,
          kind: (kindFilter || undefined) as RecommendationKind | undefined,
          limit: 100,
        },
        signal,
      ),
    [statusFilter, accountId, kindFilter],
  );

  const rows = useMemo(
    () =>
      filterRecommendations(list.data?.items ?? [], {
        status: statusFilter,
        kind: kindFilter,
        accountId,
        platform,
        start: range.start,
        end: range.end,
      }),
    [list.data, statusFilter, kindFilter, accountId, platform, range.start, range.end],
  );

  const selected = rows.find((row) => row.id === selectedId) ?? rows[0] ?? null;
  const detail = useAsyncData(
    (signal) => (selected ? api.recommendation(selected.id, signal) : Promise.resolve(null)),
    [selected?.id ?? ''],
  );

  // Evidence is re-read from the ledger: the ids an item cites are looked up in the stored snapshots,
  // so "resolved" means "this row exists", not "this JSON parsed".
  const evidenceMetricKeys = useMemo(() => {
    const items = detail.data?.evidence ?? [];
    const keys = new Set<string>();
    for (const item of items) if (item.metric_key) keys.add(item.metric_key);
    return [...keys].sort();
  }, [detail.data]);

  const evidenceSnapshots = useAsyncData(
    (signal) =>
      selected && evidenceMetricKeys.length
        ? api.snapshots({ account_id: selected.account_id ?? undefined, limit: 200 }, signal)
        : Promise.resolve(null),
    [selected?.account_id ?? '', evidenceMetricKeys.join(',')],
  );

  const foundSnapshotIds = useMemo(
    () => new Set((evidenceSnapshots.data?.items ?? []).map((row) => row.id)),
    [evidenceSnapshots.data],
  );

  const evidenceViews = useMemo(
    () => (detail.data?.evidence ?? []).map((item) => evidenceView(item, foundSnapshotIds)),
    [detail.data, foundSnapshotIds],
  );
  const evidenceCounts = useMemo(() => evidenceSummary(evidenceViews), [evidenceViews]);

  function reloadAll() {
    accounts.reload();
    list.reload();
    detail.reload();
    evidenceSnapshots.reload();
  }

  async function submitAccept() {
    if (!selected) return;
    setBusy(true);
    setActionError(null);
    setActionInfo(null);
    try {
      // No optimistic update: the row below is whatever the server returned.
      const updated = await api.acceptRecommendation(selected.id, { note: note.trim() ? note.trim() : null });
      setActionInfo(
        `Acceptance recorded for ${updated.id}. An approval request for the experiment was created` +
          (updated.approval_request_id ? ` (${updated.approval_request_id})` : '') +
          '. Nothing was published, scheduled or approved for publishing.',
      );
      setDecision(null);
      setNote('');
      reloadAll();
    } catch (error) {
      setActionError(explainDecisionError(error, 'accept'));
    } finally {
      setBusy(false);
    }
  }

  async function submitReject() {
    if (!selected) return;
    if (!reason.trim()) {
      setActionError('A rejection needs a reason: it is stored with the decision and read by whoever reviews the experiment later.');
      return;
    }
    setBusy(true);
    setActionError(null);
    setActionInfo(null);
    try {
      const updated = await api.rejectRecommendation(selected.id, { reason: reason.trim() });
      setActionInfo(`Rejection recorded for ${updated.id}. The reason is stored with the decision.`);
      setDecision(null);
      setReason('');
      reloadAll();
    } catch (error) {
      setActionError(explainDecisionError(error, 'reject'));
    } finally {
      setBusy(false);
    }
  }

  async function generateDrafts() {
    if (!accountId) {
      setActionError('Choose an account first: drafts are generated per account from its own measured publications.');
      return;
    }
    setBusy(true);
    setActionError(null);
    setActionInfo(null);
    try {
      const result = await api.refreshRecommendations({ account_id: accountId, window_kind: refreshWindow, limit: 10 });
      setActionInfo(
        `${result.created} draft(s) generated for this account at the ${refreshWindow} window. ` +
          'They are drafts: deciding one records an approval request for the experiment and publishes nothing.',
      );
      reloadAll();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Recommendations"
        subtitle="Evidence-backed drafts from the deterministic engine. Deciding one records a decision; it never publishes."
        actions={
          <>
            <DateRangePicker value={range} onChange={setRange} />
            <Button onClick={reloadAll} busy={list.loading}>
              Refresh
            </Button>
          </>
        }
      />

      <div className="rounded-lg border border-sky-500/30 bg-sky-500/5 px-3 py-2 text-sm text-sky-200">
        Accepting a recommendation records an approval request for the <strong>experiment</strong>. It does not
        publish, schedule, or approve publishing: carrying out the experiment is a separate workflow with its own
        approval.
      </div>

      <ErrorBlock error={accounts.error ?? list.error ?? detail.error} onRetry={reloadAll} />
      {actionError ? <ErrorBlock error={actionError} onRetry={() => setActionError(null)} /> : null}
      {actionInfo ? (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-300">
          {actionInfo}
        </div>
      ) : null}

      <Card title="Filters" subtitle="Status, account and category are applied by the API; platform and dates are applied here">
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Status</span>
            <select
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value as RecommendationStatus | '')}
            >
              <option value="">any</option>
              {RECOMMENDATION_STATUSES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Category</span>
            <select
              className="mt-1 max-w-[14rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={kindFilter}
              onChange={(event) => setKindFilter(event.target.value)}
            >
              <option value="">any</option>
              {RECOMMENDATION_CATEGORIES.map((category) => (
                <option key={category.kind} value={category.kind}>
                  {category.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Account</span>
            <select
              className="mt-1 max-w-[18rem] rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={accountId}
              onChange={(event) => setAccountId(event.target.value)}
            >
              <option value="">any</option>
              {(accounts.data?.items ?? []).map((account) => (
                <option key={account.id} value={account.id}>
                  {platformLabel(account.platform)} · {account.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Platform (client-side)</span>
            <select
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={platform}
              onChange={(event) => setPlatform(event.target.value)}
            >
              <option value="">any</option>
              {PLATFORMS.map((value) => (
                <option key={value} value={value}>
                  {platformLabel(value)}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">Window for new drafts</span>
            <select
              className="mt-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
              value={refreshWindow}
              onChange={(event) => setRefreshWindow(event.target.value as (typeof WINDOWS)[number])}
            >
              {WINDOWS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <Button onClick={generateDrafts} busy={busy} disabled={!accountId} title={accountId ? 'Generate drafts from this account' : 'Choose an account first'}>
            Generate drafts
          </Button>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          {rows.length} record(s) shown. Generating drafts runs the deterministic engine for one account and stores
          new drafts; it publishes nothing and decides nothing.
        </p>
      </Card>

      {list.loading && !list.data ? <LoadingBlock label="Loading recommendations..." /> : null}

      {rows.length === 0 && list.data ? (
        <EmptyState
          title="No recommendation matches"
          hint="The engine emits a candidate only when a rule passes its sample gate. Choose an account and generate drafts, or widen the filters."
        />
      ) : null}

      {rows.length > 0 ? (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[22rem_minmax(0,1fr)]">
          <div className="min-w-0">
          <Card title="Records" subtitle="Newest first">
            <div className="max-h-[38rem] space-y-1 overflow-auto">
              {rows.map((row) => (
                <button
                  key={row.id}
                  onClick={() => {
                    setSelectedId(row.id);
                    setDecision(null);
                  }}
                  className={`w-full rounded-lg border px-2 py-2 text-left text-xs transition ${
                    selected?.id === row.id
                      ? 'border-sky-500/60 bg-sky-500/10'
                      : 'border-slate-800 bg-slate-900/40 hover:border-slate-700'
                  }`}
                >
                  <span className="flex items-center gap-2">
                    <Badge tone={row.status === 'accepted' ? 'good' : row.status === 'rejected' ? 'bad' : 'muted'}>
                      {row.status}
                    </Badge>
                    <span className="text-slate-400">{categoryLabel(row.kind)}</span>
                    {row.platform ? <span className="text-slate-600">{platformLabel(row.platform)}</span> : null}
                  </span>
                  <span className="mt-1 block truncate text-slate-200">{row.observation}</span>
                  <span className="mt-1 block text-[10px] text-slate-500">
                    {formatWhen(row.created_at)} · {row.id}
                  </span>
                </button>
              ))}
            </div>
          </Card>
          </div>

          {selected ? (
            <div className="min-w-0 space-y-5">
              <RecommendationRecord recommendation={selected} accounts={accounts.data?.items ?? []} />

              <Card
                title="Evidence"
                subtitle="Read from the record and re-resolved against the stored ledger"
                actions={
                  <span className="flex flex-wrap gap-2 text-[11px]">
                    <Badge tone={evidenceCounts.missing ? 'bad' : 'muted'}>resolved {evidenceCounts.resolved}</Badge>
                    {evidenceCounts.missing ? <Badge tone="bad">missing {evidenceCounts.missing}</Badge> : null}
                    {evidenceCounts.not_observed ? <Badge tone="warn">not observed {evidenceCounts.not_observed}</Badge> : null}
                    {evidenceCounts.stale ? <Badge tone="warn">stale {evidenceCounts.stale}</Badge> : null}
                    {evidenceCounts.unreferenced ? <Badge tone="warn">unreferenced {evidenceCounts.unreferenced}</Badge> : null}
                  </span>
                }
              >
                {detail.loading && !detail.data ? (
                  <LoadingBlock label="Loading evidence..." />
                ) : evidenceViews.length === 0 ? (
                  <EmptyState
                    title="No evidence items"
                    hint="This record carries no evidence rows. A recommendation without evidence cannot be verified, and the engine refuses to store one — treat this as a defect."
                  />
                ) : (
                  <EvidenceTable items={evidenceViews} />
                )}
                <p className="mt-3 text-[11px] text-slate-500">
                  Resolution checks the cited snapshot ids against the account&apos;s stored ledger. An item is{' '}
                  <em>stale</em> when it was collected more than 30 days ago, <em>not observed</em> when the provider
                  returned no value (never a zero), and <em>unreferenced</em> when it cites no stored row. An item marked
                  missing is treated as a defect to investigate, not as evidence.
                </p>
              </Card>

              <Card
                title="Decision"
                subtitle="Accepting records an approval request for the experiment; rejecting records a reason. Neither publishes."
              >
                {!canDecide(selected) ? (
                  <div className="space-y-2">
                    <p className="text-sm text-slate-300">{decisionBlockedReason(selected)}</p>
                    <DecisionRecord recommendation={selected} />
                  </div>
                ) : decision === 'accept' ? (
                  <AcceptConfirmation
                    recommendation={selected}
                    note={note}
                    onNote={setNote}
                    busy={busy}
                    onCancel={() => {
                      setDecision(null);
                      setNote('');
                    }}
                    onConfirm={submitAccept}
                  />
                ) : decision === 'reject' ? (
                  <RejectConfirmation
                    recommendation={selected}
                    reason={reason}
                    onReason={setReason}
                    busy={busy}
                    onCancel={() => {
                      setDecision(null);
                      setReason('');
                    }}
                    onConfirm={submitReject}
                  />
                ) : (
                  <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Button variant="primary" onClick={() => setDecision('accept')}>
                        Review acceptance…
                      </Button>
                      <Button variant="danger" onClick={() => setDecision('reject')}>
                        Review rejection…
                      </Button>
                    </div>
                    <p className="text-[11px] text-slate-500">
                      Each decision has its own confirmation. There is no publish action here: running the proposed
                      experiment is a separate workflow with its own approval.
                    </p>
                  </div>
                )}
              </Card>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function RecommendationRecord({
  recommendation,
  accounts,
}: {
  recommendation: Recommendation;
  accounts: { id: string; display_name: string; platform: string }[];
}) {
  const missing = missingFields(recommendation);
  const account = accounts.find((item) => item.id === recommendation.account_id) ?? null;
  return (
    <Card title="Recommendation record" subtitle="Every field the API returns, including the ones a card would tidy away">
      {missing.length ? (
        <div className="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-300">
          This record is missing mandated field(s): {missing.join(', ')}. It should not have been stored.
        </div>
      ) : null}
      <div className="grid gap-2 text-xs sm:grid-cols-2">
        <Field label="Recommendation id" value={<span className="font-mono">{recommendation.id}</span>} />
        <Field label="Category" value={categoryLabel(recommendation.kind)} />
        <Field
          label="Status"
          value={
            <Badge tone={recommendation.status === 'accepted' ? 'good' : recommendation.status === 'rejected' ? 'bad' : 'muted'}>
              {recommendation.status}
            </Badge>
          }
        />
        <Field label="Created" value={formatWhen(recommendation.created_at)} />
        <Field
          label="Account"
          value={
            account
              ? `${platformLabel(account.platform)} · ${account.display_name}`
              : recommendation.account_id ?? 'not attributed'
          }
        />
        <Field label="Platform" value={recommendation.platform ? platformLabel(recommendation.platform) : 'not recorded'} />
        <Field label="Campaign" value={recommendation.campaign_id ?? '—'} />
        <Field label="Content project" value={recommendation.content_project_id ?? '—'} />
        <Field label="Generated by" value={recommendation.generated_by} />
        <Field
          label="Confidence"
          wrap
          value={
            recommendation.confidence === null
              ? `not stated (${recommendation.confidence_method ?? 'no method'})`
              : `${(recommendation.confidence * 100).toFixed(1)}% · ${recommendation.confidence_method ?? 'method not stated'}`
          }
        />
        <Field label="Sample size" value={sampleSizeLabel(recommendation.sample_size)} />
        <Field
          label="Observation period"
          value={
            recommendation.period_start || recommendation.period_end
              ? `${recommendation.period_start ? formatWhen(recommendation.period_start) : '…'} → ${
                  recommendation.period_end ? formatWhen(recommendation.period_end) : '…'
                }`
              : 'not a period-based record'
          }
        />
        <Field
          label="Expected benefit"
          value={
            <span className="inline-flex items-center justify-end gap-2">
              {benefitKindLabel(recommendation)}
              {benefitIsEstimate(recommendation) ? <Badge tone="warn">estimate</Badge> : null}
            </span>
          }
        />
        <Field label="Expiration" value="not part of the record (the schema has no expiry field)" />
      </div>

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-slate-400">Observation (the record&apos;s narrative)</summary>
        <p className="mt-2 whitespace-pre-wrap text-sm text-slate-200">{recommendation.observation}</p>
      </details>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <DictBlock title="Proposed experiment" data={recommendation.proposed_experiment} />
        <DictBlock title="Expected benefit (raw)" data={recommendation.expected_benefit} />
      </div>

      <div className="mt-3">
        <p className="text-[10px] uppercase tracking-wide text-slate-500">Limitations</p>
        {recommendation.limitations.length ? (
          <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-amber-400">
            {recommendation.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-xs text-rose-400">No limitations recorded — a defect for this record.</p>
        )}
      </div>

      <div className="mt-3 grid gap-1 text-xs sm:grid-cols-2">
        <Field label="Sample-size detail" value={JSON.stringify(recommendation.sample_size)} mono />
        <Field
          label="Approval request"
          value={recommendation.approval_request_id ?? 'none (decide to create one)'}
          mono={Boolean(recommendation.approval_request_id)}
        />
      </div>
      <p className="mt-2 text-[11px] text-slate-500">
        The API schema carries no title, priority or expiry field for a recommendation: the category is{' '}
        <span className="font-mono">kind</span> and the narrative is <span className="font-mono">observation</span>. This
        page shows those rather than inventing the missing ones.
      </p>
    </Card>
  );
}

function EvidenceTable({ items }: { items: ReturnType<typeof evidenceView>[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3">Metric</th>
            <th className="py-1 pr-3">Value</th>
            {/* Narrow screens keep the three columns a reader needs: what, how much, and whether it
                resolves. The rest stay available from 768px up. */}
            <th className="hidden py-1 pr-3 md:table-cell">Unit</th>
            <th className="hidden py-1 pr-3 md:table-cell">Period</th>
            <th className="hidden py-1 pr-3 md:table-cell">Collected</th>
            <th className="hidden py-1 pr-3 md:table-cell">Sample</th>
            <th className="hidden py-1 pr-3 md:table-cell">Refs</th>
            <th className="py-1">Resolution</th>
          </tr>
        </thead>
        <tbody className="text-slate-300">
          {items.map((item, index) => (
            <tr key={`${item.metricKey}-${index}`} className="border-t border-slate-800/70">
              <td className="py-1 pr-3">
                <span className="font-mono text-slate-200">{item.metricKey}</span>
                {item.providerMetricName ? <span className="ml-2 text-slate-500">{item.providerMetricName}</span> : null}
              </td>
              <td className="py-1 pr-3 font-mono">{item.value === null ? '—' : item.value.toLocaleString('en-US')}</td>
              <td className="hidden py-1 pr-3 text-slate-500 md:table-cell">{item.unit || '—'}</td>
              <td className="hidden py-1 pr-3 text-slate-500 md:table-cell">{item.periodKind || '—'}</td>
              <td className="hidden py-1 pr-3 text-slate-500 md:table-cell">{item.collectedAt ? formatWhen(item.collectedAt) : '—'}</td>
              <td className="hidden py-1 pr-3 text-slate-500 md:table-cell">{item.sampleSize ?? '—'}</td>
              <td className="hidden py-1 pr-3 md:table-cell">
                <span className="text-slate-500" title={`snapshots: ${item.snapshotIds.join(', ') || 'none'}; aggregations: ${item.aggregationIds.join(', ') || 'none'}`}>
                  {item.snapshotIds.length}s/{item.aggregationIds.length}a
                </span>
              </td>
              <td className="py-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="shrink-0 whitespace-nowrap">
                    <AvailabilityChip availability={item.availability as never} />
                  </span>
                  <Badge tone={item.state === 'resolved' ? 'good' : item.state === 'missing' ? 'bad' : 'warn'}>{item.state}</Badge>
                  <span className="text-[10px] text-slate-500" title={item.detail}>
                    {item.detail}
                  </span>
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AcceptConfirmation({
  recommendation,
  note,
  onNote,
  busy,
  onCancel,
  onConfirm,
}: {
  recommendation: Recommendation;
  note: string;
  onNote: (value: string) => void;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="rounded-lg border border-sky-500/40 bg-sky-500/5 p-3" data-confirmation="accept">
      <p className="text-sm font-semibold text-sky-200">Accept this recommendation?</p>
      <p className="mt-1 text-xs text-slate-300">
        You are endorsing the <strong>proposed experiment</strong> below, and the decision is recorded against the
        recommendation. An approval request for the experiment is created; nothing is published, scheduled or
        approved for publishing.
      </p>
      <div className="mt-2 grid gap-1 text-xs">
        <Field label="Recommendation" value={<span className="font-mono">{recommendation.id}</span>} />
        <Field label="Category" value={categoryLabel(recommendation.kind)} />
        <Field label="Sample size" value={sampleSizeLabel(recommendation.sample_size)} wrap />
        <Field
          label="Confidence"
          wrap
          value={recommendation.confidence === null ? 'not stated' : `${(recommendation.confidence * 100).toFixed(1)}% · ${recommendation.confidence_method ?? ''}`}
        />
      </div>
      <p className="mt-2 text-xs text-slate-400">{recommendation.observation}</p>
      <label className="mt-2 block text-xs text-slate-400">
        <span className="block text-[10px] uppercase tracking-wide text-slate-500">Decision note (optional)</span>
        <textarea
          className="mt-1 h-16 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
          value={note}
          maxLength={2000}
          onChange={(event) => onNote(event.target.value)}
          placeholder="Why you accept it, or what you will try first"
        />
      </label>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="primary" busy={busy} onClick={onConfirm}>
          Confirm acceptance
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <span className="text-[11px] text-slate-500">Cancel leaves the record untouched.</span>
      </div>
    </div>
  );
}

function RejectConfirmation({
  recommendation,
  reason,
  onReason,
  busy,
  onCancel,
  onConfirm,
}: {
  recommendation: Recommendation;
  reason: string;
  onReason: (value: string) => void;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="rounded-lg border border-rose-500/40 bg-rose-500/5 p-3" data-confirmation="reject">
      <p className="text-sm font-semibold text-rose-200">Reject this recommendation?</p>
      <p className="mt-1 text-xs text-slate-300">
        Rejection is final for this record: the reason is stored with the decision and the experiment is not proposed
        again from it. Nothing is published either way.
      </p>
      <div className="mt-2 grid gap-1 text-xs">
        <Field label="Recommendation" value={<span className="font-mono">{recommendation.id}</span>} />
        <Field label="Category" value={categoryLabel(recommendation.kind)} />
      </div>
      <label className="mt-2 block text-xs text-slate-400">
        <span className="block text-[10px] uppercase tracking-wide text-slate-500">Reason (required, stored)</span>
        <textarea
          className="mt-1 h-16 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
          value={reason}
          maxLength={2000}
          onChange={(event) => onReason(event.target.value)}
          placeholder="For example: the sample is too small for this quarter"
        />
      </label>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="danger" busy={busy} disabled={!reason.trim()} onClick={onConfirm}>
          Confirm rejection
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        {!reason.trim() ? <span className="text-[11px] text-amber-400">A reason is required to reject.</span> : null}
      </div>
    </div>
  );
}

function DecisionRecord({ recommendation }: { recommendation: Recommendation }) {
  return (
    <div className="grid gap-1 text-xs sm:grid-cols-2">
      <Field label="Decided" value={recommendation.decided_at ? formatWhen(recommendation.decided_at) : '—'} />
      <Field
        label="Approval request"
        value={recommendation.approval_request_id ?? 'none'}
        mono={Boolean(recommendation.approval_request_id)}
      />
      <Field label="Decision note" value={recommendation.decision_note ?? '—'} wrap />
      <Field label="Status" value={recommendation.status} />
    </div>
  );
}

function Field({
  label,
  value,
  mono = false,
  wrap = false,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  /** Long prose (a method, a decision note) wraps instead of being clipped to one line. */
  wrap?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-slate-800/60 py-1">
      <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-500">{label}</span>
      <span
        className={`min-w-0 text-right text-slate-200 ${wrap ? 'whitespace-normal break-words' : 'truncate'} ${mono ? 'font-mono text-[11px]' : ''}`}
        title={typeof value === 'string' ? value : undefined}
      >
        {value}
      </span>
    </div>
  );
}

function DictBlock({ title, data }: { title: string; data: Record<string, unknown> }) {
  const entries = Object.entries(data ?? {});
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-slate-500">{title}</p>
      {entries.length === 0 ? (
        <p className="mt-1 text-xs text-rose-400">Not recorded.</p>
      ) : (
        <dl className="mt-1 space-y-1 text-xs">
          {entries.map(([key, value]) => (
            <div key={key} className="flex items-baseline justify-between gap-3">
              <dt className="text-slate-500">{key.replace(/_/g, ' ')}</dt>
              <dd className="min-w-0 truncate text-right text-slate-200" title={JSON.stringify(value)}>
                {Array.isArray(value) ? value.join(', ') : typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

/** Turn a decision failure into a sentence a person can act on. */
export function explainDecisionError(error: unknown, decision: 'accept' | 'reject'): string {
  const status = typeof error === 'object' && error !== null && 'status' in error ? Number((error as { status: number }).status) : 0;
  const code = typeof error === 'object' && error !== null && 'code' in error ? String((error as { code: string }).code) : '';
  const message = error instanceof Error ? error.message : String(error);
  if (status === 409 || code === 'CONFLICT') {
    return `This recommendation can no longer be ${decision}ed: ${message}. The record was re-read; check its current status before deciding again.`;
  }
  if (status === 404 || code === 'RECOMMENDATION_NOT_FOUND') {
    return 'This recommendation no longer exists. Refresh the list before deciding.';
  }
  return message;
}

export type { MetricValue };

