/**
 * ModeAwareSettingsView — Routes to the correct settings panel based on app mode.
 *
 * Simple Mode  → SimpleSettingsView (clean, minimal)
 * Advanced Mode → AdvancedSettingsView (full technical controls)
 */

import React from 'react';
import { useAppModeStore } from '@/stores/app-mode.store';
import SimpleSettingsView from './SimpleSettingsView';
import AdvancedSettingsView from './AdvancedSettingsView';

const ModeAwareSettingsView: React.FC = () => {
  const isAdvanced = useAppModeStore((s) => s.isAdvanced);

  if (isAdvanced) {
    return <AdvancedSettingsView />;
  }

  return <SimpleSettingsView />;
};

export default ModeAwareSettingsView;
