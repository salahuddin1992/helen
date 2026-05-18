/**
 * GlobalShortcutsMount — invisible component that wires every
 * registered keyboard shortcut (except ``search``, which is owned
 * by ``GlobalSearch`` itself) to a concrete action.
 *
 * Lives in its own file so adding/removing a shortcut is one
 * ``useShortcutListener`` call, not a chore through the layout
 * components. Mounted once near the top of the React tree.
 */

import React from 'react';
import toast from 'react-hot-toast';
import { useShortcutListener } from '@/stores/keyboard-shortcuts.store';
import { useChatStore } from '@/stores/chat.store.v2';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

export const GlobalShortcutsMount: React.FC = () => {
  // ── Active call hotkeys ────────────────────────────
  // Only consume when there's an active call so the keystroke
  // doesn't shadow normal typing in chat input.
  useShortcutListener('toggle_mute', (e) => {
    const status = useCallStore.getState().status;
    if (status !== 'active') return;
    e.preventDefault();
    useCallStore.getState().toggleMute();
  });

  useShortcutListener('toggle_video', (e) => {
    const status = useCallStore.getState().status;
    if (status !== 'active') return;
    e.preventDefault();
    useCallStore.getState().toggleVideo();
  });

  useShortcutListener('end_call', (e) => {
    const status = useCallStore.getState().status;
    if (status !== 'active') return;
    e.preventDefault();
    useCallStore.getState().hangup();
  });

  // ── Chat navigation hotkeys ────────────────────────
  useShortcutListener('jump_to_unread', (e) => {
    const isAuthed = useAuthStore.getState().isAuthenticated;
    if (!isAuthed) return;
    e.preventDefault();
    const state = useChatStore.getState();
    const channels = state.channels;
    const meta = state.channelMeta;
    const activeId = state.activeChannelId;
    // Find the next channel with unread > 0 after the active one,
    // wrapping around. If none, surface a small toast.
    if (channels.length === 0) return;
    const startIdx = activeId
      ? channels.findIndex((c) => c.id === activeId)
      : -1;
    for (let i = 1; i <= channels.length; i++) {
      const cand = channels[(startIdx + i) % channels.length];
      const unread = meta[cand.id]?.unread || 0;
      if (unread > 0) {
        state.setActiveChannel(cand.id);
        return;
      }
    }
    toast('لا توجد رسائل غير مقروءة', { icon: '✓' });
  });

  useShortcutListener('mark_all_read', (e) => {
    const isAuthed = useAuthStore.getState().isAuthenticated;
    if (!isAuthed) return;
    e.preventDefault();
    const state = useChatStore.getState();
    const meta = state.channelMeta;
    let touched = 0;
    for (const ch of state.channels) {
      if ((meta[ch.id]?.unread || 0) > 0) {
        try { state.markChannelRead(ch.id); touched += 1; } catch { /* skip */ }
      }
    }
    toast.success(
      touched === 0
        ? 'لا توجد قنوات غير مقروءة'
        : `حُدِّدت ${touched} قناة كمقروءة`,
    );
  });

  // ── New-conversation hotkeys ───────────────────────
  // These dispatch a custom DOM event that MainLayout listens for.
  // The layout owns the actual modal — this mount just signals
  // intent so we don't import a sidebar internal here.
  useShortcutListener('new_dm', (e) => {
    e.preventDefault();
    window.dispatchEvent(new CustomEvent('helen:open-new-dm'));
  });

  useShortcutListener('new_group', (e) => {
    e.preventDefault();
    window.dispatchEvent(new CustomEvent('helen:open-new-group'));
  });

  return null;
};
