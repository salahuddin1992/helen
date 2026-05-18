/**
 * AdminUnlockModal — PIN entry dialog to unlock Advanced Mode.
 *
 * Two flows:
 *   1. First time (no PIN set): "Create a PIN" with confirm
 *   2. Returning: "Enter your PIN" single input
 *
 * Triggered by the secret gesture (7 taps on version label).
 * The modal is styled to be unintimidating but clearly separated from normal UI.
 */

import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Lock, X, Eye, EyeOff } from 'lucide-react';
import { useAppModeStore } from '@/stores/app-mode.store';
import { t } from '@/i18n';

interface AdminUnlockModalProps {
  isOpen: boolean;
  onClose: () => void;
  onUnlocked: () => void;
}

export const AdminUnlockModal: React.FC<AdminUnlockModalProps> = ({
  isOpen,
  onClose,
  onUnlocked,
}) => {
  const isPinConfigured = useAppModeStore((s) => s.isPinConfigured);
  const unlock = useAppModeStore((s) => s.unlock);
  const setPin = useAppModeStore((s) => s.setPin);

  // Form state
  const [pin, setInputPin] = useState('');
  const [confirmPin, setConfirmPin] = useState('');
  const [showPin, setShowPin] = useState(false);
  const [error, setError] = useState('');
  const [step, setStep] = useState<'enter' | 'create' | 'confirm'>('enter');

  const inputRef = useRef<HTMLInputElement>(null);

  // Determine initial step
  useEffect(() => {
    if (isOpen) {
      setInputPin('');
      setConfirmPin('');
      setError('');
      setShowPin(false);
      setStep(isPinConfigured ? 'enter' : 'create');
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [isOpen, isPinConfigured]);

  const handleSubmit = useCallback((e?: React.FormEvent) => {
    e?.preventDefault();
    setError('');

    if (step === 'create') {
      // Validate new PIN
      if (pin.length < 4) {
        setError(t('mode.pin_too_short'));
        return;
      }
      if (!/^\d+$/.test(pin)) {
        setError(t('mode.pin_numbers_only'));
        return;
      }
      setStep('confirm');
      setConfirmPin('');
      setTimeout(() => inputRef.current?.focus(), 50);
      return;
    }

    if (step === 'confirm') {
      if (confirmPin !== pin) {
        setError(t('mode.pin_mismatch'));
        setConfirmPin('');
        return;
      }
      // Set and unlock
      const success = unlock(pin);
      if (success) {
        onUnlocked();
        onClose();
      } else {
        setError(t('mode.pin_error'));
      }
      return;
    }

    // step === 'enter'
    if (pin.length < 4) {
      setError(t('mode.pin_too_short'));
      return;
    }
    const success = unlock(pin);
    if (success) {
      onUnlocked();
      onClose();
    } else {
      setError(t('mode.pin_wrong'));
      setInputPin('');
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [step, pin, confirmPin, unlock, onUnlocked, onClose]);

  if (!isOpen) return null;

  const titles: Record<string, string> = {
    enter: t('mode.enter_pin'),
    create: t('mode.create_pin'),
    confirm: t('mode.confirm_pin'),
  };

  const subtitles: Record<string, string> = {
    enter: t('mode.enter_pin_sub'),
    create: t('mode.create_pin_sub'),
    confirm: t('mode.confirm_pin_sub'),
  };

  const currentValue = step === 'confirm' ? confirmPin : pin;
  const setCurrentValue = step === 'confirm' ? setConfirmPin : setInputPin;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm animate-fadeIn">
      <div className="w-full max-w-sm mx-4 bg-surface-900 border border-surface-700 rounded-2xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="relative bg-gradient-to-r from-amber-600/20 to-orange-600/20 border-b border-surface-700 p-6 text-center">
          <button
            onClick={onClose}
            className="absolute top-4 right-4 p-1 rounded-lg text-gray-400 hover:text-white hover:bg-surface-800 transition-colors"
          >
            <X size={18} />
          </button>

          <div className="w-16 h-16 mx-auto mb-3 rounded-2xl bg-gradient-to-br from-amber-500 to-orange-600 flex items-center justify-center shadow-lg">
            {step === 'enter' ? <Lock size={28} className="text-white" /> :
             step === 'create' ? <Lock size={28} className="text-white" /> :
             <Lock size={28} className="text-white" />}
          </div>

          <h2 className="text-lg font-bold text-text-100">{titles[step]}</h2>
          <p className="text-sm text-text-400 mt-1">{subtitles[step]}</p>
        </div>

        {/* PIN Input */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div className="relative">
            <input
              ref={inputRef}
              type={showPin ? 'text' : 'password'}
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={8}
              value={currentValue}
              onChange={(e) => {
                const val = e.target.value.replace(/\D/g, '');
                setCurrentValue(val);
                setError('');
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSubmit();
              }}
              placeholder="••••"
              className="w-full text-center text-2xl tracking-[0.5em] font-mono px-4 py-4 bg-surface-800 border border-surface-700 rounded-xl text-text-100 focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 placeholder:tracking-[0.3em] placeholder:text-text-600"
              autoComplete="off"
            />
            <button
              type="button"
              onClick={() => setShowPin(!showPin)}
              className="absolute right-3 top-1/2 -translate-y-1/2 p-1.5 rounded-lg text-gray-500 hover:text-gray-300 transition-colors"
            >
              {showPin ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div>

          {/* Error */}
          {error && (
            <p className="text-sm text-red-400 text-center animate-fadeIn">{error}</p>
          )}

          {/* Actions */}
          <div className="flex gap-3">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-3 bg-surface-800 hover:bg-surface-700 text-text-200 rounded-xl font-medium transition-colors"
            >
              {t('common.cancel')}
            </button>
            <button
              type="submit"
              disabled={currentValue.length < 4}
              className="flex-1 px-4 py-3 bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 disabled:from-surface-700 disabled:to-surface-700 disabled:text-text-600 text-white rounded-xl font-semibold transition-all"
            >
              {step === 'confirm' ? t('mode.unlock') :
               step === 'create' ? t('onboarding.next') :
               t('mode.unlock')}
            </button>
          </div>

          {/* Back button for confirm step */}
          {step === 'confirm' && (
            <button
              type="button"
              onClick={() => { setStep('create'); setConfirmPin(''); setError(''); }}
              className="w-full text-sm text-text-500 hover:text-text-300 transition-colors"
            >
              {t('onboarding.back')}
            </button>
          )}
        </form>
      </div>
    </div>
  );
};

/**
 * VersionTapTarget — The "version label" that users tap 7 times.
 * Drop this into the bottom of SimpleSettings or the profile area.
 */
export const VersionTapTarget: React.FC<{
  onThresholdReached: () => void;
  className?: string;
}> = ({ onThresholdReached, className = '' }) => {
  const registerTap = useAppModeStore((s) => s.registerTap);
  const isAdvanced = useAppModeStore((s) => s.isAdvanced);

  const handleTap = () => {
    if (isAdvanced) return;  // Already in advanced mode
    const reached = registerTap();
    if (reached) {
      onThresholdReached();
    }
  };

  return (
    <button
      onClick={handleTap}
      className={`text-xs text-text-600 hover:text-text-500 transition-colors select-none cursor-default ${className}`}
      tabIndex={-1}
      aria-hidden="true"
    >
      Helen v1.0.0
    </button>
  );
};

export default AdminUnlockModal;
