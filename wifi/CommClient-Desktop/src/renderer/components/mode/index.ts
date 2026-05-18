/**
 * Mode system — barrel exports.
 *
 * Dual-mode architecture:
 *   Simple Mode:   Clean UI for normal users and children
 *   Advanced Mode:  Technical controls for admins
 */

// Gate components (conditional rendering)
export { ModeGate, AdvancedOnly, SimpleOnly, useIsAdvanced } from './ModeGate';

// Unlock mechanism
export { AdminUnlockModal, VersionTapTarget } from './AdminUnlockModal';

// Settings views
export { default as SimpleSettingsView } from './SimpleSettingsView';
export { default as AdvancedSettingsView } from './AdvancedSettingsView';
export { default as ModeAwareSettingsView } from './ModeAwareSettingsView';

// Navigation
export { ModeAwareSidebar } from './ModeAwareSidebar';

// Dashboard (advanced only)
export { default as AdvancedDashboard } from './AdvancedDashboard';
