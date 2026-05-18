/**
 * useAppListeners — global socket listener orchestrator hook.
 *
 * Aggregates cross-module real-time events that don't belong to
 * a single store but need coordinated handling at the app root.
 *
 * Responsibilities:
 *   - Keyboard shortcuts → CallEngine actions
 *   - Electron IPC events (e.g., window focus for read receipts)
 *   - Cross-module events (e.g., call started → pause chat notifications)
 *   - System-level events (online/offline, battery saver → quality hints)
 *
 * This hook is used exclusively in App.v2.tsx.
 */

import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { socketManager } from '../services/socket.manager';
import { useCallStore } from '../stores/call.store.v2';
import { useChatStore } from '../stores/chat.store.v2';
import { useAuthStore } from '../stores/auth.store';

export function useAppListeners(): void {
  const navigate = useNavigate();
  const callStatus = useCallStore((s) => s.status);
  const activeChannelId = useChatStore((s) => s.activeChannelId);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const prevChannelRef = useRef<string | null>(null);

  // ── Keyboard Shortcuts (Electron IPC) ─────────────
  useEffect(() => {
    if (!isAuthenticated) return;

    const unsubs: (() => void)[] = [];

    if (window.electronAPI?.onShortcut) {
      unsubs.push(
        window.electronAPI.onShortcut('shortcut:toggle-mute', () => {
          if (useCallStore.getState().status === 'active') {
            useCallStore.getState().toggleMute();
          }
        }),
      );
      unsubs.push(
        window.electronAPI.onShortcut('shortcut:toggle-video', () => {
          if (useCallStore.getState().status === 'active') {
            useCallStore.getState().toggleVideo();
          }
        }),
      );
      unsubs.push(
        window.electronAPI.onShortcut('shortcut:end-call', () => {
          if (useCallStore.getState().status === 'active') {
            useCallStore.getState().hangup();
          }
        }),
      );
    }

    return () => unsubs.forEach((u) => u());
  }, [isAuthenticated]);

  // ── Browser-level keyboard shortcuts ────────────────
  // These fire from the renderer's keydown listener so they work even
  // when the underlying Electron globalShortcut isn't registered (dev,
  // sandboxed builds, web preview). The handler refuses to act when an
  // editable element has focus so Ctrl+M etc. don't intercept native
  // text-area shortcuts.
  useEffect(() => {
    if (!isAuthenticated) return;
    const onKey = (e: KeyboardEvent) => {
      const isMod = e.ctrlKey || e.metaKey;
      if (!isMod) return;
      // Don't hijack shortcuts while a modal/dialog is open. Otherwise
      // pressing Ctrl+1 inside the "New DM" or "Create Group" dialog
      // would dismiss the dialog and route to /chats mid-edit, losing
      // whatever the user typed. Any element with role="dialog" or our
      // own .helen-modal-open marker counts as "modal active".
      const modalOpen =
        document.querySelector('[role="dialog"]') !== null ||
        document.body.classList.contains('helen-modal-open');
      if (modalOpen) return;
      const tag = (document.activeElement?.tagName || '').toLowerCase();
      const editable =
        tag === 'input' ||
        tag === 'textarea' ||
        (document.activeElement as HTMLElement | null)?.isContentEditable;
      // Allow Ctrl+, even from inside text inputs — the user clearly
      // wants to leave the input. Same for Ctrl+1/2/3.
      const navigateKeys = ['1', '2', '3', ','];
      if (e.key === '1') {
        e.preventDefault();
        navigate('/chats');
        return;
      }
      if (e.key === '2') {
        e.preventDefault();
        navigate('/contacts');
        return;
      }
      if (e.key === '3') {
        e.preventDefault();
        navigate('/calls');
        return;
      }
      if (e.key === ',') {
        e.preventDefault();
        navigate('/settings');
        return;
      }
      // Call-control shortcuts only fire when we're not typing.
      if (editable) return;
      if (e.key.toLowerCase() === 'm') {
        if (useCallStore.getState().status === 'active') {
          e.preventDefault();
          useCallStore.getState().toggleMute();
        }
        return;
      }
      if (e.key.toLowerCase() === 'e') {
        if (useCallStore.getState().status === 'active') {
          e.preventDefault();
          useCallStore.getState().hangup();
        }
        return;
      }
      // Avoid unused warnings in non-Vite TS configs
      void navigateKeys;
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isAuthenticated, navigate]);

  // ── Auto-mark channel as read on focus/switch ─────
  useEffect(() => {
    if (!activeChannelId) return;

    // Mark as read immediately when switching to a channel
    if (prevChannelRef.current !== activeChannelId) {
      useChatStore.getState().markChannelRead(activeChannelId);
      prevChannelRef.current = activeChannelId;
    }

    // Also mark as read when window regains focus
    const handleFocus = () => {
      const currentChannel = useChatStore.getState().activeChannelId;
      if (currentChannel) {
        useChatStore.getState().markChannelRead(currentChannel);
      }
    };
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  }, [activeChannelId]);

  // ── Suppress chat notifications during active call ─
  useEffect(() => {
    if (callStatus === 'active') {
      // While in a call, mute desktop notifications for messages
      // (The MessagingEngine still stores them; we just skip the native popup)
      (window as any).__commclient_suppress_chat_notif = true;
    } else {
      (window as any).__commclient_suppress_chat_notif = false;
    }
  }, [callStatus]);

  // ── Handle Electron window events ─────────────────
  useEffect(() => {
    if (!isAuthenticated) return;

    // Listen for app going to tray — stop typing indicators
    const handleBlur = () => {
      const ch = useChatStore.getState().activeChannelId;
      if (ch) useChatStore.getState().stopTyping(ch);
    };
    window.addEventListener('blur', handleBlur);

    return () => {
      window.removeEventListener('blur', handleBlur);
    };
  }, [isAuthenticated]);

  // ── Socket health logging ─────────────────────────
  useEffect(() => {
    if (!isAuthenticated) return;

    const unsubPong = socketManager.on('pong', () => {
      // Heartbeat healthy — noop in production
    });

    const unsubErr = socketManager.on('connect_error', (err: any) => {
      console.warn('[AppListeners] Socket error:', err?.message);
    });

    return () => {
      unsubPong();
      unsubErr();
    };
  }, [isAuthenticated]);
}
