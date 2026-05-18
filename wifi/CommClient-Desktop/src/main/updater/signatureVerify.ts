/**
 * Ed25519 signature verification for update manifests + installer hashes.
 *
 * Manifest shape (simplified):
 *   { version: "1.2.3", sha512: "...", signature: "<base64 ed25519 sig>", ... }
 *
 * The signed payload is the UTF-8 bytes of the SHA-512 field. This keeps
 * the producer side simple: the CI pipeline computes sha512 of the NSIS
 * installer, signs just that digest, and attaches it. We don't re-hash
 * the installer here; electron-updater does that itself and rejects
 * mismatches. Our job is to assert the publisher approved this SHA.
 *
 * Node crypto supports Ed25519 natively since Node 12; no external dep.
 */

import { createPublicKey, verify as cryptoVerify } from 'crypto';

export interface SignatureCheckResult {
  ok: boolean;
  reason?: string;
}

function base64ToBuffer(b64: string): Buffer {
  return Buffer.from(b64, 'base64');
}

/**
 * Public key accepted in two forms:
 *   * Raw 32-byte Ed25519 public key, Base64-encoded
 *   * PEM "BEGIN PUBLIC KEY" block
 */
function loadKey(publicKeyBase64OrPem: string) {
  const trimmed = publicKeyBase64OrPem.trim();
  if (trimmed.includes('-----BEGIN')) {
    return createPublicKey({ key: trimmed, format: 'pem' });
  }
  // Raw 32-byte Ed25519 key → wrap in DER SPKI.
  const raw = base64ToBuffer(trimmed);
  if (raw.length !== 32) {
    throw new Error(`Ed25519 public key must be 32 bytes raw, got ${raw.length}`);
  }
  // DER prefix for SubjectPublicKeyInfo of Ed25519.
  const derPrefix = Buffer.from('302a300506032b6570032100', 'hex');
  const spki = Buffer.concat([derPrefix, raw]);
  return createPublicKey({
    key: spki,
    format: 'der',
    type: 'spki',
  });
}

export function verifyEd25519(
  payloadUtf8: string,
  signatureBase64: string,
  publicKeyMaterial: string
): SignatureCheckResult {
  try {
    const key = loadKey(publicKeyMaterial);
    const sig = base64ToBuffer(signatureBase64);
    const ok = cryptoVerify(null, Buffer.from(payloadUtf8, 'utf-8'), key, sig);
    return ok ? { ok: true } : { ok: false, reason: 'signature mismatch' };
  } catch (err) {
    return { ok: false, reason: (err as Error).message };
  }
}
