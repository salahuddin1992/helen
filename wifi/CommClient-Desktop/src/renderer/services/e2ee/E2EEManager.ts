/**
 * E2EE Manager — High-level encryption manager.
 * Session establishment, message encryption/decryption, key bundle refresh.
 */
import { KeyManager, KeyBundle } from './KeyManager';
import { X3DHProtocol, X3DHResult } from './X3DHProtocol';
import { DoubleRatchet, EncryptedMessage } from './DoubleRatchet';
import { CryptoUtils } from './CryptoUtils';
import { api } from '../api.client';

export interface E2EESession {
  userId: string;
  rootKey: ArrayBuffer;
  doubleRatchet: DoubleRatchet;
  createdAt: number;
  lastActivity: number;
}

export interface PendingEncryption {
  userId: string;
  messageId: string;
  plaintext: string;
  retries: number;
}

export class E2EEManager {
  private keyManager: KeyManager;
  private sessions: Map<string, E2EESession> = new Map();
  private pendingEncryptions: Map<string, PendingEncryption> = new Map();
  // Audit fix M3: track the refresh interval so we can clear it on
  // destroy(). Without this, every login starts a NEW 24h interval
  // and the old ones leak alongside the previous user's identity
  // material.
  private _refreshIntervalId: ReturnType<typeof setInterval> | null = null;
  private encryptionEnabled = false;
  private keyBundleLastUpload = 0;
  private keyBundleRefreshInterval = 24 * 60 * 60 * 1000; // 24 hours

  constructor() {
    this.keyManager = new KeyManager();
  }

  /**
   * Initialize E2EE system
   */
  async initialize(): Promise<void> {
    await this.keyManager.initialize();
    this.encryptionEnabled = true;

    // Schedule periodic key bundle refresh. Tracked so destroy() can
    // tear it down (audit fix M3).
    if (this._refreshIntervalId) clearInterval(this._refreshIntervalId);
    this._refreshIntervalId = setInterval(
      () => this.refreshKeyBundle(),
      this.keyBundleRefreshInterval,
    );
  }

  /**
   * Tear down all in-memory state. Called on logout so the next user
   * doesn't inherit the previous user's identity keys / sessions /
   * pending queue. Audit fix M3.
   *
   * Note: this DOES NOT erase persisted keys (we want users to be
   * able to log back in without losing their sessions). For full
   * device-wipe, also call `keyManager.eraseFromStorage()`.
   */
  destroy(): void {
    if (this._refreshIntervalId) {
      clearInterval(this._refreshIntervalId);
      this._refreshIntervalId = null;
    }
    this.sessions.clear();
    this.pendingEncryptions.clear();
    this.encryptionEnabled = false;
  }

  /**
   * Enable/disable E2EE
   */
  setEncryptionEnabled(enabled: boolean): void {
    this.encryptionEnabled = enabled;
  }

  /**
   * Upload key bundle to server
   */
  async uploadKeyBundle(): Promise<boolean> {
    try {
      const bundle = await this.keyManager.generateKeyBundle();
      await (api as any).uploadKeyBundle?.(bundle);
      this.keyBundleLastUpload = Date.now();
      return true;
    } catch (error) {
      console.error('[E2EE] Key bundle upload failed:', error);
      return false;
    }
  }

  /**
   * Establish session with user (X3DH)
   */
  async establishSession(
    userId: string,
    theirKeyBundle: KeyBundle
  ): Promise<boolean> {
    try {
      if (this.sessions.has(userId)) {
        return true; // Session already exists
      }

      // Import their keys
      const ikB = await CryptoUtils.importPublicKeyJwk(theirKeyBundle.identity_key);
      const spkB = await CryptoUtils.importPublicKeyJwk(theirKeyBundle.signed_pre_key.public_key);

      let opkB: CryptoKey | null = null;
      if (theirKeyBundle.one_time_pre_keys.length > 0) {
        opkB = await CryptoUtils.importPublicKeyJwk(
          theirKeyBundle.one_time_pre_keys[0].public_key
        );
      }

      // Generate ephemeral key for X3DH
      const ekA = await CryptoUtils.generateEcdhKeyPair();
      const ikA = this.keyManager.getIdentityKeyPair();

      // Perform X3DH
      const x3dhResult = await X3DHProtocol.performKeyAgreement(
        ikA,
        ekA,
        ikB,
        spkB,
        opkB
      );

      // Create Double Ratchet session
      const doubleRatchet = new DoubleRatchet(x3dhResult.sharedSecret);

      this.sessions.set(userId, {
        userId,
        rootKey: x3dhResult.sharedSecret,
        doubleRatchet,
        createdAt: Date.now(),
        lastActivity: Date.now(),
      });

      return true;
    } catch (error) {
      console.error('[E2EE] Session establishment failed:', error);
      return false;
    }
  }

  /**
   * Encrypt a message for a user
   */
  async encryptMessage(userId: string, plaintext: string): Promise<EncryptedMessage | null> {
    if (!this.encryptionEnabled) return null;

    let session = this.sessions.get(userId);

    if (!session) {
      // Try to establish session
      try {
        const theirBundle = await (api as any).getKeyBundle?.(userId);
        const established = await this.establishSession(userId, theirBundle);
        if (!established) return null;
        session = this.sessions.get(userId)!;
      } catch {
        console.warn(`[E2EE] Could not establish session with ${userId}`);
        return null;
      }
    }

    try {
      const encrypted = await session.doubleRatchet.encrypt(plaintext);
      session.lastActivity = Date.now();
      return encrypted;
    } catch (error) {
      console.error('[E2EE] Encryption failed:', error);
      return null;
    }
  }

  /**
   * Decrypt a message from a user
   */
  async decryptMessage(
    userId: string,
    encrypted: EncryptedMessage
  ): Promise<string | null> {
    if (!this.encryptionEnabled) return null;

    let session = this.sessions.get(userId);

    if (!session) {
      // Cannot decrypt without session (should have been established)
      console.warn(`[E2EE] No session with ${userId}`);
      return null;
    }

    try {
      const plaintext = await session.doubleRatchet.decrypt(encrypted);
      session.lastActivity = Date.now();
      return plaintext;
    } catch (error) {
      console.error('[E2EE] Decryption failed:', error);
      return null;
    }
  }

  /**
   * Refresh key bundle if needed
   */
  async refreshKeyBundle(): Promise<void> {
    const now = Date.now();
    if (now - this.keyBundleLastUpload < this.keyBundleRefreshInterval) {
      return;
    }

    const success = await this.uploadKeyBundle();
    if (success) {
      console.log('[E2EE] Key bundle refreshed');
    }
  }

  /**
   * Rotate keys (monthly or on demand)
   */
  async rotateKeys(): Promise<boolean> {
    try {
      const newBundle = await this.keyManager.rotateIdentityKeys();
      await (api as any).uploadKeyBundle?.(newBundle);
      console.log('[E2EE] Keys rotated successfully');
      return true;
    } catch (error) {
      console.error('[E2EE] Key rotation failed:', error);
      return false;
    }
  }

  /**
   * Get session state (for debugging)
   */
  getSessionState(userId: string): E2EESession | null {
    return this.sessions.get(userId) || null;
  }

  /**
   * Check encryption status
   */
  isEncryptionEnabled(): boolean {
    return this.encryptionEnabled;
  }

  /**
   * Get OTK count
   */
  getOneTimePreKeyCount(): number {
    return this.keyManager.getOneTimePreKeyCount();
  }

  /**
   * Clear old sessions (older than 30 days)
   */
  cleanupOldSessions(): void {
    const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;

    for (const [userId, session] of this.sessions) {
      if (session.lastActivity < thirtyDaysAgo) {
        this.sessions.delete(userId);
      }
    }
  }

  /**
   * Queue pending encryption (for offline scenarios)
   */
  queuePendingEncryption(userId: string, messageId: string, plaintext: string): void {
    this.pendingEncryptions.set(messageId, {
      userId,
      messageId,
      plaintext,
      retries: 0,
    });
  }

  /**
   * Retry pending encryptions
   */
  async processPendingEncryptions(): Promise<void> {
    const maxRetries = 3;

    for (const [msgId, pending] of this.pendingEncryptions) {
      if (pending.retries >= maxRetries) {
        this.pendingEncryptions.delete(msgId);
        continue;
      }

      const encrypted = await this.encryptMessage(pending.userId, pending.plaintext);
      if (encrypted) {
        this.pendingEncryptions.delete(msgId);
      } else {
        pending.retries++;
      }
    }
  }
}
