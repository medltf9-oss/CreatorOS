/**
 * Report download integrity (P8.23).
 *
 * A report is only offered for saving after the bytes that arrived hash to the digest the *metadata*
 * recorded when the file was written. Two rules follow from that and are enforced here rather than in
 * the page:
 *
 * * the hash is computed over the received bytes — never over a re-serialised object, and never taken
 *   from a field inside the downloaded document (a payload that carries its own hash proves nothing);
 * * an empty body is a failure even if some digest happens to match, because a 0-byte report is not
 *   the report.
 */

export type VerificationFailure = 'no_expected_hash' | 'no_bytes' | 'digest_mismatch' | 'not_available';

export type IntegrityReport = {
  ok: boolean;
  expected: string | null;
  actual: string | null;
  bytes: number;
  failure: VerificationFailure | null;
  message: string;
};

/** Hex SHA-256 of the given bytes, using the platform's WebCrypto implementation. */
export async function sha256Hex(bytes: Uint8Array | ArrayBuffer): Promise<string> {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  // Copy into a fresh buffer: WebCrypto refuses a view over a larger SharedArrayBuffer/offset slice.
  const buffer = new Uint8Array(view.byteLength);
  buffer.set(view);
  const digest = await globalThis.crypto.subtle.digest('SHA-256', buffer);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * Compare the received bytes with the digest recorded in the report metadata.
 *
 * `expected` must come from `GET /analytics/reports/{id}` (the trusted record), never from the file.
 */
export async function verifyReportBytes(expected: string | null, bytes: Uint8Array): Promise<IntegrityReport> {
  if (!expected) {
    return {
      ok: false,
      expected: null,
      actual: null,
      bytes: bytes.byteLength,
      failure: 'no_expected_hash',
      message:
        'The report record carries no content hash, so this download cannot be verified. It is not offered for saving.',
    };
  }
  if (bytes.byteLength === 0) {
    return {
      ok: false,
      expected,
      actual: null,
      bytes: 0,
      failure: 'no_bytes',
      message: 'The server returned an empty body; an empty file is never accepted as a report.',
    };
  }
  const actual = await sha256Hex(bytes);
  if (actual !== expected.toLowerCase()) {
    return {
      ok: false,
      expected,
      actual,
      bytes: bytes.byteLength,
      failure: 'digest_mismatch',
      message:
        'The received bytes do not hash to the digest recorded for this report. The file was not saved.',
    };
  }
  return {
    ok: true,
    expected,
    actual,
    bytes: bytes.byteLength,
    failure: null,
    message: 'Verified: the downloaded bytes hash to the digest recorded for this report.',
  };
}

/** First 12 hex characters, for a table cell; the full digest stays available in the details. */
export function shortDigest(hex: string | null): string {
  return hex ? `${hex.slice(0, 12)}…` : '—';
}
