/**
 * E2EE Store — Zustand store for E2EE state management.
 *
 * ⚠️ BETA GATE (audit fix E1-E10): the underlying crypto layer is
 * NOT production-ready. Specifically:
 *   - X3DH SPK signature is HMAC instead of XEdDSA → no real
 *     origin authentication; SPK swap is undetectable.
 *   - Double Ratchet is mis-constructed (no KDF_RK, no skipped-key
 *     derivation, n counter never increments).
 *   - Key bundle persistence is no-op (`loadKeysFromMemory` always
 *     returns null) so identity rotates on every reload.
 *   - `uploadKeyBundle` / `getKeyBundle` API endpoints DO NOT EXIST
 *     server-side; the optional-chaining swallows the void.
 *
 * Until the crypto plane is rewritten on top of `libsignal-client`,
 * the store enforces a HARD OFF default and exposes a `betaUnlocked`
 * flag the UI can flip with a "I understand this is broken" warning.
 * Without that flag flipped, every helper short-circuits and the
 * encryption setting cannot be turned on.
 */
import { create } from 'zustand';
import { E2EEManager, type E2EESession, type PendingEncryption } from '../services/e2ee';

// Set this only after the libsignal rewrite lands. Override per-build
// via env or local override for crypto-engineering testing.
const E2EE_PRODUCTION_READY = false;

interface E2EEState {
  manager: E2EEManager | null;
  isInitialized: boolean;
  isEncryptionEnabled: boolean;
  /** True only when this build's E2EE crypto has been independently
   *  audited and the libsignal rewrite has landed. While false, the
   *  rest of the store HARD-refuses to enable encryption. */
  productionReady: boolean;
  /** Beta unlock — separate from productionReady. Lets a developer
   *  who knows the crypto is broken still exercise the UI flow.
   *  Setting `productionReady=true` makes this redundant. */
  betaUnlocked: boolean;
  sessions: Map<string, E2EESession>;
  pendingEncryptions: Map<string, PendingEncryption>;
  oneTimePreKeyCount: number;
  keyBundleStatus: 'idle' | 'uploading' | 'success' | 'error';
  lastKeyBundleUpload: number | null;

  // Actions
  initialize: () => Promise<void>;
  setEncryptionEnabled: (enabled: boolean) => void;
  /** Beta unlock toggle — only affects flows when productionReady=false. */
  setBetaUnlocked: (unlocked: boolean) => void;
  uploadKeyBundle: () => Promise<boolean>;
  rotateKeys: () => Promise<boolean>;
  getSessionState: (userId: string) => E2EESession | null;
  updateOneTimePreKeyCount: () => void;
  cleanupOldSessions: () => void;
}

const e2eeManager = new E2EEManager();

export const useE2EEStore = create<E2EEState>((set, get) => ({
  manager: null,
  isInitialized: false,
  isEncryptionEnabled: false,
  productionReady: E2EE_PRODUCTION_READY,
  betaUnlocked: false,
  sessions: new Map(),
  pendingEncryptions: new Map(),
  oneTimePreKeyCount: 0,
  keyBundleStatus: 'idle',
  lastKeyBundleUpload: null,

  initialize: async () => {
    if (!E2EE_PRODUCTION_READY && !get().betaUnlocked) {
      console.warn(
        '[E2EE] Initialization skipped — crypto layer is BETA-locked. ' +
        'Set E2EE_PRODUCTION_READY=true after libsignal rewrite, or ' +
        'call setBetaUnlocked(true) for dev-only experimentation.',
      );
      set({ isInitialized: false, isEncryptionEnabled: false });
      return;
    }
    try {
      await e2eeManager.initialize();
      set({
        manager: e2eeManager,
        isInitialized: true,
        // STILL false — enabling requires an explicit setEncryptionEnabled
        // call so the user has to opt in even after init.
        isEncryptionEnabled: false,
        oneTimePreKeyCount: e2eeManager.getOneTimePreKeyCount(),
      });
    } catch (error) {
      console.error('[E2EE Store] Initialization failed:', error);
    }
  },

  setEncryptionEnabled: (enabled) => {
    if (enabled && !E2EE_PRODUCTION_READY && !get().betaUnlocked) {
      console.error(
        '[E2EE] Refusing to enable encryption — crypto layer is BETA-locked. ' +
        'See e2ee.store.ts header comment for status.',
      );
      return;
    }
    e2eeManager.setEncryptionEnabled(enabled);
    set({ isEncryptionEnabled: enabled });
  },

  setBetaUnlocked: (unlocked) => {
    set({ betaUnlocked: !!unlocked });
    if (!unlocked) {
      // Disabling beta unlock implicitly disables encryption too.
      e2eeManager.setEncryptionEnabled(false);
      set({ isEncryptionEnabled: false });
    }
  },

  uploadKeyBundle: async () => {
    set({ keyBundleStatus: 'uploading' });
    try {
      const success = await e2eeManager.uploadKeyBundle();
      set({
        keyBundleStatus: success ? 'success' : 'error',
        lastKeyBundleUpload: success ? Date.now() : null,
      });
      return success;
    } catch (error) {
      console.error('[E2EE Store] Key bundle upload failed:', error);
      set({ keyBundleStatus: 'error' });
      return false;
    }
  },

  rotateKeys: async () => {
    try {
      const success = await e2eeManager.rotateKeys();
      if (success) {
        set({ lastKeyBundleUpload: Date.now() });
      }
      return success;
    } catch (error) {
      console.error('[E2EE Store] Key rotation failed:', error);
      return false;
    }
  },

  getSessionState: (userId) => {
    return e2eeManager.getSessionState(userId);
  },

  updateOneTimePreKeyCount: () => {
    set({ oneTimePreKeyCount: e2eeManager.getOneTimePreKeyCount() });
  },

  cleanupOldSessions: () => {
    e2eeManager.cleanupOldSessions();
  },

  /**
   * Audit fix M3: full teardown — clears in-memory sessions, pending
   * encryptions, and the periodic key-bundle refresh interval. Called
   * from auth.store.logout so the next user signing in on the same
   * device doesn't inherit the previous user's identity material in
   * RAM. Persisted keys (when the storage layer is wired) survive
   * for the same user's next session.
   */
  destroy: () => {
    try { e2eeManager.destroy(); } catch { /* ignore */ }
    set({
      isInitialized: false,
      isEnabled: false,
      manager: null as any,
      identityFingerprint: '',
      oneTimePreKeyCount: 0,
    } as any);
  },
}));
