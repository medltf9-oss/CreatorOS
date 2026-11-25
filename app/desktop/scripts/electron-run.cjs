// Runs an Electron main-process script with a clean environment.
//
// Some hosts (IDEs, agent harnesses, CI wrappers) export ELECTRON_RUN_AS_NODE=1. With that variable
// set, `electron` behaves like plain Node and `require('electron')` returns a path string instead of
// the API, so any script that needs BrowserWindow or app fails with "cannot read properties of
// undefined". We strip it for the child process only; the host environment is never modified.
//
//   node scripts/electron-run.cjs scripts/make-icons.cjs [args...]
const { spawn } = require('node:child_process');
const path = require('node:path');

const electronPath = require('electron');
const projectRoot = path.join(__dirname, '..');
const [, , script, ...rest] = process.argv;

if (!script) {
  console.error('usage: node scripts/electron-run.cjs <main-process-script.cjs> [...args]');
  process.exit(2);
}

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
delete env.ELECTRON_NO_ATTACH_CONSOLE;

const child = spawn(electronPath, [path.resolve(projectRoot, script), ...rest], {
  stdio: 'inherit',
  cwd: projectRoot,
  env,
});
child.on('exit', (code) => process.exit(code ?? 0));
child.on('error', (error) => {
  console.error('[electron-run] failed to start Electron:', error);
  process.exit(1);
});
