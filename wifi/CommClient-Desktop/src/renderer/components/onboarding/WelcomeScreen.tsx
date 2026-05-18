/**
 * WelcomeScreen.tsx — First screen the user ever sees.
 *
 * Goals:
 *   - Warm, inviting first impression (no technical language)
 *   - Language selection (English/Arabic) with instant visual feedback
 *   - Single large CTA button to proceed
 *   - Animated entrance for delight
 *   - RTL-aware layout
 *
 * Design:
 *   - Full-screen gradient background matching splash
 *   - Animated Wifi icon with gentle pulse
 *   - Two language buttons (flag-free, text-only to avoid political sensitivity)
 *   - "Get Started" button at bottom
 */

import React, { useEffect, useState } from 'react';
import { Wifi, Globe, ArrowRight, ArrowLeft } from 'lucide-react';
import { t, setLanguage as setI18nLanguage } from '@/i18n';
import { useOnboardingStore } from '@/stores/onboarding.store';

const WelcomeScreen: React.FC = () => {
  const {
    selectedLanguage,
    setLanguage,
    nextStep,
  } = useOnboardingStore();

  const [fadeIn, setFadeIn] = useState(false);
  const [logoReady, setLogoReady] = useState(false);
  const [contentReady, setContentReady] = useState(false);

  useEffect(() => {
    const t1 = setTimeout(() => setFadeIn(true), 50);
    const t2 = setTimeout(() => setLogoReady(true), 300);
    const t3 = setTimeout(() => setContentReady(true), 600);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      clearTimeout(t3);
    };
  }, []);

  const handleLanguageChange = (lang: 'en' | 'ar') => {
    setLanguage(lang);
    setI18nLanguage(lang);
    document.documentElement.dir = lang === 'ar' ? 'rtl' : 'ltr';
    document.documentElement.lang = lang;
  };

  const handleContinue = () => {
    nextStep();
  };

  const isRTL = selectedLanguage === 'ar';
  const ArrowIcon = isRTL ? ArrowLeft : ArrowRight;

  return (
    <div
      className={`flex flex-col items-center justify-center min-h-screen px-6 transition-opacity duration-700 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      {/* ── Animated Logo ──────────────────────── */}
      <div
        className={`relative mb-8 transition-all duration-700 ease-out ${
          logoReady ? 'opacity-100 scale-100' : 'opacity-0 scale-90'
        }`}
      >
        <div className="w-28 h-28 rounded-full bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center shadow-2xl shadow-blue-500/30">
          <Wifi size={52} className="text-white" />
        </div>
        {/* Gentle pulse rings */}
        <div className="absolute inset-0 rounded-full border-2 border-blue-400/30 animate-ping" style={{ animationDuration: '2.5s' }} />
        <div className="absolute inset-0 rounded-full border border-blue-400/15 animate-ping" style={{ animationDuration: '2.5s', animationDelay: '0.8s' }} />
      </div>

      {/* ── Title + Subtitle ──────────────────── */}
      <div
        className={`text-center mb-10 transition-all duration-700 delay-100 ${
          contentReady ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-4'
        }`}
      >
        <h1 className="text-3xl font-bold text-white mb-3">
          {t('ob.welcome_title')}
        </h1>
        <p className="text-gray-400 text-lg max-w-sm mx-auto leading-relaxed">
          {t('ob.welcome_subtitle')}
        </p>
      </div>

      {/* ── Language Selection ─────────────────── */}
      <div
        className={`w-full max-w-xs mb-10 transition-all duration-700 delay-200 ${
          contentReady ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-4'
        }`}
      >
        <div className="flex items-center justify-center gap-2 mb-3">
          <Globe size={16} className="text-gray-500" />
          <span className="text-sm text-gray-500">
            {t('ob.choose_language')}
          </span>
        </div>

        <div className="flex gap-3">
          <button
            onClick={() => handleLanguageChange('en')}
            className={`flex-1 py-3.5 px-4 rounded-xl font-medium text-base transition-all duration-300 ${
              selectedLanguage === 'en'
                ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/30 scale-[1.02]'
                : 'bg-surface-800 text-gray-400 hover:bg-surface-700 hover:text-gray-300'
            }`}
          >
            English
          </button>
          <button
            onClick={() => handleLanguageChange('ar')}
            className={`flex-1 py-3.5 px-4 rounded-xl font-medium text-base transition-all duration-300 ${
              selectedLanguage === 'ar'
                ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/30 scale-[1.02]'
                : 'bg-surface-800 text-gray-400 hover:bg-surface-700 hover:text-gray-300'
            }`}
          >
            العربية
          </button>
        </div>
      </div>

      {/* ── Get Started Button ─────────────────── */}
      <div
        className={`w-full max-w-xs transition-all duration-700 delay-300 ${
          contentReady ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-4'
        }`}
      >
        <button
          onClick={handleContinue}
          className="w-full py-4 bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white font-semibold rounded-xl transition-all duration-200 flex items-center justify-center gap-3 text-lg shadow-lg shadow-blue-600/20 hover:shadow-blue-600/30"
        >
          {t('ob.get_started')}
          <ArrowIcon size={20} />
        </button>
      </div>

      {/* ── Bottom tagline ─────────────────────── */}
      <div className="mt-8">
        <p className="text-xs text-gray-700 flex items-center gap-1.5">
          <Wifi size={10} />
          {t('ob.works_on_wifi')}
        </p>
      </div>
    </div>
  );
};

export default WelcomeScreen;
