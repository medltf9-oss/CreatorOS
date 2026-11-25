/**
 * Analytics Center — Settings (P8.23).
 *
 * Five stored preferences, grouped the way the plan groups them, each with the bound the backend
 * publishes. Three rules:
 *
 * * **no clamping.** A value outside `[min, max]` is an error; silently clamping would save a different
 *   configuration than the one a person typed.
 * * **no partial saves.** Every field is validated before the single `PUT`, which only carries changed
 *   keys. The response is authoritative, so the form is re-seeded from it rather than from the draft.
 * * **the caveat is shown, not implied.** These are read by the sweeper on its next tick; a change does
 *   not touch a collect that is already running, a job already queued, or data already stored.
 *
 * Settings the API does not store (report defaults, per-run budgets, a defaults list) are reported as
 * gaps instead of being faked with controls that save nowhere.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { type AnalyticsClient, type RetentionExecution, type RetentionPreview } from '../../services/apiAnalytics';
import {
  RETENTION_CONFIRM_WORD,
  SETTING_SPECS,
  UNSETTLED_SETTINGS_AREAS,
  hasUnsavedChanges,
  retentionArmed,
  retentionCountLabel,
  retentionView,
  validateSettingsDraft,
  type SettingSpec,
} from '../../components/analyticsDecisions';
import { Badge, Button, Card, formatBytes } from '../../components/ui';
import { ErrorBlock, LoadingBlock, PageHeader, useAsyncData } from './shared';

const SECTION_TITLES: Record<SettingSpec['section'], { title: string; subtitle: string }> = {
  collection: { title: 'Collection', subtitle: 'Whether and how often the sweeper collects' },
  data: { title: 'Data management', subtitle: 'How far back collection reaches and how long rows are kept' },
  resources: { title: 'Resources', subtitle: 'Guard rails that keep collection and inference bounded' },
  reporting: { title: 'Reporting', subtitle: 'What the settings endpoint does (and does not) store for reports' },
};

export default function AnalyticsSettings({
  api,
  autoRetention = 'none',
}: {
  api: AnalyticsClient;
  /** Harness only: open the preview, or preview and then execute, so both panels can be captured. */
  autoRetention?: 'none' | 'preview' | 'execute';
}) {
  const state = useAsyncData((signal) => api.settings(signal), []);

  const saved = useMemo(() => ({ ...(state.data?.settings ?? {}) }), [state.data]);
  const bounds = state.data?.bounds ?? {};
  const note = state.data?.note ?? '';

  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveInfo, setSaveInfo] = useState<string | null>(null);

  // Seed from the stored values, and re-seed whenever the server answers (load, save, reload).
  useEffect(() => {
    if (state.data) setDraft({ ...(state.data.settings ?? {}) });
  }, [state.data]);

  const dirty = state.data ? hasUnsavedChanges(draft, saved) : false;
  const validation = useMemo(() => validateSettingsDraft(draft, saved, bounds), [draft, saved, bounds]);
  const errorFor = (key: string) => validation.errors.find((error) => error.key === key)?.message ?? null;

  // ---------------------------------------------------------------- retention (P8.24)
  const [preview, setPreview] = useState<RetentionPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [typed, setTyped] = useState('');
  const [executing, setExecuting] = useState(false);
  const [execution, setExecution] = useState<RetentionExecution | null>(null);

  async function loadPreview() {
    setPreviewBusy(true);
    setPreviewError(null);
    setExecution(null);
    try {
      setPreview(await api.retentionPreview({}));
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : String(error));
    } finally {
      setPreviewBusy(false);
    }
  }

  async function executeRetention(confirmWord: string = typed) {
    if (!retentionArmed(confirmWord)) return;
    setExecuting(true);
    setPreviewError(null);
    try {
      // The execution is followed by a fresh preview, so the page shows what is left rather than what
      // the deletion response happened to count.
      const result = await api.retentionExecute({ dry_run: false });
      setExecution(result);
      setTyped('');
      setPreview(await api.retentionPreview({}));
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : String(error));
    } finally {
      setExecuting(false);
    }
  }

  // The visual harness drives the retention panel so it can be screenshotted; a normal page only ever
  // reaches this code because a person pressed a button.
  const autoRan = useRef(false);
  useEffect(() => {
    if (autoRetention === 'none' || autoRan.current) return;
    autoRan.current = true;
    void (async () => {
      await loadPreview();
      if (autoRetention !== 'execute') return;
      setTyped(RETENTION_CONFIRM_WORD);
      await executeRetention(RETENTION_CONFIRM_WORD);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRetention]);

  async function save() {
    if (!validation.ok) {
      setSaveError('Nothing was saved: one or more values are outside the published bounds. Fix them first.');
      return;
    }
    setBusy(true);
    setSaveError(null);
    setSaveInfo(null);
    try {
      const response = await api.updateSettings(validation.changes);
      setDraft({ ...(response.settings ?? {}) });
      setSaveInfo(
        `Saved. The endpoint returned the persisted values; the sweeper reads them on its next tick. ` +
          (response.note || note),
      );
      state.reload();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  function cancel() {
    setDraft({ ...saved });
    setSaveError(null);
    setSaveInfo('Edits discarded; the stored values are shown again.');
  }

  if (!state.data) {
    return (
      <div className="space-y-5">
        <PageHeader
          title="Analytics settings"
          subtitle="Stored preferences for collection, retention and recommendations."
          actions={<Button onClick={state.reload} busy={state.loading}>Reload</Button>}
        />
        {state.error ? (
          <Card title="Settings could not be read" subtitle="No values are shown, and nothing can be saved until they load">
            <ErrorBlock error={state.error} onRetry={state.reload} />
            <p className="mt-2 text-[11px] text-slate-500">
              An unloaded form would show every field as invalid, so it is withheld rather than rendered blank.
            </p>
          </Card>
        ) : (
          <LoadingBlock label="Loading settings..." />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-5 pb-20">
      <PageHeader
        title="Analytics settings"
        subtitle="Stored in app_setting as analytics.settings and read by the analytics sweeper."
        actions={
          <Button onClick={state.reload} busy={state.loading}>
            Reload
          </Button>
        }
      />

      <ErrorBlock error={state.error ?? saveError} onRetry={state.error ? state.reload : () => setSaveError(null)} />

      {note ? (
        <div className="rounded-lg border border-sky-500/30 bg-sky-500/5 px-3 py-2 text-sm text-sky-200">
          {note} A change here therefore does not alter a collect that is already running, a job that is already
          queued, the provider&apos;s own rate limits, the Resource Governor, or the Security layer.
        </div>
      ) : null}

      {saveInfo ? (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-300">
          {saveInfo}
        </div>
      ) : null}

      {(['collection', 'data', 'resources'] as const).map((section) => {
        const specs = SETTING_SPECS.filter((spec) => spec.section === section);
        return (
          <Card key={section} title={SECTION_TITLES[section].title} subtitle={SECTION_TITLES[section].subtitle}>
            <div className="space-y-3">
              {specs.map((spec) => (
                <SettingRow
                  key={spec.key}
                  spec={spec}
                  value={draft[spec.key]}
                  storedValue={saved[spec.key]}
                  bound={bounds[spec.key] ?? null}
                  error={errorFor(spec.key)}
                  onChange={(value) => {
                    setDraft((current) => ({ ...current, [spec.key]: value }));
                    setSaveInfo(null);
                  }}
                />
              ))}
            </div>
          </Card>
        );
      })}

      <Card title={SECTION_TITLES.reporting.title} subtitle={SECTION_TITLES.reporting.subtitle}>
        <p className="text-sm text-slate-300">
          Report defaults are configured when a report is created, not stored here. There is no reporting key in{' '}
          <span className="font-mono text-xs">analytics.settings</span>, so this page shows no reporting controls.
        </p>
      </Card>

      <Card title="Not editable here" subtitle="Reported as contract gaps rather than shown as controls that save nowhere">
        <ul className="space-y-2 text-xs text-slate-400">
          {UNSETTLED_SETTINGS_AREAS.map((item) => (
            <li key={item.area}>
              <span className="text-slate-200">{item.area}:</span> {item.note}
            </li>
          ))}
        </ul>
      </Card>

      <Card
        title="Retention"
        subtitle="Analytics history expires; publications, campaigns, accounts, media and decisions do not"
      >
        <p className="text-sm text-slate-300">
          Observations are kept for the stored <span className="font-mono text-xs">retention_days</span>, reports for at
          least two years. A rollup is deleted only when every observation it cites is also expiring, and no
          publication, campaign, account, media asset, metric definition or decided recommendation is ever touched.
          Retention also runs daily on the analytics lane; this panel previews and can execute it now.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button onClick={loadPreview} busy={previewBusy}>
            Preview what would be deleted
          </Button>
          {preview ? (
            <Badge tone={preview.total_rows ? 'warn' : 'good'}>
              {preview.total_rows.toLocaleString('en-US')} row(s) eligible · {formatBytes(preview.total_bytes)}
            </Badge>
          ) : (
            <span className="text-[11px] text-slate-500">Read-only: the preview deletes nothing.</span>
          )}
        </div>
        {previewError ? (
          <div className="mt-2">
            <ErrorBlock error={previewError} onRetry={() => setPreviewError(null)} />
          </div>
        ) : null}

        {preview ? (
          <div className="mt-3 space-y-2">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-slate-500">
                  <tr>
                    <th className="py-1 pr-3">Category</th>
                    <th className="py-1 pr-3">Rows</th>
                    <th className="py-1 pr-3">Bytes</th>
                    <th className="py-1">Why it is eligible</th>
                  </tr>
                </thead>
                <tbody className="text-slate-300">
                  {retentionView(preview).categories.map((category) => (
                    <tr key={category.name} className="border-t border-slate-800/70">
                      <td className="py-1 pr-3 font-mono text-slate-200">{category.name}</td>
                      <td className="py-1 pr-3">{category.count.toLocaleString('en-US')}</td>
                      <td className="py-1 pr-3 text-slate-500">{formatBytes(category.bytes)}</td>
                      <td className="py-1 text-slate-500">{category.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[11px] text-slate-500">
              Policy: keep {preview.policy.retention_days} days of observations (cutoff{' '}
              {preview.policy.cutoff.slice(0, 10)}); reports kept {preview.policy.report_retention_days} days (cutoff{' '}
              {preview.policy.report_cutoff.slice(0, 10)}). Protected now:{' '}
              {retentionView(preview)
                .protectedRows.map((row) => retentionCountLabel(row.count, row.name))
                .join(', ')}
              .
            </p>

            <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3">
              <p className="text-sm font-semibold text-rose-200">Executing retention is permanent</p>
              <p className="mt-1 text-xs text-slate-300">
                A deleted observation cannot be re-read from the provider once its own retention window has passed, and
                a deleted report cannot be regenerated for a period whose data has expired. Type{' '}
                <span className="font-mono text-slate-100">{RETENTION_CONFIRM_WORD}</span> to arm the button.
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <input
                  className="w-40 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 font-mono text-sm text-slate-200"
                  value={typed}
                  onChange={(event) => setTyped(event.target.value)}
                  placeholder={RETENTION_CONFIRM_WORD}
                  aria-label="Type DELETE to confirm retention"
                />
                <Button variant="danger" disabled={!retentionArmed(typed)} busy={executing} onClick={() => void executeRetention()}>
                  Execute retention
                </Button>
                <Button
                  variant="ghost"
                  disabled={executing}
                  onClick={() => {
                    setTyped('');
                    setPreview(null);
                    setExecution(null);
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          </div>
        ) : null}

        {execution ? (
          <div className="mt-3 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-200">
            Executed in {execution.duration_ms} ms. Deleted{' '}
            {Object.entries(execution.deleted)
              .map(([name, count]) => retentionCountLabel(count, name))
              .join(', ') || 'nothing'}
            {execution.bytes_freed ? ` · ${formatBytes(execution.bytes_freed)} freed` : ''}. Foreign-key violations:{' '}
            {execution.foreign_key_violations}; integrity check {execution.integrity_ok ? 'ok' : 'FAILED'}. Audit record{' '}
            <span className="font-mono">{execution.audit_event_id ?? 'not written'}</span>.
            {execution.errors.length ? ` Errors: ${execution.errors.join('; ')}` : ''}
          </div>
        ) : null}
      </Card>

      <div className="sticky bottom-0 flex flex-wrap items-center gap-3 border-t border-slate-800 bg-slate-950/90 px-1 py-3 backdrop-blur">
        <Button variant="primary" busy={busy} disabled={!dirty || !validation.ok} onClick={save}>
          Save changes
        </Button>
        <Button variant="ghost" disabled={!dirty || busy} onClick={cancel}>
          Cancel
        </Button>
        <Button
          variant="ghost"
          disabled
          title="The API returns stored values and bounds but not its defaults, so a reset would be a guess."
        >
          Reset to defaults (not available)
        </Button>
        {dirty ? (
          <Badge tone="warn">unsaved changes ({Object.keys(validation.changes).length})</Badge>
        ) : (
          <Badge tone="good">in sync with stored values</Badge>
        )}
        {!validation.ok ? <Badge tone="bad">{validation.errors.length} validation error(s)</Badge> : null}
      </div>
    </div>
  );
}

function SettingRow({
  spec,
  value,
  storedValue,
  bound,
  error,
  onChange,
}: {
  spec: SettingSpec;
  value: unknown;
  storedValue: unknown;
  bound: { min: number; max: number } | null;
  error: string | null;
  onChange: (value: unknown) => void;
}) {
  const changed = value !== storedValue;
  return (
    <div className={`rounded-lg border px-3 py-2 ${error ? 'border-rose-500/40 bg-rose-500/5' : changed ? 'border-sky-500/40 bg-sky-500/5' : 'border-slate-800'}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-sm text-slate-200">
            {spec.label}
            <span className="font-mono text-[10px] text-slate-500">{spec.key}</span>
            {changed ? <Badge tone="info">changed</Badge> : null}
          </p>
          <p className="mt-0.5 text-xs text-slate-400">{spec.description}</p>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Current <span className="font-mono text-slate-300">{formatValue(storedValue, spec)}</span>
            {bound ? (
              <>
                {' · '}Allowed <span className="font-mono text-slate-300">{bound.min}</span>–
                <span className="font-mono text-slate-300">{bound.max}</span> {spec.unit}
              </>
            ) : (
              <> · no numeric bound published</>
            )}
            {' · '}Takes effect: {spec.effect}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {spec.kind === 'boolean' ? (
            <label className="flex items-center gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-slate-600 bg-slate-900"
                checked={Boolean(value)}
                onChange={(event) => onChange(event.target.checked)}
              />
              {value ? 'enabled' : 'disabled'}
            </label>
          ) : (
            <input
              type="number"
              inputMode="numeric"
              step={1}
              min={bound?.min}
              max={bound?.max}
              className="w-32 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-right font-mono text-sm text-slate-200"
              value={typeof value === 'number' ? value : String(value ?? '')}
              onChange={(event) => onChange(event.target.value === '' ? '' : Number(event.target.value))}
              aria-label={spec.label}
            />
          )}
          {spec.unit ? <span className="w-24 text-xs text-slate-500">{spec.unit}</span> : null}
        </div>
      </div>
      {error ? <p className="mt-1 text-xs text-rose-300">{error}</p> : null}
    </div>
  );
}

function formatValue(value: unknown, spec: SettingSpec): string {
  if (spec.kind === 'boolean') return value ? 'true' : 'false';
  if (value === undefined || value === null) return '—';
  return `${String(value)}${spec.unit ? ` ${spec.unit}` : ''}`;
}
