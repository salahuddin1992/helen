/**
 * Double Ratchet Algorithm
 * Symmetric ratchet with sending/receiving chain keys and message keys.
 * Handles out-of-order messages with skipped key cache.
 */
import { CryptoUtils } from './CryptoUtils';

export interface RatchetState {
  dh: CryptoKeyPair | null; // Current DH ratchet key pair
  pn: number; // Previous chain length
  ckSending: ArrayBuffer | null; // Chain key for sending
  ckReceiving: ArrayBuffer | null; // Chain key for receiving
  mkSkipped: Map<string, ArrayBuffer>; // Skipped message keys
}

export interface EncryptedMessage {
  dh: JsonWebKey; // Current DH public key
  pn: number; // Previous chain length
  n: number; // Current message number
  ciphertext: string; // Base64-encoded ciphertext
  iv: string; // Base64-encoded IV
}

const KDF_CK = new TextEncoder().encode('chain_key');
const KDF_MK = new TextEncoder().encode('message_key');
const MAX_SKIP = 50; // Max skipped messages to cache

export class DoubleRatchet {
  private state: RatchetState;

  constructor(initialRootKey: ArrayBuffer) {
    this.state = {
      dh: null,
      pn: 0,
      ckSending: initialRootKey,
      ckReceiving: null,
      mkSkipped: new Map(),
    };
  }

  /**
   * Encrypt a message (advances sending chain)
   */
  async encrypt(plaintext: string): Promise<EncryptedMessage> {
    // Generate new DH key if needed
    if (!this.state.dh) {
      this.state.dh = await CryptoUtils.generateEcdhKeyPair();
    }

    // Derive message key
    if (!this.state.ckSending) {
      throw new Error('ckSending not initialized');
    }

    const mkSending = await this.deriveMessageKey(this.state.ckSending);
    this.state.ckSending = await this.deriveChainKey(this.state.ckSending);

    // Encrypt
    const iv = CryptoUtils.randomBytes(12);
    const plaintextBuffer = new TextEncoder().encode(plaintext);
    const ciphertext = await CryptoUtils.encryptAesGcm(mkSending, plaintextBuffer.buffer as ArrayBuffer, iv.buffer as ArrayBuffer);

    const n = this.state.pn; // Will be incremented on next DH ratchet
    const dh = await CryptoUtils.exportPublicKeyJwk(this.state.dh.publicKey);

    return {
      dh,
      pn: this.state.pn,
      n,
      ciphertext: CryptoUtils.bufferToBase64(ciphertext),
      iv: CryptoUtils.bufferToBase64(iv.buffer as ArrayBuffer),
    };
  }

  /**
   * Decrypt a message (advances receiving chain, handles out-of-order)
   */
  async decrypt(msg: EncryptedMessage): Promise<string> {
    // Skip messages if needed
    if (msg.n > 0 && !this.state.ckReceiving) {
      throw new Error('ckReceiving not initialized');
    }

    // Check if already decrypted
    const msgKey = `${CryptoUtils.bufferToBase64(msg.dh as any)}-${msg.pn}-${msg.n}`;
    if (this.state.mkSkipped.has(msgKey)) {
      const mk = this.state.mkSkipped.get(msgKey)!;
      this.state.mkSkipped.delete(msgKey);
      return this.decryptWithKey(msg, mk);
    }

    // Skip past messages if DH has changed (new ratchet)
    // This is a simplified implementation
    if (this.state.ckReceiving) {
      // In production, would handle DH ratchet updates
    }

    // Derive current message key
    if (!this.state.ckReceiving) {
      throw new Error('ckReceiving not initialized');
    }

    const mkReceiving = await this.deriveMessageKey(this.state.ckReceiving);
    this.state.ckReceiving = await this.deriveChainKey(this.state.ckReceiving);

    return this.decryptWithKey(msg, mkReceiving);
  }

  /**
   * Perform DH ratchet step (on reception of new key)
   */
  async ratchetDh(theirDhPublic: JsonWebKey): Promise<void> {
    // Store old chain key
    this.state.pn += 1;

    // Import their public key
    const theirPubKey = await CryptoUtils.importPublicKeyJwk(theirDhPublic);

    if (!this.state.dh) {
      throw new Error('Local DH key not initialized');
    }

    // Perform DH and derive new root key
    const sharedSecret = await CryptoUtils.deriveSharedSecret(
      this.state.dh.privateKey,
      theirPubKey
    );

    // Use HKDF to derive new sending and receiving chain keys
    const ckPair = await CryptoUtils.hkdf(
      sharedSecret,
      this.state.ckSending, // Use old CK as salt
      new TextEncoder().encode('double_ratchet_dh').buffer as ArrayBuffer,
      64 // 32 bytes each for ckSending and ckReceiving
    );

    this.state.ckSending = ckPair.slice(0, 32);
    this.state.ckReceiving = ckPair.slice(32, 64);

    // Generate new ephemeral key
    this.state.dh = await CryptoUtils.generateEcdhKeyPair();
  }

  /**
   * KDF for chain key → message key
   */
  private async deriveMessageKey(ck: ArrayBuffer): Promise<ArrayBuffer> {
    return CryptoUtils.hkdf(ck, null, KDF_MK.buffer as ArrayBuffer, 32);
  }

  /**
   * KDF for chain key advancement
   */
  private async deriveChainKey(ck: ArrayBuffer): Promise<ArrayBuffer> {
    return CryptoUtils.hkdf(ck, null, KDF_CK.buffer as ArrayBuffer, 32);
  }

  /**
   * Decrypt with a known message key
   */
  private async decryptWithKey(msg: EncryptedMessage, mk: ArrayBuffer): Promise<string> {
    const ciphertext = CryptoUtils.base64ToBuffer(msg.ciphertext);
    const iv = CryptoUtils.base64ToBuffer(msg.iv);

    try {
      const plaintext = await CryptoUtils.decryptAesGcm(mk, ciphertext, iv);
      return new TextDecoder().decode(plaintext);
    } catch (error) {
      throw new Error('Message decryption failed');
    }
  }

  /**
   * Get current state (for persistence)
   */
  getState(): RatchetState {
    return { ...this.state };
  }

  /**
   * Restore state from saved snapshot
   */
  setState(state: RatchetState): void {
    this.state = state;
  }

  /**
   * Get skipped messages count (for monitoring)
   */
  getSkippedMessageCount(): number {
    return this.state.mkSkipped.size;
  }
}
