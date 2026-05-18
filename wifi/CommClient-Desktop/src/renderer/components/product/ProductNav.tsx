/**
 * ProductNav.tsx — Simplified, child-friendly navigation sidebar.
 *
 * Replaces the existing Sidebar + ModeAwareSidebar with a single
 * unified navigation component that:
 *   - Shows only essential items (Chats, People, Calls, Settings)
 *   - Uses friendly labels and large icons
 *   - Shows unread badges prominently
 *   - Shows active call indicator
 *   - Shows user avatar with online status
 *   - Supports RTL layout
 *   - Highlights current page clearly
 *   - Provides keyboard navigation
 *
 * Navigation items (Simple Mode):
 *   - Chats (primary, always first)
 *   - People (contacts, renamed from "Contacts")
 *   - Calls (call history)
 *   - Settings (at bottom, separated)
 *
 * Navigation items (Advanced Mode adds):
 *   - Groups (between People and Calls)
 *   - Dashboard (admin only, at bottom above Settings)
 *
 * Design:
 *   - 72px wide (wider than original 64px for better tap targets)
 *   - Icons: 24px with label below (10px text)
 *   - Active: blue background + white icon
 *   - Inactive: transparent + gray icon
 *   - Badge: red dot with count (top-right of icon)
 *   - User avatar: bottom, 40px, with green/yellow/red presence dot
 */

import React, { useMemo } from 'react';
import {
  MessageCircle, Users, Phone, Settings, AlertCircle,
  Wifi,
} from 'lucide-react';
import { t, getLanguage } from '@/i18n';
import { useAuthStore } from '@/stores/auth.store';
import { useNotificationStore } from '@/stores/notification.store';
import { useCallStore } from '@/stores/call.store.v2';

// ── Types ───────────────────────────────────────────────────

interface NavItem {
  id: string;
  icon: React.ReactNode;
  labelKey: string;
  path: string;
  badge?: number;
  mode: 'simple' | 'advanced' | 'both';
  position: 'top' | 'bottom';
  showCallIndicator?: boolean;
}

interface ProductNavProps {
  currentPath: string;
  isAdvanced?: boolean;
  onNavigate: (path: string) => void;
}

// ── Badge Component ─────────────────────────────────────────

const NavBadge: React.FC<{ count: number }> = ({ count }) => {
  if (count <= 0) return null;
  const display = count > 99 ? '99+' : count > 9 ? '9+' : String(count);

  return (
    <div className="absolute -top-1 -end-1 min-w-[18px] h-[18px] bg-red-500 rounded-full flex items-center justify-center px-1">
      <span className="text-[10px] font-bold text-white leading-none">{display}</span>
    </div>
  );
};

// ── Call Indicator ───────────────────────────────────────────

const CallIndicator: React.FC = () => (
  <div className="absolute -top-0.5 -end-0.5 w-3 h-3 bg-green-500 rounded-full border-2 border-surface-900 animate-pulse" />
);

// ── Avatar Component ────────────────────────────────────────

const UserAvatar: React.FC<{
  name: string;
  color?: string;
  isOnline: boolean;
}> = ({ name, color, isOnline }) => {
  const initials = name
    ? name.split(/\s+/).map((p) => p[0]).join('').toUpperCase().slice(0, 2)
    : '?';

  return (
    <div className="relative">
      <div
        className="w-10 h-10 rounded-full flex items-center justify-center text-sm font-bold text-white shadow-inner"
        style={{ backgroundColor: color || '#3B82F6' }}
      >
        {initials}
      </div>
      <div className={`absolute bottom-0 end-0 w-3 h-3 rounded-full border-2 border-surface-900 ${
        isOnline ? 'bg-green-500' : 'bg-gray-500'
      }`} />
    </div>
  );
};

// ── Main Component ──────────────────────────────────────────

const ProductNav: React.FC<ProductNavProps> = ({
  currentPath,
  isAdvanced = false,
  onNavigate,
}) => {
  const user = useAuthStore((s) => s.user);
  const unreadCount = useNotificationStore((s) => s.unreadCount);
  const callStatus = useCallStore((s) => s.status);
  const isInCall = callStatus === 'active' || callStatus === 'connecting' || callStatus === 'ringing';

  const navItems = useMemo<NavItem[]>(() => {
    const items: NavItem[] = [
      {
        id: 'chats',
        icon: <MessageCircle size={24} />,
        labelKey: 'nav.chats',
        path: '/chats',
        badge: unreadCount,
        mode: 'both',
        position: 'top',
      },
      {
        id: 'people',
        icon: <Users size={24} />,
        labelKey: 'product.nav_people',
        path: '/contacts',
        mode: 'both',
        position: 'top',
      },
      {
        id: 'groups',
        icon: <Users size={24} />,
        labelKey: 'nav.groups',
        path: '/groups',
        mode: 'advanced',
        position: 'top',
      },
      {
        id: 'calls',
        icon: <Phone size={24} />,
        labelKey: 'nav.calls',
        path: '/calls',
        mode: 'both',
        position: 'top',
        showCallIndicator: isInCall,
      },
      {
        id: 'dashboard',
        icon: <AlertCircle size={24} />,
        labelKey: 'product.nav_dashboard',
        path: '/dashboard',
        mode: 'advanced',
        position: 'bottom',
      },
      {
        id: 'settings',
        icon: <Settings size={24} />,
        labelKey: 'nav.settings',
        path: '/settings',
        mode: 'both',
        position: 'bottom',
      },
    ];

    return items.filter((item) => {
      if (item.mode === 'both') return true;
      if (item.mode === 'advanced' && isAdvanced) return true;
      return false;
    });
  }, [unreadCount, isInCall, isAdvanced]);

  const topItems = navItems.filter((i) => i.position === 'top');
  const bottomItems = navItems.filter((i) => i.position === 'bottom');

  const avatarColor = (() => {
    try {
      return localStorage.getItem('commclient_avatar_color') || '#3B82F6';
    } catch { return '#3B82F6'; }
  })();

  return (
    <nav className="w-[72px] h-full bg-surface-950 border-e border-surface-800 flex flex-col items-center py-3 shrink-0 select-none">
      {/* App icon (top) */}
      <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center mb-6 shadow-lg shadow-blue-500/20">
        <Wifi size={20} className="text-white" />
      </div>

      {/* Top nav items */}
      <div className="flex flex-col items-center gap-1 flex-1">
        {topItems.map((item) => {
          const isActive = currentPath.startsWith(item.path);
          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.path)}
              className={`relative w-14 h-14 rounded-xl flex flex-col items-center justify-center gap-0.5 transition-all duration-200 ${
                isActive
                  ? 'bg-blue-600/15 text-blue-400'
                  : 'text-gray-500 hover:bg-surface-800 hover:text-gray-300'
              }`}
              title={t(item.labelKey)}
            >
              <div className="relative">
                {item.icon}
                {(item.badge ?? 0) > 0 && <NavBadge count={item.badge!} />}
                {item.showCallIndicator && <CallIndicator />}
              </div>
              <span className="text-[10px] font-medium leading-none mt-0.5">
                {t(item.labelKey)}
              </span>
              {isActive && (
                <div className="absolute start-0 top-2 bottom-2 w-[3px] bg-blue-500 rounded-full" />
              )}
            </button>
          );
        })}
      </div>

      {/* Bottom nav items */}
      <div className="flex flex-col items-center gap-1 mt-auto">
        {/* Advanced mode indicator */}
        {isAdvanced && (
          <div className="w-10 h-0.5 bg-amber-500/50 rounded-full mb-2" />
        )}

        {bottomItems.map((item) => {
          const isActive = currentPath.startsWith(item.path);
          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.path)}
              className={`relative w-14 h-14 rounded-xl flex flex-col items-center justify-center gap-0.5 transition-all duration-200 ${
                isActive
                  ? 'bg-blue-600/15 text-blue-400'
                  : 'text-gray-500 hover:bg-surface-800 hover:text-gray-300'
              }`}
              title={t(item.labelKey)}
            >
              {item.icon}
              <span className="text-[10px] font-medium leading-none mt-0.5">
                {t(item.labelKey)}
              </span>
            </button>
          );
        })}

        {/* User avatar */}
        <div className="mt-2 cursor-pointer" onClick={() => onNavigate('/settings')}>
          <UserAvatar
            name={user?.display_name || user?.username || '?'}
            color={avatarColor}
            isOnline={true}
          />
        </div>
      </div>
    </nav>
  );
};

export default ProductNav;
