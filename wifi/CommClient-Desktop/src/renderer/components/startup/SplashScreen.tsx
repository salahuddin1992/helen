/**
 * SplashScreen.tsx — Animated splash with progress indicator.
 *
 * Shows the CommClient logo with a pulsing animation and a status line
 * that reflects the current startup phase. Minimum display time: 1.2s
 * to ensure the brand impression registers before transitioning.
 */

import React, { useEffect, useState } from 'react';
import { Wifi } from 'lucide-react';
import { t } from '@/i18n';
import { useAppStore, AppPhase, PHASE_INFO } from '@/stores/app.store';

interface SplashScreenProps {
  phase: AppPhase;
  progress?: number;
  statusText?: string;
}

const SplashScreen: React.FC<SplashScreenProps> = ({ phase, progress = 0, statusText }) => {
  const [fadeIn, setFadeIn] = useState(false);
  const [showProgress, setShowProgress] = useState(false);

  useEffect(() => {
    // Stagger animations for visual polish
    const t1 = setTimeout(() => setFadeIn(true), 50);
    const t2 = setTimeout(() => setShowProgress(true), 600);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, []);

  const phaseInfo = PHASE_INFO[phase] || PHASE_INFO.splash;
  const displayText = statusText || t(phaseInfo.label) || '';
  const displayProgress = progress || phaseInfo.progress;

  return (
    <div className="fixed inset-0 z-[100] flex flex-col items-center justify-center bg-gradient-to-br from-surface-950 via-surface-900 to-surface-950 select-none">
      {/* Logo area */}
      <div
        className={`flex flex-col items-center gap-6 transition-all duration-700 ease-out ${
          fadeIn ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-4'
        }`}
      >
        {/* App icon — pulsing wifi symbol in a circle */}
        <div className="relative">
          <div className="w-24 h-24 rounded-full bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center shadow-2xl shadow-blue-500/30">
            <Wifi size={44} className="text-white" />
          </div>
          {/* Pulse ring animation */}
          {(phase === 'splash' || phase === 'backend_check' || phase === 'discovery') && (
            <>
              <div className="absolute inset-0 rounded-full border-2 border-blue-400/40 animate-ping" />
              <div
                className="absolute inset-0 rounded-full border border-blue-400/20 animate-ping"
                style={{ animationDelay: '0.5s' }}
              />
            </>
          )}
        </div>

        {/* App name */}
        <div className="text-center">
          <h1 className="text-3xl font-bold text-white tracking-wide">
            {t('app.name') || 'Helen'}
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            {t('startup.tagline') || 'Local network communication'}
          </p>
        </div>
      </div>

      {/* Progress section */}
      <div
        className={`mt-12 w-64 flex flex-col items-center gap-3 transition-all duration-500 ${
          showProgress ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2'
        }`}
      >
        {/* Progress bar */}
        <div className="w-full h-1 bg-surface-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-blue-500 to-blue-400 rounded-full transition-all duration-700 ease-out"
            style={{ width: `${Math.min(displayProgress, 100)}%` }}
          />
        </div>

        {/* Status text */}
        <p className="text-xs text-gray-500 text-center min-h-[1.2em]">
          {displayText}
        </p>
      </div>

      {/* Version (bottom) */}
      <div className="absolute bottom-6 text-xs text-gray-700">
        v1.0.0
      </div>
    </div>
  );
};

export default SplashScreen;
