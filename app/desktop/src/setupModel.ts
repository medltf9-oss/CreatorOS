/** Pure presentation helpers for the setup wizard.
 *
 * Kept out of the component so the states that matter (a failed check, an unverifiable file) can be
 * asserted without rendering React.
 */

export type Tone = 'good' | 'bad' | 'warn' | 'muted';

export type StateLike = { state: string };

const STATE_TONES: Record<string, Tone> = {
  ok: 'good',
  pass: 'good',
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
  if (result.ok) return `Verified: ${passed} check${passed === 1 ? '' : 's'} passed.`;
  const parts = [`${failed} failed`, `${unknown} could not be checked`];
  return `Not verified: ${parts.join(', ')}.`;
}
