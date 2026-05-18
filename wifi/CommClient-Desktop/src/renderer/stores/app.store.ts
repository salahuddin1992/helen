/**
 * App Lifecycle Store — Central state machine for one-click startup flow.
 *
 * Manages the entire app lifecycle from cold start to fully operational,
 * including backend health, discovery, session restore, and error recovery.
 *
 * Startup Phases:
 *   splash        → Animated branding (1.2s minimum for visual polish)
 *   backend_check → Verifying backend server is running and healthy
 *   discovery     → Auto-discovering server on LAN (if no saved URL)
 *   session_restore → Attempting to restore previous session with saved credentials
 *   onboarding    → First-run experience (only if no previous session exists)
 *   login         → Manual login (session restore failed, user has account)
 *   ready         → App is fully operational, show main content
 *   error         → Unrecoverable error with retry option
 *
 * Returning User (zero-click):
 *   splash → backend_check → discovery → session_restore → ready
 *
 * First-Time User (one-click):
 *   splash → backend_check → discovery → onboarding → ready
 *
 * Error Recovery:
 *   error → (retry) → backend_check → ...
 */

import { create } from 'zustand';

// ── Phase Definitions ────────────────────────────────────

export type AppPhase =
  | 'splash'
  | 'backend_check'
  | 'discovery'
  | 'session_restore'
  | 'onboarding'
  | 'login'
  | 'ready'
  | 'error';

export type StartupError =
  | 'backend_unreachable'
  | 'no_server_found'
  | 'session_expired'
  | 'network_offline'
  | 'unknown';

// ── Phase Metadata (for UI) ──────────────────────────────

export interface PhaseInfo {
  phase: AppPhase;
  label: string;           // i18n key
  progress: number;        // 0-100 for progress bar
  canRetry: boolean;
}

export const PHASE_INFO: Record<AppPhase, PhaseInfo> = {
  splash:          { phase: 'splash',          label: 'startup.loading',          progress: 0,   canRetry: false },
  backend_check:   { phase: 'backend_check',   label: 'startup.checking_server',  progress: 20,  canRetry: true  },
  discovery:       { phase: 'discovery',        label: 'startup.finding_server',   progress: 40,  canRetry: true  },
  session_restore: { phase: 'session_restore',  label: 'startup.restoring',        progress: 70,  canRetry: false },
  onboarding:      { phase: 'onboarding',       label: 'startup.welcome',          progress: 100, canRetry: false },
  login:           { phase: 'login',            label: 'startup.sign_in',          progress: 100, canRetry: false },
  ready:           { phase: 'ready',            label: 'startup.ready',            progress: 100, canRetry: false },
  error:           { phase: 'error',            label: 'startup.error',            progress: 0,   canRetry: true  },
};

// ── Store Interface ──────────────────────────────────────

interface AppState {
  // Lifecycle state
  phase: AppPhase;
  phaseInfo: PhaseInfo;
  error: StartupError | null;
  errorMessage: string;

  // Startup context
  isFirstRun: boolean;
  splashMinElapsed: boolean;
  backendHealthy: boolean;
  serverUrl: string | null;
  hasSession: boolean;

  // Retry tracking
  retryCount: number;
  maxRetries: number;

  // Phase transition actions
  setPhase: (phase: AppPhase) => void;
  setError: (type: StartupError, message?: string) => void;
  clearError: () => void;
  setSplashElapsed: () => void;
  setBackendHealthy: (healthy: boolean) => void;
  setServerUrl: (url: string) => void;
  setIsFirstRun: (first: boolean) => void;
  setHasSession: (has: boolean) => void;
  incrementRetry: () => number;
  resetRetries: () => void;

  // High-level orchestration
  transitionToNextPhase: () => void;
}

// ── First-Run Detection ──────────────────────────────────

function detectFirstRun(): boolean {
  try {
    // Check multiple signals for first-run detection:
    // 1. No saved server URL → never connected
    const savedUrl = localStorage.getItem('commclient_server_url');
    // 2. No saved auth data → never logged in
    const savedAuth = localStorage.getItem('commclient_auth');
    // 3. No settings → never customized
    const savedSettings = localStorage.getItem('commclient_settings');
    // 4. Explicit first-run flag (set after onboarding completes)
    const completedOnboarding = localStorage.getItem('commclient_onboarding_complete');

    if (completedOnboarding === 'true') return false;
    if (savedUrl || savedAuth || savedSettings) return false;
    return true;
  } catch {
    return true;
  }
}

// ── Store Implementation ─────────────────────────────────

export const useAppStore = create<AppState>((set, get) => ({
  phase: 'splash',
  phaseInfo: PHASE_INFO.splash,
  error: null,
  errorMessage: '',
  isFirstRun: detectFirstRun(),
  splashMinElapsed: false,
  backendHealthy: false,
  serverUrl: null,
  hasSession: false,
  retryCount: 0,
  maxRetries: 3,

  setPhase: (phase) => {
    set({ phase, phaseInfo: PHASE_INFO[phase] });
  },

  setError: (type, message = '') => {
    set({
      phase: 'error',
      phaseInfo: PHASE_INFO.error,
      error: type,
      errorMessage: message,
    });
  },

  clearError: () => {
    set({ error: null, errorMessage: '' });
  },

  setSplashElapsed: () => {
    set({ splashMinElapsed: true });
  },

  setBackendHealthy: (healthy) => {
    set({ backendHealthy: healthy });
  },

  setServerUrl: (url) => {
    set({ serverUrl: url });
  },

  setIsFirstRun: (first) => {
    set({ isFirstRun: first });
  },

  setHasSession: (has) => {
    set({ hasSession: has });
  },

  incrementRetry: () => {
    const next = get().retryCount + 1;
    set({ retryCount: next });
    return next;
  },

  resetRetries: () => {
    set({ retryCount: 0 });
  },

  /**
   * Determine and transition to the next logical phase
   * based on current state. Called by the orchestrator after
   * each phase completes its work.
   */
  transitionToNextPhase: () => {
    const state = get();
    const { phase, backendHealthy, serverUrl, hasSession, isFirstRun, splashMinElapsed } = state;

    switch (phase) {
      case 'splash':
        // Wait for minimum splash time, then check backend
        if (splashMinElapsed) {
          set({ phase: 'backend_check', phaseInfo: PHASE_INFO.backend_check });
        }
        break;

      case 'backend_check':
        if (backendHealthy) {
          // Backend is up → discover server (or skip if we have saved URL)
          if (serverUrl) {
            // Already have a URL → try session restore directly
            set({ phase: 'session_restore', phaseInfo: PHASE_INFO.session_restore });
          } else {
            set({ phase: 'discovery', phaseInfo: PHASE_INFO.discovery });
          }
        }
        // If not healthy, error will be set by the orchestrator
        break;

      case 'discovery':
        if (serverUrl) {
          if (isFirstRun) {
            set({ phase: 'onboarding', phaseInfo: PHASE_INFO.onboarding });
          } else {
            set({ phase: 'session_restore', phaseInfo: PHASE_INFO.session_restore });
          }
        }
        // If no server found, error will be set by the orchestrator
        break;

      case 'session_restore':
        if (hasSession) {
          set({ phase: 'ready', phaseInfo: PHASE_INFO.ready });
        } else if (isFirstRun) {
          set({ phase: 'onboarding', phaseInfo: PHASE_INFO.onboarding });
        } else {
          set({ phase: 'login', phaseInfo: PHASE_INFO.login });
        }
        break;

      case 'onboarding':
        // After onboarding completes, go to ready
        set({ phase: 'ready', phaseInfo: PHASE_INFO.ready });
        break;

      case 'error':
        // Retry → restart from backend_check
        set({
          phase: 'backend_check',
          phaseInfo: PHASE_INFO.backend_check,
          error: null,
          errorMessage: '',
        });
        break;

      default:
        break;
    }
  },
}));

// ── Utility: Mark onboarding complete ────────────────────

export function markOnboardingComplete(): void {
  try {
    localStorage.setItem('commclient_onboarding_complete', 'true');
  } catch {}
}

// ── Utility: Reset first-run (for testing) ───────────────

export function resetFirstRun(): void {
  try {
    localStorage.removeItem('commclient_onboarding_complete');
    localStorage.removeItem('commclient_server_url');
    localStorage.removeItem('commclient_auth');
    localStorage.removeItem('commclient_settings');
  } catch {}
}
