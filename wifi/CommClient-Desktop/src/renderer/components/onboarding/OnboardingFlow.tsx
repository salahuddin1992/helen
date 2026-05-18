/**
 * OnboardingFlow.tsx — Master orchestrator for the first-run experience.
 *
 * Responsibilities:
 *   - Renders the correct screen based on current onboarding step
 *   - Manages slide transitions (fade + translate) between screens
 *   - Shows step progress indicator (dots)
 *   - Handles the overall container (gradient background, centering)
 *   - Calls onComplete when the user finishes onboarding
 *
 * Architecture:
 *   OnboardingFlow (orchestrator)
 *     ├── StepIndicator (progress dots)
 *     ├── WelcomeScreen (step 0)
 *     ├── ProfileSetupScreen (step 1)
 *     ├── PermissionsScreen (step 2)
 *     ├── ServerDiscoveryScreen (step 3)
 *     └── ReadyScreen (step 4, handles registration + enter)
 *
 * Transition:
 *   - Forward: slide left + fade
 *   - Backward: slide right + fade
 *   - Duration: 350ms ease-out
 *
 * Integration with AppBootstrapScreen:
 *   This component replaces OnboardingWizard when the app detects
 *   a first-run scenario. It receives the same props interface
 *   (serverUrl, onComplete) for drop-in compatibility.
 */

import React, { useEffect, useMemo } from 'react';
import { useOnboardingStore, ONBOARDING_STEPS } from '@/stores/onboarding.store';

// Screens
import WelcomeScreen from './WelcomeScreen';
import ProfileSetupScreen from './ProfileSetupScreen';
import PermissionsScreen from './PermissionsScreen';
import ServerDiscoveryScreen from './ServerDiscoveryScreen';
import ReadyScreen from './ReadyScreen';

// ── Step Indicator (dot progress) ───────────────────────────

interface StepIndicatorProps {
  currentIndex: number;
  totalSteps: number;
}

const StepIndicator: React.FC<StepIndicatorProps> = ({ currentIndex, totalSteps }) => (
  <div className="flex items-center justify-center gap-2 py-4">
    {Array.from({ length: totalSteps }, (_, i) => (
      <div
        key={i}
        className={`rounded-full transition-all duration-500 ${
          i === currentIndex
            ? 'w-8 h-2 bg-blue-500'
            : i < currentIndex
              ? 'w-2 h-2 bg-blue-400/60'
              : 'w-2 h-2 bg-surface-700'
        }`}
      />
    ))}
  </div>
);

// ── Transition Wrapper ──────────────────────────────────────

interface SlideTransitionProps {
  direction: 'forward' | 'backward';
  isTransitioning: boolean;
  children: React.ReactNode;
}

const SlideTransition: React.FC<SlideTransitionProps> = ({
  direction,
  isTransitioning,
  children,
}) => {
  const translateClass = isTransitioning
    ? direction === 'forward'
      ? 'translate-x-8 opacity-0'
      : '-translate-x-8 opacity-0'
    : 'translate-x-0 opacity-100';

  return (
    <div className={`w-full transition-all duration-350 ease-out ${translateClass}`}>
      {children}
    </div>
  );
};

// ── Main Orchestrator ───────────────────────────────────────

export interface OnboardingFlowProps {
  serverUrl: string;
  onComplete: () => void;
}

const OnboardingFlow: React.FC<OnboardingFlowProps> = ({ serverUrl, onComplete }) => {
  const {
    currentStep,
    stepIndex,
    totalSteps,
    isTransitioning,
    direction,
    selectServer,
  } = useOnboardingStore();

  // Pre-populate discovered server from bootstrap
  useEffect(() => {
    if (serverUrl) {
      selectServer(serverUrl);
    }
  }, [serverUrl]);

  // Render the current step screen
  const screenContent = useMemo(() => {
    switch (currentStep) {
      case 'welcome':
        return <WelcomeScreen />;
      case 'profile':
        return <ProfileSetupScreen />;
      case 'permissions':
        return <PermissionsScreen />;
      case 'discovery':
        return <ServerDiscoveryScreen />;
      case 'ready':
        return <ReadyScreen onComplete={onComplete} />;
      default:
        return <WelcomeScreen />;
    }
  }, [currentStep, onComplete]);

  return (
    <div className="fixed inset-0 z-[90] bg-gradient-to-br from-surface-950 via-surface-900 to-surface-950 select-none overflow-hidden">
      {/* Step indicator (hidden on ready screen) */}
      {currentStep !== 'ready' && (
        <div className="absolute top-4 left-0 right-0 z-10">
          <StepIndicator
            currentIndex={stepIndex}
            totalSteps={totalSteps - 1} // Don't count 'ready' as a visible step
          />
        </div>
      )}

      {/* Screen content with slide transition */}
      <div className="h-full overflow-y-auto">
        <SlideTransition direction={direction} isTransitioning={isTransitioning}>
          {screenContent}
        </SlideTransition>
      </div>
    </div>
  );
};

export default OnboardingFlow;
