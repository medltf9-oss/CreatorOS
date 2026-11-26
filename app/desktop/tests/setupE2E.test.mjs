/**
 * Desktop end-to-end test for the first-run setup wizard.
 *
 * Real pieces: the packaged Electron binary (node_modules/electron/dist/electron.exe) is launched
 * exactly like a user launch, its main process spawns the real backend ("python -m creatoros
 * serve") against a throwaway CREATOROS_ROOT, and the real built renderer
 * (app/desktop/dist/index.html) is driven through the Chrome DevTools Protocol. Clicks, React state
 * and HTTP all run in the real window; nothing is mocked and no screen is rendered by a test DOM.
 *
 * Substituted pieces: the data root (temporary directory), the backend/renderer ports and the
 * CREATOROS_DISABLE_WORKERS / CREATOROS_SOCIAL_CALLBACK_ENABLED isolation flags. The third test
 * completes through the renderer's own fetch/bridge (the "out-of-band" path a second process
 * would take) and is a companion to, not a substitute for, the click-through override journey.
 *
 * Run from app/desktop:
 *   node --test tests/setupE2E.test.mjs
 */
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { createServer } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, '..');
const REPO = path.resolve(DESKTOP, '..', '..');
const BACKEND = path.join(REPO, 'app', 'backend');
const PYTHON = process.env.CREATOROS_E2E_PYTHON || path.join(BACKEND, '.venv', 'Scripts', 'python.exe');
const ELECTRON = path.join(DESKTOP, 'node_modules', 'electron', 'dist', 'electron.exe');
const DIST_INDEX = path.join(DESKTOP, 'dist', 'index.html');
const MANIFEST = path.join(BACKEND, 'src', 'creatoros', 'resources', 'prerequisites.v1.json');
const FFMPEG = path.join(REPO, 'tools', 'ffmpeg', 'bin', 'ffmpeg.exe');
const FFPROBE = path.join(REPO, 'tools', 'ffmpeg', 'bin', 'ffprobe.exe');
const START_TIMEOUT = 180000;
const UI_TIMEOUT = 90000;

const manifest = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
const ffmpegComponent = manifest.components.find((component) => component.id === 'ffmpeg');
const PINNED_SHA = String(ffmpegComponent.acquisition.sha256).toLowerCase();

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function removeRoot(root) {
  try {
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
  } catch (error) {
    // A force-killed Chromium child can hold the temp directory for a moment longer than the
    // test itself; leaking a temp root is noisy but must not disguise a passing journey.
    console.warn('could not remove temporary root ' + root + ': ' + String(error));
  }
}

function childEnv(root) {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    if (key.startsWith('CREATOROS_')) continue;
    if (key === 'ELECTRON_RUN_AS_NODE') continue;
    env[key] = value;
  }
  env.CREATOROS_ROOT = root;
  env.CREATOROS_DISABLE_WORKERS = '1';
  env.CREATOROS_SOCIAL_CALLBACK_ENABLED = '0';
  env.CREATOROS_DEV_URL = pathToFileURL(DIST_INDEX).href;
  env.PYTHONUNBUFFERED = '1';
  return env;
}

async function prepareRoot({ withFfmpeg }) {
  if (!fs.existsSync(PYTHON)) throw new Error('backend interpreter not found: ' + PYTHON);
  if (!fs.existsSync(ELECTRON)) throw new Error('Electron binary not found: ' + ELECTRON);
  if (!fs.existsSync(DIST_INDEX)) throw new Error('built renderer not found: ' + DIST_INDEX + ' (run the desktop build first)');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), withFfmpeg ? 'creatoros-ui-present-' : 'creatoros-ui-missing-'));
  const migrated = spawnSync(PYTHON, ['-m', 'creatoros', 'migrate'], {
    cwd: BACKEND,
    env: childEnv(root),
    encoding: 'utf8',
    timeout: 600000,
  });
  if (migrated.status !== 0) {
    throw new Error('creatoros migrate failed: ' + (migrated.stderr || migrated.stdout || String(migrated.status)));
  }
  if (withFfmpeg) {
    const target = path.join(root, 'tools', 'ffmpeg', 'bin');
    fs.mkdirSync(target, { recursive: true });
    for (const source of [FFMPEG, FFPROBE]) {
      if (!fs.existsSync(source)) throw new Error('real FFmpeg build is missing: ' + source);
      fs.copyFileSync(source, path.join(target, path.basename(source)));
    }
  }
  return root;
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener('message', (event) => {
      const raw = typeof event.data === 'string' ? event.data : Buffer.from(event.data).toString('utf8');
      let message;
      try {
        message = JSON.parse(raw);
      } catch {
        return;
      }
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error('CDP error: ' + JSON.stringify(message.error)));
        else resolve(message.result);
      }
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', () => reject(new Error('CDP socket failed: ' + url)), { once: true });
    });
    return new Cdp(socket);
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async eval(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error('page evaluation failed: ' + JSON.stringify(result.exceptionDetails).slice(0, 2000));
    }
    return result.result.value;
  }

  close() {
    try {
      this.socket.close();
    } catch {
      // already closed
    }
  }
}

class ElectronApp {
  constructor(root, child, logPath, logFd) {
    this.root = root;
    this.child = child;
    this.logPath = logPath;
    this.logFd = logFd;
    this.cdp = null;
  }

  static async launch(root) {
    const port = await freePort();
    const logPath = path.join(root, 'electron.log');
    const logFd = fs.openSync(logPath, 'a');
    const child = spawn(ELECTRON, ['.', '--remote-debugging-port=' + port], {
      cwd: DESKTOP,
      env: childEnv(root),
      stdio: ['ignore', logFd, logFd],
    });
    const app = new ElectronApp(root, child, logPath, logFd);
    child.on('error', (error) => {
      fs.appendFileSync(logPath, 'spawn error: ' + String(error) + '\n');
    });
    try {
      const target = await app.waitForPage(port);
      app.cdp = await Cdp.connect(target.webSocketDebuggerUrl);
      return app;
    } catch (error) {
      await app.close();
      throw new Error(String(error) + '\n--- electron log ---\n' + app.logTail());
    }
  }

  async waitForPage(port) {
    const deadline = Date.now() + START_TIMEOUT;
    let last = 'no attempt';
    while (Date.now() < deadline) {
      if (this.child.exitCode !== null) {
        throw new Error('Electron exited before the renderer came up (code ' + this.child.exitCode + ')');
      }
      try {
        const response = await fetch('http://127.0.0.1:' + port + '/json/list', { signal: AbortSignal.timeout(2000) });
        const targets = await response.json();
        const page = targets.find((target) => target.type === 'page' && target.webSocketDebuggerUrl && !target.url.startsWith('devtools://'));
        if (page) return page;
        last = 'targets: ' + targets.map((target) => target.type + ' ' + target.url).join(', ');
      } catch (error) {
        last = String(error);
      }
      await sleep(250);
    }
    throw new Error('renderer target did not appear within ' + START_TIMEOUT + 'ms (' + last + ')');
  }

  logTail(lines = 60) {
    try {
      return fs.readFileSync(this.logPath, 'utf8').split(/\r?\n/).slice(-lines).join('\n');
    } catch {
      return '<no electron log>';
    }
  }

  body() {
    return this.cdp.eval('document.body ? document.body.innerText : ""');
  }

  async waitForBodyWithout(needle, timeout = UI_TIMEOUT) {
    const deadline = Date.now() + timeout;
    let text = '';
    while (Date.now() < deadline) {
      try {
        text = await this.body();
      } catch {
        text = '';
      }
      if (text !== '' && !text.includes(needle)) return text;
      await sleep(200);
    }
    throw new Error('timed out waiting for ' + JSON.stringify(needle) + ' to disappear\n--- page text ---\n' + text.slice(0, 5000) + '\n--- electron log ---\n' + this.logTail());
  }

  async waitForBody(needle, timeout = UI_TIMEOUT) {
    const deadline = Date.now() + timeout;
    let text = '';
    while (Date.now() < deadline) {
      try {
        text = await this.body();
      } catch {
        text = '';
      }
      if (text.includes(needle)) return text;
      await sleep(200);
    }
    throw new Error('timed out waiting for ' + JSON.stringify(needle) + '\n--- page text ---\n' + text.slice(0, 5000) + '\n--- electron log ---\n' + this.logTail());
  }

  section(title) {
    const expression =
      '(() => { const wanted = ' + JSON.stringify(title) + ';' +
      ' const sections = Array.from(document.querySelectorAll("section"));' +
      ' const section = sections.find(function (candidate) {' +
      '   const heading = candidate.querySelector("h2");' +
      '   return heading && heading.textContent && heading.textContent.indexOf(wanted) >= 0;' +
      ' });' +
      ' return section ? section.innerText : null; })()';
    return this.cdp.eval(expression);
  }

  async waitForSection(title, needle, timeout = UI_TIMEOUT) {
    const deadline = Date.now() + timeout;
    let text = null;
    while (Date.now() < deadline) {
      try {
        text = await this.section(title);
      } catch {
        text = null;
      }
      if (text && text.includes(needle)) return text;
      await sleep(200);
    }
    throw new Error('timed out waiting for ' + JSON.stringify(needle) + ' inside section ' + JSON.stringify(title) + '\n--- section ---\n' + String(text).slice(0, 5000) + '\n--- page text ---\n' + (await this.body()).slice(0, 3000));
  }

  clickInSection(title, label) {
    const expression =
      '(() => { const wanted = ' + JSON.stringify(title) + '; const label = ' + JSON.stringify(label) + ';' +
      ' const sections = Array.from(document.querySelectorAll("section"));' +
      ' const section = sections.find(function (candidate) {' +
      '   const heading = candidate.querySelector("h2");' +
      '   return heading && heading.textContent && heading.textContent.indexOf(wanted) >= 0;' +
      ' });' +
      ' if (!section) return false;' +
      ' const button = Array.from(section.querySelectorAll("button")).find(function (candidate) {' +
      '   return (candidate.textContent || "").indexOf(label) >= 0;' +
      ' });' +
      ' if (!button) return false;' +
      ' button.click(); return true; })()';
    return this.cdp.eval(expression);
  }

  clickButton(label) {
    const expression =
      '(() => { const label = ' + JSON.stringify(label) + ';' +
      ' const button = Array.from(document.querySelectorAll("button")).find(function (candidate) {' +
      '   return (candidate.textContent || "").indexOf(label) >= 0;' +
      ' });' +
      ' if (!button) return false;' +
      ' button.click(); return true; })()';
    return this.cdp.eval(expression);
  }

  api(method, pathname, body) {
    const expression =
      '(async () => {' +
      ' const boot = await window.creatoros.bootstrap();' +
      ' const init = { method: ' + JSON.stringify(method) + ', headers: { "Content-Type": "application/json", Authorization: "Bearer " + boot.token } };' +
      (body === undefined ? '' : ' init.body = JSON.stringify(' + JSON.stringify(body) + ');') +
      ' const response = await fetch(boot.apiBase + ' + JSON.stringify(pathname) + ', init);' +
      ' const text = await response.text();' +
      ' return { status: response.status, body: text ? JSON.parse(text) : null };' +
      '})()';
    return this.cdp.eval(expression);
  }

  async close() {
    if (this.cdp) this.cdp.close();
    this.cdp = null;
    if (this.child && this.child.exitCode === null) {
      if (process.platform === 'win32') {
        spawnSync('taskkill', ['/PID', String(this.child.pid), '/T', '/F'], { stdio: 'ignore' });
      } else {
        try {
          this.child.kill('SIGTERM');
        } catch {
          // already gone
        }
      }
      const deadline = Date.now() + 10000;
      while (this.child.exitCode === null && Date.now() < deadline) await sleep(100);
    }
    if (this.logFd !== undefined) {
      try {
        fs.closeSync(this.logFd);
      } catch {
        // already closed
      }
      this.logFd = undefined;
    }
  }
}

async function withApp(root, run) {
  const app = await ElectronApp.launch(root);
  try {
    await run(app);
  } finally {
    await app.close();
  }
}

test('setup wizard shows present-unverified, verifies the real bytes, notices a changed file and surfaces the 409', { timeout: 300000 }, async () => {
  const root = await prepareRoot({ withFfmpeg: true });
  try {
    await withApp(root, async (app) => {
      let text = await app.waitForBody('Present, not verified');
      assert.match(text, /First-run setup/);
      assert.match(text, /CreatorOS version/);

      let section = await app.waitForSection('FFmpeg and ffprobe', 'Present, not verified');
      assert.match(section, /Present, not verified/);
      assert.match(section, /not verified/);
      assert.match(section, /Pinned version 9\.1-essentials_build-www\.gyan\.dev/);

      assert.equal(await app.clickInSection('FFmpeg and ffprobe', 'Verify'), true, 'the FFmpeg card has a Verify button');
      section = await app.waitForSection('FFmpeg and ffprobe', 'recorded verification');
      assert.match(section, /recorded verification/);
      assert.ok(section.includes(PINNED_SHA), 'the UI shows the manifest SHA-256 ' + PINNED_SHA + '\n--- section ---\n' + section);
      assert.match(section, /File\s/);

      fs.unlinkSync(path.join(root, 'tools', 'ffmpeg', 'bin', 'ffmpeg.exe'));
      assert.equal(await app.clickButton('Re-check'), true, 'the page has a Re-check button');
      section = await app.waitForSection('FFmpeg and ffprobe', 'Unverified — file changed');
      assert.match(section, /Unverified — file changed/);
      assert.match(section, /verify it again/);

      assert.equal(await app.clickButton('Finish setup'), true, 'the Finish card has a Finish setup button');
      const finish = await app.waitForSection('Finish', 'media.ffmpeg');
      assert.match(finish, /media\.ffmpeg/);

      const status = await app.api('GET', '/setup/status');
      assert.equal(status.status, 200);
      assert.equal(status.body.setup_complete, false, 'a refused completion must not record setup');
      assert.equal(status.body.first_run.completed, false);

      const body = await app.body();
      const offersOverride = body.includes('Completion needs an explicit override');
      console.log('[observed] 409 Finish card text:\n' + finish.slice(0, 1200));
      console.log('[observed] override panel offered after 409: ' + offersOverride);
    });
  } finally {
    removeRoot(root);
  }
});

test('the 409 decision point offers Complete anyway, pressing it records the override and leaves the wizard', { timeout: 300000 }, async () => {
  const root = await prepareRoot({ withFfmpeg: false });
  try {
    await withApp(root, async (app) => {
      await app.waitForBody('What this machine already has');
      await app.waitForSection('FFmpeg and ffprobe', 'Missing');

      assert.equal(await app.clickButton('Finish setup'), true, 'the Finish card has a Finish setup button');
      const finish = await app.waitForSection('Finish', 'media.ffmpeg');
      assert.match(finish, /Completion needs an explicit override/, 'the 409 rendered a decision point, not a bare error\n--- Finish card ---\n' + finish.slice(0, 2000));
      assert.match(finish, /Required checks are failing: media\.ffmpeg/, 'the decision point names the failing check');
      assert.match(finish, /override_required_checks=true/, 'the decision point states what the override records');

      const before = await app.api('GET', '/setup/status');
      assert.equal(before.body.setup_complete, false, 'a refused completion must not record setup');
      assert.equal(before.body.first_run.completed, false);

      assert.equal(await app.clickButton('Complete anyway'), true, 'the override panel exposes Complete anyway');

      // onDone() sends the shell to the overview as soon as the 200 lands, so the wizard and its
      // "Setup is complete" line are intentionally gone here; assert the transition, then the record.
      const overview = await app.waitForBodyWithout('Finish setup');
      assert.match(overview, /First-run setup/, 'the shell keeps its First-run setup navigation entry');

      const status = await app.api('GET', '/setup/status');
      assert.equal(status.body.setup_complete, true, 'the override completion recorded setup');
      assert.equal(status.body.first_run.completed, true);
      assert.equal(status.body.first_run.skipped_ai, false);
      assert.equal(status.body.first_run.version_changed, false);

      const audit = await app.api('GET', '/audit?action=setup.first_run.complete');
      assert.equal(audit.status, 200);
      const events = audit.body.items.filter((event) => event.action === 'setup.first_run.complete');
      assert.equal(events.length, 1, 'exactly one completion event was recorded');
      assert.equal(events[0].metadata_redacted_json.override_required_checks, true);
      assert.deepEqual(events[0].metadata_redacted_json.failed_checks, ['media.ffmpeg']);

      // Reopening the wizard must show the recorded decision, not a fresh first-run prompt.
      assert.equal(await app.clickButton('First-run setup'), true);
      await app.waitForBody('Setup is complete. Re-running the wizard only refreshes these checks.');
      const completed = await app.body();
      assert.match(completed, /First run recorded/);
      assert.doesNotMatch(completed, /First run recorded\nnot yet/);
    });
  } finally {
    removeRoot(root);
  }
});

test('a completion recorded outside the wizard (renderer API) is reflected by the wizard after Re-check', { timeout: 300000 }, async () => {
  const root = await prepareRoot({ withFfmpeg: false });
  try {
    await withApp(root, async (app) => {
      await app.waitForBody('What this machine already has');
      await app.waitForSection('FFmpeg and ffprobe', 'Missing');

      assert.equal(await app.clickButton('Finish setup'), true);
      await app.waitForSection('Finish', 'media.ffmpeg');

      // Deliberately not a click-through: this covers a second writer (another process, a script)
      // completing setup while the wizard is open, which must be picked up by Re-check.
      const response = await app.api('POST', '/setup/complete', {
        override_required_checks: true,
        skipped_ai: false,
        notes: 'desktop E2E: completed outside the wizard to prove Re-check picks it up',
      });
      assert.equal(response.status, 200, 'override completion answered ' + JSON.stringify(response.body));
      assert.equal(response.body.setup_complete, true);
      assert.equal(response.body.first_run.version_changed, false);

      assert.equal(await app.clickButton('Re-check'), true);
      await app.waitForBody('Setup is complete. Re-running the wizard only refreshes these checks.');
      const text = await app.body();
      assert.match(text, /First run recorded/);
      assert.doesNotMatch(text, /First run recorded\nnot yet/);
      assert.match(text, /media\.ffmpeg/);
      console.log('[out-of-band] the wizard reflects a completion recorded by another writer');
    });
  } finally {
    removeRoot(root);
  }
});
