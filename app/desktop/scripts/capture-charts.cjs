// Runs the chart-screenshot Electron main with a clean environment.
//
// Same reason as launch-electron.cjs: some hosts export ELECTRON_RUN_AS_NODE=1, which makes Electron
// behave as plain Node and breaks `require('electron')`. The host environment is never modified.
const { spawn } = require('node:child_process');
const path = require('node:path');

const electronPath = require('electron');
const projectRoot = path.join(__dirname, '..');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
delete env.ELECTRON_NO_ATTACH_CONSOLE;

const child = spawn(electronPath, ['scripts/charts-screenshot.cjs', ...process.argv.slice(2)], {
  stdio: 'inherit',
  cwd: projectRoot,
  env,
});
child.on('exit', (code) => process.exit(code ?? 0));
child.on('error', (error) => {
  console.error('[capture-charts] failed to start Electron:', error);
  process.exit(1);
});
