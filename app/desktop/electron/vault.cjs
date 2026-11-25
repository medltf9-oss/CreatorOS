// CreatorOS credential vault (ADR-07 / ADR-22).
//
// Encryption happens here and nowhere else. Records are written by `safeStorage` (Windows
// DPAPI, user scope) through the asynchronous API - the deprecated synchronous methods are
// never called. The backend never sees a key and never writes plaintext to disk; it asks the
// broker in `vault-broker.cjs` for one secret at a time.
//
// File layout: <root>/config/secrets/<secret-id>.bin
//   ASCII header "CRVAULT1\n" + JSON envelope {v, id, alg, created_at, rotated_at, bytes,
//   sha256, purpose, provider, alias, data}. `data` is base64 of the DPAPI ciphertext.
// The envelope carries no secret material; the sha256 covers the ciphertext so a truncated or
// edited file is detected before decryption is attempted.

'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const MAGIC = 'CRVAULT1';
const SCHEMA_VERSION = 1;
const MIN_SECRET_BYTES = 1;
const MAX_SECRET_BYTES = 64 * 1024;
const ID_PATTERN = /^[0-9A-HJKMNP-TV-Z]{26}$/; // ULID
const PURPOSES = new Set([
  'provider_app_secret',
  'access_token',
  'refresh_token',
  'oauth_pkce_verifier',
  'rotation_journal',
  'token_metadata',
  // Phase 7: S3-compatible delivery credentials, and the short-lived upload URLs that providers
  // return with credentials in the query string.
  'delivery_credentials',
  'provider_upload_session',
]);

class VaultError extends Error {
  constructor(code, message, detail) {
    super(message);
    this.name = 'VaultError';
    this.code = code;
    this.detail = detail ?? null;
  }
}

function nowIso() {
  return new Date().toISOString();
}

function isSafeId(id) {
  return typeof id === 'string' && ID_PATTERN.test(id);
}

/**
 * @param {{root: string, safeStorage?: object, logger?: (line: string) => void}} options
 */
function createVault(options) {
  const root = options.root;
  const logger = options.logger ?? (() => {});
  // Injected so tests can exercise the real code path with a stand-in for DPAPI. Production
  // always receives Electron's `safeStorage`.
  const safeStorage = options.safeStorage;
  const dir = path.join(root, 'config', 'secrets');
  const quarantineDir = path.join(dir, 'quarantine');
  const state = {
    available: false,
    backend: 'unknown',
    lastOperation: null,
    lastOperationAt: null,
    operations: 0,
    failures: 0,
    reencrypted: 0,
  };

  if (!safeStorage || typeof safeStorage.isAsyncEncryptionAvailable !== 'function') {
    throw new VaultError('VAULT_UNAVAILABLE', 'safeStorage is not available in this process');
  }

  function recordPath(id) {
    if (!isSafeId(id)) throw new VaultError('VAULT_INVALID_ID', 'secret id must be a ULID');
    return path.join(dir, `${id}.bin`);
  }

  function ensureDir() {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  }

  function mark(operation, ok, extra) {
    state.operations += 1;
    state.lastOperation = ok ? operation : `${operation}:failed`;
    state.lastOperationAt = nowIso();
    if (!ok) state.failures += 1;
    logger(`vault ${state.lastOperation}${extra ? ` ${extra}` : ''}`);
  }

  function encode(record) {
    const body = JSON.stringify(record);
    return Buffer.concat([Buffer.from(`${MAGIC}\n`, 'ascii'), Buffer.from(body, 'utf8')]);
  }

  function decode(buffer, id) {
    if (buffer.length < MAGIC.length + 2) throw new VaultError('VAULT_CORRUPT', 'record is too small');
    const header = buffer.subarray(0, MAGIC.length).toString('ascii');
    if (header !== MAGIC) throw new VaultError('VAULT_CORRUPT', 'record header is not a CreatorOS vault record');
    let record;
    try {
      record = JSON.parse(buffer.subarray(MAGIC.length + 1).toString('utf8'));
    } catch {
      throw new VaultError('VAULT_CORRUPT', 'record envelope is not valid JSON');
    }
    if (record.v !== SCHEMA_VERSION) {
      throw new VaultError('VAULT_VERSION_UNSUPPORTED', `record schema ${record.v} is not supported`);
    }
    if (record.id !== id) throw new VaultError('VAULT_CORRUPT', 'record id does not match its filename');
    if (typeof record.data !== 'string' || record.data.length === 0) {
      throw new VaultError('VAULT_CORRUPT', 'record has no ciphertext');
    }
    const ciphertext = Buffer.from(record.data, 'base64');
    const digest = crypto.createHash('sha256').update(ciphertext).digest('hex');
    if (digest !== record.sha256) throw new VaultError('VAULT_CORRUPT', 'ciphertext digest does not match the envelope');
    if (typeof record.bytes !== 'number' || record.bytes !== ciphertext.length) {
      throw new VaultError('VAULT_CORRUPT', 'ciphertext length does not match the envelope');
    }
    return { record, ciphertext };
  }

  async function refreshAvailability() {
    try {
      state.available = await safeStorage.isAsyncEncryptionAvailable();
    } catch (error) {
      state.available = false;
      logger(`vault availability check failed: ${error && error.message ? error.message : error}`);
    }
    if (typeof safeStorage.getSelectedStorageBackend === 'function') {
      try {
        state.backend = safeStorage.getSelectedStorageBackend() || 'unknown';
      } catch {
        state.backend = 'unknown';
      }
    } else {
      state.backend = process.platform === 'win32' ? 'dpapi' : 'platform';
    }
    return state.available;
  }

  async function put({ id, secret, purpose, provider, alias }) {
    if (!isSafeId(id)) throw new VaultError('VAULT_INVALID_ID', 'secret id must be a ULID');
    if (typeof secret !== 'string') throw new VaultError('VAULT_INVALID_SECRET', 'secret must be a string');
    const size = Buffer.byteLength(secret, 'utf8');
    if (size < MIN_SECRET_BYTES) throw new VaultError('VAULT_INVALID_SECRET', 'secret is empty');
    if (size > MAX_SECRET_BYTES) throw new VaultError('VAULT_INVALID_SECRET', 'secret is too large');
    if (!PURPOSES.has(purpose)) throw new VaultError('VAULT_INVALID_PURPOSE', `unsupported purpose ${purpose}`);
    if (!(await refreshAvailability())) throw new VaultError('VAULT_UNAVAILABLE', 'OS encryption is not available');
    ensureDir();

    const ciphertext = await safeStorage.encryptStringAsync(secret);
    if (!Buffer.isBuffer(ciphertext) || ciphertext.length === 0) {
      throw new VaultError('VAULT_ENCRYPT_FAILED', 'encryption produced no ciphertext');
    }
    const target = recordPath(id);
    if (fs.existsSync(target)) {
      // Overwriting a live credential is a rotation, and rotation has its own path.
      throw new VaultError('VAULT_EXISTS', 'a record with this id already exists');
    }
    const record = {
      v: SCHEMA_VERSION,
      id,
      alg: state.backend === 'dpapi' ? 'dpapi-safeStorage' : `safeStorage-${state.backend}`,
      created_at: nowIso(),
      rotated_at: null,
      bytes: ciphertext.length,
      sha256: crypto.createHash('sha256').update(ciphertext).digest('hex'),
      purpose,
      provider: provider ?? null,
      alias: alias ?? null,
      data: ciphertext.toString('base64'),
    };
    const temporary = `${target}.tmp-${process.pid}`;
    fs.writeFileSync(temporary, encode(record), { mode: 0o600 });
    fs.renameSync(temporary, target);
    mark('put', true, `purpose=${purpose}`);
    return describe(record);
  }

  function describe(record) {
    return {
      id: record.id,
      alg: record.alg,
      created_at: record.created_at,
      rotated_at: record.rotated_at,
      bytes: record.bytes,
      fingerprint: record.sha256.slice(0, 32),
      purpose: record.purpose,
      provider: record.provider,
      alias: record.alias,
      version: record.v,
    };
  }

  async function read(id) {
    const file = recordPath(id);
    if (!fs.existsSync(file)) throw new VaultError('VAULT_NOT_FOUND', 'no such secret');
    return decode(fs.readFileSync(file), id);
  }

  /**
   * Decrypt exactly one record. Used by the broker only. Never returns the record envelope to
   * a caller that is not the broker, and never logs the plaintext.
   */
  async function get(id, purpose) {
    if (!(await refreshAvailability())) throw new VaultError('VAULT_UNAVAILABLE', 'OS encryption is not available');
    const { record, ciphertext } = await read(id);
    if (purpose && record.purpose !== purpose) {
      throw new VaultError('VAULT_PURPOSE_MISMATCH', `record is ${record.purpose}, request is ${purpose}`);
    }
    let decrypted;
    try {
      decrypted = await safeStorage.decryptStringAsync(ciphertext);
    } catch (error) {
      // Never overwrite the record: the ciphertext is the only evidence of the failure.
      mark('get', false, 'decrypt-failed');
      throw new VaultError('VAULT_DECRYPT_FAILED', 'the OS refused to decrypt this record', {
        id,
        reason: error && error.message ? String(error.message).slice(0, 200) : 'unknown',
      });
    }
    if (!decrypted || typeof decrypted.result !== 'string') {
      mark('get', false, 'empty-decrypt');
      throw new VaultError('VAULT_DECRYPT_FAILED', 'decryption returned no value', { id });
    }
    if (decrypted.shouldReEncrypt) {
      // Documented by Electron: the key rotated or a stronger one is available.
      try {
        await rotate(id, decrypted.result);
        state.reencrypted += 1;
        mark('get', true, 'reencrypted');
      } catch (error) {
        mark('get', true, `reencrypt-failed:${error.code ?? 'unknown'}`);
      }
    } else {
      mark('get', true);
    }
    return decrypted.result;
  }

  async function rotate(id, secret) {
    if (!isSafeId(id)) throw new VaultError('VAULT_INVALID_ID', 'secret id must be a ULID');
    if (typeof secret !== 'string' || secret.length === 0) {
      throw new VaultError('VAULT_INVALID_SECRET', 'secret is empty');
    }
    if (!(await refreshAvailability())) throw new VaultError('VAULT_UNAVAILABLE', 'OS encryption is not available');
    const file = recordPath(id);
    if (!fs.existsSync(file)) throw new VaultError('VAULT_NOT_FOUND', 'no such secret');
    const { record } = await read(id);
    const ciphertext = await safeStorage.encryptStringAsync(secret);
    const next = {
      ...record,
      rotated_at: nowIso(),
      bytes: ciphertext.length,
      sha256: crypto.createHash('sha256').update(ciphertext).digest('hex'),
      data: ciphertext.toString('base64'),
    };
    const temporary = `${file}.tmp-${process.pid}`;
    fs.writeFileSync(temporary, encode(next), { mode: 0o600 });
    fs.renameSync(temporary, file);
    mark('rotate', true);
    return describe(next);
  }

  function remove(id) {
    const file = recordPath(id);
    let removed = false;
    if (fs.existsSync(file)) {
      fs.unlinkSync(file);
      removed = true;
    }
    mark('delete', true, removed ? 'removed' : 'absent');
    return { removed };
  }

  /**
   * Integrity check without decrypting: header, envelope, digest, size. A record that fails is
   * moved to quarantine (kept for diagnosis) so a single corrupt file cannot break the vault.
   */
  function verify(id) {
    const file = recordPath(id);
    if (!fs.existsSync(file)) return { id, present: false, ok: false, code: 'VAULT_NOT_FOUND' };
    try {
      const { record } = decode(fs.readFileSync(file), id);
      return {
        id,
        present: true,
        ok: true,
        purpose: record.purpose,
        bytes: record.bytes,
        alg: record.alg,
        // Metadata only: the digest covers the ciphertext, so it identifies the record without
        // revealing anything a decrypt would. The backend stores it as the secret fingerprint.
        fingerprint: record.sha256.slice(0, 32),
        version: record.v,
        created_at: record.created_at,
        rotated_at: record.rotated_at,
      };
    } catch (error) {
      fs.mkdirSync(quarantineDir, { recursive: true });
      try {
        fs.renameSync(file, path.join(quarantineDir, `${id}.${Date.now()}.corrupt`));
      } catch {
        /* if the rename fails the file stays where it is; verify() still reports it */
      }
      mark('verify', false, error.code ?? 'corrupt');
      return { id, present: true, ok: false, code: error.code ?? 'VAULT_CORRUPT' };
    }
  }

  function list() {
    ensureDir();
    return fs
      .readdirSync(dir)
      .filter((name) => name.endsWith('.bin'))
      .map((name) => {
        const id = name.slice(0, -4);
        try {
          const { record } = decode(fs.readFileSync(path.join(dir, name)), id);
          return describe(record);
        } catch (error) {
          return { id, corrupt: true, code: error.code ?? 'VAULT_CORRUPT' };
        }
      });
  }

  async function health() {
    await refreshAvailability();
    let entries = [];
    let corrupt = 0;
    try {
      entries = list();
      corrupt = entries.filter((entry) => entry.corrupt).length;
    } catch (error) {
      logger(`vault listing failed: ${error.message}`);
    }
    return {
      available: state.available,
      backend: state.backend,
      schema_version: SCHEMA_VERSION,
      alg: state.backend === 'dpapi' ? 'dpapi-safeStorage' : `safeStorage-${state.backend}`,
      directory: dir,
      entries: entries.length,
      corrupt,
      reencrypted: state.reencrypted,
      operations: state.operations,
      failures: state.failures,
      last_operation: state.lastOperation,
      last_operation_at: state.lastOperationAt,
      // Stated plainly, because the UI shows it verbatim.
      protection: 'Windows DPAPI, current user. Protects against other Windows users and offline disk access; it does not isolate credentials from software running as this user.',
    };
  }

  return { put, get, rotate, remove, verify, list, health, refreshAvailability, recordPath, describe, VaultError };
}

module.exports = { createVault, VaultError, MAGIC, SCHEMA_VERSION, PURPOSES, MAX_SECRET_BYTES, isSafeId };
