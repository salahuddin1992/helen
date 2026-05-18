/**
 * ProfileSetupScreen.tsx — Name, avatar color, and password in one clean screen.
 *
 * Goals:
 *   - Minimal fields: name (required), display name (auto-filled), password
 *   - Visual avatar preview with selectable background colors
 *   - Large, touch-friendly inputs
 *   - Inline validation (no scary error modals)
 *   - Show/hide password toggle
 *   - Back button to return to welcome
 *
 * The username for registration = name (lowercased, spaces→underscores).
 * The display name defaults to the typed name but can be overridden.
 */

import React, { useState, useRef, useEffect } from 'react';
import { ArrowRight, ArrowLeft, Eye, EyeOff, User } from 'lucide-react';
import { t } from '@/i18n';
import { useOnboardingStore, AVATAR_COLORS } from '@/stores/onboarding.store';

const ProfileSetupScreen: React.FC = () => {
  const {
    userName,
    displayName,
    avatarColor,
    avatarInitials,
    password,
    confirmPassword,
    errors,
    selectedLanguage,
    setUserName,
    setDisplayName,
    setAvatarColor,
    setPassword,
    setConfirmPassword,
    validateProfile,
    clearError,
    nextStep,
    prevStep,
  } = useOnboardingStore();

  const [showPassword, setShowPassword] = useState(false);
  const [fadeIn, setFadeIn] = useState(false);
  const [showColorPicker, setShowColorPicker] = useState(false);
  const nameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const timer = setTimeout(() => setFadeIn(true), 50);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (fadeIn && nameInputRef.current) {
      nameInputRef.current.focus();
    }
  }, [fadeIn]);

  const handleContinue = () => {
    if (validateProfile()) {
      nextStep();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleContinue();
    }
  };

  const isRTL = selectedLanguage === 'ar';
  const ArrowForward = isRTL ? ArrowLeft : ArrowRight;

  return (
    <div
      className={`flex flex-col items-center min-h-screen px-6 py-10 transition-opacity duration-500 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      {/* ── Avatar Preview ───────────────────── */}
      <div className="relative mb-6 group">
        <button
          onClick={() => setShowColorPicker(!showColorPicker)}
          className="w-24 h-24 rounded-full flex items-center justify-center text-3xl font-bold text-white shadow-xl transition-transform hover:scale-105 active:scale-95 cursor-pointer"
          style={{ backgroundColor: avatarColor }}
          title={t('ob.change_color')}
        >
          {avatarInitials === '?' ? (
            <User size={36} className="text-white/70" />
          ) : (
            avatarInitials
          )}
        </button>
        <div className="absolute -bottom-1 -right-1 w-7 h-7 bg-surface-700 border-2 border-surface-900 rounded-full flex items-center justify-center">
          <span className="text-xs">🎨</span>
        </div>
      </div>

      {/* ── Color Picker (collapsible) ─────── */}
      {showColorPicker && (
        <div className="flex flex-wrap justify-center gap-2 mb-6 max-w-xs animate-fadeIn">
          {AVATAR_COLORS.map((color) => (
            <button
              key={color}
              onClick={() => {
                setAvatarColor(color);
                setShowColorPicker(false);
              }}
              className={`w-9 h-9 rounded-full transition-all duration-200 hover:scale-110 active:scale-95 ${
                avatarColor === color
                  ? 'ring-2 ring-white ring-offset-2 ring-offset-surface-900 scale-110'
                  : ''
              }`}
              style={{ backgroundColor: color }}
            />
          ))}
        </div>
      )}

      {/* ── Title ────────────────────────────── */}
      <div className="text-center mb-8">
        <h1 className="text-2xl font-bold text-white mb-2">
          {t('ob.profile_title')}
        </h1>
        <p className="text-gray-400 text-base">
          {t('ob.profile_subtitle')}
        </p>
      </div>

      {/* ── Form ─────────────────────────────── */}
      <div className="w-full max-w-sm space-y-4">
        {/* Name */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1.5">
            {t('ob.your_name')}
          </label>
          <input
            ref={nameInputRef}
            type="text"
            value={userName}
            onChange={(e) => {
              setUserName(e.target.value);
              clearError('userName');
            }}
            onKeyDown={handleKeyDown}
            placeholder={t('ob.name_placeholder')}
            className={`w-full px-5 py-4 bg-surface-800 border rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg ${
              errors.userName ? 'border-red-500/60' : 'border-surface-700'
            }`}
            autoComplete="off"
            maxLength={30}
          />
          {errors.userName && (
            <p className="mt-1.5 text-sm text-red-400">{t(errors.userName)}</p>
          )}
        </div>

        {/* Display Name (optional) */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1.5">
            {t('ob.display_name')}
            <span className="text-gray-600 ms-1">({t('ob.optional')})</span>
          </label>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={userName || t('ob.display_placeholder')}
            className="w-full px-5 py-4 bg-surface-800 border border-surface-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg"
            autoComplete="off"
            maxLength={40}
          />
        </div>

        {/* Password */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1.5">
            {t('ob.password')}
          </label>
          <div className="relative">
            <input
              type={showPassword ? 'text' : 'password'}
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                clearError('password');
              }}
              onKeyDown={handleKeyDown}
              placeholder="••••••••"
              className={`w-full px-5 py-4 bg-surface-800 border rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg pe-14 ${
                errors.password ? 'border-red-500/60' : 'border-surface-700'
              }`}
            />
            <button
              type="button"
              onClick={() => setShowPassword(!showPassword)}
              className="absolute end-4 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 transition-colors"
            >
              {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
            </button>
          </div>
          {errors.password ? (
            <p className="mt-1.5 text-sm text-red-400">{t(errors.password)}</p>
          ) : (
            <p className="mt-1.5 text-xs text-gray-600">{t('ob.password_hint')}</p>
          )}
        </div>

        {/* Confirm Password */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1.5">
            {t('ob.confirm_password')}
          </label>
          <input
            type={showPassword ? 'text' : 'password'}
            value={confirmPassword}
            onChange={(e) => {
              setConfirmPassword(e.target.value);
              clearError('confirmPassword');
            }}
            onKeyDown={handleKeyDown}
            placeholder="••••••••"
            className={`w-full px-5 py-4 bg-surface-800 border rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-lg ${
              errors.confirmPassword ? 'border-red-500/60' : 'border-surface-700'
            }`}
          />
          {errors.confirmPassword && (
            <p className="mt-1.5 text-sm text-red-400">{t(errors.confirmPassword)}</p>
          )}
        </div>
      </div>

      {/* ── Navigation ───────────────────────── */}
      <div className="w-full max-w-sm mt-8 space-y-3">
        <button
          onClick={handleContinue}
          className="w-full py-4 bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white font-semibold rounded-xl transition-all flex items-center justify-center gap-3 text-lg shadow-lg shadow-blue-600/20"
        >
          {t('ob.continue')}
          <ArrowForward size={20} />
        </button>

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

export default ProfileSetupScreen;
