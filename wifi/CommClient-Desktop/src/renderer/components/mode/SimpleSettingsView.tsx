/**
 * SimpleSettingsView — Settings panel for Simple Mode.
 *
 * Only shows:
 *   - Profile (name, avatar)
 *   - Language
 *   - Theme (dark/light)
 *   - Notifications on/off
 *   - Sign out
 *   - Version label (secret tap target for admin unlock)
 *
 * No server URLs, no ports, no diagnostics, no technical terms.
 */

import React, { useState } from 'react';
import {
  User, Moon, Sun, Bell, BellOff, Globe, LogOut,
  Edit, AlertTriangle, X, ChevronRight,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import { useSettingsStore } from '@/stores/settings.store';
import { t, setLanguage } from '@/i18n';
import { VersionTapTarget, AdminUnlockModal } from './AdminUnlockModal';
import { useAppModeStore } from '@/stores/app-mode.store';
import { Handle } from '@/components/common/Handle';

const SimpleSettingsView: React.FC = () => {
  const { user, logout } = useAuthStore();
  const settings = useSettingsStore((s) => s.settings);
  const updateSettings = useSettingsStore((s) => s.update);

  const [isEditingName, setIsEditingName] = useState(false);
  const [newDisplayName, setNewDisplayName] = useState(user?.display_name || '');
  const [showUnlockModal, setShowUnlockModal] = useState(false);

  const handleThemeToggle = () => {
    const next = settings.theme === 'dark' ? 'light' : 'dark';
    updateSettings({ theme: next });
  };

  const handleLanguageToggle = () => {
    const next = settings.language === 'en' ? 'ar' : 'en';
    updateSettings({ language: next });
    setLanguage(next);
  };

  const handleNotificationsToggle = () => {
    updateSettings({ notifications: !settings.notifications });
  };

  const handleSaveName = () => {
    if (newDisplayName.trim().length >= 2) {
      // API call to update display name would go here
      setIsEditingName(false);
    }
  };

  const handleLogout = () => {
    logout();
  };

  return (
    <div className="w-full h-full bg-surface-950 overflow-y-auto">
      <div className="max-w-md mx-auto py-6 px-4">

        {/* Page title */}
        <h1 className="text-2xl font-bold text-text-100 mb-6">{t('nav.settings')}</h1>

        {/* ── Profile Card ─────────────────────────── */}
        <div className="mb-4 p-5 bg-surface-900 border border-surface-800 rounded-2xl">
          <div className="flex items-center gap-4">
            <div className="w-16 h-16 rounded-full bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center text-xl font-bold text-white shrink-0">
              {user?.display_name?.charAt(0)?.toUpperCase() || '?'}
            </div>
            <div className="flex-1 min-w-0">
              {isEditingName ? (
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={newDisplayName}
                    onChange={(e) => setNewDisplayName(e.target.value)}
                    className="flex-1 px-3 py-1.5 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                    autoFocus
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') handleSaveName();
                      if (e.key === 'Escape') setIsEditingName(false);
                    }}
                  />
                  <button onClick={handleSaveName} className="p-1.5 text-green-400 hover:bg-surface-800 rounded-lg"><AlertTriangle size={16} /></button>
                  <button onClick={() => setIsEditingName(false)} className="p-1.5 text-gray-400 hover:bg-surface-800 rounded-lg"><X size={16} /></button>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <p className="text-lg font-semibold text-text-100 truncate">{user?.display_name}</p>
                  <button onClick={() => { setNewDisplayName(user?.display_name || ''); setIsEditingName(true); }} className="p-1 text-blue-400 hover:bg-surface-800 rounded-lg"><Edit size={14} /></button>
                </div>
              )}
              <Handle user={user as any} className="text-sm text-text-500 block" />
            </div>
          </div>
        </div>

        {/* ── Setting Rows ─────────────────────────── */}
        <div className="bg-surface-900 border border-surface-800 rounded-2xl divide-y divide-surface-800 overflow-hidden mb-4">

          {/* Theme */}
          <SettingRow
            icon={settings.theme === 'dark' ? <Moon size={20} /> : <Sun size={20} />}
            label={t('settings.theme')}
            value={settings.theme === 'dark' ? t('settings.dark') : t('settings.light')}
            onClick={handleThemeToggle}
          />

          {/* Language */}
          <SettingRow
            icon={<Globe size={20} />}
            label={t('settings.language')}
            value={settings.language === 'en' ? 'English' : 'العربية'}
            onClick={handleLanguageToggle}
          />

          {/* Notifications */}
          <SettingRow
            icon={settings.notifications ? <Bell size={20} /> : <BellOff size={20} />}
            label={t('settings.notifications')}
            value={settings.notifications ? t('mode.on') : t('mode.off')}
            onClick={handleNotificationsToggle}
            valueColor={settings.notifications ? 'text-green-400' : 'text-gray-500'}
          />
        </div>

        {/* ── Sign Out ─────────────────────────────── */}
        <button
          onClick={handleLogout}
          className="w-full flex items-center justify-center gap-2 p-4 bg-surface-900 border border-red-900/30 rounded-2xl text-red-400 hover:bg-red-600/10 font-medium transition-colors mb-8"
        >
          <LogOut size={18} />
          {t('settings.logout')}
        </button>

        {/* ── Version Label (secret tap target) ───── */}
        <div className="text-center">
          <VersionTapTarget
            onThresholdReached={() => setShowUnlockModal(true)}
          />
        </div>
      </div>

      {/* Admin Unlock Modal */}
      <AdminUnlockModal
        isOpen={showUnlockModal}
        onClose={() => setShowUnlockModal(false)}
        onUnlocked={() => {
          setShowUnlockModal(false);
        }}
      />
    </div>
  );
};

// ── Setting Row Component ──────────────────────────────

const SettingRow: React.FC<{
  icon: React.ReactNode;
  label: string;
  value: string;
  onClick: () => void;
  valueColor?: string;
}> = ({ icon, label, value, onClick, valueColor = 'text-text-300' }) => (
  <button
    onClick={onClick}
    className="w-full flex items-center gap-4 px-5 py-4 hover:bg-surface-800/50 transition-colors text-left"
  >
    <span className="text-text-400">{icon}</span>
    <span className="flex-1 text-text-100 font-medium">{label}</span>
    <span className={`text-sm ${valueColor}`}>{value}</span>
    <ChevronRight size={16} className="text-text-600" />
  </button>
);

export default SimpleSettingsView;
