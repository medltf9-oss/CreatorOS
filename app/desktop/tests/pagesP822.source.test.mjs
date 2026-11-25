// P8.22 page-source invariants. Run with: node --test --test-name-pattern= tests/pagesP822.source.test.mjs
//
// A few requirements for these three pages are *textual*: the causation warning must be present, the
// retry/loading/empty states must exist, and — most important — no analytics page may offer a publish
// control. Those cannot be expressed against a pure function, so they are asserted against the page
// sources. This is a guard against a future edit quietly removing the disclosure, not a substitute
// for the behaviour tests in analyticsPagesP822.test.mjs.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (name) => readFileSync(new URL(`../src/pages/analytics/${name}`, import.meta.url), 'utf8');

/** Comments describe the rule; only shipped markup and strings can break it. */
const withoutComments = (source) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

const RAW_VIDEO = read('VideoPerformance.tsx');
const RAW_AUDIENCE = read('AudienceGrowth.tsx');
const RAW_HASHTAGS = read('HashtagAnalysis.tsx');

const VIDEO = withoutComments(RAW_VIDEO);
const AUDIENCE = withoutComments(RAW_AUDIENCE);
const HASHTAGS = withoutComments(RAW_HASHTAGS);

test('hashtag analysis states that association is not causation', () => {
  assert.match(RAW_HASHTAGS, /associated with the performance/i);
  assert.match(RAW_HASHTAGS, /does not cause it/i);
  assert.match(RAW_HASHTAGS, /no comparison here is evidence of causation/i);
});

test('hashtag analysis never claims a hashtag is trending', () => {
  assert.doesNotMatch(HASHTAGS, /\btrending\b/i);
  assert.doesNotMatch(HASHTAGS, /\bgoing viral\b/i);
});

test('hashtag analysis discloses sample size next to every ranked group', () => {
  assert.match(HASHTAGS, /n=\{group\.n\}/);
  assert.match(HASHTAGS, /not ranked \(disclosed, not hidden\)/);
});

test('no analytics page offers a publish, schedule or approval control', () => {
  for (const [name, source] of [
    ['VideoPerformance.tsx', VIDEO],
    ['AudienceGrowth.tsx', AUDIENCE],
    ['HashtagAnalysis.tsx', HASHTAGS],
  ]) {
    assert.doesNotMatch(source, /\bpublish\s*\(/i, `${name} must not call a publish function`);
    assert.doesNotMatch(source, /job_state\s*=|setState\('queued'\)/, `${name} must not queue work`);
    assert.doesNotMatch(source, /approve|accept recommendation/i, `${name} must not decide an approval`);
  }
});

test('video performance shows retention only from a provider curve and says when there is none', () => {
  assert.match(VIDEO, /Retention not available/);
  assert.match(VIDEO, /Only a curve the provider actually returned; nothing is interpolated/);
  assert.match(VIDEO, /no retention curve is stored|retentionResult\.reason/);
});

test('video performance explains that a thumbnail is unavailable rather than faking one', () => {
  assert.match(VIDEO, /Thumbnail unavailable: the analytics API exposes no media asset/);
});

test('video performance discloses the aggregation page cap instead of ranking a silent subset', () => {
  assert.match(VIDEO, /Showing \$\{page\.items\.length\} of \$\{page\.total\} stored aggregations/);
});

test('audience growth never merges platforms and labels a derived change as derived', () => {
  assert.match(AUDIENCE, /never merged|not merged|never summed/i);
  assert.match(AUDIENCE, /\(derived\)/);
  assert.match(AUDIENCE, /not identical/);
});

test('audience growth explains the reason a follower metric is missing', () => {
  assert.match(AUDIENCE, /need a permission this account has not granted/);
  assert.match(AUDIENCE, /does not expose follower statistics/);
  assert.match(AUDIENCE, /below a threshold/);
  assert.match(AUDIENCE, /collection has not produced a follower snapshot/);
});

test('all three pages expose loading, empty and error states', () => {
  for (const [name, source] of [
    ['VideoPerformance.tsx', VIDEO],
    ['AudienceGrowth.tsx', AUDIENCE],
    ['HashtagAnalysis.tsx', HASHTAGS],
  ]) {
    assert.match(source, /LoadingBlock/, `${name} needs a loading state`);
    assert.match(source, /EmptyState/, `${name} needs an empty state`);
    assert.match(source, /ErrorBlock/, `${name} needs an error state`);
  }
});

test('request cancellation is wired through useAsyncData on every page', () => {
  for (const [name, source] of [
    ['VideoPerformance.tsx', VIDEO],
    ['AudienceGrowth.tsx', AUDIENCE],
    ['HashtagAnalysis.tsx', HASHTAGS],
  ]) {
    assert.match(source, /useAsyncData\(/, `${name} must use the cancelling loader`);
    assert.match(source, /signal/, `${name} must pass the abort signal to the API`);
  }
});
