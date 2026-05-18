/**
 * Cryptographic utilities using Web Crypto API.
 * HKDF, HMAC-SHA256, AES-256-GCM, key serialization.
 */

export class CryptoUtils {
  /**
   * HKDF-SHA256: RFC 5869
   * Extract and expand a pseudo-random key.
   */
  static async hkdf(
    ikm: ArrayBuffer,
    salt: ArrayBuffer | null,
    info: ArrayBuffer,
    length: number
  ): Promise<ArrayBuffer> {
    // Extract
    const extractedKey = await crypto.subtle.importKey(
      'raw',
      salt || new ArrayBuffer(32),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign']
    );

    const prk = await crypto.subtle.sign('HMAC', extractedKey, ikm);

    // Expand
    const prkKey = await crypto.subtle.importKey(
      'raw',
      prk,
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign']
    );

    const hashLen = 32; // SHA-256 output length
    const n = Math.ceil(length / hashLen);
    let t = new ArrayBuffer(0);
    let okm = new ArrayBuffer(0);

    for (let i = 0; i < n; i++) {
      const input = this.concatBuffers(
        t,
        info,
        new Uint8Array([i + 1])
      );
      t = await crypto.subtle.sign('HMAC', prkKey, input);
      okm = this.concatBuffers(okm, t);
    }

    return okm.slice(0, length);
  }

  /**
   * HMAC-SHA256
   */
  static async hmacSha256(key: ArrayBuffer, message: ArrayBuffer): Promise<ArrayBuffer> {
    const importedKey = await crypto.subtle.importKey(
      'raw',
      key,
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign']
    );
    return crypto.subtle.sign('HMAC', importedKey, message);
  }

  /**
   * AES-256-GCM encryption
   */
  static async encryptAesGcm(
    key: ArrayBuffer,
    plaintext: ArrayBuffer,
    iv: ArrayBuffer
  ): Promise<ArrayBuffer> {
    const importedKey = await crypto.subtle.importKey(
      'raw',
      key,
      { name: 'AES-GCM' },
      false,
      ['encrypt']
    );

    return crypto.subtle.encrypt(
      { name: 'AES-GCM', iv },
      importedKey,
      plaintext
    );
  }

  /**
   * AES-256-GCM decryption
   */
  static async decryptAesGcm(
    key: ArrayBuffer,
    ciphertext: ArrayBuffer,
    iv: ArrayBuffer
  ): Promise<ArrayBuffer> {
    const importedKey = await crypto.subtle.importKey(
      'raw',
      key,
      { name: 'AES-GCM' },
      false,
      ['decrypt']
    );

    return crypto.subtle.decrypt(
      { name: 'AES-GCM', iv },
      importedKey,
      ciphertext
    );
  }

  /**
   * Generate random bytes
   */
  static randomBytes(length: number): Uint8Array {
    return crypto.getRandomValues(new Uint8Array(length));
  }

  /**
   * Concatenate multiple buffers
   */
  static concatBuffers(...buffers: (ArrayBuffer | Uint8Array)[]): ArrayBuffer {
    let totalLength = 0;
    for (const buf of buffers) {
      totalLength += buf.byteLength;
    }

    const result = new Uint8Array(totalLength);
    let offset = 0;
    for (const buf of buffers) {
      result.set(new Uint8Array(buf), offset);
      offset += buf.byteLength;
    }

    return result.buffer;
  }

  /**
   * Convert ArrayBuffer to Base64
   */
  static bufferToBase64(buffer: ArrayBuffer): string {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.byteLength; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }

  /**
   * Convert Base64 to ArrayBuffer
   */
  static base64ToBuffer(base64: string): ArrayBuffer {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes.buffer;
  }

  /**
   * SHA-256 hash
   */
  static async sha256(data: ArrayBuffer): Promise<ArrayBuffer> {
    return crypto.subtle.digest('SHA-256', data);
  }

  /**
   * Generate ECDH key pair (P-256).
   *
   * `extractable` controls whether the PRIVATE half can ever leave
   * the keystore (via crypto.subtle.exportKey). Identity / signed-
   * pre-keys must NEVER be exportable so a compromised renderer
   * can't dump them. Ephemeral / one-time pre-keys are exportable
   * because the protocol requires serializing them for envelope
   * delivery.
   *
   * Audit fix: previous version was `extractable=true` for every
   * key including identity. Now caller opts-in.
   */
  static async generateEcdhKeyPair(extractable = false): Promise<CryptoKeyPair> {
    return crypto.subtle.generateKey(
      {
        name: 'ECDH',
        namedCurve: 'P-256',
      },
      extractable,
      ['deriveBits', 'deriveKey']
    );
  }

  /**
   * Derive shared secret via ECDH
   */
  static async deriveSharedSecret(
    privateKey: CryptoKey,
    publicKey: CryptoKey
  ): Promise<ArrayBuffer> {
    return crypto.subtle.deriveBits(
      { name: 'ECDH', public: publicKey },
      privateKey,
      256
    );
  }

  /**
   * Export public key to JWK
   */
  static async exportPublicKeyJwk(publicKey: CryptoKey): Promise<JsonWebKey> {
    return crypto.subtle.exportKey('jwk', publicKey);
  }

  /**
   * Import public key from JWK
   */
  static async importPublicKeyJwk(jwk: JsonWebKey): Promise<CryptoKey> {
    return crypto.subtle.importKey(
      'jwk',
      jwk,
      { name: 'ECDH', namedCurve: 'P-256' },
      true,
      []
    );
  }

  /**
   * Export private key (for storage)
   */
  static async exportPrivateKeyPkcs8(privateKey: CryptoKey): Promise<ArrayBuffer> {
    return crypto.subtle.exportKey('pkcs8', privateKey);
  }

  /**
   * Import private key from PKCS8
   */
  static async importPrivateKeyPkcs8(buffer: ArrayBuffer): Promise<CryptoKey> {
    return crypto.subtle.importKey(
      'pkcs8',
      buffer,
      { name: 'ECDH', namedCurve: 'P-256' },
      true,
      ['deriveBits', 'deriveKey']
    );
  }
}
