/**
 * ReadyScreen.tsx — Celebration screen after onboarding completes.
 *
 * Goals:
 *   - Celebration moment (confetti-like animation, checkmark)
 *   - Summary of what was set up (name, permissions, server)
 *   - Single "Enter App" CTA
 *   - Auto-transitions after 3 seconds if user doesn't click
 *   - Quick tips for first actions (optional)
 *
 * This screen handles the actual registration with the server,
 * then shows the success state.
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Check, Star, MessageCircle, Phone, Users,
  Loader2, AlertTriangle, ArrowRight, ArrowLeft,
} from 'lucide-react';
import { t } from '@/i18n';
import { useOnboardingStore } from '@/stores/onboarding.store';
import { useAuthStore } from '@/stores/auth.store';
import { markOnboardingComplete } from '@/stores/app.store';

type ReadyPhase = 'registering' | 'success' | 'error';

const ReadyScreen: React.FC = () => {
  const {
    userName,
    displayName,
    password,
    avatarColor,
    avatarInitials,
    selectedServerUrl,
    selectedLanguage,
    permissions,
  } = useOnboardingStore();

  const { register, setServerUrl: setAuthServerUrl } = useAuthStore();

  const [phase, setPhase] = useState<ReadyPhase>('registering');
  const [fadeIn, setFadeIn] = useState(false);
  const [showTips, setShowTips] = useState(false);
  const [error, setError] = useState('');
  const [autoEnterTimer, setAutoEnterTimer] = useState(5);
  const onCompleteRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setFadeIn(true), 50);
    return () => clearTimeout(timer);
  }, []);

  // ── Register user on mount ──────────────────────────
  useEffect(() => {
    let cancelled = false;

    const doRegister = async () => {
      try {
        // Configure server URL
        setAuthServerUrl(selectedServerUrl);

        // Generate username from name (lowercase, spaces→underscores)
        const username = userName.trim().toLowerCase().replace(/\s+/g, '_');
        const display = (displayName.trim() || userName.trim());

        await register(username, display, password);

        if (!cancelled) {
          // AlertTriangle avatar color preference
          try {
            localStorage.setItem('commclient_avatar_color', avatarColor);
          } catch {}

          // Mark onboarding as complete
          markOnboardingComplete();

          setPhase('success');

          // Show tips after a beat
          setTimeout(() => {
            if (!cancelled) setShowTips(true);
          }, 800);
        }
      } catch (e: any) {
        if (!cancelled) {
          setError(e?.message || 'Registration failed');
          setPhase('error');
        }
      }
    };

    doRegister();
    return () => { cancelled = true; };
  }, []);

  // ── Auto-enter countdown ────────────────────────────
  useEffect(() => {
    if (phase !== 'success') return;

    const interval = setInterval(() => {
      setAutoEnterTimer((prev) => {
        if (prev <= 1) {
          clearInterval(interval);
          // Trigger app entry
          if (onCompleteRef.current) onCompleteRef.current();
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(interval);
  }, [phase]);

  // ── Summary items ───────────────────────────────────
  const summaryItems = [
    {
      label: t('ob.ready_name'),
      value: displayName || userName,
    },
    {
      label: t('ob.ready_mic'),
      value: permissions.microphone === 'granted' ? t('ob.ready_enabled') : t('ob.ready_not_set'),
    },
    {
      label: t('ob.ready_camera'),
      value: permissions.camera === 'granted' ? t('ob.ready_enabled') : t('ob.ready_not_set'),
    },
  ];

  const isRTL = selectedLanguage === 'ar';
  const ArrowForward = isRTL ? ArrowLeft : ArrowRight;

  return (
    <div
      className={`flex flex-col items-center min-h-screen px-6 py-10 transition-opacity duration-500 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      {/* ── Registering Phase ──────────────────── */}
      {phase === 'registering' && (
        <div className="flex flex-col items-center justify-center flex-1 gap-6">
          <div className="relative">
            <div
              className="w-24 h-24 rounded-full flex items-center justify-center text-3xl font-bold text-white shadow-xl"
              style={{ backgroundColor: avatarColor }}
            >
              {avatarInitials}
            </div>
            <div className="absolute -bottom-2 -right-2 w-10 h-10 bg-blue-600 rounded-full flex items-center justify-center shadow-lg">
              <Loader2 size={20} className="text-white animate-spin" />
            </div>
          </div>
          <div className="text-center">
            <h2 className="text-xl font-bold text-white mb-2">
              {t('ob.ready_creating')}
            </h2>
            <p className="text-gray-400 text-sm">
              {t('ob.ready_creating_hint')}
            </p>
          </div>
        </div>
      )}

      {/* ── Success Phase ──────────────────────── */}
      {phase === 'success' && (
        <>
          {/* Celebration icon */}
          <div className="relative mb-6 mt-4">
            <div className="w-20 h-20 rounded-full bg-green-500/15 flex items-center justify-center">
              <Check size={40} className="text-green-400" />
            </div>
            {/* Sparkle decorations */}
            <Star
              size={18}
              className="text-yellow-400 absolute -top-2 -right-1 animate-pulse"
            />
            <Star
              size={14}
              className="text-blue-400 absolute -bottom-1 -left-2 animate-pulse"
              style={{ animationDelay: '0.5s' }}
            />
          </div>

          <div className="text-center mb-8">
            <h1 className="text-2xl font-bold text-white mb-2">
              {t('ob.ready_title')}
            </h1>
            <p className="text-gray-400 text-base">
              {t('ob.ready_subtitle')}
            </p>
          </div>

          {/* ── Setup Summary ──────────────────── */}
          <div className="w-full max-w-sm mb-6">
            <div className="bg-surface-800/50 rounded-xl border border-surface-700 divide-y divide-surface-700">
              {summaryItems.map((item, idx) => (
                <div key={idx} className="flex items-center justify-between px-4 py-3">
                  <span className="text-sm text-gray-400">{item.label}</span>
                  <span className="text-sm text-white font-medium">{item.value}</span>
                </div>
              ))}
            </div>
          </div>

          {/* ── Quick Tips ──────────────────────── */}
          {showTips && (
            <div className="w-full max-w-sm mb-6 animate-fadeIn">
              <p className="text-xs text-gray-500 font-medium mb-3 text-center">
                {t('ob.ready_tips_title')}
              </p>
              <div className="grid grid-cols-3 gap-3">
                <div className="flex flex-col items-center gap-2 p-3 bg-surface-800/40 rounded-xl">
                  <MessageCircle size={20} className="text-blue-400" />
                  <span className="text-xs text-gray-400 text-center">{t('ob.ready_tip_chat')}</span>
                </div>
                <div className="flex flex-col items-center gap-2 p-3 bg-surface-800/40 rounded-xl">
                  <Phone size={20} className="text-green-400" />
                  <span className="text-xs text-gray-400 text-center">{t('ob.ready_tip_call')}</span>
                </div>
                <div className="flex flex-col items-center gap-2 p-3 bg-surface-800/40 rounded-xl">
                  <Users size={20} className="text-purple-400" />
                  <span className="text-xs text-gray-400 text-center">{t('ob.ready_tip_group')}</span>
                </div>
              </div>
            </div>
          )}

          {/* ── Enter App Button ────────────────── */}
          <div className="w-full max-w-sm mt-auto space-y-2">
            <button
              onClick={() => {
                if (onCompleteRef.current) onCompleteRef.current();
              }}
              className="w-full py-4 bg-green-600 hover:bg-green-700 text-white font-semibold rounded-xl transition-all flex items-center justify-center gap-3 text-lg shadow-lg shadow-green-600/20"
            >
              {t('ob.ready_enter')}
              <ArrowForward size={20} />
            </button>
            {autoEnterTimer > 0 && (
              <p className="text-xs text-gray-600 text-center">
                {t('ob.ready_auto_enter')} {autoEnterTimer}s
              </p>
            )}
          </div>
        </>
      )}

      {/* ── Error Phase ────────────────────────── */}
      {phase === 'error' && (
        <div className="flex flex-col items-center justify-center flex-1 gap-6 max-w-sm">
          <div className="w-16 h-16 rounded-full bg-red-500/10 flex items-center justify-center">
            <AlertTriangle size={32} className="text-red-400" />
          </div>
          <div className="text-center">
            <h2 className="text-xl font-bold text-white mb-2">
              {t('ob.ready_error_title')}
            </h2>
            <p className="text-gray-400 text-sm mb-2">
              {t('ob.ready_error_subtitle')}
            </p>
            {error && (
              <p className="text-xs text-red-400/80 bg-red-500/5 p-2 rounded-lg">
                {error}
              </p>
            )}
          </div>
          <button
            onClick={() => {
              setPhase('registering');
              setError('');
              // Retry registration
              const doRetry = async () => {
                try {
                  setAuthServerUrl(selectedServerUrl);
                  const username = userName.trim().toLowerCase().replace(/\s+/g, '_');
                  const display = (displayName.trim() || userName.trim());
                  await register(username, display, password);
                  markOnboardingComplete();
                  setPhase('success');
                } catch (e: any) {
                  setError(e?.message || 'Registration failed');
                  setPhase('error');
                }
              };
              doRetry();
            }}
            className="w-full py-3 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-colors"
          >
            {t('ob.ready_retry')}
          </button>
        </div>
      )}
    </div>
  );
};

// Wrap to receive onComplete from parent
export interface ReadyScreenProps {
  onComplete: () => void;
}

const ReadyScreenWrapper: React.FC<ReadyScreenProps> = ({ onComplete }) => {
  // Pass onComplete via ref so ReadyScreen can call it
  const WrapperInner = () => {
    const ref = useRef(onComplete);
    ref.current = onComplete;

    // Inject into the ReadyScreen's onCompleteRef
    // We use a context-free approach: render ReadyScreen and let it call the ref
    return <ReadyScreenInner onComplete={onComplete} />;
  };
  return <WrapperInner />;
};

const ReadyScreenInner: React.FC<{ onComplete: () => void }> = ({ onComplete }) => {
  const {
    userName,
    displayName,
    password,
    avatarColor,
    avatarInitials,
    selectedServerUrl,
    selectedLanguage,
    permissions,
  } = useOnboardingStore();

  const { register, setServerUrl: setAuthServerUrl } = useAuthStore();

  const [phase, setPhase] = useState<ReadyPhase>('registering');
  const [fadeIn, setFadeIn] = useState(false);
  const [showTips, setShowTips] = useState(false);
  const [error, setError] = useState('');
  const [autoEnterTimer, setAutoEnterTimer] = useState(5);

  useEffect(() => {
    const timer = setTimeout(() => setFadeIn(true), 50);
    return () => clearTimeout(timer);
  }, []);

  // ── Register user on mount ──────────────────────────
  useEffect(() => {
    let cancelled = false;

    const doRegister = async () => {
      try {
        setAuthServerUrl(selectedServerUrl);
        const username = userName.trim().toLowerCase().replace(/\s+/g, '_');
        const display = (displayName.trim() || userName.trim());
        await register(username, display, password);

        if (!cancelled) {
          try {
            localStorage.setItem('commclient_avatar_color', avatarColor);
          } catch {}
          markOnboardingComplete();
          setPhase('success');
          setTimeout(() => {
            if (!cancelled) setShowTips(true);
          }, 800);
        }
      } catch (e: any) {
        if (!cancelled) {
          setError(e?.message || 'Registration failed');
          setPhase('error');
        }
      }
    };

    doRegister();
    return () => { cancelled = true; };
  }, []);

  // ── Auto-enter countdown ────────────────────────────
  useEffect(() => {
    if (phase !== 'success') return;

    const interval = setInterval(() => {
      setAutoEnterTimer((prev) => {
        if (prev <= 1) {
          clearInterval(interval);
          onComplete();
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(interval);
  }, [phase]);

  const handleRetry = useCallback(async () => {
    setPhase('registering');
    setError('');
    try {
      setAuthServerUrl(selectedServerUrl);
      const username = userName.trim().toLowerCase().replace(/\s+/g, '_');
      const display = (displayName.trim() || userName.trim());
      await register(username, display, password);
      try { localStorage.setItem('commclient_avatar_color', avatarColor); } catch {}
      markOnboardingComplete();
      setPhase('success');
    } catch (e: any) {
      setError(e?.message || 'Registration failed');
      setPhase('error');
    }
  }, [selectedServerUrl, userName, displayName, password, avatarColor]);

  const summaryItems = [
    { label: t('ob.ready_name'), value: displayName || userName },
    { label: t('ob.ready_mic'), value: permissions.microphone === 'granted' ? t('ob.ready_enabled') : t('ob.ready_not_set') },
    { label: t('ob.ready_camera'), value: permissions.camera === 'granted' ? t('ob.ready_enabled') : t('ob.ready_not_set') },
  ];

  const isRTL = selectedLanguage === 'ar';
  const ArrowForward = isRTL ? ArrowLeft : ArrowRight;

  return (
    <div
      className={`flex flex-col items-center min-h-screen px-6 py-10 transition-opacity duration-500 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      {/* ── Registering Phase ──────────────────── */}
      {phase === 'registering' && (
        <div className="flex flex-col items-center justify-center flex-1 gap-6">
          <div className="relative">
            <div
              className="w-24 h-24 rounded-full flex items-center justify-center text-3xl font-bold text-white shadow-xl"
              style={{ backgroundColor: avatarColor }}
            >
              {avatarInitials}
            </div>
            <div className="absolute -bottom-2 -right-2 w-10 h-10 bg-blue-600 rounded-full flex items-center justify-center shadow-lg">
              <Loader2 size={20} className="text-white animate-spin" />
            </div>
          </div>
          <div className="text-center">
            <h2 className="text-xl font-bold text-white mb-2">{t('ob.ready_creating')}</h2>
            <p className="text-gray-400 text-sm">{t('ob.ready_creating_hint')}</p>
          </div>
        </div>
      )}

      {/* ── Success Phase ──────────────────────── */}
      {phase === 'success' && (
        <>
          <div className="relative mb-6 mt-4">
            <div className="w-20 h-20 rounded-full bg-green-500/15 flex items-center justify-center">
              <Check size={40} className="text-green-400" />
            </div>
            <Star size={18} className="text-yellow-400 absolute -top-2 -right-1 animate-pulse" />
            <Star size={14} className="text-blue-400 absolute -bottom-1 -left-2 animate-pulse" style={{ animationDelay: '0.5s' }} />
          </div>

          <div className="text-center mb-8">
            <h1 className="text-2xl font-bold text-white mb-2">{t('ob.ready_title')}</h1>
            <p className="text-gray-400 text-base">{t('ob.ready_subtitle')}</p>
          </div>

          <div className="w-full max-w-sm mb-6">
            <div className="bg-surface-800/50 rounded-xl border border-surface-700 divide-y divide-surface-700">
              {summaryItems.map((item, idx) => (
                <div key={idx} className="flex items-center justify-between px-4 py-3">
                  <span className="text-sm text-gray-400">{item.label}</span>
                  <span className="text-sm text-white font-medium">{item.value}</span>
                </div>
              ))}
            </div>
          </div>

          {showTips && (
            <div className="w-full max-w-sm mb-6 animate-fadeIn">
              <p className="text-xs text-gray-500 font-medium mb-3 text-center">{t('ob.ready_tips_title')}</p>
              <div className="grid grid-cols-3 gap-3">
                <div className="flex flex-col items-center gap-2 p-3 bg-surface-800/40 rounded-xl">
                  <MessageCircle size={20} className="text-blue-400" />
                  <span className="text-xs text-gray-400 text-center">{t('ob.ready_tip_chat')}</span>
                </div>
                <div className="flex flex-col items-center gap-2 p-3 bg-surface-800/40 rounded-xl">
                  <Phone size={20} className="text-green-400" />
                  <span className="text-xs text-gray-400 text-center">{t('ob.ready_tip_call')}</span>
                </div>
                <div className="flex flex-col items-center gap-2 p-3 bg-surface-800/40 rounded-xl">
                  <Users size={20} className="text-purple-400" />
                  <span className="text-xs text-gray-400 text-center">{t('ob.ready_tip_group')}</span>
                </div>
              </div>
            </div>
          )}

          <div className="w-full max-w-sm mt-auto space-y-2">
            <button
              onClick={onComplete}
              className="w-full py-4 bg-green-600 hover:bg-green-700 text-white font-semibold rounded-xl transition-all flex items-center justify-center gap-3 text-lg shadow-lg shadow-green-600/20"
            >
              {t('ob.ready_enter')}
              <ArrowForward size={20} />
            </button>
            {autoEnterTimer > 0 && (
              <p className="text-xs text-gray-600 text-center">
                {t('ob.ready_auto_enter')} {autoEnterTimer}s
              </p>
            )}
          </div>
        </>
      )}

      {/* ── Error Phase ────────────────────────── */}
      {phase === 'error' && (
        <div className="flex flex-col items-center justify-center flex-1 gap-6 max-w-sm">
          <div className="w-16 h-16 rounded-full bg-red-500/10 flex items-center justify-center">
            <AlertTriangle size={32} className="text-red-400" />
          </div>
          <div className="text-center">
            <h2 className="text-xl font-bold text-white mb-2">{t('ob.ready_error_title')}</h2>
            <p className="text-gray-400 text-sm mb-2">{t('ob.ready_error_subtitle')}</p>
            {error && (
              <p className="text-xs text-red-400/80 bg-red-500/5 p-2 rounded-lg">{error}</p>
            )}
          </div>
          <button
            onClick={handleRetry}
            className="w-full py-3 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-colors"
          >
            {t('ob.ready_retry')}
          </button>
        </div>
      )}
    </div>
  );
};

export default ReadyScreenInner;
