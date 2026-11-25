// Renders the brand SVG into the icon set the installer and the running app need.
//
// Run with Electron, which is already a dependency and carries Chromium:
//
//   ./node_modules/.bin/electron scripts/make-icons.cjs
//
// It writes:
//   assets/brand/creatoros-icon-16/24/32/48/64/128/256/512/1024.png
//   app/desktop/build/icon.ico            (multi-image ICO: 16,24,32,48,64,128,256)
//   app/desktop/build/icon.png            (512, for electron-builder's Linux/mac fallback)
//   app/desktop/build/installerIcon.ico   (same bytes as icon.ico; NSIS wants its own name)
//
// Why an ICO container written by hand: electron-builder accepts a 256 px PNG and converts it, but
// the converted file is opaque and every size is a resample of one bitmap. Writing the container lets
// each size be rasterised at its own size by Chromium, which is what keeps 16 px legible.
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const projectRoot = path.join(__dirname, '..');
const repoRoot = path.join(projectRoot, '..', '..');
const svgPath = path.join(repoRoot, 'assets', 'brand', 'creatoros-icon.svg');
// <= 32 px is drawn from a redrawn variant: at that size the gradient ring thins to one pixel and
// the opening disappears. Same mark, different geometry budget.
const smallSvgPath = path.join(repoRoot, 'assets', 'brand', 'creatoros-icon-small.svg');
const SMALL_UP_TO = 32;
const brandDir = path.dirname(svgPath);
const buildDir = path.join(projectRoot, 'build');

// 16 and 24 are the shell sizes that matter; 256 is what Explorer and NSIS use for large tiles.
const PNG_SIZES = [16, 24, 32, 48, 64, 128, 256, 512, 1024];
const ICO_SIZES = [16, 24, 32, 48, 64, 128, 256];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function waitForPaint(win, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (!settled) {
        settled = true;
        resolve();
      }
    };
    win.webContents.once('paint', finish);
    setTimeout(finish, timeoutMs);
  });
}

async function captureWithRetry(win, attempts = 8) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await win.webContents.capturePage();
    } catch (error) {
      lastError = error;
      await sleep(250);
    }
  }
  throw lastError ?? new Error('capturePage failed');
}

async function renderPng(svg, size) {
  const win = new BrowserWindow({
    width: size,
    height: size,
    show: false,
    transparent: true,
    frame: false,
    resizable: false,
    useContentSize: true,
    webPreferences: { offscreen: true, backgroundThrottling: false, javascript: false },
  });
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;padding:0;background:transparent;overflow:hidden}
    svg{display:block;width:${size}px;height:${size}px}
  </style></head><body>${svg}</body></html>`;
  const tmp = path.join(buildDir, `.icon-${size}.html`);
  fs.writeFileSync(tmp, html, 'utf8');
  try {
    await win.loadURL(pathToFileURL(tmp).toString());
    await waitForPaint(win);
    await sleep(150);
    let image = await captureWithRetry(win);
    // The offscreen surface is clamped by the display scale factor; re-render explicitly at the
    // requested size so a 1024 px icon is not an upscaled 700 px one.
    image = image.resize({ width: size, height: size, quality: 'best' });
    return image.toPNG();
  } finally {
    fs.unlinkSync(tmp);
    win.destroy();
  }
}

function icoContainer(entries) {
  // ICONDIR (6 bytes) + ICONDIRENTRY (16 each) + payloads. Each payload is a PNG, which Windows
  // accepts for every size since Vista; it keeps the alpha channel without a 1990s AND mask.
  const count = entries.length;
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // 1 = icon
  header.writeUInt16LE(count, 4);

  const directory = Buffer.alloc(16 * count);
  let offset = 6 + 16 * count;
  entries.forEach((entry, index) => {
    const base = index * 16;
    directory.writeUInt8(entry.size >= 256 ? 0 : entry.size, base + 0); // 0 means 256
    directory.writeUInt8(entry.size >= 256 ? 0 : entry.size, base + 1);
    directory.writeUInt8(0, base + 2); // palette size
    directory.writeUInt8(0, base + 3); // reserved
    directory.writeUInt16LE(1, base + 4); // colour planes
    directory.writeUInt16LE(32, base + 6); // bits per pixel
    directory.writeUInt32LE(entry.png.length, base + 8);
    directory.writeUInt32LE(offset, base + 12);
    offset += entry.png.length;
  });

  return Buffer.concat([header, directory, ...entries.map((entry) => entry.png)]);
}

app.disableHardwareAcceleration();
app.commandLine.appendSwitch('enable-unsafe-swiftshader');
app.on('window-all-closed', () => {});

app
  .whenReady()
  .then(async () => {
    if (!fs.existsSync(svgPath)) {
      console.error(`[icons] missing brand source: ${svgPath}`);
      app.exit(2);
      return;
    }
    const svg = fs.readFileSync(svgPath, 'utf8');
    const smallSvg = fs.existsSync(smallSvgPath) ? fs.readFileSync(smallSvgPath, 'utf8') : svg;
    fs.mkdirSync(brandDir, { recursive: true });
    fs.mkdirSync(buildDir, { recursive: true });

    const rendered = new Map();
    for (const size of PNG_SIZES) {
      const png = await renderPng(size <= SMALL_UP_TO ? smallSvg : svg, size);
      rendered.set(size, png);
      const out = path.join(brandDir, `creatoros-icon-${size}.png`);
      fs.writeFileSync(out, png);
      console.log('[icons]', path.relative(repoRoot, out), `${size}x${size}`, `${png.length} B`);
    }

    const ico = icoContainer(ICO_SIZES.map((size) => ({ size, png: rendered.get(size) })));
    const icoPath = path.join(buildDir, 'icon.ico');
    fs.writeFileSync(icoPath, ico);
    fs.writeFileSync(path.join(buildDir, 'installerIcon.ico'), ico);
    fs.copyFileSync(path.join(brandDir, 'creatoros-icon-512.png'), path.join(buildDir, 'icon.png'));
    console.log('[icons]', path.relative(repoRoot, icoPath), `${ICO_SIZES.join('/')}`, `${ico.length} B`);
    app.exit(0);
  })
  .catch((error) => {
    console.error('[icons] failed:', error);
    app.exit(1);
  });
