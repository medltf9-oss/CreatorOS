/**
 * Pure presentation helpers for the setup wizard.
 *
 * Kept out of the component so the states that matter (a failed check, an unverifiable file, a stale
 * verification record) can be asserted without rendering React. Every helper here follows one rule:
 * a green state must be backed by a recorded verification object, never by "the file exists".
 */

import type { CompleteRequest, SetupVerification } from './api';

export type Tone = 'good' | 'bad' | 'warn' | 'muted';

export type StateLike = { state: string };

const STATE_TONES: Record<string, Tone> = {
  ok: 'good',
  pass: 'good',
  verified: 'good',
  fail: 'bad',
  warn: 'warn',
  unknown: 'muted',
  skipped: 'muted',
  missing: 'bad',
  unverified: 'warn',
};

export function toneOf(state: string): Tone {
  return STATE_TONES[state] ?? 'muted';
}

/** Count checks by state so the header can say "2 failed, 1 unknown" instead of a vague icon. */
export function countStates(checks: StateLike[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const check of checks) {
    counts[check.state] = (counts[check.state] ?? 0) + 1;
  }
  return counts;
}

/**
 * One honest sentence about a verification response. `unknown` counts as not verified: the file was
 * not proven, and the UI must not imply otherwise.
 */
export function verificationSummary(result: { ok: boolean; checks: StateLike[] }): string {
  const counts = countStates(result.checks);
  const passed = counts.pass ?? 0;
  const failed = counts.fail ?? 0;
  const unknown = counts.unknown ?? 0;
  if (result.ok) return 'Verified: ' + passed + ' check' + (passed === 1 ? '' : 's') + ' passed.';
  const parts = [failed + ' failed', unknown + ' could not be checked'];
  return 'Not verified: ' + parts.join(', ') + '.';
}

export type PrerequisiteTruthKey =
  | 'verified'
  | 'verification_failed'
  | 'stale'
  | 'unverified'
  | 'present_unverified'
  | 'missing'
  | 'unknown';

export type PrerequisiteTruth = {
  key: PrerequisiteTruthKey;
  label: string;
  tone: Tone;
  detail: string;
  /** True unless a current, passing verification record exists. */
  requires_verification: boolean;
};

export type PrerequisiteLike = {
  state: string;
  verified?: boolean;
  detail?: string;
  verification?: SetupVerification | null;
};

/**
 * Translate the API's presence/verification fields into what the wizard is allowed to claim.
 *
 * A passing record is shown as verified only when it is also current. A record that failed, or one
 * whose file changed afterwards, is never green. Presence alone is "Present, not verified".
 */
export function prerequisiteTruth(component: PrerequisiteLike): PrerequisiteTruth {
  const record = component.verification ?? null;
  const observed = (component.detail ?? '').trim();

  if (record) {
    if (record.ok && record.current) {
      return {
        key: 'verified',
        label: 'Verified',
        tone: 'good',
        detail:
          'Recorded verification passed at ' +
          record.verified_at +
          ' and the file is unchanged since: its SHA-256 and signature match the pinned manifest. This is a recorded result, not a live re-hash on this page.',
        requires_verification: false,
      };
    }
    if (!record.ok) {
      return {
        key: 'verification_failed',
        label: 'Verification failed',
        tone: 'bad',
        detail:
          'The verification recorded at ' +
          record.verified_at +
          ' failed. The failure is kept so it can be explained; run Verify to see which checks fail now.',
        requires_verification: true,
      };
    }
    return {
      key: 'stale',
      label: 'Unverified — file changed',
      tone: 'warn',
      detail:
        'A verification was recorded at ' +
        record.verified_at +
        ', but ' +
        record.path +
        ' no longer has the recorded size and modified time. Treat it as unverified and verify it again.',
      requires_verification: true,
    };
  }

  if (component.state === 'missing') {
    return {
      key: 'missing',
      label: 'Missing',
      tone: 'bad',
      detail:
        (observed || 'The component is not on this machine.') +
        ' Missing is a gap to close; nothing has been verified.',
      requires_verification: true,
    };
  }

  if (component.verified === true) {
    return {
      key: 'unverified',
      label: 'Unverified — no record',
      tone: 'warn',
      detail:
        'The API reported this component as verified but returned no verification record, so the wizard does not show it as verified. Run Verify to produce one.',
      requires_verification: true,
    };
  }

  if (component.state === 'unverified') {
    return {
      key: 'unverified',
      label: 'Unverified',
      tone: 'warn',
      detail:
        (observed || 'A file was found but it did not match the pinned manifest.') +
        ' Presence alone is not proof; verify this exact file.',
      requires_verification: true,
    };
  }

  if (component.state === 'ok' || component.state === 'pass') {
    return {
      key: 'present_unverified',
      label: 'Present, not verified',
      tone: 'warn',
      detail:
        (observed || 'The file exists on this machine.') +
        ' Presence only means the file is there; it is not proof that its bytes or signer match the pinned release.',
      requires_verification: true,
    };
  }

  if (component.state === 'verified') {
    return {
      key: 'unverified',
      label: 'Unverified — no record',
      tone: 'warn',
      detail:
        'The API reported the state "verified" without a verification record; without that record the wizard cannot show it as verified.',
      requires_verification: true,
    };
  }

  return {
    key: 'unknown',
    label: 'Not checked',
    tone: 'muted',
    detail:
      (observed || 'No detector reported this component.') + ' Nothing is asserted about it.',
    requires_verification: true,
  };
}

export type VerificationEvidenceRow = { label: string; value: string };

/** The recorded facts behind a verification, for display under the state label. */
export function verificationEvidence(record: SetupVerification): VerificationEvidenceRow[] {
  return [
    { label: 'Recorded at', value: record.verified_at },
    { label: 'SHA-256', value: record.sha256 },
    { label: 'Size', value: record.size_bytes + ' bytes' },
    { label: 'Signer', value: record.signer_subject ?? 'not recorded' },
    {
      label: 'Timestamped',
      value: record.timestamped === null ? 'not recorded' : record.timestamped ? 'yes' : 'no',
    },
    { label: 'File', value: record.path },
    { label: 'Unchanged since', value: record.current ? 'yes (size and modified time)' : 'no — verify again' },
  ];
}

/** The ids of checks that did not pass. An uncheckable check is reported, not silently dropped. */
export function failingCheckIds(checks: { state: string; id?: string }[]): string[] {
  return checks
    .filter((check) => check.state === 'fail' || check.state === 'unknown')
    .map((check) => check.id)
    .filter((id): id is string => typeof id === 'string' && id.length > 0);
}

export type VerificationOutcome = {
  key: 'verified' | 'verification_failed' | 'unverified';
  label: string;
  tone: Tone;
  detail: string;
  failing_ids: string[];
  recorded: boolean;
};

/**
 * The truth about one Verify response. The response's own record wins when present; the live checks
 * are only used to explain a failure.
 */
export function verificationOutcome(result: {
  ok: boolean;
  verification?: SetupVerification | null;
  checks?: { state: string; id?: string }[];
}): VerificationOutcome {
  const failing = failingCheckIds(result.checks ?? []);
  const record = result.verification ?? null;
  if (record) {
    const truth = prerequisiteTruth({ state: result.ok ? 'ok' : 'fail', verification: record });
    const key =
      truth.key === 'verified' ? 'verified' : truth.key === 'verification_failed' ? 'verification_failed' : 'unverified';
    return {
      key,
      label: truth.label,
      tone: truth.tone,
      detail: truth.detail,
      failing_ids: truth.key === 'verified' ? [] : failing,
      recorded: true,
    };
  }
  if (result.ok) {
    return {
      key: 'verified',
      label: 'Verified',
      tone: 'good',
      detail: 'This verify run passed its hash and signature checks.',
      failing_ids: [],
      recorded: false,
    };
  }
  return {
    key: 'verification_failed',
    label: 'Verification failed',
    tone: 'bad',
    detail:
      failing.length > 0
        ? 'This verify run did not pass: ' + failing.join(', ') + '.'
        : 'This verify run did not pass.',
    failing_ids: failing,
    recorded: false,
  };
}

const REQUIRED_PREFIX = 'REQUIRED_CHECKS_FAILING:';

export type RequiredChecksFailure = { code: 'REQUIRED_CHECKS_FAILING'; ids: string[]; message: string };

/**
 * Recognise the API's 409 for failing required checks. The message carries the ids, so the wizard can
 * name them instead of showing a generic error.
 */
export function requiredChecksFailure(error: unknown): RequiredChecksFailure | null {
  if (!error || typeof error !== 'object') return null;
  const status = (error as { status?: unknown }).status;
  const message = (error as { message?: unknown }).message;
  if (typeof message !== 'string' || !message.startsWith(REQUIRED_PREFIX)) return null;
  if (status !== undefined && status !== 409) return null;
  const ids = message
    .slice(REQUIRED_PREFIX.length)
    .split(/[,\s]+/)
    .filter(Boolean);
  return { code: 'REQUIRED_CHECKS_FAILING', ids, message };
}

/**
 * The completion payload. The override flag is sent only for the explicit "Complete anyway" action;
 * a first attempt never carries it.
 */
export function completeSetupBody(skippedAi: boolean, override = false): CompleteRequest {
  return override ? { skipped_ai: skippedAi, override_required_checks: true } : { skipped_ai: skippedAi };
}

/** Plain-language warning for the explicit override panel. */
export function overrideWarning(ids: string[]): string {
  const list = ids.length > 0 ? ids.join(', ') : 'one or more required checks';
  return (
    'Required checks are failing: ' +
    list +
    '. Completing anyway records override_required_checks=true on the setup record and in the audit log. It does not change the failing results.'
  );
}

/** A non-blocking upgrade notice. A completed first run is never reset or reopened by an upgrade. */
export function firstRunNotice(firstRun: {
  completed: boolean;
  version?: string | null;
  version_changed?: boolean;
}): string | null {
  if (!firstRun.completed || firstRun.version_changed !== true) return null;
  return (
    'CreatorOS was set up on an older version (' +
    (firstRun.version ?? 'unknown') +
    '); this app is newer. Nothing was reset and the wizard will not reopen on its own — the recorded setup decision still stands.'
  );
}
