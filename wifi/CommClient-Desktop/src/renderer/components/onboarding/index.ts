/**
 * Onboarding Module — First-run experience for CommClient.
 *
 * Architecture:
 *   ┌──────────────────────────────────────────────────┐
 *   │              OnboardingFlow (orchestrator)        │
 *   │  ┌────────┬──────────┬───────────┬────────────┐  │
 *   │  │Welcome │ Profile  │Permissions│ Discovery  │  │
 *   │  │Screen  │ Setup    │  Screen   │  Screen    │  │
 *   │  │        │ Screen   │           │            │  │
 *   │  └────────┴──────────┴───────────┴────────────┘  │
 *   │                      │                            │
 *   │                 ReadyScreen                       │
 *   │           (registration + enter)                  │
 *   └──────────────────────────────────────────────────┘
 *
 *   ┌──────────────────────────────────────────────────┐
 *   │              EmptyStates (post-onboarding)       │
 *   │  EmptyChats | EmptyContacts | EmptyCalls         │
 *   │  EmptyGroups | EmptyNotifications                │
 *   └──────────────────────────────────────────────────┘
 *
 *   ┌──────────────────────────────────────────────────┐
 *   │              OnboardingStore (Zustand)            │
 *   │  Steps: welcome → profile → permissions →        │
 *   │         discovery → ready                         │
 *   │  State: language, profile, permissions, server    │
 *   └──────────────────────────────────────────────────┘
 *
 * Integration:
 *   - Drop-in replacement for OnboardingWizard in AppBootstrapScreen
 *   - Same props interface: { serverUrl, onComplete }
 *   - EmptyStates can be imported individually into existing pages
 *
 * i18n: All strings use 'ob.*' and 'empty.*' key prefixes
 */

// ── Main flow ──────────────────────────────────────────
export { default as OnboardingFlow } from './OnboardingFlow';
export type { OnboardingFlowProps } from './OnboardingFlow';

// ── Individual screens (for testing/storybook) ─────────
export { default as WelcomeScreen } from './WelcomeScreen';
export { default as ProfileSetupScreen } from './ProfileSetupScreen';
export { default as PermissionsScreen } from './PermissionsScreen';
export { default as ServerDiscoveryScreen } from './ServerDiscoveryScreen';
export { default as ReadyScreen } from './ReadyScreen';

// ── Empty states (for use in main app pages) ───────────
export {
  EmptyChats,
  EmptyContacts,
  EmptyCalls,
  EmptyGroups,
  EmptyNotifications,
  EmptyScreenShare,
} from './EmptyStates';
export type {
  EmptyChatsProps,
  EmptyContactsProps,
  EmptyCallsProps,
  EmptyGroupsProps,
  EmptyScreenShareProps,
} from './EmptyStates';

// ── Store re-export ────────────────────────────────────
export { useOnboardingStore } from '@/stores/onboarding.store';
export type { OnboardingStep, PermissionStatus, DiscoveredServer } from '@/stores/onboarding.store';
