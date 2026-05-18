import React, { useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { MessageSquare, Users, Phone, Settings, Bell, ShieldCheck, Bookmark, Copy, Check } from 'lucide-react';
import toast from 'react-hot-toast';
import { Avatar } from '../common/Avatar';
import { useAuthStore } from '@/stores/auth.store';
import { useNotificationStore } from '@/stores/notification.store';
import { usePresenceStore, type SelfPresence } from '@/stores/presence.store';
import { socketManager } from '@/services/socket.manager';
import { t } from '@/i18n';

const NotificationBadge: React.FC<{ count: number }> = ({ count }) => {
  if (count === 0) return null;
  return (
    <div className="absolute top-1 right-1 w-5 h-5 rounded-full bg-red-500 text-white text-xs font-semibold flex items-center justify-center">
      {count > 9 ? '9+' : count}
    </div>
  );
};

/**
 * ConnectionQualityDot — small green/yellow/red dot below the avatar that
 * reflects the live socket health. Uses socket.io ping/pong RTT to grade
 * the link without needing any extra server-side wiring.
 *
 * Three buckets:
 *   ≤ 80 ms  → green (excellent)
 *   ≤ 200 ms → yellow (acceptable)
 *   > 200 ms → red (degraded)
 *   no socket → grey (offline)
 *
 * Sampling is once every 8 s and the value is shown as a tooltip so power
 * users can see the actual latency on hover. Cheap enough to leave on by
 * default; turning it off would require a settings flag we can add later.
 */
const ConnectionQualityDot: React.FC = () => {
  const [rtt, setRtt] = useState<number | null>(null);
  const [connected, setConnected] = useState<boolean>(false);
  // Wire the dot to the user's chosen presence so when the link is
  // healthy the dot shows their advertised state (green/amber/red)
  // instead of the previous flat grey "Connected, latency unknown"
  // colour. The detailed connection status + RTT still live in the
  // title-bar pill — this dot's job is to mirror the picker.
  const myPresence = usePresenceStore((s) => s.status);

  useEffect(() => {
    let cancelled = false;
    const measure = async () => {
      if (!socketManager.isConnected()) {
        if (!cancelled) { setConnected(false); setRtt(null); }
        return;
      }
      const start = performance.now();
      try {
        // Use a benign typing-stop emit as a ping. The server replies
        // promptly to such emits and the round-trip latency is what we
        // want; the actual ack content is irrelevant for this gauge.
        await socketManager.emit('chat:typing_stop', { channel_id: '' }, 2000);
        const elapsed = performance.now() - start;
        if (!cancelled) { setConnected(true); setRtt(Math.round(elapsed)); }
      } catch {
        // Server may not implement an ack on typing_stop — that still
        // counts the socket as healthy if isConnected() is true.
        if (!cancelled) { setConnected(true); setRtt(null); }
      }
    };
    measure();
    const id = setInterval(measure, 8000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Disconnected → flat grey, no presence info worth advertising.
  // Connected → presence-driven colour (matches the avatar ring + the
  // "Helen" wordmark). RTT moves into the tooltip so the diagnostic
  // is still one hover away.
  const presenceDot: Record<SelfPresence, string> = {
    online: 'bg-green-400',
    away:   'bg-amber-400',
    busy:   'bg-red-400',
    dnd:    'bg-red-500',
  };
  const presenceLabel: Record<SelfPresence, string> = {
    online: 'متاح',
    away:   'بعيد',
    busy:   'مشغول',
    dnd:    'لا تزعجني',
  };

  const color = connected ? presenceDot[myPresence] : 'bg-gray-500';
  const status = connected ? presenceLabel[myPresence] : 'غير متصل';
  const rttText = connected
    ? (rtt == null ? '' : ` · ${rtt} ms`)
    : '';

  return (
    <div
      className={`w-2 h-2 rounded-full transition-colors ${color}`}
      title={`${status}${rttText}`}
    />
  );
};

export const Sidebar: React.FC = () => {
  const user = useAuthStore((s) => s.user);
  const unreadCount = useNotificationStore((s) => s.unreadCount);
  // Live self-presence (online / away / busy / dnd) — same store the
  // TitleBar's "Helen" wordmark and the MyPresencePill read from. We
  // tint the avatar ring + the device-tag chip with the matching tone
  // so the user's chosen status is visible right next to the "H" icon
  // at the bottom of the sidebar, not just inside the pill above.
  const myPresence = usePresenceStore((s) => s.status);
  const presenceVisuals: Record<SelfPresence, { ring: string; chipText: string; chipDot: string; label: string }> = {
    online: { ring: 'ring-green-400', chipText: 'text-green-200', chipDot: 'bg-green-400', label: 'متاح' },
    away:   { ring: 'ring-amber-400', chipText: 'text-amber-200', chipDot: 'bg-amber-400', label: 'بعيد' },
    busy:   { ring: 'ring-red-400',   chipText: 'text-red-200',   chipDot: 'bg-red-400',   label: 'مشغول' },
    dnd:    { ring: 'ring-red-500',   chipText: 'text-red-200',   chipDot: 'bg-red-500',   label: 'لا تزعجني' },
  };
  const pv = presenceVisuals[myPresence];

  const isAdmin = user?.role === 'admin';

  // Pull the device tag from the main-process config exactly once.
  // It's the last 10 hex chars of the SMBIOS Machine UUID — same number
  // the user sees in Settings → System → About → Device ID. We append
  // it to the username so two `helen` accounts on different physical
  // machines are distinguishable to peers and globally searchable.
  const [deviceTag, setDeviceTag] = useState<string>('');
  const [tagCopied, setTagCopied] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await (window as any).electronAPI?.getClientConfig?.();
        if (!cancelled && cfg?.deviceTag) setDeviceTag(String(cfg.deviceTag));
      } catch { /* preload may not expose it (web fallback) */ }
    })();
    return () => { cancelled = true; };
  }, []);

  const fullId = user && deviceTag ? `${user.username}#${deviceTag}` : '';
  const handleCopyId = async () => {
    if (!fullId) return;
    try {
      await navigator.clipboard.writeText(fullId);
      setTagCopied(true);
      toast.success('تم النسخ: ' + fullId);
      setTimeout(() => setTagCopied(false), 1500);
    } catch {
      toast.error('فشل النسخ — انسخ يدوياً: ' + fullId);
    }
  };

  const navItems = [
    {
      to: '/chats',
      icon: MessageSquare,
      label: t('nav.chats'),
    },
    {
      to: '/contacts',
      icon: Users,
      label: t('nav.contacts'),
    },
    {
      to: '/calls',
      icon: Phone,
      label: t('nav.calls'),
    },
    {
      to: '/notifications',
      icon: Bell,
      label: t('notifications.title'),
      badge: unreadCount,
    },
    {
      to: '/saved',
      icon: Bookmark,
      label: t('saved.title') || 'Saved',
    },
    // Admin entry — only rendered when the JWT-decoded role is 'admin'.
    // Server still enforces the role on every /api/admin/* endpoint, so
    // this is a UX guard, not a security boundary.
    ...(isAdmin ? [{
      to: '/admin',
      icon: ShieldCheck,
      label: 'Admin',
    }] : []),
    {
      to: '/settings',
      icon: Settings,
      label: t('nav.settings'),
    },
  ];

  return (
    <div className="w-16 bg-surface-950 border-r border-surface-800 flex flex-col items-center py-4 gap-4">
      {/* Navigation links */}
      <nav className="flex flex-col gap-2">
        {navItems.map((item: any) => {
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
              {item.badge !== undefined && <NotificationBadge count={item.badge} />}
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
        <div
          className="w-full flex flex-col items-center gap-2 p-2 rounded-lg hover:bg-surface-800 transition-colors group"
          title={fullId ? `${user.display_name} (${pv.label})\n${fullId}\nانقر للنسخ` : user.display_name}
        >
          {/* Tinted ring around the H avatar — the same color family
              as the "أنا متاح" pill, so the avatar at the bottom of
              the sidebar reads as the user's current presence. The
              ring uses Tailwind's `ring-*` utility (an outline-style
              shadow) so the avatar's own size is preserved. */}
          <div
            className={`rounded-full ring-2 ${pv.ring} ring-offset-2 ring-offset-surface-950 transition-colors`}
          >
            <Avatar
              src={user.avatar_url}
              name={user.display_name}
              // Use *my* picked presence as the avatar's status so the
              // built-in corner dot flips instantly when I tap "بعيد"
              // / "مشغول". Otherwise we'd show whatever the server has
              // last echoed onto `user.status`, which lags by a round-
              // trip and stays "online" until the server broadcasts
              // back the new state.
              status={myPresence}
              size="md"
            />
          </div>
          <div className="flex items-center gap-1">
            <ConnectionQualityDot />
            {/* Username tracks the presence colour too — same tone as
                the avatar ring, the device-tag chip, and the "Helen"
                wordmark in the title bar — so the entire identity
                column reads as one consistent badge. */}
            <div className={`text-xs font-medium text-center px-1 line-clamp-2 transition-colors ${pv.chipText}`}>
              {user.display_name}
            </div>
          </div>

          {/* Globally-unique handle: username#TAG. Click to copy so
              the user can hand it to a peer searching for them. The
              chip's text colour mirrors `pv.chipText` so it follows
              the presence change too — keeping every identity-related
              surface (avatar ring → username → tag chip) on the same
              colour. */}
          {fullId && (
            <button
              onClick={handleCopyId}
              className={`flex items-center gap-1 px-1.5 py-0.5 rounded bg-surface-800 hover:bg-surface-700 text-[10px] font-mono leading-none transition-colors ${pv.chipText}`}
              title={`انقر للنسخ: ${fullId}`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${pv.chipDot}`} aria-hidden />
              {tagCopied
                ? <Check size={10} className="text-green-400" />
                : <Copy size={10} className="opacity-60 group-hover:opacity-100" />}
              <span className="truncate max-w-[64px]" dir="ltr">
                #{deviceTag}
              </span>
            </button>
          )}
        </div>
      )}
    </div>
  );
};
