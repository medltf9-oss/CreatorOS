// Vault broker: the only way the Python backend can touch a secret (ADR-22).
//
// A loopback HTTP server on an ephemeral port. It is reachable only by a process that knows
// `CREATOROS_VAULT_TOKEN`, a per-launch random value passed to the backend child and to nobody
// else (the renderer never sees it, and there is no IPC handle that exposes it).
//
// Every request must name one secret id and one purpose. There is no enumeration, no wildcard
// read, no bulk fetch. Responses contain exactly one value and are marked no-store.

'use strict';

const crypto = require('node:crypto');
const http = require('node:http');

const PROTOCOL_VERSION = 1;
const MAX_BODY_BYTES = 16 * 1024;
const RATE_LIMIT_PER_MINUTE = 120;
const ALLOWED_PURPOSES = new Set([
  'provider_app_secret',
  'access_token',
  'refresh_token',
  'oauth_pkce_verifier',
  'rotation_journal',
  'token_metadata',
  'delivery_credentials',
  'provider_upload_session',
]);

function timingSafeEqual(a, b) {
  const left = Buffer.from(String(a ?? ''), 'utf8');
  const right = Buffer.from(String(b ?? ''), 'utf8');
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    request.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('body too large'), { code: 'VAULT_REQUEST_TOO_LARGE' }));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8');
      if (!text) return resolve({});
      try {
        resolve(JSON.parse(text));
      } catch {
        reject(Object.assign(new Error('body is not JSON'), { code: 'VAULT_INVALID_REQUEST' }));
      }
    });
    request.on('error', reject);
  });
}

/**
 * @param {{vault: object, token: string, logger?: (line: string) => void, now?: () => number}} options
 */
function createVaultBroker(options) {
  const vault = options.vault;
  const token = options.token;
  const logger = options.logger ?? (() => {});
  const now = options.now ?? Date.now;
  if (!token || token.length < 32) throw new Error('vault broker requires a per-launch token');
  const grants = new Map(); // id -> {purpose, expiresAt, remaining}
  const calls = [];
  let windowStart = now();
  let windowCount = 0;

  function rateLimited() {
    if (now() - windowStart > 60_000) {
      windowStart = now();
      windowCount = 0;
    }
    windowCount += 1;
    return windowCount > RATE_LIMIT_PER_MINUTE;
  }

  function audit(entry) {
    // Purpose, id, outcome - never a value.
    calls.push({ at: new Date(now()).toISOString(), ...entry });
    if (calls.length > 200) calls.shift();
    logger(`vault-broker ${entry.operation} ${entry.outcome} purpose=${entry.purpose ?? '-'} id=${entry.id ?? '-'}`);
  }

  function reply(response, status, payload) {
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    response.writeHead(status, {
      'Content-Type': 'application/json',
      'Content-Length': body.length,
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Cross-Origin-Resource-Policy': 'same-site',
    });
    response.end(body);
  }

  async function handle(request, response) {
    if (request.method !== 'POST') return reply(response, 405, { code: 'METHOD_NOT_ALLOWED', message: 'POST only' });
    if (request.headers['x-creatoros-vault-proto'] !== String(PROTOCOL_VERSION)) {
      return reply(response, 400, { code: 'VAULT_PROTOCOL', message: 'unsupported broker protocol' });
    }
    if (!timingSafeEqual(request.headers['x-creatoros-vault'], token)) {
      audit({ operation: request.url, outcome: 'unauthorized' });
      // Same shape and timing regardless of which part was wrong.
      return reply(response, 403, { code: 'VAULT_UNAUTHORIZED', message: 'broker token rejected' });
    }
    if (rateLimited()) {
      audit({ operation: request.url, outcome: 'rate_limited' });
      return reply(response, 429, { code: 'VAULT_RATE_LIMITED', message: 'too many vault requests' });
    }
    let body;
    try {
      body = await readBody(request);
    } catch (error) {
      return reply(response, 400, { code: error.code ?? 'VAULT_INVALID_REQUEST', message: error.message });
    }
    const purpose = body.purpose;
    if (purpose !== undefined && !ALLOWED_PURPOSES.has(purpose)) {
      audit({ operation: request.url, outcome: 'bad_purpose', purpose: String(purpose).slice(0, 40) });
      return reply(response, 400, { code: 'VAULT_INVALID_PURPOSE', message: 'purpose is not allowed' });
    }

    try {
      if (request.url === '/vault/put') {
        const info = await vault.put(body);
        audit({ operation: 'put', outcome: 'ok', purpose, id: body.id });
        return reply(response, 200, { ok: true, record: info });
      }
      if (request.url === '/vault/get') {
        if (!body.id) return reply(response, 400, { code: 'VAULT_INVALID_REQUEST', message: 'id is required' });
        // A grant, when supplied, must match the id and purpose and is consumed exactly once.
        if (body.grant) {
          const grant = grants.get(body.grant);
          if (!grant || grant.expiresAt < now() || grant.id !== body.id || grant.purpose !== purpose) {
            audit({ operation: 'get', outcome: 'bad_grant', purpose, id: body.id });
            return reply(response, 403, { code: 'VAULT_GRANT_INVALID', message: 'grant is unknown, expired or out of scope' });
          }
          grants.delete(body.grant);
        }
        const value = await vault.get(body.id, purpose);
        audit({ operation: 'get', outcome: 'ok', purpose, id: body.id });
        return reply(response, 200, { ok: true, id: body.id, value });
      }
      if (request.url === '/vault/grant') {
        if (!body.id) return reply(response, 400, { code: 'VAULT_INVALID_REQUEST', message: 'id is required' });
        const grantId = crypto.randomUUID();
        const ttl = Math.min(Math.max(Number(body.ttl_s) || 60, 5), 300);
        grants.set(grantId, { id: body.id, purpose, expiresAt: now() + ttl * 1000 });
        audit({ operation: 'grant', outcome: 'ok', purpose, id: body.id });
        return reply(response, 200, { ok: true, grant: grantId, expires_in_s: ttl });
      }
      if (request.url === '/vault/rotate') {
        const info = await vault.rotate(body.id, body.secret);
        audit({ operation: 'rotate', outcome: 'ok', purpose, id: body.id });
        return reply(response, 200, { ok: true, record: info });
      }
      if (request.url === '/vault/delete') {
        const result = vault.remove(body.id);
        audit({ operation: 'delete', outcome: 'ok', purpose, id: body.id });
        return reply(response, 200, { ok: true, ...result });
      }
      if (request.url === '/vault/verify') {
        const result = vault.verify(body.id);
        audit({ operation: 'verify', outcome: result.ok ? 'ok' : 'failed', purpose, id: body.id });
        return reply(response, 200, { ok: true, result });
      }
      if (request.url === '/vault/health') {
        const result = await vault.health();
        audit({ operation: 'health', outcome: 'ok' });
        return reply(response, 200, { ok: true, health: result });
      }
      if (request.url === '/vault/audit') {
        audit({ operation: 'audit', outcome: 'ok' });
        return reply(response, 200, { ok: true, calls: calls.slice(-50) });
      }
      return reply(response, 404, { code: 'VAULT_UNKNOWN_OPERATION', message: 'unknown vault operation' });
    } catch (error) {
      const code = error.code ?? 'VAULT_ERROR';
      audit({ operation: request.url, outcome: code, purpose, id: body.id });
      // error.message is written by the vault and never contains a secret.
      return reply(response, code === 'VAULT_NOT_FOUND' ? 404 : 400, { code, message: error.message });
    }
  }

  function listen() {
    return new Promise((resolve, reject) => {
      const server = http.createServer((request, response) => {
        handle(request, response).catch((error) => {
          try {
            reply(response, 500, { code: 'VAULT_INTERNAL', message: 'vault broker failure' });
          } catch {
            /* response already closed */
          }
          logger(`vault-broker internal error: ${error.message}`);
        });
      });
      server.on('error', reject);
      server.listen(0, '127.0.0.1', () => {
        const address = server.address();
        resolve({ server, port: address.port, url: `http://127.0.0.1:${address.port}` });
      });
    });
  }

  return { listen, grants, calls, handle };
}

module.exports = { createVaultBroker, PROTOCOL_VERSION, ALLOWED_PURPOSES };
