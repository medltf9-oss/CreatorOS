// CreatorOS Studio - Electron main process (Phase 1 walking skeleton)
//
// Responsibilities in this phase:
//  1. resolve the D: data root
//  2. mint a per-launch session token (never written to disk)
//  3. pick a free loopback port and spawn the Python backend
//  4. wait for /health/live, then show the window
//  5. kill the backend tree on quit
//
// Security: contextIsolation on, nodeIntegration off, sandbox on, navigation and
// new windows blocked, CSP enforced from index.html, no remote content.

const { app, BrowserWindow, dialog, ipcMain, safeStorage, shell } = require('electron');
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const http = require('node:http');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { createVault } = require('./vault.cjs');
const { createVaultBroker } = require('./vault-broker.cjs');
const shellPaths = require('./paths.cjs');

const ROOT = shellPaths.resolveRoot();
// Packaged builds keep the backend in resources/backend, never inside app.asar.
const BACKEND_DIR = shellPaths.resolveBackendDir({
  isPackaged: app.isPackaged,
  resourcesPath: process.resourcesPath,
  moduleDir: __dirname,
  exists: fs.existsSync,
});
const isDev = !app.isPackaged;
const DEV_URL = process.env.CREATOROS_DEV_URL || 'http://localhost:5173';

// The credential vault's DPAPI master key lives in userData/Local State. Electron derives that
// directory from the product name, so an installer that renamed the product would orphan every
// stored token. Pin it before anything touches safeStorage.
app.setPath('userData', shellPaths.resolveUserData(app.getPath('appData')));

const state = {
  token: crypto.randomBytes(32).toString('hex'),
  // Phase 5: proof that a media import came from this shell. The renderer never sees it,
  // so it cannot ask the backend to read an arbitrary path.
  ipcSecret: crypto.randomBytes(32).toString('hex'),
  // Phase 6: the backend's only route to a secret. Minted per launch, handed to the backend
  // child process and to nothing else - the renderer has no IPC handle that reveals it.
  vaultToken: crypto.randomBytes(32).toString('hex'),
  vault: null,
  vaultBroker: null,
  vaultUrl: null,
  port: 0,
  backend: null,
  migrating: false,
  backendLog: [],
  ready: false,
  lastError: null,
};

function log(line) {
  const entry = `${new Date().toISOString()} ${line}`;
  state.backendLog.push(entry);
  if (state.backendLog.length > 400) state.backendLog.shift();
  try {
    fs.mkdirSync(path.join(ROOT, 'logs', 'backend'), { recursive: true });
    fs.appendFileSync(path.join(ROOT, 'logs', 'backend', 'backend.log'), entry + '\n');
  } catch {
    /* logging must never crash the app */
  }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function backendEnv(port) {
  const tempDir = path.join(ROOT, 'temp');
  fs.mkdirSync(tempDir, { recursive: true });
  return {
    ...process.env,
    CREATOROS_ROOT: ROOT,
    CREATOROS_SESSION_TOKEN: state.token,
    CREATOROS_IPC_SECRET: state.ipcSecret,
    ...(state.vaultUrl
      ? {
          CREATOROS_VAULT_URL: state.vaultUrl,
          CREATOROS_VAULT_TOKEN: state.vaultToken,
          CREATOROS_VAULT_ROOT: ROOT,
        }
      : {}),
    CREATOROS_PORT: String(port),
    CREATOROS_LOG_LEVEL: 'INFO',
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: "utf-8",
    // ADR-05: per-process temp only - the machine TEMP/TMP stay untouched
    TEMP: tempDir,
    TMP: tempDir,
  };
}

function backendCommand() {
  return shellPaths.resolveBackendCommand({ backendDir: BACKEND_DIR, isPackaged: app.isPackaged, exists: fs.existsSync });
}

/**
 * Bring the database up to head before the API starts.
 *
 * A fresh install has no database at all, and the API's boot preflight treats a missing migration
 * table as "not migrated" rather than creating it. Migrating is therefore a launch step, not an
 * installer step: the data root can be pointed elsewhere by the user, and it must work the same way
 * on the second launch as on the first (an upgrade applies only the outstanding revisions).
 */
function migrate() {
  return new Promise((resolve) => {
    const { command, prefix } = backendCommand();
    const args = [...prefix, ...shellPaths.backendArgs('migrate')];
    log(`migrate: ${command} ${args.join(' ')}`);
    state.migrating = true;
    let output = '';
    let child;
    try {
      child = spawn(command, args, { cwd: BACKEND_DIR, env: backendEnv(0), windowsHide: true });
    } catch (error) {
      state.migrating = false;
      log(`migrate failed to start: ${error}`);
      resolve({ ok: false, error: String(error) });
      return;
    }
    child.stdout.on('data', (d) => {
      output += String(d);
      log(`[migrate] ${String(d).trimEnd()}`);
    });
    child.stderr.on('data', (d) => {
      output += String(d);
      log(`[migrate!] ${String(d).trimEnd()}`);
    });
    child.on('error', (error) => {
      state.migrating = false;
      log(`migrate error: ${error}`);
      resolve({ ok: false, error: String(error) });
    });
    child.on('exit', (code) => {
      state.migrating = false;
      log(`migrate exited code=${code}`);
      resolve({ ok: code === 0, code, output });
    });
  });
}

async function startBackend() {
  const migration = await migrate();
  if (!migration.ok) {
    state.lastError = `Database migration failed (code ${migration.code ?? 'spawn'}). See logs/backend/backend.log`;
    log(state.lastError);
    return false;
  }

  state.port = await freePort();
  const { command, prefix, bundled } = backendCommand();
  if (!bundled) log(`WARN no bundled backend found in ${BACKEND_DIR}; falling back to ${command}`);
  const args = [...prefix, ...shellPaths.backendArgs('serve', { port: state.port })];
  log(`spawn: ${command} ${args.join(' ')}`);

  state.backend = spawn(command, args, { cwd: BACKEND_DIR, env: backendEnv(state.port), windowsHide: true });
  state.backend.stdout.on('data', (d) => log(`[out] ${String(d).trimEnd()}`));
  state.backend.stderr.on('data', (d) => log(`[err] ${String(d).trimEnd()}`));
  state.backend.on('exit', (code, signal) => {
    log(`backend exited code=${code} signal=${signal}`);
    state.ready = false;
    state.lastError = `Backend exited (code ${code}). See logs/backend/backend.log`;
  });

  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${state.port}/health/live`);
      if (res.ok) {
        state.ready = true;
        state.lastError = null;
        log(`backend ready on 127.0.0.1:${state.port}`);
        return true;
      }
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  state.lastError = 'Backend did not become ready within 45s.';
  log(state.lastError);
  return false;
}

function stopBackend() {
  if (!state.backend || state.backend.killed) return;
  const pid = state.backend.pid;
  try {
    if (process.platform === 'win32') spawn('taskkill', ['/pid', String(pid), '/T', '/F']);
    else state.backend.kill('SIGTERM');
    log(`stopped backend pid=${pid}`);
  } catch (err) {
    log(`stop failed: ${err}`);
  }
}

function createWindow() {
  const icon = shellPaths.resolveIcon({ moduleDir: __dirname, resourcesPath: process.resourcesPath, exists: fs.existsSync });
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    backgroundColor: '#0f1115',
    show: false,
    title: 'CreatorOS',
    ...(icon ? { icon } : {}),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  win.once('ready-to-show', () => win.show());

  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    const allowed = isDev ? [DEV_URL] : ['file://'];
    if (!allowed.some((prefix) => url.startsWith(prefix))) event.preventDefault();
  });

  const renderer = shellPaths.resolveRenderer({ isPackaged: app.isPackaged, moduleDir: __dirname, devUrl: DEV_URL });
  if (renderer.kind === 'url') win.loadURL(renderer.target);
  else win.loadFile(renderer.target);
  return win;
}

ipcMain.handle('creatoros:bootstrap', () => ({
  apiBase: `http://127.0.0.1:${state.port}`,
  token: state.token,
  ready: state.ready,
  error: state.lastError,
  root: ROOT,
  isDev,
  version: app.getVersion(),
  // Where the shell keeps its own log, so an error dialog can point at a real file.
  logPath: path.join(ROOT, 'logs', 'backend', 'backend.log'),
}));

ipcMain.handle('creatoros:backendLog', () => state.backendLog.slice(-120));

function signIpc(model) {
  const body = Buffer.from(JSON.stringify(model), 'utf8');
  const stamp = Math.floor(Date.now() / 1000);
  const digest = crypto.createHmac('sha256', state.ipcSecret).update(`${stamp}.`).update(body).digest('hex');
  return { body, header: `${stamp}.${digest}` };
}

function postJson(routePath, model, header) {
  return new Promise((resolve) => {
    const payload = Buffer.from(JSON.stringify(model), 'utf8');
    const request = http.request(
      {
        host: '127.0.0.1',
        port: state.port,
        path: routePath,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': payload.length,
          Authorization: `Bearer ${state.token}`,
          ...(header ? { 'x-creatoros-ipc': header } : {}),
        },
      },
      (response) => {
        const chunks = [];
        response.on('data', (chunk) => chunks.push(chunk));
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let json = null;
          try {
            json = text ? JSON.parse(text) : null;
          } catch {
            json = { message: text };
          }
          resolve({ ok: response.statusCode >= 200 && response.statusCode < 300, status: response.statusCode, json });
        });
      },
    );
    request.on('error', (error) => resolve({ ok: false, status: 0, json: { message: String(error) } }));
    request.end(payload);
  });
}

const MEDIA_FILTERS = [
  { name: 'Video, audio and images', extensions: ['mp4', 'mov', 'm4v', 'mkv', 'webm', 'avi', 'mp3', 'wav', 'm4a', 'aac', 'flac', 'jpg', 'jpeg', 'png', 'webp'] },
  { name: 'All files', extensions: ['*'] },
];

ipcMain.handle('creatoros:pickMedia', async () => {
  const result = await dialog.showOpenDialog({
    title: 'Import media into CreatorOS',
    properties: ['openFile', 'multiSelections'],
    filters: MEDIA_FILTERS,
  });
  if (result.canceled) return { canceled: true, files: [] };
  const files = [];
  for (const file of result.filePaths) {
    try {
      const stats = fs.lstatSync(file);
      // Only regular files: a symlink or directory could point outside the data root.
      if (!stats.isFile() || stats.isSymbolicLink()) continue;
      files.push({ path: file, name: path.basename(file), size_bytes: stats.size });
    } catch {
      /* unreadable entry: skip, the backend reports rejections too */
    }
  }
  return { canceled: false, files };
});

ipcMain.handle('creatoros:importMedia', async (_event, paths) => {
  if (!state.ready) return { ok: false, error: 'the backend is not ready' };
  if (!Array.isArray(paths) || paths.length === 0) return { ok: false, error: 'no files selected' };
  if (paths.length > 100) return { ok: false, error: 'at most 100 files per import' };
  const entries = [];
  for (const candidate of paths) {
    if (typeof candidate !== 'string' || candidate.length > 4000) return { ok: false, error: 'invalid path' };
    const resolved = path.resolve(candidate);
    if (!fs.existsSync(resolved)) return { ok: false, error: `file does not exist: ${path.basename(resolved)}` };
    const stats = fs.lstatSync(resolved);
    if (!stats.isFile() || stats.isSymbolicLink()) return { ok: false, error: 'only regular files can be imported' };
    entries.push({ path: resolved, original_filename: path.basename(resolved) });
  }
  const model = { paths: entries };
  const signed = signIpc(model);
  const response = await postJson('/library/import', model, signed.header);
  return { ok: response.ok, status: response.status, payload: response.json };
});

ipcMain.handle('creatoros:openPath', async (_event, target) => {
  const resolved = path.resolve(target);
  if (!resolved.toLowerCase().startsWith(ROOT.toLowerCase())) return { ok: false, error: 'outside data root' };
  const result = await shell.openPath(resolved);
  return { ok: result === '', error: result };
});

async function startVault() {
  // safeStorage needs an app; the async API and DPAPI are Windows-only in practice here.
  if (process.platform !== 'win32' && !process.env.CREATOROS_ALLOW_NON_WINDOWS_VAULT) {
    log('vault disabled: unsupported platform');
    return false;
  }
  try {
    state.vault = createVault({ root: ROOT, safeStorage, logger: log });
    const available = await state.vault.refreshAvailability();
    if (!available) {
      log('vault unavailable: OS encryption is not available for this user session');
      return false;
    }
    state.vaultBroker = createVaultBroker({ vault: state.vault, token: state.vaultToken, logger: log });
    const listener = await state.vaultBroker.listen();
    state.vaultUrl = listener.url;
    log(`vault broker listening on ${listener.url}`);
    return true;
  } catch (error) {
    log(`vault startup failed: ${error && error.message ? error.message : error}`);
    return false;
  }
}

// Provider authorization hosts. The renderer may ask the shell to open an OAuth URL, and only
// these hosts are accepted - this is not a general-purpose URL opener.
const AUTH_HOSTS = new Set([
  'www.facebook.com',
  'facebook.com',
  'www.instagram.com',
  'api.instagram.com',
  'www.tiktok.com',
]);

ipcMain.handle('creatoros:vaultHealth', async () => {
  if (!state.vault) {
    return {
      available: false,
      backend: 'none',
      reason: 'the credential vault is not available in this process',
      protection: 'no credential has been stored',
    };
  }
  try {
    return await state.vault.health();
  } catch (error) {
    return { available: false, backend: 'error', reason: String(error && error.message), protection: 'unknown' };
  }
});

ipcMain.handle('creatoros:vaultStore', async (_event, request) => {
  if (!state.vault) return { ok: false, code: 'VAULT_UNAVAILABLE', error: 'the credential vault is not available' };
  const payload = request && typeof request === 'object' ? request : {};
  try {
    const record = await state.vault.put({
      id: payload.id,
      secret: payload.secret,
      purpose: payload.purpose,
      provider: payload.provider ?? null,
      alias: payload.alias ?? null,
    });
    // Only non-secret metadata goes back to the renderer.
    return { ok: true, record };
  } catch (error) {
    return { ok: false, code: error.code ?? 'VAULT_ERROR', error: String(error.message).slice(0, 300) };
  }
});

ipcMain.handle('creatoros:vaultDelete', async (_event, id) => {
  if (!state.vault) return { ok: false, code: 'VAULT_UNAVAILABLE', error: 'the credential vault is not available' };
  try {
    return { ok: true, ...state.vault.remove(id) };
  } catch (error) {
    return { ok: false, code: error.code ?? 'VAULT_ERROR', error: String(error.message).slice(0, 300) };
  }
});

ipcMain.handle('creatoros:openAuthUrl', async (_event, url) => {
  if (typeof url !== 'string' || url.length > 4000) return { ok: false, error: 'invalid url' };
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { ok: false, error: 'invalid url' };
  }
  if (parsed.protocol !== 'https:') return { ok: false, error: 'only https authorization urls are allowed' };
  if (!AUTH_HOSTS.has(parsed.hostname)) return { ok: false, error: `host not allowed: ${parsed.hostname}` };
  await shell.openExternal(url);
  return { ok: true, host: parsed.hostname };
});

function focusExistingWindow() {
  const [win] = BrowserWindow.getAllWindows();
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.focus();
}

// One shell instance per user session: two instances would spawn two backends against the same
// SQLite file and two vault brokers, which is how a WAL database gets corrupted.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', focusExistingWindow);

  app.whenReady().then(async () => {
    await startVault();
    const started = await startBackend();
    createWindow();
    if (!started) {
      // The window still opens: it shows the failure honestly instead of a blank screen, and the
      // dialog points at the log file rather than asking the user to guess.
      const { response } = await dialog.showMessageBox({
        type: 'error',
        title: 'CreatorOS could not start its backend',
        message: state.lastError || 'The backend did not start.',
        detail: `Data root: ${ROOT}
Log: ${path.join(ROOT, 'logs', 'backend', 'backend.log')}`,
        buttons: ['Open log folder', 'Close'],
        defaultId: 1,
        cancelId: 1,
      });
      if (response === 0) shell.openPath(path.join(ROOT, 'logs', 'backend'));
    }
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on('window-all-closed', () => {
  stopBackend();
  if (process.platform !== 'darwin') app.quit();
});
app.on('before-quit', stopBackend);
process.on('exit', stopBackend);
