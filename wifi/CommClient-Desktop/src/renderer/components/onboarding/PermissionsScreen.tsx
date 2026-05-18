/**
 * PermissionsScreen.tsx — Friendly, step-by-step permission request flow.
 *
 * Goals:
 *   - Ask one permission at a time (never both simultaneously)
 *   - Explain WHY each permission is needed in simple language
 *   - Show visual illustration for each permission
 *   - Allow skipping (permissions can be granted later from settings)
 *   - Show clear granted/denied status with friendly messaging
 *   - No technical error codes or scary system dialogs mentioned
 *
 * Flow:
 *   intro → microphone request → camera request → done
 *   (user can skip any step, or skip all)
 *
 * Permission Request Strategy:
 *   Uses navigator.mediaDevices.getUserMedia() one device at a time.
 *   On grant: stops tracks immediately, records 'granted'.
 *   On deny: records 'denied', shows gentle encouragement.
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Mic, Camera, Lock, Check, X, ChevronRight,
  ArrowRight, ArrowLeft, Volume2, Video,
} from 'lucide-react';
import { t } from '@/i18n';
import { useOnboardingStore, PermissionStatus } from '@/stores/onboarding.store';

// ── Permission Request Helper ───────────────────────────────

async function requestPermission(type: 'microphone' | 'camera'): Promise<PermissionStatus> {
  try {
    const constraints: MediaStreamConstraints =
      type === 'microphone'
        ? { audio: true, video: false }
        : { audio: false, video: true };

    const stream = await navigator.mediaDevices.getUserMedia(constraints);

    // Permission granted — stop all tracks immediately (we don't need them yet)
    stream.getTracks().forEach((track) => track.stop());
    return 'granted';
  } catch (err: any) {
    // NotAllowedError = user denied, NotFoundError = no device
    if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
      return 'denied';
    }
    // Device not found — treat as skipped (they might not have one)
    if (err.name === 'NotFoundError' || err.name === 'NotReadableError') {
      return 'skipped';
    }
    return 'denied';
  }
}

// ── Sub-Components ──────────────────────────────────────────

interface PermissionCardProps {
  type: 'microphone' | 'camera';
  status: PermissionStatus;
  isActive: boolean;
  onRequest: () => void;
  onSkip: () => void;
  isRequesting: boolean;
}

const PermissionCard: React.FC<PermissionCardProps> = ({
  type,
  status,
  isActive,
  onRequest,
  onSkip,
  isRequesting,
}) => {
  const isMic = type === 'microphone';
  const Icon = isMic ? Mic : Camera;
  const IllustrationIcon = isMic ? Volume2 : Video;

  const statusConfig = {
    pending: {
      border: isActive ? 'border-blue-500/50' : 'border-surface-700',
      bg: isActive ? 'bg-surface-800/80' : 'bg-surface-800/40',
      badge: null,
    },
    granted: {
      border: 'border-green-500/40',
      bg: 'bg-green-500/5',
      badge: (
        <div className="flex items-center gap-1.5 text-green-400 text-sm font-medium">
          <Check size={16} />
          {t('ob.perm_granted')}
        </div>
      ),
    },
    denied: {
      border: 'border-orange-500/40',
      bg: 'bg-orange-500/5',
      badge: (
        <div className="flex items-center gap-1.5 text-orange-400 text-sm">
          <X size={16} />
          {t('ob.perm_denied')}
        </div>
      ),
    },
    skipped: {
      border: 'border-surface-600',
      bg: 'bg-surface-800/40',
      badge: (
        <div className="flex items-center gap-1.5 text-gray-500 text-sm">
          <ChevronRight size={14} />
          {t('ob.perm_skipped')}
        </div>
      ),
    },
  };

  const cfg = statusConfig[status];

  return (
    <div className={`p-5 rounded-2xl border transition-all duration-300 ${cfg.border} ${cfg.bg}`}>
      <div className="flex items-start gap-4">
        {/* Icon */}
        <div className={`w-12 h-12 rounded-xl flex items-center justify-center shrink-0 ${
          status === 'granted' ? 'bg-green-500/15' :
          isActive ? 'bg-blue-500/15' : 'bg-surface-700'
        }`}>
          <Icon
            size={24}
            className={
              status === 'granted' ? 'text-green-400' :
              isActive ? 'text-blue-400' : 'text-gray-500'
            }
          />
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between mb-1">
            <h3 className="text-base font-semibold text-white">
              {t(isMic ? 'ob.perm_mic_title' : 'ob.perm_cam_title')}
            </h3>
            {cfg.badge}
          </div>
          <p className="text-sm text-gray-400 leading-relaxed mb-3">
            {t(isMic ? 'ob.perm_mic_why' : 'ob.perm_cam_why')}
          </p>

          {/* Action buttons — only show when active and pending */}
          {isActive && status === 'pending' && (
            <div className="flex gap-2">
              <button
                onClick={onRequest}
                disabled={isRequesting}
                className="flex-1 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-600/50 text-white font-medium rounded-lg transition-colors flex items-center justify-center gap-2 text-sm"
              >
                {isRequesting ? (
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                ) : (
                  <IllustrationIcon size={16} />
                )}
                {t('ob.perm_allow')}
              </button>
              <button
                onClick={onSkip}
                className="px-4 py-2.5 bg-surface-700 hover:bg-surface-600 text-gray-400 font-medium rounded-lg transition-colors text-sm"
              >
                {t('ob.perm_skip')}
              </button>
            </div>
          )}

          {/* Denied hint */}
          {status === 'denied' && (
            <p className="text-xs text-orange-400/70 mt-1">
              {t('ob.perm_denied_hint')}
            </p>
          )}
        </div>
      </div>
    </div>
  );
};

// ── Main Screen ─────────────────────────────────────────────

const PermissionsScreen: React.FC = () => {
  const {
    permissions,
    permissionPhase,
    selectedLanguage,
    setPermission,
    setPermissionPhase,
    skipAllPermissions,
    nextStep,
    prevStep,
  } = useOnboardingStore();

  const [fadeIn, setFadeIn] = useState(false);
  const [isRequesting, setIsRequesting] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setFadeIn(true), 50);
    return () => clearTimeout(timer);
  }, []);

  // Auto-advance phases
  useEffect(() => {
    if (permissionPhase === 'intro') {
      // Start with microphone after a brief pause
      const timer = setTimeout(() => setPermissionPhase('microphone'), 100);
      return () => clearTimeout(timer);
    }
  }, [permissionPhase]);

  const handleRequestMic = useCallback(async () => {
    setIsRequesting(true);
    const status = await requestPermission('microphone');
    setPermission('microphone', status);
    setIsRequesting(false);
    // Auto-advance to camera
    setPermissionPhase('camera');
  }, []);

  const handleSkipMic = useCallback(() => {
    setPermission('microphone', 'skipped');
    setPermissionPhase('camera');
  }, []);

  const handleRequestCamera = useCallback(async () => {
    setIsRequesting(true);
    const status = await requestPermission('camera');
    setPermission('camera', status);
    setIsRequesting(false);
    setPermissionPhase('done');
  }, []);

  const handleSkipCamera = useCallback(() => {
    setPermission('camera', 'skipped');
    setPermissionPhase('done');
  }, []);

  const handleContinue = () => {
    nextStep();
  };

  const handleSkipAll = () => {
    skipAllPermissions();
    nextStep();
  };

  const allDone = permissionPhase === 'done' ||
    (permissions.microphone !== 'pending' && permissions.camera !== 'pending');

  const isRTL = selectedLanguage === 'ar';
  const ArrowForward = isRTL ? ArrowLeft : ArrowRight;

  return (
    <div
      className={`flex flex-col items-center min-h-screen px-6 py-10 transition-opacity duration-500 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      {/* ── Header ─────────────────────────────── */}
      <div className="w-14 h-14 rounded-full bg-blue-600/15 flex items-center justify-center mb-5">
        <Lock size={28} className="text-blue-400" />
      </div>
      <div className="text-center mb-8">
        <h1 className="text-2xl font-bold text-white mb-2">
          {t('ob.perm_title')}
        </h1>
        <p className="text-gray-400 text-base max-w-sm mx-auto">
          {t('ob.perm_subtitle')}
        </p>
      </div>

      {/* ── Permission Cards ───────────────────── */}
      <div className="w-full max-w-sm space-y-3">
        <PermissionCard
          type="microphone"
          status={permissions.microphone}
          isActive={permissionPhase === 'microphone'}
          onRequest={handleRequestMic}
          onSkip={handleSkipMic}
          isRequesting={isRequesting && permissionPhase === 'microphone'}
        />
        <PermissionCard
          type="camera"
          status={permissions.camera}
          isActive={permissionPhase === 'camera'}
          onRequest={handleRequestCamera}
          onSkip={handleSkipCamera}
          isRequesting={isRequesting && permissionPhase === 'camera'}
        />
      </div>

      {/* ── Privacy Note ───────────────────────── */}
      <div className="mt-6 px-4 py-3 bg-surface-800/50 rounded-xl max-w-sm">
        <p className="text-xs text-gray-500 text-center leading-relaxed">
          {t('ob.perm_privacy')}
        </p>
      </div>

      {/* ── Navigation ─────────────────────────── */}
      <div className="w-full max-w-sm mt-8 space-y-3">
        {allDone ? (
          <button
            onClick={handleContinue}
            className="w-full py-4 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-all flex items-center justify-center gap-3 text-lg shadow-lg shadow-blue-600/20"
          >
            {t('ob.continue')}
            <ArrowForward size={20} />
          </button>
        ) : (
          <button
            onClick={handleSkipAll}
            className="w-full py-3 bg-surface-800 hover:bg-surface-700 text-gray-400 font-medium rounded-xl transition-colors text-sm"
          >
            {t('ob.perm_skip_all')}
          </button>
        )}

        <button
          onClick={prevStep}
          className="w-full py-2.5 text-gray-500 hover:text-gray-300 text-sm transition-colors"
        >
          {t('ob.back')}
        </button>
      </div>
    </div>
  );
};

export default PermissionsScreen;
