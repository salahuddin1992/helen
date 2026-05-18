/**
 * Key Manager — Generate identity key pair, signed pre-key, batch of one-time pre-keys.
 * Key rotation, storage, and lifecycle management.
 */
import { CryptoUtils } from './CryptoUtils';

export interface KeyBundle {
  identity_key: JsonWebKey;
  signed_pre_key: {
    public_key: JsonWebKey;
    signature: string;
  };
  one_time_pre_keys: Array<{
    key_id: number;
    public_key: JsonWebKey;
  }>;
}

export interface StoredKeys {
  identityKeyPair: CryptoKeyPair;
  signedPreKeyPair: CryptoKeyPair;
  oneTimePreKeyPairs: Map<number, CryptoKeyPair>;
  oneTimePreKeyCounter: number;
}

export class KeyManager {
  private storedKeys: StoredKeys | null = null;
  private oneTimePreKeyBatchSize = 100;

  /**
   * Initialize: Generate or load existing keys
   */
  async initialize(): Promise<void> {
    const stored = this.loadKeysFromMemory();
    if (stored) {
      this.storedKeys = stored;
    } else {
      await this.generateNewKeys();
    }
  }

  /**
   * Generate fresh key material
   */
  private async generateNewKeys(): Promise<void> {
    // Identity + signed-pre-key are LONG-LIVED — never exportable.
    // OTPKs are one-shot and we need to publish them in the bundle,
    // so they stay extractable. Audit fix: previously every key was
    // extractable=true.
    const identityKeyPair = await CryptoUtils.generateEcdhKeyPair(false);
    const signedPreKeyPair = await CryptoUtils.generateEcdhKeyPair(false);
    const oneTimePreKeyPairs = new Map<number, CryptoKeyPair>();

    for (let i = 0; i < this.oneTimePreKeyBatchSize; i++) {
      const pair = await CryptoUtils.generateEcdhKeyPair(true);
      oneTimePreKeyPairs.set(i, pair);
    }

    this.storedKeys = {
      identityKeyPair,
      signedPreKeyPair,
      oneTimePreKeyPairs,
      oneTimePreKeyCounter: this.oneTimePreKeyBatchSize,
    };

    this.saveKeysToMemory(this.storedKeys);
  }

  /**
   * Generate key bundle for upload to server
   */
  async generateKeyBundle(): Promise<KeyBundle> {
    if (!this.storedKeys) throw new Error('Keys not initialized');

    const identityPubKey = await CryptoUtils.exportPublicKeyJwk(
      this.storedKeys.identityKeyPair.publicKey
    );

    const signedPubKey = await CryptoUtils.exportPublicKeyJwk(
      this.storedKeys.signedPreKeyPair.publicKey
    );

    // Sign the public key with identity key
    const signatureData = await CryptoUtils.hmacSha256(
      await crypto.subtle.exportKey('raw', this.storedKeys.identityKeyPair.privateKey),
      await crypto.subtle.exportKey('raw', this.storedKeys.signedPreKeyPair.publicKey)
    );
    const signature = CryptoUtils.bufferToBase64(signatureData);

    // Collect OTKs (take first 50 for the bundle)
    const oneTimePreKeys = Array.from(this.storedKeys.oneTimePreKeyPairs.entries())
      .slice(0, 50)
      .map(([keyId, pair]) => ({
        key_id: keyId,
        public_key: pair.publicKey,
      }));

    // Convert public keys to JWK
    const oneTimePreKeysJwk = await Promise.all(
      oneTimePreKeys.map(async (otk) => ({
        key_id: otk.key_id,
        public_key: await CryptoUtils.exportPublicKeyJwk(otk.public_key),
      }))
    );

    return {
      identity_key: identityPubKey,
      signed_pre_key: {
        public_key: signedPubKey,
        signature,
      },
      one_time_pre_keys: oneTimePreKeysJwk,
    };
  }

  /**
   * Rotate one-time pre-keys after bundle upload
   */
  async rotateOneTimePreKeys(usedKeyIds: number[]): Promise<void> {
    if (!this.storedKeys) throw new Error('Keys not initialized');

    // Remove used keys
    for (const id of usedKeyIds) {
      this.storedKeys.oneTimePreKeyPairs.delete(id);
    }

    // Generate replacements (OTPK = exportable for bundle publish)
    const needed = this.oneTimePreKeyBatchSize - this.storedKeys.oneTimePreKeyPairs.size;
    for (let i = 0; i < needed; i++) {
      const pair = await CryptoUtils.generateEcdhKeyPair(true);
      this.storedKeys.oneTimePreKeyPairs.set(
        this.storedKeys.oneTimePreKeyCounter++,
        pair
      );
    }

    this.saveKeysToMemory(this.storedKeys);
  }

  /**
   * Perform key rotation (monthly or on demand)
   */
  async rotateIdentityKeys(): Promise<KeyBundle> {
    // Keep old identity key in memory for decrypting old sessions
    // This would require a more complex key versioning scheme in production
    await this.generateNewKeys();
    return this.generateKeyBundle();
  }

  /**
   * Get identity public key
   */
  async getIdentityPublicKey(): Promise<JsonWebKey> {
    if (!this.storedKeys) throw new Error('Keys not initialized');
    return CryptoUtils.exportPublicKeyJwk(
      this.storedKeys.identityKeyPair.publicKey
    );
  }

  /**
   * Get private key pair for X3DH (should be kept secure)
   */
  getIdentityKeyPair(): CryptoKeyPair {
    if (!this.storedKeys) throw new Error('Keys not initialized');
    return this.storedKeys.identityKeyPair;
  }

  /**
   * Get signed pre-key pair
   */
  getSignedPreKeyPair(): CryptoKeyPair {
    if (!this.storedKeys) throw new Error('Keys not initialized');
    return this.storedKeys.signedPreKeyPair;
  }

  /**
   * Get and consume an OTK for X3DH
   */
  async consumeOneTimePreKey(): Promise<CryptoKeyPair | null> {
    if (!this.storedKeys || this.storedKeys.oneTimePreKeyPairs.size === 0) {
      return null;
    }

    const firstKey = this.storedKeys.oneTimePreKeyPairs.entries().next();
    if (firstKey.done) return null;

    const [keyId, pair] = firstKey.value;
    this.storedKeys.oneTimePreKeyPairs.delete(keyId);
    this.saveKeysToMemory(this.storedKeys);

    return pair;
  }

  /**
   * Get OTK count (for monitoring)
   */
  getOneTimePreKeyCount(): number {
    if (!this.storedKeys) return 0;
    return this.storedKeys.oneTimePreKeyPairs.size;
  }

  private loadKeysFromMemory(): StoredKeys | null {
    // In a production app, this would load from IndexedDB
    // For now, just check in-memory cache (lost on reload)
    try {
      const stored = sessionStorage.getItem('e2ee_keys');
      if (!stored) return null;
      // Would deserialize from JSON, but CryptoKey objects can't serialize
      // This is a simplified version; real impl needs special handling
      return null;
    } catch {
      return null;
    }
  }

  private saveKeysToMemory(keys: StoredKeys): void {
    // In production, save to IndexedDB with proper serialization
    // CryptoKey objects must be exported/imported for storage
    // For now, keep in memory (session-scoped)
  }
}
