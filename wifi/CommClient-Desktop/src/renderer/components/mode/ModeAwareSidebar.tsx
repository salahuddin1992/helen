/**
 * ModeAwareSidebar — Renders different navigation items based on app mode.
 *
 * Simple Mode navigation:
 *   - Chats (MessageSquare)
 *   - Contacts (Users)
 *   - Calls (Phone)
 *   - Notifications (Bell) — with badge
 *   - Settings (Settings)
 *
 * Advanced Mode adds:
 *   - Groups (Users) — full group management
 *   - Dashboard (AlertCircle) — diagnostics/tools route
 *
 * The sidebar also shows an amber dot on the Settings icon when in Advanced Mode
 * to remind the admin they're in elevated mode.
 */

import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  MessageSquare, Users, Phone, Settings, Bell,
  AlertCircle, Lock,
} from 'lucide-react';
import { Avatar } from '../common/Avatar';
import { useAuthStore } from '@/stores/auth.store';
import { useNotificationStore } from '@/stores/notification.store';
import { useAppModeStore } from '@/stores/app-mode.store';
import { t } from '@/i18n';

// ── Notification Badge ──────────────────────────────────

const NotificationBadge: React.FC<{ count: number }> = ({ count }) => {
  if (count === 0) return null;
  return (
    <div className="absolute top-1 right-1 w-5 h-5 rounded-full bg-red-500 text-white text-xs font-semibold flex items-center justify-center">
      {count > 9 ? '9+' : count}
    </div>
  );
};

// ── Mode Badge (amber dot for advanced) ─────────────────

const ModeBadge: React.FC = () => (
  <div className="absolute top-1 right-1 w-2.5 h-2.5 rounded-full bg-amber-500 ring-2 ring-surface-950" />
);

// ── Nav Item Definition ─────────────────────────────────

interface NavItem {
  to: string;
  icon: any;
  label: string;
  badge?: number;
  modeBadge?: boolean;
  mode?: 'simple' | 'advanced' | 'both';
}

// ── Component ───────────────────────────────────────────

export const ModeAwareSidebar: React.FC = () => {
  const user = useAuthStore((s) => s.user);
  const unreadCount = useNotificationStore((s) => s.unreadCount);
  const isAdvanced = useAppModeStore((s) => s.isAdvanced);

  const navItems: NavItem[] = [
    {
      to: '/chats',
      icon: MessageSquare,
      label: t('nav.chats'),
      mode: 'both',
    },
    {
      to: '/contacts',
      icon: Users,
      label: t('nav.contacts'),
      mode: 'both',
    },
    {
      to: '/calls',
      icon: Phone,
      label: t('nav.calls'),
      mode: 'both',
    },
    {
      to: '/notifications',
      icon: Bell,
      label: t('notifications.title'),
      badge: unreadCount,
      mode: 'both',
    },
    // Advanced-only: full Group Manager
    {
      to: '/groups',
      icon: Users,
      label: t('nav.groups'),
      mode: 'advanced',
    },
    // Advanced-only: Dashboard / diagnostics
    {
      to: '/dashboard',
      icon: AlertCircle,
      label: t('mode.dashboard'),
      mode: 'advanced',
    },
    {
      to: '/settings',
      icon: Settings,
      label: t('nav.settings'),
      modeBadge: isAdvanced,
      mode: 'both',
    },
  ];

  // Filter items based on mode
  const visibleItems = navItems.filter(item => {
    if (item.mode === 'both') return true;
    if (item.mode === 'advanced') return isAdvanced;
    if (item.mode === 'simple') return !isAdvanced;
    return true;
  });

  return (
    <div className="w-16 bg-surface-950 border-r border-surface-800 flex flex-col items-center py-4 gap-4">
      {/* Advanced mode indicator at top */}
      {isAdvanced && (
        <div className="w-10 h-1 rounded-full bg-gradient-to-r from-amber-500 to-orange-500 mb-1" title={t('mode.advanced')} />
      )}

      {/* Navigation links */}
      <nav className="flex flex-col gap-2">
        {visibleItems.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `relative p-3 rounded-lg transition-colors group ${
                  isActive
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-400 hover:text-white hover:bg-surface-800'
                }`
              }
              title={item.label}
            >
              <Icon size={24} />
              {item.badge !== undefined && item.badge > 0 && <NotificationBadge count={item.badge} />}
              {item.modeBadge && <ModeBadge />}
              {/* Active indicator */}
              <div className="absolute left-0 top-0 bottom-0 w-1 bg-blue-500 rounded-r hidden group-[.active]:block" />
            </NavLink>
          );
        })}
      </nav>

      {/* Spacer */}
      <div className="flex-1" />

      {/* User profile at bottom */}
      {user && (
        <div className="w-full flex flex-col items-center gap-2 p-2 rounded-lg hover:bg-surface-800 transition-colors cursor-pointer group" title={user.display_name}>
          <Avatar
            src={user.avatar_url}
            name={user.display_name}
            status={user.status}
            size="md"
          />
          <div className="text-xs text-gray-400 font-medium text-center px-1 line-clamp-2">
            {user.display_name}
          </div>
        </div>
      )}
    </div>
  );
};

export default ModeAwareSidebar;
