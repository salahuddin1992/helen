/**
 * Onboarding Store — State machine for the complete first-run experience.
 *
 * Manages the full onboarding journey from welcome screen through profile
 * setup, permissions, server discovery, and room creation/joining.
 *
 * Flow Steps:
 *   welcome       → Language select + app intro (animated)
 *   profile       → Name + avatar color picker (minimal fields)
 *   permissions   → Mic/camera permission requests (one at a time, skippable)
 *   discovery     → Auto-detect server + create/join room
 *   ready         → Celebration + enter app
 *
 * Design Principles:
 *   - Each step is independently completable
 *   - User can go back to previous steps (except past registration)
 *   - Permissions are always skippable (can be granted later)
 *   - Server discovery runs silently in background starting from step 1
 *   - All state is ephemeral (not persisted until final commit)
 */

import { create } from 'zustand';

// ── Step Definitions ────────────────────────────────────────

export type OnboardingStep =
  | 'welcome'
  | 'profile'
  | 'permissions'
  | 'discovery'
  | 'ready';

export const ONBOARDING_STEPS: OnboardingStep[] = [
  'welcome',
  'profile',
  'permissions',
  'discovery',
  'ready',
];

export const STEP_INDEX: Record<OnboardingStep, number> = {
  welcome: 0,
  profile: 1,
  permissions: 2,
  discovery: 3,
  ready: 4,
};

// ── Avatar Palette ──────────────────────────────────────────
// Curated set of friendly avatar background colors for non-technical users.

export const AVATAR_COLORS = [
  '#3B82F6', // blue
  '#8B5CF6', // violet
  '#EC4899', // pink
  '#EF4444', // red
  '#F97316', // orange
  '#EAB308', // yellow
  '#22C55E', // green
  '#06B6D4', // cyan
  '#6366F1', // indigo
  '#A855F7', // purple
  '#14B8A6', // teal
  '#F43F5E', // rose
] as const;

// ── Permission State ────────────────────────────────────────

export type PermissionStatus = 'pending' | 'granted' | 'denied' | 'skipped';

export interface PermissionState {
  microphone: PermissionStatus;
  camera: PermissionStatus;
}

// ── Discovery State ─────────────────────────────────────────

export type DiscoveryStatus = 'idle' | 'searching' | 'found' | 'not_found' | 'manual';

export interface DiscoveredServer {
  url: string;
  name: string;
  userCount: number;
  verified: boolean;
}

// ── Room/Action Choice ──────────────────────────────────────

export type PostSetupAction = 'none' | 'create_room' | 'join_room' | 'skip';

// ── Store Interface ─────────────────────────────────────────

interface OnboardingState {
  // Flow control
  currentStep: OnboardingStep;
  stepIndex: number;
  totalSteps: number;
  isTransitioning: boolean;
  direction: 'forward' | 'backward';

  // Welcome step
  selectedLanguage: 'en' | 'ar';

  // Profile step
  userName: string;
  displayName: string;
  avatarColor: string;
  avatarInitials: string;
  password: string;
  confirmPassword: string;

  // Permissions step
  permissions: PermissionState;
  permissionPhase: 'intro' | 'microphone' | 'camera' | 'done';

  // Discovery step
  discoveryStatus: DiscoveryStatus;
  discoveredServers: DiscoveredServer[];
  selectedServerUrl: string;
  manualServerUrl: string;
  postSetupAction: PostSetupAction;
  roomName: string;

  // Validation
  errors: Record<string, string>;

  // Actions — navigation
  goToStep: (step: OnboardingStep) => void;
  nextStep: () => void;
  prevStep: () => void;
  setTransitioning: (v: boolean) => void;

  // Actions — welcome
  setLanguage: (lang: 'en' | 'ar') => void;

  // Actions — profile
  setUserName: (name: string) => void;
  setDisplayName: (name: string) => void;
  setAvatarColor: (color: string) => void;
  setPassword: (pw: string) => void;
  setConfirmPassword: (pw: string) => void;

  // Actions — permissions
  setPermission: (type: 'microphone' | 'camera', status: PermissionStatus) => void;
  setPermissionPhase: (phase: 'intro' | 'microphone' | 'camera' | 'done') => void;
  skipAllPermissions: () => void;

  // Actions — discovery
  setDiscoveryStatus: (status: DiscoveryStatus) => void;
  addDiscoveredServer: (server: DiscoveredServer) => void;
  selectServer: (url: string) => void;
  setManualServerUrl: (url: string) => void;
  setPostSetupAction: (action: PostSetupAction) => void;
  setRoomName: (name: string) => void;

  // Actions — validation
  setError: (field: string, message: string) => void;
  clearError: (field: string) => void;
  clearAllErrors: () => void;
  validateProfile: () => boolean;

  // Actions — finalize
  reset: () => void;
}

// ── Helpers ─────────────────────────────────────────────────

function computeInitials(name: string): string {
  if (!name.trim()) return '?';
  const parts = name.trim().split(/\s+/);
  if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
  return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
}

function randomAvatarColor(): string {
  return AVATAR_COLORS[Math.floor(Math.random() * AVATAR_COLORS.length)];
}

// ── Initial State ───────────────────────────────────────────

const INITIAL_STATE = {
  currentStep: 'welcome' as OnboardingStep,
  stepIndex: 0,
  totalSteps: ONBOARDING_STEPS.length,
  isTransitioning: false,
  direction: 'forward' as const,

  selectedLanguage: 'en' as const,

  userName: '',
  displayName: '',
  avatarColor: randomAvatarColor(),
  avatarInitials: '?',
  password: '',
  confirmPassword: '',

  permissions: {
    microphone: 'pending' as PermissionStatus,
    camera: 'pending' as PermissionStatus,
  },
  permissionPhase: 'intro' as const,

  discoveryStatus: 'idle' as DiscoveryStatus,
  discoveredServers: [] as DiscoveredServer[],
  selectedServerUrl: '',
  manualServerUrl: '',
  postSetupAction: 'none' as PostSetupAction,
  roomName: '',

  errors: {} as Record<string, string>,
};

// ── Store Implementation ────────────────────────────────────

export const useOnboardingStore = create<OnboardingState>((set, get) => ({
  ...INITIAL_STATE,

  // ── Navigation ──────────────────────────────────────────

  goToStep: (step) => {
    const idx = STEP_INDEX[step];
    const currentIdx = get().stepIndex;
    set({
      currentStep: step,
      stepIndex: idx,
      direction: idx > currentIdx ? 'forward' : 'backward',
      isTransitioning: true,
    });
    // Auto-clear transitioning after animation
    setTimeout(() => set({ isTransitioning: false }), 400);
  },

  nextStep: () => {
    const { stepIndex, totalSteps } = get();
    if (stepIndex >= totalSteps - 1) return;
    const nextIdx = stepIndex + 1;
    const nextStep = ONBOARDING_STEPS[nextIdx];
    set({
      currentStep: nextStep,
      stepIndex: nextIdx,
      direction: 'forward',
      isTransitioning: true,
    });
    setTimeout(() => set({ isTransitioning: false }), 400);
  },

  prevStep: () => {
    const { stepIndex } = get();
    if (stepIndex <= 0) return;
    const prevIdx = stepIndex - 1;
    const prevStep = ONBOARDING_STEPS[prevIdx];
    set({
      currentStep: prevStep,
      stepIndex: prevIdx,
      direction: 'backward',
      isTransitioning: true,
    });
    setTimeout(() => set({ isTransitioning: false }), 400);
  },

  setTransitioning: (v) => set({ isTransitioning: v }),

  // ── Welcome ─────────────────────────────────────────────

  setLanguage: (lang) => set({ selectedLanguage: lang }),

  // ── Profile ─────────────────────────────────────────────

  setUserName: (name) => {
    const initials = computeInitials(name);
    set((s) => ({
      userName: name,
      avatarInitials: initials,
      // Auto-sync displayName if user hasn't manually edited it
      displayName: s.displayName === '' || s.displayName === s.userName ? name : s.displayName,
    }));
  },

  setDisplayName: (name) => set({ displayName: name }),

  setAvatarColor: (color) => set({ avatarColor: color }),

  setPassword: (pw) => set({ password: pw }),

  setConfirmPassword: (pw) => set({ confirmPassword: pw }),

  // ── Permissions ─────────────────────────────────────────

  setPermission: (type, status) =>
    set((s) => ({
      permissions: { ...s.permissions, [type]: status },
    })),

  setPermissionPhase: (phase) => set({ permissionPhase: phase }),

  skipAllPermissions: () =>
    set({
      permissions: { microphone: 'skipped', camera: 'skipped' },
      permissionPhase: 'done',
    }),

  // ── Discovery ───────────────────────────────────────────

  setDiscoveryStatus: (status) => set({ discoveryStatus: status }),

  addDiscoveredServer: (server) =>
    set((s) => {
      const exists = s.discoveredServers.some((x) => x.url === server.url);
      if (exists) {
        return {
          discoveredServers: s.discoveredServers.map((x) =>
            x.url === server.url ? server : x
          ),
        };
      }
      return { discoveredServers: [...s.discoveredServers, server] };
    }),

  selectServer: (url) => set({ selectedServerUrl: url }),

  setManualServerUrl: (url) => set({ manualServerUrl: url }),

  setPostSetupAction: (action) => set({ postSetupAction: action }),

  setRoomName: (name) => set({ roomName: name }),

  // ── Validation ──────────────────────────────────────────

  setError: (field, message) =>
    set((s) => ({ errors: { ...s.errors, [field]: message } })),

  clearError: (field) =>
    set((s) => {
      const next = { ...s.errors };
      delete next[field];
      return { errors: next };
    }),

  clearAllErrors: () => set({ errors: {} }),

  validateProfile: () => {
    const { userName, password, confirmPassword } = get();
    const errors: Record<string, string> = {};

    if (!userName.trim()) {
      errors.userName = 'ob.error_name_required';
    } else if (userName.trim().length < 2) {
      errors.userName = 'ob.error_name_short';
    } else if (userName.trim().length > 30) {
      errors.userName = 'ob.error_name_long';
    } else if (!/^[a-zA-Z0-9_\u0600-\u06FF\s]+$/.test(userName.trim())) {
      errors.userName = 'ob.error_name_invalid';
    }

    if (!password) {
      errors.password = 'ob.error_password_required';
    } else if (password.length < 6) {
      errors.password = 'ob.error_password_short';
    }

    if (password && confirmPassword && password !== confirmPassword) {
      errors.confirmPassword = 'ob.error_password_mismatch';
    }

    set({ errors });
    return Object.keys(errors).length === 0;
  },

  // ── Reset ───────────────────────────────────────────────

  reset: () => set({ ...INITIAL_STATE, avatarColor: randomAvatarColor() }),
}));
