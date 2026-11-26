// Path resolution for the desktop shell, isolated from Electron so it can be tested with `node --test`.
//
// Three environments have to work with the same code:
//
//   dev        repo checkout, backend run from its virtualenv, renderer from Vite
//   packaged   installer output: resources/backend holds a frozen backend, renderer in app.asar
//   override   an operator pointing the shell at a different backend or data root
//
// Two rules are load-bearing and are the reason this file exists rather than inline path.join calls:
//
//  1. The backend is NEVER resolved relative to `__dirname` when packaged: inside app.asar that
//     points into the archive, where no executable exists. It comes from `process.resourcesPath`.
//  2. The Electron `userData` directory is PINNED to the name the app has always used. Electron
//     derives it from the product name, and the credential vault's DPAPI master key lives in
//     `userData/Local State`. A new product name ("CreatorOS" instead of "creatoros-desktop") would
//     silently orphan every stored credential, so the directory name is a constant here, not a
//     side effect of a rename.
const path = require('node:path');

//: The data root (ADR-01). Existing installs keep their location through CREATOROS_ROOT or
//: config/bootstrap.json; this compiled default only applies to a first run on a new machine.
//: It is derived from the user profile so no developer path is baked into the build.
const DEFAULT_ROOT = path.join(process.env.USERPROFILE || process.env.HOME || '', 'CreatorOS');

//: The userData directory name in use since Phase 1. Changing this loses the vault master key.
const USER_DATA_DIRNAME = 'creatoros-desktop';

//: The bundled backend is a signed Python interpreter plus the creatoros package, not a compiled
//: executable (ADR-34): Smart App Control blocks newly built unsigned executables, and a copy of the
//: Python Software Foundation interpreter keeps its signature.
const BUNDLED_BACKEND = 'python.exe';

/** The compiled default data root for a fresh install, derived from the user profile. */
function defaultRoot(env = process.env) {
  const home = (env.USERPROFILE || env.HOME || '').trim();
  return home ? path.join(home, 'CreatorOS') : 'CreatorOS';
}

/** The data root: env override first, then the compiled default (ADR-01 / core/settings.py). */
function resolveRoot(env = process.env) {
  const candidate = (env.CREATOROS_ROOT || '').trim();
  return candidate || defaultRoot(env);
}

/**
 * Where the backend lives.
 *
 * Order: explicit override, then `resources/backend` when packaged, then the repo checkout.
 * `exists` is injected so a test can describe a layout without creating files.
 */
function resolveBackendDir({ env = process.env, isPackaged = false, resourcesPath, moduleDir, exists }) {
  const override = (env.CREATOROS_BACKEND_DIR || '').trim();
  if (override) return path.resolve(override);
  if (isPackaged) {
    if (!resourcesPath) throw new Error('resourcesPath is required when packaged');
    return path.join(resourcesPath, 'backend');
  }
  return path.resolve(path.join(moduleDir, '..', '..', 'backend'));
}

/**
 * The command that starts the backend, and the command that migrates it.
 *
 * Packaged: `<resources>/backend/python.exe -m creatoros serve|migrate`
 * Dev:      `<backend>/.venv/Scripts/python.exe -m creatoros serve|migrate`, falling back to the
 *           `creatoros` module on whatever `python` is on PATH (with a warning at the call site).
 *
 * Both use `-m creatoros`, so the command surface cannot differ between a development checkout and
 * an installed copy.
 */
function resolveBackendCommand({ backendDir, isPackaged, exists, env = process.env }) {
  const bundled = path.join(backendDir, BUNDLED_BACKEND);
  if (isPackaged || (exists && exists(bundled))) {
    return { command: bundled, prefix: ['-m', 'creatoros'], bundled: true };
  }
  const venv = path.join(backendDir, '.venv', 'Scripts', 'python.exe');
  const python = exists && exists(venv) ? venv : env.CREATOROS_PYTHON || 'python';
  return { command: python, prefix: ['-m', 'creatoros'], bundled: false };
}

/** Argument vectors, so `serve` and `migrate` cannot drift between dev and packaged builds. */
function backendArgs(kind, { port, host = '127.0.0.1', logLevel = 'warning' } = {}) {
  if (kind === 'serve') return ['serve', '--host', host, '--port', String(port), '--log-level', logLevel];
  if (kind === 'migrate') return ['migrate'];
  if (kind === 'version') return ['version', '--json'];
  throw new Error(`unknown backend command: ${kind}`);
}

/** The Electron userData directory: always the directory this app has always used. */
function resolveUserData(appData) {
  if (!appData) throw new Error('appData is required');
  return path.join(appData, USER_DATA_DIRNAME);
}

/** Where a packaged app's renderer lives, and where a dev renderer is served from. */
function resolveRenderer({ isPackaged, moduleDir, devUrl }) {
  if (!isPackaged) return { kind: 'url', target: devUrl || 'http://localhost:5173' };
  return { kind: 'file', target: path.join(moduleDir, '..', 'dist', 'index.html') };
}

/**
 * The window/taskbar icon.
 *
 * Packaged builds ship it as `resources/icon.ico` (extraResources) because the executable keeps the
 * vendor's signature and therefore the vendor's embedded icon; dev reads it from `build/`. Without
 * this the running app wears the Electron logo on the taskbar while the shortcut wears ours.
 */
function resolveIcon({ moduleDir, resourcesPath, exists }) {
  const candidates = [
    resourcesPath ? path.join(resourcesPath, 'icon.ico') : null,
    path.join(moduleDir, '..', 'build', 'icon.ico'),
    path.join(moduleDir, 'build', 'icon.ico'),
  ].filter(Boolean);
  return candidates.find((candidate) => (exists ? exists(candidate) : true)) || null;
}

module.exports = {
  BUNDLED_BACKEND,
  DEFAULT_ROOT,
  USER_DATA_DIRNAME,
  backendArgs,
  defaultRoot,
  resolveBackendCommand,
  resolveBackendDir,
  resolveIcon,
  resolveRenderer,
  resolveRoot,
  resolveUserData,
};
