/**
 * OnboardingWizard.tsx — 3-step first-run experience.
 *
 * Steps:
 *   1. Welcome + Choose name (username + display name)
 *   2. Create password (+ confirm)
 *   3. Done — auto-transitions to app
 *
 * Design principles:
 *   - No technical jargon (no "server URL", no "username")
 *   - Very large inputs and buttons (child-friendly)
 *   - Auto-connects to discovered server (hidden from user)
 *   - Friendly, encouraging language
 *   - Arabic/English ready
 */

import React, { useState, useEffect } from 'react';
import { Wifi, ArrowRight, Check, Loader2, Eye, EyeOff } from 'lucide-react';
import { t } from '@/i18n';
import { useAuthStore } from '@/stores/auth.store';
import { useAppStore, markOnboardingComplete } from '@/stores/app.store';

interface OnboardingWizardProps {
  serverUrl: string;
  onComplete: () => void;
}

const OnboardingWizard: React.FC<OnboardingWizardProps> = ({ serverUrl, onComplete }) => {
  const [step, setStep] = useState(1);
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [fadeIn, setFadeIn] = useState(false);

  const { register, setServerUrl: setAuthServerUrl } = useAuthStore();

  useEffect(() => {
    // Fade in animation
    const t = setTimeout(() => setFadeIn(true), 50);
    return () => clearTimeout(t);
  }, []);

  // Auto-sync display name from name if user hasn't customized it
  const [displayNameTouched, setDisplayNameTouched] = useState(false);
  useEffect(() => {
    if (!displayNameTouched && name) {
      setDisplayName(name);
    }
  }, [name, displayNameTouched]);

  const handleStep1Next = () => {
    setError('');
    if (!name.trim()) {
      setError(t('onboarding.name_required') || 'Please enter your name');
      return;
    }
    if (name.trim().length < 2) {
      setError(t('onboarding.name_too_short') || 'Name is too short');
      return;
    }
    setStep(2);
  };

  const handleStep2Submit = async () => {
    setError('');

    if (!password) {
      setError(t('onboarding.password_required') || 'Please choose a password');
      return;
    }
    if (password.length < 6) {
      setError(t('onboarding.password_too_short') || 'Password must be at least 6 characters');
      return;
    }
    if (password !== confirmPassword) {
      setError(t('onboarding.password_mismatch') || 'Passwords do not match');
      return;
    }

    setIsSubmitting(true);
    try {
      // Set the server URL from auto-discovery
      setAuthServerUrl(serverUrl);
      // Register the user
      await register(name.trim(), displayName.trim() || name.trim(), password);

      // Mark onboarding as done
      markOnboardingComplete();

      // Show success step briefly, then transition
      setStep(3);
      setTimeout(() => {
        onComplete();
      }, 1500);
    } catch (e: any) {
      setError(e?.message || t('onboarding.register_failed') || 'Something went wrong. Please try again.');
      setIsSubmitting(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent, action: () => void) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      action();
    }
  };

  return (
    <div
      className={`fixed inset-0 z-[90] flex items-center justify-center bg-gradient-to-br from-surface-950 via-surface-900 to-surface-950 select-none transition-opacity duration-500 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      <div className="w-full max-w-md px-6">
        {/* Step indicator */}
        <div className="flex items-center justify-center gap-2 mb-8">
          {[1, 2, 3].map((s) => (
            <div
              key={s}
              className={`h-2 rounded-full transition-all duration-500 ${
                s === step
                  ? 'w-8 bg-blue-500'
                  : s < step
                    ? 'w-2 bg-blue-400'
                    : 'w-2 bg-surface-700'
              }`}
            />
          ))}
        </div>

        {/* ─── Step 1: Welcome + Name ─── */}
        {step === 1 && (
          <div className="animate-fadeIn">
            {/* Friendly header */}
            <div className="text-center mb-8">
              <div className="w-16 h-16 rounded-full bg-blue-600/20 flex items-center justify-center mx-auto mb-4">
                <Wifi size={28} className="text-blue-400" />
              </div>
              <h1 className="text-2xl font-bold text-white mb-2">
                {t('onboarding.welcome') || 'Welcome!'}
              </h1>
              <p className="text-gray-400 text-base">
                {t('onboarding.welcome_sub') || "Let's get you set up in just a moment"}
              </p>
            </div>

            {/* Name input */}
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-300 mb-2">
                  {t('onboarding.your_name') || 'What should we call you?'}
                </label>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => handleKeyDown(e, handleStep1Next)}
                  placeholder={t('onboarding.name_placeholder') || 'Your name'}
                  className="w-full px-5 py-4 bg-surface-800 border border-surface-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg"
                  autoFocus
                  autoComplete="off"
                />
              </div>

              {/* Display name (optional, collapsed by default) */}
              <div>
                <label className="block text-sm font-medium text-gray-300 mb-2">
                  {t('onboarding.display_name') || 'Display name (shown to others)'}
                </label>
                <input
                  type="text"
                  value={displayName}
                  onChange={(e) => {
                    setDisplayName(e.target.value);
                    setDisplayNameTouched(true);
                  }}
                  onKeyDown={(e) => handleKeyDown(e, handleStep1Next)}
                  placeholder={name || (t('onboarding.display_placeholder') || 'Same as your name')}
                  className="w-full px-5 py-4 bg-surface-800 border border-surface-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg"
                  autoComplete="off"
                />
              </div>

              {/* Error */}
              {error && (
                <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-xl">
                  <p className="text-sm text-red-400">{error}</p>
                </div>
              )}

              {/* Next button */}
              <button
                onClick={handleStep1Next}
                className="w-full py-4 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-3 text-lg mt-4"
              >
                {t('onboarding.next') || 'Continue'}
                <ArrowRight size={20} />
              </button>
            </div>
          </div>
        )}

        {/* ─── Step 2: Password ─── */}
        {step === 2 && (
          <div className="animate-fadeIn">
            <div className="text-center mb-8">
              <h1 className="text-2xl font-bold text-white mb-2">
                {t('onboarding.create_password') || 'Create a password'}
              </h1>
              <p className="text-gray-400 text-base">
                {t('onboarding.password_sub') || 'This keeps your account secure on this network'}
              </p>
            </div>

            <div className="space-y-4">
              {/* Password input */}
              <div className="relative">
                <label className="block text-sm font-medium text-gray-300 mb-2">
                  {t('onboarding.password_label') || 'Password'}
                </label>
                <div className="relative">
                  <input
                    type={showPassword ? 'text' : 'password'}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••"
                    className="w-full px-5 py-4 bg-surface-800 border border-surface-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg pr-14"
                    autoFocus
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-4 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 transition-colors"
                  >
                    {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
                  </button>
                </div>
              </div>

              {/* Confirm password */}
              <div>
                <label className="block text-sm font-medium text-gray-300 mb-2">
                  {t('onboarding.confirm_password') || 'Confirm password'}
                </label>
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  onKeyDown={(e) => handleKeyDown(e, handleStep2Submit)}
                  placeholder="••••••••"
                  className="w-full px-5 py-4 bg-surface-800 border border-surface-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg"
                />
              </div>

              {/* Password strength hint */}
              <p className="text-xs text-gray-500">
                {t('onboarding.password_hint') || 'At least 6 characters'}
              </p>

              {/* Error */}
              {error && (
                <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-xl">
                  <p className="text-sm text-red-400">{error}</p>
                </div>
              )}

              {/* Submit button */}
              <button
                onClick={handleStep2Submit}
                disabled={isSubmitting}
                className="w-full py-4 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-600/50 disabled:cursor-not-allowed text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-3 text-lg mt-4"
              >
                {isSubmitting ? (
                  <>
                    <Loader2 size={20} className="animate-spin" />
                    {t('onboarding.creating') || 'Setting up...'}
                  </>
                ) : (
                  <>
                    {t('onboarding.get_started') || 'Get started'}
                    <ArrowRight size={20} />
                  </>
                )}
              </button>

              {/* Back button */}
              <button
                onClick={() => { setStep(1); setError(''); }}
                disabled={isSubmitting}
                className="w-full py-2 text-gray-500 hover:text-gray-300 text-sm transition-colors"
              >
                {t('onboarding.back') || '← Back'}
              </button>
            </div>
          </div>
        )}

        {/* ─── Step 3: Done ─── */}
        {step === 3 && (
          <div className="animate-fadeIn text-center">
            <div className="w-20 h-20 rounded-full bg-green-600/20 flex items-center justify-center mx-auto mb-6">
              <Check size={36} className="text-green-400" />
            </div>
            <h1 className="text-2xl font-bold text-white mb-2">
              {t('onboarding.all_set') || "You're all set!"}
            </h1>
            <p className="text-gray-400 text-base">
              {t('onboarding.launching') || 'Launching Helen...'}
            </p>
          </div>
        )}

        {/* Server status (subtle, bottom) */}
        <div className="mt-8 text-center">
          <p className="text-xs text-gray-700 flex items-center justify-center gap-1">
            <Wifi size={10} />
            {t('onboarding.auto_connected') || 'Automatically connected to your local network'}
          </p>
        </div>
      </div>
    </div>
  );
};

export default OnboardingWizard;
