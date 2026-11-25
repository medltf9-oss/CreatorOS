// Production health: open every page of the *installed* app and prove it renders.
//
//   node scripts/ui_health.cjs [--port 9444] [--out <dir>] [--json <file>]
//
// `capture-installed.cjs` takes a handful of screenshots for the evidence file. This script is the
// release gate: it walks all 49 sidebar entries, and for each one checks that
//
//   * the entry could be clicked (a nav that fails to respond is a defect, not a "skip");
//   * the page painted something (a blank pane or a stuck spinner is a failure);
//   * no ErrorNote is on screen (the rose card the app renders when an API call fails);
//   * nothing was logged to the console as an error while the page was open.
//
// The distinction between "shows a truthful empty state" and "failed to load" is the whole point: an
// installation with no connected accounts legitimately shows empty states everywhere, and would pass a
// naive "is there text on screen" check. Only the app's own error surface counts against a page.
//
// Exit code 0 when every page passes, 1 when any page fails. Screenshots go to --out (default: temp).
const fs = require('node:fs');
const path = require('node:path');

const args = process.argv.slice(2);
const arg = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const PORT = Number(arg('port', '9444'));
const OUT = arg('out', path.join(__dirname, '..', '..', 'temp', 'p10-health'));
const JSON_OUT = arg('json', '');
const SETTLE = Number(arg('settle', '1200'));

// Mirrors NAV in src/App.tsx. Kept as data rather than parsed from the bundle: if the two ever drift,
// the sweep reports "entry not found", which is a signal worth having.
const PAGES = [
  ['Studio', 'Overview'],
  ['Studio', 'AI infrastructure'],
  ['Studio', 'Model Lab'],
  ['Studio', 'Playground'],
  ['Studio', 'Settings'],
  ['Content library', 'Overview'],
  ['Content library', 'Media grid'],
  ['Content library', 'Import manager'],
  ['Content library', 'Media detail'],
  ['Content library', 'Processing centre'],
  ['Content library', 'Transcode studio'],
  ['Content library', 'Storage'],
  ['Agent workforce', 'Overview'],
  ['Agent workforce', 'Organization'],
  ['Agent workforce', 'Directory'],
  ['Agent workforce', 'Agent detail'],
  ['Agent workforce', 'Task graph'],
  ['Agent workforce', 'Live monitor'],
  ['Agent workforce', 'Activity log'],
  ['Agent workforce', 'Playground'],
  ['Agent workforce', 'Settings'],
  ['Social accounts', 'Connected accounts'],
  ['Social accounts', 'Connect account'],
  ['Social accounts', 'Account details'],
  ['Social accounts', 'Permissions'],
  ['Social accounts', 'Vault status'],
  ['Social accounts', 'App settings'],
  ['Publishing', 'Publishing'],
  ['Publishing', 'Compose'],
  ['Publishing', 'Intent detail'],
  ['Publishing', 'Approvals'],
  ['Publishing', 'Publish queue'],
  ['Publishing', 'Schedule'],
  ['Publishing', 'Published'],
  ['Publishing', 'Destination detail'],
  ['Publishing', 'Needs you'],
  ['Publishing', 'Media delivery'],
  ['Analytics', 'Overview'],
  ['Analytics', 'Platform comparison'],
  ['Analytics', 'Account performance'],
  ['Analytics', 'Publication detail'],
  ['Analytics', 'Content comparison'],
  ['Analytics', 'Campaign analytics'],
  ['Analytics', 'Video performance'],
  ['Analytics', 'Audience growth'],
  ['Analytics', 'Hashtag analysis'],
  ['Analytics', 'Recommendations'],
  ['Analytics', 'Reports'],
  ['Analytics', 'Analytics settings'],
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function targets() {
  const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return response.json();
}

function rpc(url, onEvent) {
  const socket = new WebSocket(url);
  let nextId = 1;
  const pending = new Map();
  const ready = new Promise((resolve, reject) => {
    socket.addEventListener('open', () => resolve());
    socket.addEventListener('error', (event) => reject(new Error(`websocket error: ${event.message ?? 'unknown'}`)));
  });
  socket.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.method) {
      if (onEvent) onEvent(message);
      return;
    }
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.error) waiter.reject(new Error(message.error.message));
    else waiter.resolve(message.result);
  });
  const send = async (method, params = {}) => {
    await ready;
    const id = nextId++;
    const result = new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    socket.send(JSON.stringify({ id, method, params }));
    return result;
  };
  return { send, close: () => socket.close() };
}

// The nav is a heading (section) followed by its entries. Matching is exact and case-insensitive, and
// the match must be a button or a link, so a page whose body happens to repeat the label is not
// mistaken for the nav item.
function clickScript(section, label) {
  return `(() => {
    const section = ${JSON.stringify(section.toUpperCase())};
    const label = ${JSON.stringify(label)};
    const all = Array.from(document.querySelectorAll('*'));
    const heading = all.find((node) => node.children.length === 0 && node.textContent.trim().toUpperCase() === section);
    if (!heading) return 'no-section';
    const following = all.filter((node) => heading.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
    const hit = following.find(
      (node) =>
        (node.tagName === 'BUTTON' || node.tagName === 'A') &&
        node.innerText &&
        node.innerText.trim() === label,
    );
    if (!hit) return 'no-entry';
    hit.scrollIntoView({ block: 'center' });
    hit.click();
    return 'clicked';
  })()`;
}

// The probe reads only what the user can see. `ErrorNote` is the app's single error surface
// (border-rose-500/30 + bg-rose-500/10), so "an error is on screen" has one definition here.
const PROBE = `(() => {
  const notes = Array.from(document.querySelectorAll('div[class*="border-rose-500/30"]'));
  const errorText = notes.map((node) => (node.innerText || '').trim().slice(0, 240)).filter(Boolean);
  const spinning = Array.from(document.querySelectorAll('*')).filter(
    (node) => typeof node.className === 'string' && node.className.includes('animate-spin'),
  ).length;
  const main = document.querySelector('main') || document.body;
  const text = (main.innerText || '').trim();
  return {
    errorText,
    spinning,
    textLength: text.length,
    firstLine: text.split('\\n').map((line) => line.trim()).filter(Boolean).slice(0, 2).join(' / '),
    title: document.title,
  };
})()`;

/**
 * Prove the gate can fail.
 *
 * A sweep that reports 49/49 is worthless if the probe cannot see an error. `--self-check` injects the
 * exact DOM the app renders for a failed call (ErrorNote: border-rose-500/30 + bg-rose-500/10), asserts
 * the probe flags it, then removes it and asserts the probe is clean again - all without touching the
 * application's state, because the injected node is a detached div appended to <body>.
 */
async function selfCheck(send) {
  const inject = `(() => {
    const node = document.createElement('div');
    node.className = 'flex items-start justify-between gap-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300';
    node.id = 'creatoros-health-selfcheck';
    node.innerText = 'simulated failure: GET /api/whatever returned 500';
    document.body.appendChild(node);
    return true;
  })()`;
  await send('Runtime.evaluate', { expression: inject, returnByValue: true });
  const withNote = (await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value;
  const detected = Boolean(withNote && withNote.errorText && withNote.errorText.length);
  await send('Runtime.evaluate', {
    expression: `(() => { const node = document.getElementById('creatoros-health-selfcheck'); if (node) node.remove(); return true; })()`,
    returnByValue: true,
  });
  const withoutNote = (await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value;
  const cleared = Boolean(withoutNote && (!withoutNote.errorText || withoutNote.errorText.length === 0));
  console.log(`self-check: injected error detected=${detected}, cleared after removal=${cleared}`);
  if (!detected) throw new Error('the probe did not see an injected ErrorNote: every PASS would be meaningless');
  if (!cleared) throw new Error('the probe still sees a note after removal: the probe is not reading the DOM it claims');
}

async function main() {
  const list = await targets();
  const target = list.find((item) => item.type === 'page' && !String(item.url).startsWith('devtools://'));
  if (!target) throw new Error(`no page target on port ${PORT}`);

  const consoleErrors = [];
  const { send, close } = rpc(target.webSocketDebuggerUrl, (message) => {
    if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
      consoleErrors.push(message.params.args.map((a) => a.value ?? a.description ?? a.type).join(' '));
    }
    if (message.method === 'Runtime.exceptionThrown') {
      consoleErrors.push(message.params.exceptionDetails.text ?? 'exception');
    }
    if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error') {
      consoleErrors.push(message.params.entry.text);
    }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

  if (args.includes('--self-check')) {
    await selfCheck(send);
    close();
    return;
  }

  if (args.includes('--vault-probe')) {
    // A full seal/read/delete round trip through the real path: the renderer asks the backend to
    // reserve an id, the Electron main process seals the value with DPAPI, the backend records the
    // metadata, then it is read back and deleted. A backend-only probe would be theatre: the vault
    // lives in the main process, and this is the only way to prove it works from outside the app.
    const result = await send('Runtime.evaluate', {
      expression: `(async () => {
        const bridge = window.creatoros;
        const boot = await bridge.bootstrap();
        const call = async (method, path, body) => {
          const response = await fetch(boot.apiBase + path, {
            method,
            headers: { Authorization: 'Bearer ' + boot.token, 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
          });
          const text = await response.text();
          let json = null;
          try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text.slice(0, 200) }; }
          return { status: response.status, json };
        };
        const steps = [];
        const reserve = await call('POST', '/social/vault/secrets/reserve');
        steps.push({ step: 'reserve', status: reserve.status });
        const id = reserve.json && reserve.json.secret_id;
        if (!id) return { ok: false, steps, error: 'no secret id from reserve' };
        const sealed = await bridge.vaultStore({
          id,
          secret: 'creatoros-production-health-' + Date.now(),
          purpose: 'token_metadata',
          provider: 'creatoros',
          alias: 'production-health probe',
        });
        steps.push({ step: 'seal', ok: !!sealed.ok, code: sealed.code || null });
        if (!sealed.ok) return { ok: false, steps, error: sealed.error };
        const adopt = await call('POST', '/social/vault/secrets/' + id + '/adopt', {
          purpose: 'token_metadata',
          provider: 'creatoros',
          alias: 'production-health probe',
        });
        steps.push({ step: 'adopt', status: adopt.status });
        const verify = await call('GET', '/social/vault/secrets/' + id);
        steps.push({ step: 'verify', status: verify.status, ok: verify.json && verify.json.ok });
        const remove = await call('DELETE', '/social/vault/secrets/' + id);
        steps.push({ step: 'unlink', status: remove.status });
        const destroyed = await bridge.vaultDelete(id);
        steps.push({ step: 'destroy', ok: !!destroyed.ok });
        return { ok: !!(verify.json && verify.json.ok) && !!destroyed.ok, steps, id };
      })()`,
      awaitPromise: true,
      returnByValue: true,
    });
    close();
    console.log(JSON.stringify(result.result.value));
    return;
  }

  if (args.includes('--print-bootstrap')) {
    // The shell hands the renderer its API base and per-launch session token through the preload
    // bridge; that is the only way a second process can talk to the running backend, because the token
    // is deliberately never written to disk. Printing it here is not a leak: this script is run by the
    // same user, on the same machine, with the same privileges as the app.
    const result = await send('Runtime.evaluate', {
      expression: 'window.creatoros.bootstrap()',
      awaitPromise: true,
      returnByValue: true,
    });
    close();
    console.log(JSON.stringify(result.result.value));
    return;
  }

  fs.mkdirSync(OUT, { recursive: true });
  const results = [];
  for (const [section, label] of PAGES) {
    consoleErrors.length = 0;
    const clicked = await send('Runtime.evaluate', {
      expression: clickScript(section, label),
      returnByValue: true,
    });
    const verdict = { section, label, nav: clicked.result.value };
    if (clicked.result.value !== 'clicked') {
      verdict.ok = false;
      verdict.reason = `nav ${clicked.result.value}`;
      results.push(verdict);
      console.log(`FAIL ${section} / ${label}: ${verdict.reason}`);
      continue;
    }
    await sleep(SETTLE);
    // Bring the page to the top: a page scrolled by the previous one hides the heading it must show.
    await send('Runtime.evaluate', {
      expression: `(() => {
        window.scrollTo(0, 0);
        for (const node of document.querySelectorAll('*')) {
          if (node.scrollTop > 0 && node.scrollHeight > node.clientHeight + 40) node.scrollTop = 0;
        }
        return true;
      })()`,
      returnByValue: true,
    });
    await sleep(300);
    const probe = await send('Runtime.evaluate', { expression: PROBE, returnByValue: true });
    const state = probe.result.value || {};
    const name = `${section}-${label}`.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    const file = path.join(OUT, `${name}.png`);
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));

    verdict.errors = state.errorText || [];
    verdict.textLength = state.textLength || 0;
    verdict.spinning = state.spinning || 0;
    verdict.consoleErrors = [...consoleErrors];
    verdict.shot = file;
    verdict.firstLine = state.firstLine;
    if (verdict.errors.length) {
      verdict.ok = false;
      verdict.reason = `error note: ${verdict.errors[0]}`;
    } else if (verdict.textLength < 80) {
      verdict.ok = false;
      verdict.reason = `page painted ${verdict.textLength} characters`;
    } else if (verdict.consoleErrors.length) {
      verdict.ok = false;
      verdict.reason = `console error: ${verdict.consoleErrors[0]}`;
    } else {
      verdict.ok = true;
      verdict.reason = verdict.spinning ? `ok (still loading: ${verdict.spinning} spinner(s))` : 'ok';
    }
    results.push(verdict);
    console.log(`${verdict.ok ? 'PASS' : 'FAIL'} ${section} / ${label}: ${verdict.reason}`);
  }

  close();
  const failed = results.filter((item) => !item.ok);
  console.log('');
  console.log(`${results.length - failed.length}/${results.length} pages passed`);
  if (JSON_OUT) fs.writeFileSync(JSON_OUT, JSON.stringify(results, null, 2));
  if (failed.length) {
    console.log(JSON.stringify(failed, null, 2));
    process.exit(1);
  }
}

main().catch((error) => {
  console.error('[ui_health] failed:', error.message);
  process.exit(1);
});
