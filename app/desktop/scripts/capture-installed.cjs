// Drives the *installed* CreatorOS app over the Chrome DevTools Protocol and saves screenshots.
//
//   node scripts/capture-installed.cjs [--port 9444] [--out <PROJECT_ROOT>\docs\evidence]
//
// Why this exists: the packaged app is the deliverable, and a screenshot of the dev server proves
// nothing about it. This connects to the running installed app, clicks a sidebar entry by its visible
// text, waits for the page to settle, and captures the window contents exactly as the user sees them.
//
// Node 22+ ships a global WebSocket, so no dependency is needed.
const fs = require('node:fs');
const path = require('node:path');

const args = process.argv.slice(2);
function arg(name, fallback) {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}

const PORT = Number(arg('port', '9444'));
const OUT = arg('out', path.join(__dirname, '..', '..', '..', 'docs', 'evidence'));

// Sidebar labels repeat across sections ("Overview" exists under Studio, Content library, Agent
// workforce, Analytics), so a shot names the section first and only then the entry inside it. The
// click walks the page: a section heading is a plain element, so we scroll it into view and use the
// nav item that follows it.
const SHOTS = [
  { name: 'p9-installed-overview', nav: ['Studio', 'Overview'], settle: 1500 },
  { name: 'p9-installed-analytics-overview', nav: ['Analytics', 'Overview'], settle: 3000 },
  { name: 'p9-installed-analytics-reports', nav: ['Analytics', 'Reports'], settle: 2000 },
  { name: 'p9-installed-analytics-settings', nav: ['Analytics', 'Analytics settings'], settle: 2000 },
  { name: 'p9-installed-workforce', nav: ['Agent workforce', 'Directory'], settle: 2000 },
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function targets() {
  const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return response.json();
}

function rpc(url) {
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

/**
 * Click a sidebar entry (or a tab inside a page) by its exact visible text.
 *
 * The app's nav items are buttons whose label is the text node, so the search walks elements with a
 * short innerText and picks the innermost match — clicking a wrapper would hit the wrong item.
 */
function clickScript(section, label) {
  return `(() => {
    const section = ${JSON.stringify(section)};
    const label = ${JSON.stringify(label)};
    const all = Array.from(document.querySelectorAll('*'));
    const heading = all.find((node) => node.children.length === 0 && node.textContent.trim().toUpperCase() === section.toUpperCase());
    if (!heading) return 'no-section';
    // The nav entry is a sibling further down the same list; take the first match after the heading.
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

async function main() {
  const list = await targets();
  const target = list.find((item) => item.type === 'page' && !String(item.url).startsWith('devtools://'));
  if (!target) throw new Error(`no page target on port ${PORT}: ${JSON.stringify(list.map((i) => i.type))}`);

  const { send, close } = rpc(target.webSocketDebuggerUrl);
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

  fs.mkdirSync(OUT, { recursive: true });
  const results = [];
  for (const shot of SHOTS) {
    if (shot.nav) {
      const clicked = await send('Runtime.evaluate', {
        expression: clickScript(shot.nav[0], shot.nav[1]),
        returnByValue: true,
      });
      if (clicked.result.value !== 'clicked') {
        results.push({ name: shot.name, status: `nav ${shot.nav.join('/')} -> ${clicked.result.value}` });
        console.log(`[installed] SKIP ${shot.name}: ${clicked.result.value}`);
        continue;
      }
      await sleep(shot.settle ?? 1500);
      // Clicking a nav item can leave the scroll position of the sidebar (and of the page) where the
      // previous page left it; a screenshot of a scrolled page hides the heading it is meant to show.
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
      await sleep(400);
    }
    const snapshot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    const file = path.join(OUT, `${shot.name}.png`);
    fs.writeFileSync(file, Buffer.from(snapshot.data, 'base64'));
    const title = await send('Runtime.evaluate', { expression: 'document.title + " | " + location.hash', returnByValue: true });
    results.push({ name: shot.name, status: 'saved', title: title.result.value });
    console.log(`[installed] ${file} — ${title.result.value}`);
  }
  close();
  console.log(JSON.stringify(results, null, 2));
}

main().catch((error) => {
  console.error('[installed] failed:', error.message);
  process.exit(1);
});
