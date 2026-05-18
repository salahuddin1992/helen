/**
 * adminStore — Zustand state for the operator console.
 *
 * Holds:
 *   - selected panel slug (sync'd with URL hash so deep links work)
 *   - per-panel notification feed
 *   - permissions cache (so we don't re-fetch on every navigation)
 *   - WS health snapshot
 *
 * Notifications are append-only with a soft cap (200) and auto-prune on
 * read. Each notification carries `seen` and `dismissed` flags so the bell
 * icon can show unread counts and the drawer can render history.
 */

import { create } from 'zustand';
import type { AdminPanelSlug, AdminWsChannel } from '../components/admin/panels/AdminPanelRegistry';

export type NotificationSeverity = 'info' | 'success' | 'warn' | 'error' | 'critical';

export interface AdminNotification {
  id: string;
  slug?: AdminPanelSlug;          // origin panel, when known
  channel?: AdminWsChannel;       // origin WS channel, when known
  severity: NotificationSeverity;
  title: string;
  body?: string;
  ts: number;                     // unix ms
  seen: boolean;
  dismissed: boolean;
  /** Optional deep-link target — slug of the panel that should open on click. */
  link?: AdminPanelSlug;
  /** Raw payload — kept for debugging / future drill-down. */
  raw?: unknown;
}

const NOTIFICATION_CAP = 200;
let _notifSeq = 0;

interface AdminState {
  // Navigation
  selectedSlug: AdminPanelSlug | null;
  setSelectedSlug: (slug: AdminPanelSlug | null) => void;

  // Notifications
  notifications: AdminNotification[];
  unreadCount: number;
  drawerOpen: boolean;
  setDrawerOpen: (open: boolean) => void;
  pushNotification: (n: Omit<AdminNotification, 'id' | 'seen' | 'dismissed' | 'ts'> & { ts?: number }) => void;
  markAllSeen: () => void;
  markSeen: (id: string) => void;
  dismissNotification: (id: string) => void;
  clearNotifications: () => void;

  // Permissions cache (string set the user holds on the server).
  permissions: Set<string>;
  setPermissions: (perms: Iterable<string>) => void;
  hasPermission: (perm: string) => boolean;

  // WS health
  wsStatus: Partial<Record<AdminWsChannel, 'open' | 'connecting' | 'closed'>>;
  setWsStatus: (status: Partial<Record<AdminWsChannel, 'open' | 'connecting' | 'closed'>>) => void;

  // Last error per panel (used for nav rail red-dot badges).
  panelErrors: Partial<Record<AdminPanelSlug, string>>;
  setPanelError: (slug: AdminPanelSlug, msg: string | null) => void;
}

export const useAdminStore = create<AdminState>((set, get) => ({
  selectedSlug: null,
  setSelectedSlug: (slug) => set({ selectedSlug: slug }),

  notifications: [],
  unreadCount: 0,
  drawerOpen: false,
  setDrawerOpen: (drawerOpen) => set({ drawerOpen }),

  pushNotification: (n) => set((s) => {
    const id = `n_${++_notifSeq}_${Date.now().toString(36)}`;
    const ts = n.ts ?? Date.now();
    const notif: AdminNotification = {
      id,
      slug: n.slug,
      channel: n.channel,
      severity: n.severity,
      title: n.title,
      body: n.body,
      link: n.link,
      raw: n.raw,
      ts,
      seen: false,
      dismissed: false,
    };
    const next = [notif, ...s.notifications].slice(0, NOTIFICATION_CAP);
    return {
      notifications: next,
      unreadCount: next.filter((x) => !x.seen && !x.dismissed).length,
    };
  }),

  markAllSeen: () => set((s) => ({
    notifications: s.notifications.map((n) => ({ ...n, seen: true })),
    unreadCount: 0,
  })),

  markSeen: (id) => set((s) => {
    const notifications = s.notifications.map((n) => n.id === id ? { ...n, seen: true } : n);
    return {
      notifications,
      unreadCount: notifications.filter((x) => !x.seen && !x.dismissed).length,
    };
  }),

  dismissNotification: (id) => set((s) => {
    const notifications = s.notifications.map((n) => n.id === id ? { ...n, dismissed: true, seen: true } : n);
    return {
      notifications,
      unreadCount: notifications.filter((x) => !x.seen && !x.dismissed).length,
    };
  }),

  clearNotifications: () => set({ notifications: [], unreadCount: 0 }),

  permissions: new Set<string>(),
  setPermissions: (perms) => set({ permissions: new Set(perms) }),
  hasPermission: (perm) => {
    const ps = get().permissions;
    // Admin users implicitly have every permission — server still enforces.
    if (ps.has('admin.*')) return true;
    return ps.has(perm);
  },

  wsStatus: {},
  setWsStatus: (status) => set((s) => ({ wsStatus: { ...s.wsStatus, ...status } })),

  panelErrors: {},
  setPanelError: (slug, msg) => set((s) => {
    const next = { ...s.panelErrors };
    if (!msg) delete next[slug];
    else next[slug] = msg;
    return { panelErrors: next };
  }),
}));

// ── External helpers ──────────────────────────────────────────────────
/** Push a notification from anywhere in the renderer. */
export function notifyAdmin(n: Parameters<AdminState['pushNotification']>[0]): void {
  useAdminStore.getState().pushNotification(n);
}
