/**
 * notification.store.ts — Zustand store for application notifications.
 *
 * Manages notification list, unread count, and socket listener integration.
 * Fetches notifications from GET /api/notifications.
 * Listens for 'notification:new' socket events.
 */

import { create } from 'zustand';
import { api } from '../services/api.client';
import { socketManager } from '../services/socket.manager';

export type NotificationType =
  | 'message'
  | 'call_missed'
  | 'call_incoming'
  | 'call_ended'
  | 'contact_request'
  | 'contact_accepted'
  | 'group_invite'
  | 'mention'
  | 'system';

export interface Notification {
  id: string;
  type: NotificationType;
  title: string;
  body: string;
  read: boolean;
  image_url?: string | null;
  action_url?: string | null;
  created_at: string;
  related_user_id?: string;
  related_channel_id?: string;
}

interface NotificationStoreState {
  // State
  notifications: Notification[];
  unreadCount: number;
  isLoading: boolean;
  error: string | null;

  // Actions
  fetchNotifications: (limit?: number, offset?: number, unreadOnly?: boolean) => Promise<void>;
  markRead: (ids: string[]) => Promise<void>;
  markAllRead: () => Promise<void>;
  deleteNotification: (id: string) => Promise<void>;
  deleteAllNotifications: () => Promise<void>;
  fetchUnreadCount: () => Promise<void>;
  addNotification: (notification: Notification) => void;
  clearError: () => void;
}

/**
 * Push the unread count to the OS-level badge surfaces (window title
 * prefix, taskbar/dock badge, tray tooltip). Best-effort — older builds
 * without the IPC handler silently no-op.
 */
function _pushBadge(count: number): void {
  try {
    const api: any = (window as any).electronAPI;
    if (api?.setUnreadBadge) {
      void api.setUnreadBadge(count);
    } else {
      // Web fallback — at least keep the document title accurate so the
      // browser tab favicon shows the count via OS notification APIs.
      const base = document.title.replace(/^\(\d+\+?\)\s+/, '');
      document.title = count > 0 ? `(${count > 99 ? '99+' : count}) ${base}` : base;
    }
  } catch {
    /* never let a failed badge push break the store update */
  }
}

export const useNotificationStore = create<NotificationStoreState>((set, get) => {
  // ── Socket Listener Setup ───────────────────────────────
  // This runs once on store creation
  socketManager.on('notification:new', (notification: Notification) => {
    get().addNotification(notification);
  });

  return {
    // Initial state
    notifications: [],
    unreadCount: 0,
    isLoading: false,
    error: null,

    // ── Fetch Notifications ────────────────────────────────
    fetchNotifications: async (limit = 50, offset = 0, unreadOnly = false) => {
      set({ isLoading: true, error: null });
      try {
        const response = await api.getNotifications({ limit, offset, unread_only: unreadOnly });

        const next = response.unread_count ?? get().unreadCount;
        set({
          notifications: response.notifications || [],
          unreadCount: next,
          isLoading: false,
        });
        _pushBadge(next);
      } catch (error) {
        const errorMsg =
          error instanceof Error ? error.message : 'Failed to fetch notifications';
        set({
          isLoading: false,
          error: errorMsg,
        });
      }
    },

    // ── Mark Notifications as Read ────────────────────────
    markRead: async (ids: string[]) => {
      if (ids.length === 0) return;

      try {
        await api.markNotificationsRead(ids);

        const nextCount = Math.max(0, get().unreadCount - ids.length);
        set((state) => ({
          notifications: state.notifications.map((n) =>
            ids.includes(n.id) ? { ...n, read: true } : n
          ),
          unreadCount: nextCount,
        }));
        _pushBadge(nextCount);
      } catch (error) {
        const errorMsg =
          error instanceof Error ? error.message : 'Failed to mark notifications as read';
        set({ error: errorMsg });
      }
    },

    // ── Mark All as Read ───────────────────────────────────
    markAllRead: async () => {
      try {
        await api.markAllNotificationsRead();

        set((state) => ({
          notifications: state.notifications.map((n) => ({ ...n, read: true })),
          unreadCount: 0,
        }));
        _pushBadge(0);
      } catch (error) {
        const errorMsg =
          error instanceof Error ? error.message : 'Failed to mark all as read';
        set({ error: errorMsg });
      }
    },

    // ── Delete Single Notification ────────────────────────
    deleteNotification: async (id: string) => {
      try {
        await api.deleteNotification(id);

        set((state) => ({
          notifications: state.notifications.filter((n) => n.id !== id),
          unreadCount: state.notifications.find((n) => n.id === id && !n.read)
            ? state.unreadCount - 1
            : state.unreadCount,
        }));
      } catch (error) {
        const errorMsg =
          error instanceof Error ? error.message : 'Failed to delete notification';
        set({ error: errorMsg });
      }
    },

    // ── Delete All Notifications ───────────────────────────
    deleteAllNotifications: async () => {
      try {
        await api.deleteAllNotifications();

        set({
          notifications: [],
          unreadCount: 0,
        });
      } catch (error) {
        const errorMsg =
          error instanceof Error ? error.message : 'Failed to delete notifications';
        set({ error: errorMsg });
      }
    },

    // ── Fetch Unread Count ────────────────────────────────
    fetchUnreadCount: async () => {
      try {
        const response = await api.getUnreadCount();
        const next = response.unread_count ?? 0;
        set({ unreadCount: next });
        _pushBadge(next);
      } catch (error) {
        console.error('Failed to fetch unread count:', error);
      }
    },

    // ── Add Single Notification ───────────────────────────
    addNotification: (notification: Notification) => {
      const nextCount = notification.read
        ? get().unreadCount
        : get().unreadCount + 1;
      set((state) => ({
        notifications: [notification, ...state.notifications],
        unreadCount: nextCount,
      }));
      _pushBadge(nextCount);
    },

    // ── Clear Error ──────────────────────────────────────
    clearError: () => set({ error: null }),
  };
});
