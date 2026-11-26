import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { countStates, requiredChecksFailure, toneOf, verificationSummary } from '../src/setupModel.ts';

const read = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');

const SETUP = read('pages/Setup.tsx');
const APP = read('App.tsx');
const API = read('api.ts');

test('a check that could not be read is never coloured as a pass', () => {
  assert.equal(toneOf('pass'), 'good');
  assert.equal(toneOf('unknown'), 'muted');
  assert.equal(toneOf('fail'), 'bad');
  assert.equal(toneOf('missing'), 'bad');
  assert.equal(toneOf('unverified'), 'warn');
  assert.equal(toneOf('something-new'), 'muted');
});

test('states are counted rather than collapsed into a single icon', () => {
  const counts = countStates([{ state: 'pass' }, { state: 'pass' }, { state: 'unknown' }]);
  assert.deepEqual(counts, { pass: 2, unknown: 1 });
});

test('a verification summary never reports an unverified file as verified', () => {
  assert.equal(
    verificationSummary({ ok: true, checks: [{ state: 'pass' }, { state: 'pass' }, { state: 'skipped' }] }),
    'Verified: 2 checks passed.',
  );
  assert.equal(
    verificationSummary({ ok: false, checks: [{ state: 'pass' }, { state: 'fail' }, { state: 'unknown' }] }),
    'Not verified: 1 failed, 1 could not be checked.',
  );
});

test('the wizard reads status, verifies and completes through the typed client', () => {
  assert.match(SETUP, /api\.setupStatus\(/);
  assert.match(SETUP, /api\.verifyPrerequisite\(/);
  assert.match(SETUP, /api\.completeSetup\(/);
  assert.match(SETUP, /verificationSummary\(/);
  assert.doesNotMatch(SETUP, /\bTODO\b|mock data|fake/i);
});

test('the client exposes the first-run endpoints and their types', () => {
  assert.match(API, /'\/setup\/status'/);
  assert.match(API, /'\/setup\/prerequisites'/);
  assert.match(API, /`\/setup\/prerequisites\/\$\{encodeURIComponent\(componentId\)\}\/verify`/);
  assert.match(API, /'\/setup\/complete'/);
  for (const type of ['SetupStatus', 'PrerequisiteState', 'VerifyResponse', 'CompleteResponse']) {
    assert.match(API, new RegExp(`export type ${type} = `));
  }
});

test('the shell routes the wizard and offers it in the rail', () => {
  assert.match(APP, /\{ id: 'setup', label: 'First-run setup'/);
  assert.match(APP, /page === 'setup'/);
  assert.match(APP, /<Setup api=\{api\} onDone=/);
  assert.match(APP, /setup: Wrench/);
});

test('a first run that is not finished opens the wizard once', () => {
  assert.match(APP, /\/setup\/status/);
  assert.match(APP, /setupData\?\.setup_complete === false\) setPage\('setup'\)/);
});

test('the wizard does not offer a download it cannot verify', () => {
  // Acquisition is a user action: the page opens the official page and verifies a local file,
  // it never posts to a download endpoint or fetches bytes itself.
  assert.doesNotMatch(SETUP, /fetch\(/);
  assert.doesNotMatch(API, /\/setup\/download/);
  assert.match(SETUP, /Official download page/);
});

test('the wizard shows a verification record and never trusts presence alone', () => {
  assert.match(SETUP, /prerequisiteTruth\(component\)/);
  assert.match(SETUP, /verificationEvidence\(record\)/);
  assert.doesNotMatch(SETUP, /toneOf\(component\.state\)/);
  assert.match(SETUP, /truth\.key === 'verified'/);
  assert.match(SETUP, /verificationOutcome\(result\)/);
  assert.match(SETUP, /outcome\.failing_ids/);
});

test('the 409 envelope is recognised from its code, not only a prefixed message', () => {
  const envelope = requiredChecksFailure({ status: 409, code: 'REQUIRED_CHECKS_FAILING', message: 'media.ffmpeg, ai.ollama' });
  assert.deepEqual(envelope, { code: 'REQUIRED_CHECKS_FAILING', ids: ['media.ffmpeg', 'ai.ollama'], message: 'media.ffmpeg, ai.ollama' });
  const prefixed = requiredChecksFailure({ status: 409, message: 'REQUIRED_CHECKS_FAILING: media.ffmpeg' });
  assert.deepEqual(prefixed?.ids, ['media.ffmpeg']);
  assert.equal(requiredChecksFailure({ status: 409, code: 'SOMETHING_ELSE', message: 'media.ffmpeg' }), null);
  assert.equal(requiredChecksFailure({ status: 500, code: 'REQUIRED_CHECKS_FAILING', message: 'media.ffmpeg' }), null);
  assert.equal(requiredChecksFailure(new Error('network down')), null);
});

test('a failing required check requires a second, explicit override', () => {
  assert.match(SETUP, /requiredChecksFailure\(err\)/);
  assert.match(SETUP, /completeSetupBody\(skippedAi, override\)/);
  assert.match(SETUP, /Complete anyway/);
  assert.match(SETUP, /finish\(pendingOverride\.skipped_ai, true\)/);
  assert.match(SETUP, /overrideWarning\(pendingOverride\.ids\)/);
  assert.match(SETUP, /disabled=\{finishing !== null \|\| pendingOverride !== null\}/);
});

test('an upgrade is surfaced without reopening the wizard', () => {
  assert.match(SETUP, /firstRunNotice\(data\.first_run\)/);
  assert.match(SETUP, /upgradeNotice/);
  assert.doesNotMatch(SETUP, /setPage\('setup'\)/);
});

test('the setup types carry the frozen verification and completion contract', () => {
  assert.match(API, /export type SetupVerification = \{/);
  assert.match(API, /verification: SetupVerification \| null;/);
  assert.match(API, /version_changed: boolean;/);
  assert.match(API, /export type CompleteRequest = \{/);
  assert.match(API, /override_required_checks\?: boolean;/);
  assert.match(API, /completeSetup\(body: CompleteRequest\)/);
});
