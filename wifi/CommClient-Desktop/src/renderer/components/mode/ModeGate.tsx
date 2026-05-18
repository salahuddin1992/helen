/**
 * ModeGate — Conditional renderer based on current app mode.
 *
 * Usage:
 *   <ModeGate mode="advanced">
 *     <SomethingOnlyAdvancedUsersSee />
 *   </ModeGate>
 *
 *   <ModeGate mode="simple">
 *     <SimplifiedVersion />
 *   </ModeGate>
 *
 *   <ModeGate mode="advanced" fallback={<SimplifiedVersion />}>
 *     <FullVersion />
 *   </ModeGate>
 *
 * Renders nothing (or fallback) if the current mode doesn't match.
 * This is the primary mechanism for hiding advanced UI from simple users.
 */

import React from 'react';
import { useAppModeStore, type AppMode } from '@/stores/app-mode.store';

interface ModeGateProps {
  /** Which mode is required to show children */
  mode: AppMode;
  /** Content to render when mode matches */
  children: React.ReactNode;
  /** Optional fallback when mode doesn't match (defaults to null) */
  fallback?: React.ReactNode;
}

export const ModeGate: React.FC<ModeGateProps> = ({ mode, children, fallback = null }) => {
  const currentMode = useAppModeStore((s) => s.mode);

  // "advanced" gate: only show if in advanced mode
  if (mode === 'advanced' && currentMode !== 'advanced') {
    return <>{fallback}</>;
  }

  // "simple" gate: show in simple mode (also show in advanced, since advanced sees everything)
  // Actually, simple gate means "only in simple mode, hide in advanced"
  if (mode === 'simple' && currentMode !== 'simple') {
    return <>{fallback}</>;
  }

  return <>{children}</>;
};

/**
 * AdvancedOnly — shorthand for ModeGate mode="advanced".
 * Content is ONLY visible when the app is in Advanced Mode.
 */
export const AdvancedOnly: React.FC<{ children: React.ReactNode; fallback?: React.ReactNode }> = ({
  children,
  fallback = null,
}) => {
  const isAdvanced = useAppModeStore((s) => s.isAdvanced);
  if (!isAdvanced) return <>{fallback}</>;
  return <>{children}</>;
};

/**
 * SimpleOnly — Content is ONLY visible when the app is in Simple Mode.
 * Advanced users do NOT see this; they see the fallback or nothing.
 */
export const SimpleOnly: React.FC<{ children: React.ReactNode; fallback?: React.ReactNode }> = ({
  children,
  fallback = null,
}) => {
  const isAdvanced = useAppModeStore((s) => s.isAdvanced);
  if (isAdvanced) return <>{fallback}</>;
  return <>{children}</>;
};

/**
 * useIsAdvanced — Hook to check if current mode is advanced.
 */
export function useIsAdvanced(): boolean {
  return useAppModeStore((s) => s.isAdvanced);
}

export default ModeGate;
