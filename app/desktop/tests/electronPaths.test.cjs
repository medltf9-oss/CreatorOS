// Path resolution for the packaged shell. Plain `node --test`, no Electron.
//
// These are the tests that would have caught the two failures that matter most in an installer:
// pointing at a backend inside app.asar (where no executable exists) and repointing Electron's
// userData (which orphans every stored credential).
const assert = require('node:assert/strict');
const path = require('node:path');
const { test } = require('node:test');

const paths = require('../electron/paths.cjs');

const RESOURCES = path.join('C:', 'Program Files', 'CreatorOS', 'resources');
const MODULE_DIR = path.join('C:', 'Program Files', 'CreatorOS', 'resources', 'app.asar', 'electron');

test('packaged builds resolve the backend outside the asar archive', () => {
  const dir = paths.resolveBackendDir({
    env: {},
    isPackaged: true,
    resourcesPath: RESOURCES,
    moduleDir: MODULE_DIR,
  });
  assert.equal(dir, path.join(RESOURCES, 'backend'));
  assert.equal(dir.includes('.asar'), false, 'a backend inside app.asar can never be executed');
});

test('an explicit backend override wins in every mode', () => {
  const override = path.join('D:', 'CreatorOS', 'app', 'backend');
  for (const isPackaged of [false, true]) {
    assert.equal(
      paths.resolveBackendDir({ env: { CREATOROS_BACKEND_DIR: override }, isPackaged, resourcesPath: RESOURCES, moduleDir: MODULE_DIR }),
      override,
    );
  }
});

test('dev builds resolve the repo checkout relative to electron/', () => {
  const dir = paths.resolveBackendDir({
    env: {},
    isPackaged: false,
    moduleDir: path.join('D:', 'CreatorOS', 'app', 'desktop', 'electron'),
  });
  assert.equal(dir, path.join('D:', 'CreatorOS', 'app', 'backend'));
});

test('packaged builds run the bundled interpreter, never a virtualenv', () => {
  const backendDir = path.join(RESOURCES, 'backend');
  const resolved = paths.resolveBackendCommand({ backendDir, isPackaged: true, exists: () => false });
  assert.equal(resolved.command, path.join(backendDir, 'python.exe'));
  assert.equal(resolved.bundled, true);
  // The bundled interpreter runs the package, so serve/migrate are the same commands in both modes.
  assert.deepEqual(resolved.prefix, ['-m', 'creatoros']);
});

test('dev builds prefer the virtualenv interpreter and fall back to PATH', () => {
  const backendDir = path.join('D:', 'CreatorOS', 'app', 'backend');
  const venv = path.join(backendDir, '.venv', 'Scripts', 'python.exe');

  const withVenv = paths.resolveBackendCommand({ backendDir, isPackaged: false, exists: (p) => p === venv });
  assert.equal(withVenv.command, venv);
  assert.deepEqual(withVenv.prefix, ['-m', 'creatoros']);
  assert.equal(withVenv.bundled, false);

  const withoutVenv = paths.resolveBackendCommand({ backendDir, isPackaged: false, exists: () => false, env: {} });
  assert.equal(withoutVenv.command, 'python');
});

test('an unpackaged build still uses a bundled backend if one is present', () => {
  const backendDir = path.join('D:', 'CreatorOS', 'installer_builds', 'backend');
  const bundled = path.join(backendDir, 'python.exe');
  const resolved = paths.resolveBackendCommand({ backendDir, isPackaged: false, exists: (p) => p === bundled });
  assert.equal(resolved.bundled, true);
  assert.equal(resolved.command, bundled);
});

test('serve and migrate argument vectors carry the port and the revision only where relevant', () => {
  assert.deepEqual(paths.backendArgs('serve', { port: 4321 }), ['serve', '--host', '127.0.0.1', '--port', '4321', '--log-level', 'warning']);
  assert.deepEqual(paths.backendArgs('migrate'), ['migrate']);
  assert.deepEqual(paths.backendArgs('version'), ['version', '--json']);
  assert.throws(() => paths.backendArgs('publish-everything'), /unknown backend command/);
});

test('the data root derives its default from the user profile and accepts an override', () => {
  const home = { USERPROFILE: 'C:\\Users\\example' };
  assert.equal(paths.defaultRoot(home), path.join('C:\\Users\\example', 'CreatorOS'));
  assert.equal(paths.resolveRoot(home), path.join('C:\\Users\\example', 'CreatorOS'));
  assert.equal(paths.resolveRoot({ ...home, CREATOROS_ROOT: '  ' }), path.join('C:\\Users\\example', 'CreatorOS'));
  assert.equal(paths.resolveRoot({ CREATOROS_ROOT: 'E:\\CreatorOS' }), 'E:\\CreatorOS');
});

test('userData is pinned to the directory name that holds the vault master key', () => {
  const appData = path.join('C:', 'Users', 'example', 'AppData', 'Roaming');
  assert.equal(paths.resolveUserData(appData), path.join(appData, 'creatoros-desktop'));
  // The constant is the contract: a product rename must not move it.
  assert.equal(paths.USER_DATA_DIRNAME, 'creatoros-desktop');
  assert.throws(() => paths.resolveUserData(''), /appData is required/);
});

test('the renderer is a URL in dev and a file inside the asar when packaged', () => {
  assert.deepEqual(paths.resolveRenderer({ isPackaged: false, moduleDir: MODULE_DIR, devUrl: 'http://127.0.0.1:5173' }), {
    kind: 'url',
    target: 'http://127.0.0.1:5173',
  });
  assert.deepEqual(paths.resolveRenderer({ isPackaged: true, moduleDir: MODULE_DIR }), {
    kind: 'file',
    target: path.join(MODULE_DIR, '..', 'dist', 'index.html'),
  });
});

test('the icon resolves from resources when packaged, then from the build directory', () => {
  const moduleDir = path.join('D:', 'CreatorOS', 'app', 'desktop', 'electron');
  const buildIcon = path.join('D:', 'CreatorOS', 'app', 'desktop', 'build', 'icon.ico');
  const packagedIcon = path.join('C:', 'Program Files', 'CreatorOS', 'resources', 'icon.ico');

  // Dev: no resourcesPath, so the build directory wins.
  assert.equal(paths.resolveIcon({ moduleDir, exists: (p) => p === buildIcon }), buildIcon);
  // Packaged: the shipped copy wins, because the exe carries the vendor's icon.
  assert.equal(
    paths.resolveIcon({ moduleDir, resourcesPath: path.dirname(packagedIcon), exists: (p) => p === packagedIcon }),
    packagedIcon,
  );
  assert.equal(paths.resolveIcon({ moduleDir, exists: () => false }), null);
});
