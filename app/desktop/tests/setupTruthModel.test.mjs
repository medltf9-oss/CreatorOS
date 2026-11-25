import assert from 'node:assert/strict';
import test from 'node:test';

import {
  completeSetupBody,
  failingCheckIds,
  firstRunNotice,
  overrideWarning,
  prerequisiteTruth,
  requiredChecksFailure,
  verificationEvidence,
  verificationOutcome,
} from '../src/setupModel.ts';

/** A recorded verification exactly as GET /setup/status returns it. */
const record = (overrides = {}) => ({
  ok: true,
  sha256: '9f2c4a1b7d3e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcd',
  size_bytes: 134217728,
  path: 'C:\\Users\\you\\Downloads\\ffmpeg-7.1-essentials_build.zip',
  signer_subject: 'CN=Gyan Doshi, O=FFmpeg',
  timestamped: true,
  verified_at: '2026-02-01T10:00:00Z',
  current: true,
  ...overrides,
});

test('a passing verification is shown as verified only while it is still current', () => {
  const current = prerequisiteTruth({ state: 'verified', verified: true, verification: record() });
  assert.equal(current.key, 'verified');
  assert.equal(current.tone, 'good');
  assert.equal(current.requires_verification, false);
  assert.match(current.detail, /recorded result/i);
  assert.match(current.detail, /not a live re-hash/i);

  const changed = prerequisiteTruth({
    state: 'verified',
    verified: true,
    verification: record({ current: false }),
  });
  assert.notEqual(changed.key, 'verified');
  assert.notEqual(changed.tone, 'good');
  assert.equal(changed.requires_verification, true);
});

test('a stale record is shown as unverified, never as a pass', () => {
  const stale = prerequisiteTruth({
    state: 'ok',
    verified: false,
    detail: 'present at C:\\ffmpeg\\ffmpeg.exe',
    verification: record({ current: false }),
  });
  assert.equal(stale.key, 'stale');
  assert.equal(stale.tone, 'warn');
  assert.match(stale.label, /Unverified/);
  assert.match(stale.detail, /no longer has the recorded size and modified time/);
});

test('a recorded failure stays failed and names no ids it was not given', () => {
  const failed = prerequisiteTruth({
    state: 'unverified',
    verification: record({ ok: false }),
  });
  assert.equal(failed.key, 'verification_failed');
  assert.equal(failed.tone, 'bad');
  assert.match(failed.label, /Verification failed/);
});

test('presence without a record is present-but-unverified, and never green', () => {
  const present = prerequisiteTruth({ state: 'ok', verified: false, detail: 'present at C:\\ffmpeg\\ffmpeg.exe' });
  assert.equal(present.key, 'present_unverified');
  assert.equal(present.label, 'Present, not verified');
  assert.notEqual(present.tone, 'good');
  assert.equal(present.requires_verification, true);
  assert.match(present.detail, /not proof/i);

  const flagged = prerequisiteTruth({ state: 'ok', verified: true });
  assert.equal(flagged.key, 'unverified');
  assert.notEqual(flagged.tone, 'good');
  assert.match(flagged.label, /no record/);

  const claimed = prerequisiteTruth({ state: 'verified', verified: false });
  assert.equal(claimed.key, 'unverified');
  assert.notEqual(claimed.tone, 'good');
});

test('missing and unchecked components are not confused with verified ones', () => {
  const missing = prerequisiteTruth({ state: 'missing', detail: 'not installed' });
  assert.equal(missing.key, 'missing');
  assert.equal(missing.tone, 'bad');
  assert.equal(missing.requires_verification, true);

  const unknown = prerequisiteTruth({ state: 'unknown' });
  assert.equal(unknown.key, 'unknown');
  assert.equal(unknown.tone, 'muted');
  assert.notEqual(unknown.tone, 'good');
});

test('recorded evidence is the facts the API returned, not an invention', () => {
  const rows = verificationEvidence(record({ signer_subject: null, timestamped: null }));
  const byLabel = Object.fromEntries(rows.map((row) => [row.label, row.value]));
  assert.equal(byLabel['SHA-256'], record().sha256);
  assert.equal(byLabel['Signer'], 'not recorded');
  assert.equal(byLabel['Timestamped'], 'not recorded');
  assert.equal(byLabel['Unchanged since'], 'yes (size and modified time)');
  assert.equal(byLabel['File'], record().path);
});

test('verify outcomes take their truth from the response record when it is present', () => {
  const failed = verificationOutcome({
    ok: false,
    verification: record({ ok: false }),
    checks: [
      { id: 'hash.sha256', state: 'fail' },
      { id: 'signature', state: 'unknown' },
      { id: 'size', state: 'pass' },
    ],
  });
  assert.equal(failed.key, 'verification_failed');
  assert.equal(failed.tone, 'bad');
  assert.deepEqual(failed.failing_ids, ['hash.sha256', 'signature']);
  assert.equal(failed.recorded, true);

  const current = verificationOutcome({ ok: true, verification: record(), checks: [{ id: 'hash.sha256', state: 'pass' }] });
  assert.equal(current.key, 'verified');
  assert.deepEqual(current.failing_ids, []);

  const stale = verificationOutcome({ ok: true, verification: record({ current: false }), checks: [] });
  assert.equal(stale.key, 'unverified');
  assert.notEqual(stale.tone, 'good');
});

test('a 409 required-checks failure becomes an explicit override, not a silent retry', () => {
  const conflict = Object.assign(new Error('REQUIRED_CHECKS_FAILING: media.ffmpeg, data.root'), {
    name: 'ApiRequestError',
    status: 409,
    code: 'HTTP_409',
  });
  const failure = requiredChecksFailure(conflict);
  assert.ok(failure);
  assert.deepEqual(failure.ids, ['media.ffmpeg', 'data.root']);

  const firstAttempt = completeSetupBody(false, false);
  assert.equal('override_required_checks' in firstAttempt, false);
  assert.equal(firstAttempt.skipped_ai, false);

  const retry = completeSetupBody(false, true);
  assert.equal(retry.override_required_checks, true);
  assert.equal(retry.skipped_ai, false);

  const warning = overrideWarning(failure.ids);
  assert.match(warning, /media\.ffmpeg/);
  assert.match(warning, /records override_required_checks=true/);
  assert.match(warning, /does not change the failing results/);
});

test('unrelated failures are not treated as a required-check conflict', () => {
  assert.equal(requiredChecksFailure(new Error('network down')), null);
  assert.equal(requiredChecksFailure({ status: 500, message: 'REQUIRED_CHECKS_FAILING: a' }), null);
  assert.equal(requiredChecksFailure(null), null);
  assert.equal(requiredChecksFailure({ status: 409, message: 'CONFLICT: something else' }), null);
});

test('an upgrade produces a non-blocking notice and never a reset instruction', () => {
  const notice = firstRunNotice({ completed: true, version: '0.8.0', version_changed: true });
  assert.ok(notice);
  assert.match(notice, /older version/);
  assert.match(notice, /0\.8\.0/);
  assert.match(notice, /Nothing was reset/);
  assert.match(notice, /will not reopen/);

  assert.equal(firstRunNotice({ completed: true, version: '0.8.0', version_changed: false }), null);
  assert.equal(firstRunNotice({ completed: false, version: null, version_changed: true }), null);
  assert.equal(firstRunNotice({ completed: true, version: '0.8.0' }), null);
});

test('uncheckable checks are reported alongside failures', () => {
  assert.deepEqual(
    failingCheckIds([
      { id: 'size', state: 'pass' },
      { id: 'hash', state: 'fail' },
      { id: 'signature', state: 'unknown' },
      { state: 'fail' },
    ]),
    ['hash', 'signature'],
  );
});
