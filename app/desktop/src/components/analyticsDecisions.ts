/**
 * Pure decision and settings logic for the P8.23 pages (recommendations, reports, analytics settings).
 *
 * Kept out of the components so the rules are testable without a DOM, and so the *page* cannot quietly
 * diverge from them:
 *
 * * a recommendation is decidable only in `draft`/`reviewed`; the backend refuses anything else with a
 *   409 and the UI must not offer the buttons;
 * * every mandated field is checked for presence, and a missing one is reported rather than hidden;
 * * evidence is classified as resolved / missing / stale against the rows actually returned;
 * * settings are validated against the bounds the backend publishes — never clamped, never invented.
 */
import type {
  AnalyticsSettings,
  MetricValue,
  Recommendation,
  RecommendationStatus,
  Report,
  ReportSection,
} from '../services/apiAnalytics';

// ------------------------------------------------------------------ recommendations

export type RecommendationCategory = {
  kind: string;
  label: string;
  /** What the rule measures, in one line, so a reader knows what a category means. */
  describes: string;
};

export const RECOMMENDATION_CATEGORIES: RecommendationCategory[] = [
  { kind: 'topic', label: 'Topic', describes: 'engagement of publications that share a topic' },
  { kind: 'format', label: 'Format', describes: 'engagement of publications that share a format' },
  { kind: 'hook', label: 'Hook', describes: 'the opening of a publication' },
  { kind: 'caption', label: 'Caption', describes: 'caption structure and length' },
  { kind: 'hashtag', label: 'Hashtag', describes: 'engagement of publications that share a hashtag' },
  { kind: 'timing', label: 'Timing', describes: 'engagement by publish time bucket' },
  { kind: 'campaign', label: 'Campaign', describes: 'measurement coverage of a campaign' },
  { kind: 'growth', label: 'Growth', describes: 'the trend of an account-level metric' },
  { kind: 'retention', label: 'Retention', describes: 'where viewers leave a video' },
  { kind: 'experiment', label: 'Experiment', describes: 'a proposed change and its success metric' },
];

export function categoryLabel(kind: string): string {
  return RECOMMENDATION_CATEGORIES.find((category) => category.kind === kind)?.label ?? kind;
}

export const RECOMMENDATION_STATUSES: RecommendationStatus[] = ['draft', 'reviewed', 'accepted', 'rejected', 'superseded'];

/** Only these two states can be decided. `superseded` is the schema's "no longer actionable" state. */
export const DECIDABLE_STATUSES: RecommendationStatus[] = ['draft', 'reviewed'];

export function canDecide(recommendation: Pick<Recommendation, 'status'> | null | undefined): boolean {
  return Boolean(recommendation) && DECIDABLE_STATUSES.includes(recommendation!.status);
}

/** Why a decision is not offered, in the words the page shows. */
export function decisionBlockedReason(recommendation: Pick<Recommendation, 'status'>): string {
  if (recommendation.status === 'accepted') return 'This recommendation was already accepted; the decision is recorded and cannot be repeated.';
  if (recommendation.status === 'rejected') return 'This recommendation was already rejected; the decision is recorded and cannot be repeated.';
  if (recommendation.status === 'superseded') return 'A newer recommendation replaced this one, so it is no longer actionable.';
  return 'This recommendation can be accepted or rejected.';
}

//: The eight mandated fields (plan §13.3) plus the record identity the UI must always show.
export const REQUIRED_RECOMMENDATION_FIELDS = [
  'id',
  'kind',
  'status',
  'observation',
  'sample_size',
  'confidence_method',
  'limitations',
  'proposed_experiment',
  'expected_benefit',
] as const;

/**
 * Fields are considered present when the *record* carries something a reader can use. An empty object
 * or list is absent: "limitations: []" is exactly the failure the engine refuses to store.
 */
export function presentFields(recommendation: Recommendation): Record<string, boolean> {
  const hasEntries = (value: unknown) =>
    Array.isArray(value) ? value.length > 0 : Boolean(value) && typeof value === 'object' && Object.keys(value as object).length > 0;
  return {
    id: Boolean(recommendation.id),
    kind: Boolean(recommendation.kind),
    status: Boolean(recommendation.status),
    observation: recommendation.observation.trim().length > 0,
    sample_size: hasEntries(recommendation.sample_size),
    confidence_method: recommendation.confidence === null ? Boolean(recommendation.confidence_method) : Boolean(recommendation.confidence_method),
    limitations: (recommendation.limitations ?? []).length > 0,
    proposed_experiment: hasEntries(recommendation.proposed_experiment),
    expected_benefit: hasEntries(recommendation.expected_benefit),
  };
}

/** The mandated fields this record does not carry. A non-empty result is shown as a defect banner. */
export function missingFields(recommendation: Recommendation): string[] {
  const present = presentFields(recommendation);
  return REQUIRED_RECOMMENDATION_FIELDS.filter((field) => !present[field]);
}

/** `expected_benefit.kind` is `estimate` or `unmeasured` (engine output); anything else is "unstated". */
export function benefitIsEstimate(recommendation: Recommendation): boolean {
  const kind = String((recommendation.expected_benefit ?? {}).kind ?? '').toLowerCase();
  return kind === 'estimate';
}

export function benefitKindLabel(recommendation: Recommendation): string {
  const kind = String((recommendation.expected_benefit ?? {}).kind ?? '').toLowerCase();
  if (kind === 'estimate') return 'estimate — not a forecast';
  if (kind === 'unmeasured') return 'unmeasured — no counterfactual';
  return 'the record does not state a kind';
}

export function sampleSizeLabel(sampleSize: Record<string, unknown>): string {
  const entries = Object.entries(sampleSize ?? {});
  if (entries.length === 0) return 'not stated';
  return entries.map(([key, value]) => `${key.replace(/_/g, ' ')} ${String(value)}`).join(' · ');
}

export type RecommendationFilters = {
  status: RecommendationStatus | '';
  kind: string;
  accountId: string;
  platform: string;
  start: string;
  end: string;
};

/**
 * Filter what the API returned. Status, account and kind are applied by the backend; platform and the
 * date range are applied here because `GET /analytics/recommendations` has no such parameters, and the
 * page says so next to the controls.
 */
export function filterRecommendations(rows: Recommendation[], filters: RecommendationFilters): Recommendation[] {
  return rows.filter((row) => {
    if (filters.platform && row.platform !== filters.platform) return false;
    if (filters.start && row.created_at < filters.start) return false;
    if (filters.end && row.created_at > filters.end) return false;
    return true;
  });
}

// ------------------------------------------------------------------ evidence

export type EvidenceState = 'resolved' | 'missing' | 'not_observed' | 'stale' | 'unreferenced';

export type EvidenceItemView = {
  metricKey: string;
  providerMetricName: string | null;
  unit: string;
  availability: string;
  value: number | null;
  valueJson: unknown;
  periodKind: string;
  collectedAt: string | null;
  sampleSize: number | null;
  snapshotIds: string[];
  aggregationIds: string[];
  state: EvidenceState;
  detail: string;
};

/** An observation older than this is shown as stale rather than as current evidence. */
export const EVIDENCE_STALE_DAYS = 30;

/**
 * Classify one evidence item against the rows the page could actually load.
 *
 * `foundSnapshotIds` are the ids present in the fetched snapshot page; an item whose ids are all
 * absent is reported as missing rather than silently rendered as if it were verified.
 */
export function evidenceView(item: MetricValue, foundSnapshotIds: Set<string>, now: Date = new Date()): EvidenceItemView {
  const snapshotIds = [...(item.snapshot_ids ?? [])];
  const aggregationIds = [...(item.aggregation_ids ?? [])];
  const base = {
    metricKey: item.metric_key,
    providerMetricName: item.provider_metric_name ?? null,
    unit: item.unit,
    availability: item.availability,
    value: item.value,
    valueJson: item.value_json,
    periodKind: item.period_kind,
    collectedAt: item.collected_at ?? null,
    sampleSize: item.sample_size ?? null,
    snapshotIds,
    aggregationIds,
  };
  if (item.availability !== 'observed') {
    return {
      ...base,
      state: 'not_observed',
      detail: `the provider did not return this metric (${item.availability}); it is not a zero`,
    };
  }
  if (snapshotIds.length === 0 && aggregationIds.length === 0) {
    return { ...base, state: 'unreferenced', detail: 'the item cites no stored row, so it cannot be traced' };
  }
  if (snapshotIds.length > 0 && snapshotIds.every((id) => !foundSnapshotIds.has(id))) {
    return { ...base, state: 'missing', detail: 'the cited snapshot(s) are not present in the stored ledger' };
  }
  const collected = base.collectedAt ? Date.parse(base.collectedAt) : NaN;
  if (Number.isFinite(collected) && now.getTime() - collected > EVIDENCE_STALE_DAYS * 86_400_000) {
    const days = Math.floor((now.getTime() - collected) / 86_400_000);
    return { ...base, state: 'stale', detail: `measured ${days} days ago; treat it as historical, not current` };
  }
  return { ...base, state: 'resolved', detail: 'resolved against the stored ledger' };
}

export function evidenceSummary(items: EvidenceItemView[]): Record<EvidenceState, number> {
  const summary: Record<EvidenceState, number> = { resolved: 0, missing: 0, not_observed: 0, stale: 0, unreferenced: 0 };
  for (const item of items) summary[item.state] += 1;
  return summary;
}

// ------------------------------------------------------------------ reports

export const REPORT_KINDS = [
  'overview',
  'platform_comparison',
  'account',
  'campaign',
  'content_comparison',
  'recommendations',
  'experiment',
] as const;

/** Formats the backend implements. The page offers exactly these and nothing else (P8.24). */
export const REPORT_FORMATS = ['json', 'csv', 'pdf'] as const;

export function reportFormatLabel(format: string): string {
  return format.toUpperCase();
}

export function reportKindLabel(kind: string): string {
  return kind.replace(/_/g, ' ');
}

/** The title a report was created with lives in `filters.title`; the schema has no title field. */
export function reportTitle(report: Report): string {
  const fromFilters = report.filters?.title;
  if (typeof fromFilters === 'string' && fromFilters.trim()) return fromFilters.trim();
  return `${reportKindLabel(report.kind)} analytics report`;
}

export function reportAccountId(report: Report): string | null {
  const value = report.filters?.account_id;
  return typeof value === 'string' && value ? value : null;
}

/** A one-line summary of what a section holds, so a reader can scan the report before expanding it. */
export function sectionSummary(section: ReportSection): string {
  const body = section.body;
  if (Array.isArray(body)) return `${body.length} row${body.length === 1 ? '' : 's'}`;
  if (body && typeof body === 'object') {
    const keys = Object.keys(body as object);
    return `${keys.length} field${keys.length === 1 ? '' : 's'}`;
  }
  return typeof body === 'string' ? `${body.length} characters` : 'empty';
}

/** Decimal megabytes for the file column; the raw byte count stays in the details. */
export function formatBytesDecimal(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} kB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

// ------------------------------------------------------------------ settings

export type RetentionView = {
  rows: number;
  bytes: number;
  categories: { name: string; count: number; reason: string; bytes: number }[];
  protectedRows: { name: string; count: number }[];
  policy: { retentionDays: number; reportRetentionDays: number; cutoff: string; reportCutoff: string };
};

/**
 * Turn a retention preview into what a person reads before deleting anything.
 *
 * The category list keeps the backend's own reason string: a preview that says "3 214 rows" without
 * saying *which* category and *why* is not a preview, it is a warning label.
 */
export function retentionView(preview: {
  categories: { name: string; count: number; reason: string; bytes: number }[];
  total_rows: number;
  total_bytes: number;
  protected: Record<string, number>;
  policy: { retention_days: number; report_retention_days: number; cutoff: string; report_cutoff: string };
}): RetentionView {
  return {
    rows: preview.total_rows,
    bytes: preview.total_bytes,
    categories: preview.categories.filter((category) => category.count > 0 || category.name === 'observations'),
    protectedRows: Object.entries(preview.protected ?? {}).map(([name, count]) => ({ name, count })),
    policy: {
      retentionDays: preview.policy.retention_days,
      reportRetentionDays: preview.policy.report_retention_days,
      cutoff: preview.policy.cutoff,
      reportCutoff: preview.policy.report_cutoff,
    },
  };
}

/** "1 report", "1841 observations": a count next to a category name, in that order. */
export function retentionCountLabel(count: number, name: string): string {
  const label = name.replace(/_/g, ' ');
  const singular = label.endsWith('s') ? label.slice(0, -1) : label;
  return `${count.toLocaleString('en-US')} ${count === 1 ? singular : label}`;
}

/** Deletion is armed only by typing the exact word, never by a second click on the same button. */
export const RETENTION_CONFIRM_WORD = 'DELETE';

export function retentionArmed(typed: string): boolean {
  return typed.trim().toUpperCase() === RETENTION_CONFIRM_WORD;
}

export type SettingKind = 'boolean' | 'integer';

export type SettingSpec = {
  key: string;
  label: string;
  section: 'collection' | 'data' | 'resources' | 'reporting';
  kind: SettingKind;
  unit: string;
  description: string;
  /** When a change takes effect, in the words the page shows. */
  effect: string;
};

/**
 * The five keys the backend actually stores (`SETTINGS_DEFAULTS` in the analytics router). Nothing is
 * added here that `PUT /analytics/settings` would reject as an unknown setting.
 */
export const SETTING_SPECS: SettingSpec[] = [
  {
    key: 'collection_enabled',
    label: 'Collection enabled',
    section: 'collection',
    kind: 'boolean',
    unit: '',
    description: 'Whether the analytics sweeper may enqueue collection jobs at all.',
    effect: 'read by the sweeper on its next tick',
  },
  {
    key: 'account_sync_interval_s',
    label: 'Account sync interval',
    section: 'collection',
    kind: 'integer',
    unit: 'seconds',
    description: 'How often an account-level sync is due (24 h default).',
    effect: 'read by the sweeper on its next tick; a job already queued keeps its schedule',
  },
  {
    key: 'retention_days',
    label: 'Snapshot retention',
    section: 'data',
    kind: 'integer',
    unit: 'days',
    description: 'How long stored observations are kept before the retention job may prune them.',
    effect: 'applies to the retention job, not to data already stored',
  },
  {
    key: 'object_window_months',
    label: 'Historical window',
    section: 'data',
    kind: 'integer',
    unit: 'months',
    description: 'How far back discovery and backfill may reach for published objects.',
    effect: 'read on the next discovery run',
  },
  {
    key: 'recommendation_min_publications',
    label: 'Recommendation sample gate',
    section: 'resources',
    kind: 'integer',
    unit: 'publications',
    description: 'Minimum publications per group before the recommendation engine emits a candidate.',
    effect: 'read the next time recommendations are generated',
  },
];

export type FieldError = { key: string; message: string };

export type DraftValidation = { ok: boolean; errors: FieldError[]; changes: Record<string, number | boolean> };

/**
 * Validate a draft against the **published** bounds. A value outside them is an error, never a clamp:
 * clamping silently turns a typo into a different configuration than the one the user asked for.
 */
export function validateSettingsDraft(
  draft: Record<string, unknown>,
  saved: Record<string, unknown>,
  bounds: Record<string, { min: number; max: number }>,
): DraftValidation {
  const errors: FieldError[] = [];
  const changes: Record<string, number | boolean> = {};
  for (const spec of SETTING_SPECS) {
    const raw = draft[spec.key];
    if (spec.kind === 'boolean') {
      if (typeof raw !== 'boolean') {
        errors.push({ key: spec.key, message: `${spec.label} must be true or false` });
        continue;
      }
      if (raw !== saved[spec.key]) changes[spec.key] = raw;
      continue;
    }
    if (raw === '' || raw === null || raw === undefined) {
      errors.push({ key: spec.key, message: `${spec.label} is required` });
      continue;
    }
    const numeric = typeof raw === 'number' ? raw : Number(String(raw).trim());
    if (!Number.isFinite(numeric) || !Number.isInteger(numeric)) {
      errors.push({ key: spec.key, message: `${spec.label} must be a whole number of ${spec.unit}` });
      continue;
    }
    const bound = bounds[spec.key];
    if (bound) {
      if (numeric < bound.min || numeric > bound.max) {
        errors.push({
          key: spec.key,
          message: `${spec.label} must be between ${bound.min} and ${bound.max} ${spec.unit}`.trim(),
        });
        continue;
      }
    }
    if (numeric !== saved[spec.key]) changes[spec.key] = numeric;
  }
  return { ok: errors.length === 0, errors, changes };
}

export function hasUnsavedChanges(draft: Record<string, unknown>, saved: Record<string, unknown>): boolean {
  return SETTING_SPECS.some((spec) => draft[spec.key] !== saved[spec.key]);
}

/**
 * Settings the API does not expose as controls. Reported rather than faked, because a control that
 * saves nowhere is worse than an honest gap.
 */
export const UNSETTLED_SETTINGS_AREAS: { area: string; note: string }[] = [
  {
    area: 'Reporting preferences',
    note: 'The settings endpoint stores no report defaults: a report is configured when it is created. This page does not invent those keys.',
  },
  {
    area: 'Per-run collection limits',
    note: 'Request budgets live in the collector and the queue (lessons from §9), not in stored settings, so they are not editable here.',
  },
  {
    area: 'Reset to defaults',
    note: 'The API returns the stored values and their bounds but not its defaults, so a reset would be a guess. Use Cancel to discard edits instead.',
  },
];

export function settingsFromResponse(response: AnalyticsSettings): {
  saved: Record<string, unknown>;
  bounds: Record<string, { min: number; max: number }>;
  note: string;
} {
  return {
    saved: { ...(response.settings ?? {}) },
    bounds: { ...(response.bounds ?? {}) },
    note: response.note ?? '',
  };
}
