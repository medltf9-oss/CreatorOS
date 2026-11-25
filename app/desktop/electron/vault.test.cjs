// Vault tests. Run with: node --test electron/ (from app/desktop)
//
// These exercise the real vault and broker code against a stand-in for DPAPI. The stand-in is
// deliberately not encryption - it is a XOR keystream - because the point of these tests is the
// record format, integrity checks, rotation, quarantine and the broker's authorization rules,
// not to re-implement DPAPI. Real DPAPI is covered by the opt-in self-test in
// `scripts/vault_selftest.cjs`, which runs inside Electron.

'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { createVault, MAGIC } = require('./vault.cjs');
const { createVaultBroker } = require('./vault-broker.cjs');

function fakeSafeStorage({ rotateAfter = Infinity } = {}) {
  const key = crypto.randomBytes(32);
  const nextKey = crypto.randomBytes(32);
  let calls = 0;
  return {
    calls: () => calls,
    async isAsyncEncryptionAvailable() {
      return true;
    },
    async encryptStringAsync(plainText) {
      calls += 1;
      const data = Buffer.from(plainText, 'utf8');
      const out = Buffer.alloc(data.length + 32);
      crypto.createHash('sha256').update(key).digest().copy(out, 0);
      for (let i = 0; i < data.length; i += 1) out[32 + i] = data[i] ^ key[i % 32];
      return out;
    },
    async decryptStringAsync(encrypted) {
      calls += 1;
      const tag = crypto.createHash('sha256').update(key).digest();
      if (encrypted.length < 32 || !crypto.timingSafeEqual(tag, encrypted.subarray(0, 32))) {
        throw new Error('DPAPI: decryption failed');
      }
      const body = encrypted.subarray(32);
      const out = Buffer.alloc(body.length);
      for (let i = 0; i < body.length; i += 1) out[i] = body[i] ^ key[i % 32];
      return { result: out.toString('utf8'), shouldReEncrypt: calls > rotateAfter };
    },
    _nextKey: nextKey,
  };
}

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'creatoros-vault-'));
}

function ulid() {
  // 26 Crockford base32 characters, enough for the id pattern.
  const alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  let out = '';
  for (let i = 0; i < 26; i += 1) out += alphabet[crypto.randomInt(0, alphabet.length)];
  return out;
}

test('encrypt/decrypt round trip writes only ciphertext', async () => {
  const root = tempRoot();
  const storage = fakeSafeStorage();
  const vault = createVault({ root, safeStorage: storage });
  const id = ulid();
  const secret = 'EAAB-secret-access-token-value';
  const info = await vault.put({ id, secret, purpose: 'access_token', provider: 'facebook', alias: 'page:1' });
  assert.equal(info.id, id);
  assert.ok(info.fingerprint.length >= 16);

  const file = path.join(root, 'config', 'secrets', `${id}.bin`);
  const raw = fs.readFileSync(file);
  assert.ok(raw.subarray(0, MAGIC.length).toString('ascii') === MAGIC);
  assert.equal(raw.includes(Buffer.from(secret, 'utf8')), false, 'plaintext must not be on disk');

  const value = await vault.get(id, 'access_token');
  assert.equal(value, secret);
});

test('purpose mismatch is refused', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const id = ulid();
  await vault.put({ id, secret: 'tok', purpose: 'access_token' });
  await assert.rejects(
    () => vault.get(id, 'refresh_token'),
    (error) => error.code === 'VAULT_PURPOSE_MISMATCH' && /refresh_token/.test(error.message),
  );
});

test('tampering with the ciphertext is detected before decryption', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const id = ulid();
  await vault.put({ id, secret: 'original-token', purpose: 'access_token' });
  const file = path.join(root, 'config', 'secrets', `${id}.bin`);
  const raw = fs.readFileSync(file);
  const text = raw.toString('utf8');
  const envelope = JSON.parse(text.slice(MAGIC.length + 1));
  const data = Buffer.from(envelope.data, 'base64');
  data[data.length - 1] ^= 0xff;
  envelope.data = data.toString('base64');
  fs.writeFileSync(file, Buffer.concat([Buffer.from(`${MAGIC}\n`), Buffer.from(JSON.stringify(envelope))]));

  await assert.rejects(() => vault.get(id, 'access_token'), (error) => error.code === 'VAULT_CORRUPT');
  const verdict = vault.verify(id);
  assert.equal(verdict.ok, false);
  // The corrupt record is preserved for diagnosis, not deleted.
  const quarantined = fs.readdirSync(path.join(root, 'config', 'secrets', 'quarantine'));
  assert.equal(quarantined.length, 1);
});

test('decryption failure does not overwrite the record', async () => {
  const root = tempRoot();
  const storage = fakeSafeStorage();
  const vault = createVault({ root, safeStorage: storage });
  const id = ulid();
  await vault.put({ id, secret: 'token', purpose: 'access_token' });
  const before = fs.readFileSync(path.join(root, 'config', 'secrets', `${id}.bin`));

  const broken = createVault({
    root,
    safeStorage: {
      async isAsyncEncryptionAvailable() {
        return true;
      },
      async encryptStringAsync() {
        return Buffer.from('x');
      },
      async decryptStringAsync() {
        throw new Error('DPAPI: wrong user');
      },
    },
  });
  await assert.rejects(() => broken.get(id, 'access_token'), (error) => error.code === 'VAULT_DECRYPT_FAILED');
  const after = fs.readFileSync(path.join(root, 'config', 'secrets', `${id}.bin`));
  assert.deepEqual(after, before);
});

test('shouldReEncrypt rotates the record transparently', async () => {
  const root = tempRoot();
  const storage = fakeSafeStorage({ rotateAfter: 0 });
  const vault = createVault({ root, safeStorage: storage });
  const id = ulid();
  await vault.put({ id, secret: 'rotating-token', purpose: 'access_token' });
  const value = await vault.get(id, 'access_token');
  assert.equal(value, 'rotating-token');
  const info = vault.list().find((entry) => entry.id === id);
  assert.ok(info.rotated_at, 'rotation timestamp is recorded');
  assert.equal((await vault.get(id, 'access_token')), 'rotating-token');
});

test('rotation via rotate() keeps the same id and changes the ciphertext', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const id = ulid();
  await vault.put({ id, secret: 'first', purpose: 'access_token' });
  const first = fs.readFileSync(path.join(root, 'config', 'secrets', `${id}.bin`));
  await vault.rotate(id, 'second');
  const second = fs.readFileSync(path.join(root, 'config', 'secrets', `${id}.bin`));
  assert.notDeepEqual(first, second);
  assert.equal(await vault.get(id, 'access_token'), 'second');
});

test('deletion removes the file and is idempotent', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const id = ulid();
  await vault.put({ id, secret: 'bye', purpose: 'access_token' });
  assert.equal(vault.remove(id).removed, true);
  assert.equal(vault.remove(id).removed, false);
  await assert.rejects(() => vault.get(id), (error) => error.code === 'VAULT_NOT_FOUND');
});

test('missing vault directory is created on demand and empty health is healthy', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const health = await vault.health();
  assert.equal(health.available, true);
  assert.equal(health.entries, 0);
  assert.equal(health.corrupt, 0);
  assert.ok(health.protection.includes('does not isolate'));
});

test('unsafe ids, empty secrets and unknown purposes are refused', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  await assert.rejects(() => vault.put({ id: '../../etc/passwd', secret: 'x', purpose: 'access_token' }), /ULID/);
  await assert.rejects(() => vault.put({ id: ulid(), secret: '', purpose: 'access_token' }), /empty/);
  await assert.rejects(() => vault.put({ id: ulid(), secret: 'x', purpose: 'anything' }), /purpose/);
});

test('duplicate put is refused so a live credential cannot be clobbered', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const id = ulid();
  await vault.put({ id, secret: 'one', purpose: 'access_token' });
  await assert.rejects(() => vault.put({ id, secret: 'two', purpose: 'access_token' }), (error) => error.code === 'VAULT_EXISTS');
});

test('no plaintext secret appears anywhere under the data root', async () => {
  const root = tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const secrets = ['ya29.facebook-token-AAAA', 'tiktok-refresh-BBBB', 'ig-app-secret-CCCC'];
  const purposes = ['access_token', 'refresh_token', 'provider_app_secret'];
  for (let i = 0; i < secrets.length; i += 1) {
    await vault.put({ id: ulid(), secret: secrets[i], purpose: purposes[i] });
  }
  const found = [];
  for (const entry of fs.readdirSync(path.join(root, 'config', 'secrets'))) {
    const full = path.join(root, 'config', 'secrets', entry);
    if (!fs.statSync(full).isFile()) continue;
    const text = fs.readFileSync(full).toString('latin1');
    for (const secret of secrets) if (text.includes(secret)) found.push(`${entry}:${secret}`);
  }
  assert.deepEqual(found, []);
});

test('vault refuses to construct without safeStorage', () => {
  assert.throws(() => createVault({ root: tempRoot(), safeStorage: null }), /safeStorage/);
});

// ---------------------------------------------------------------- broker tests

const openServers = [];
test.after(async () => {
  await Promise.all(openServers.map((server) => new Promise((resolve) => server.close(resolve))));
});

async function startBroker(overrides = {}) {
  const root = overrides.root ?? tempRoot();
  const vault = createVault({ root, safeStorage: fakeSafeStorage() });
  const token = crypto.randomBytes(32).toString('hex');
  const broker = createVaultBroker({ vault, token, logger: () => {} });
  const listener = await broker.listen();
  openServers.push(listener.server);
  return { root, vault, token, broker, listener };
}

async function call(listener, token, route, body = {}, headers = {}) {
  const response = await fetch(`${listener.url}${route}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-creatoros-vault-proto': '1',
      ...(token ? { 'x-creatoros-vault': token } : {}),
      ...headers,
    },
    body: JSON.stringify(body),
  });
  return { status: response.status, json: await response.json() };
}

test('broker rejects a missing or wrong token', async () => {
  const { listener, token } = await startBroker();
  assert.equal((await call(listener, null, '/vault/health')).status, 403);
  assert.equal((await call(listener, 'x'.repeat(64), '/vault/health')).status, 403);
  assert.equal((await call(listener, token, '/vault/health')).status, 200);
});

test('broker put then get returns exactly one secret', async () => {
  const { listener, token } = await startBroker();
  const id = ulid();
  const put = await call(listener, token, '/vault/put', {
    id,
    secret: 'brokered-token',
    purpose: 'access_token',
    provider: 'tiktok',
  });
  assert.equal(put.status, 200);
  assert.equal(JSON.stringify(put.json).includes('brokered-token'), false, 'put response must not echo the secret');
  const got = await call(listener, token, '/vault/get', { id, purpose: 'access_token' });
  assert.equal(got.status, 200);
  assert.equal(got.json.value, 'brokered-token');
});

test('broker refuses unknown purposes and unknown operations', async () => {
  const { listener, token } = await startBroker();
  assert.equal((await call(listener, token, '/vault/put', { id: ulid(), secret: 'x', purpose: 'everything' })).status, 400);
  assert.equal((await call(listener, token, '/vault/list')).status, 404);
  assert.equal((await call(listener, token, '/vault/delete-all')).status, 404);
});

test('broker grants are single use and scoped to one id and purpose', async () => {
  const { listener, token } = await startBroker();
  const id = ulid();
  await call(listener, token, '/vault/put', { id, secret: 'granted', purpose: 'access_token' });
  const grant = await call(listener, token, '/vault/grant', { id, purpose: 'access_token', ttl_s: 30 });
  assert.equal(grant.status, 200);
  const first = await call(listener, token, '/vault/get', { id, purpose: 'access_token', grant: grant.json.grant });
  assert.equal(first.status, 200);
  const second = await call(listener, token, '/vault/get', { id, purpose: 'access_token', grant: grant.json.grant });
  assert.equal(second.status, 403, 'a grant cannot be replayed');
});

test('broker audit trail contains no secret values', async () => {
  const { listener, token, broker } = await startBroker();
  const id = ulid();
  await call(listener, token, '/vault/put', { id, secret: 'audit-me-not', purpose: 'access_token' });
  await call(listener, token, '/vault/get', { id, purpose: 'access_token' });
  const serialised = JSON.stringify(broker.calls);
  assert.equal(serialised.includes('audit-me-not'), false);
  assert.ok(serialised.includes('put'));
  assert.ok(serialised.includes('get'));
});

test('broker reports vault health without exposing entries', async () => {
  const { listener, token } = await startBroker();
  const health = await call(listener, token, '/vault/health');
  assert.equal(health.status, 200);
  assert.equal(health.json.health.available, true);
  assert.equal(typeof health.json.health.entries, 'number');
  assert.equal(JSON.stringify(health.json).includes('data'), false);
});
