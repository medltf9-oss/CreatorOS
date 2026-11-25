// Launches the Electron shell with a clean environment.
//
// Why this exists: some hosts export ELECTRON_RUN_AS_NODE=1 (IDEs, agent
// harnesses, CI wrappers). With that variable set, Electron runs as plain Node
// and `require('electron')` returns a path string instead of the API, so the
// shell cannot start. We strip it for the child process only - the host
// environment is never modified.
const { spawn } = require('node:child_process');
const path = require('node:path');

const electronPath = require('electron');
const projectRoot = path.join(__dirname, '..');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
delete env.ELECTRON_NO_ATTACH_CONSOLE;

const args = ['.'];
if (process.argv.includes('--inspect-shell')) args.unshift('--inspect=9229');
// Optional DevTools protocol port for automated UI verification. Off unless requested.
if (process.env.CREATOROS_DEBUG_PORT) args.unshift(`--remote-debugging-port=${process.env.CREATOROS_DEBUG_PORT}`);

const child = spawn(electronPath, args, { stdio: 'inherit', cwd: projectRoot, env });
child.on('exit', (code) => process.exit(code ?? 0));
child.on('error', (err) => {
  console.error('[launch-electron] failed to start Electron:', err);
  process.exit(1);
});
