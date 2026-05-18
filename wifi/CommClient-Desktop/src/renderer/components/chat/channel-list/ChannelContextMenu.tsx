/**
 * ChannelContextMenu — right-click menu on a channel-list row.
 *
 * Today: Pin/Unpin (toggles ``ChannelMember.is_pinned`` so the row
 * stays at the top of the user's list). Designed to grow — Archive
 * + Mark-as-Read + Notification mode would slot in cleanly.
 *
 * Renders as a positioned floating menu that closes on outside
 * click + Esc. The parent owns the open/close state via the
 * ``state`` prop; nullable state means "closed".
 */

import React, { useEffect } from 'react';
import { Pin, PinOff } from 'lucide-react';
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';
import { useChatStore } from '@/stores/chat.store.v2';

export interface ChannelContextMenuState {
  channelId: string;
  isPinned: boolean;
  x: number;
  y: number;
}

interface Props {
  state: ChannelContextMenuState | null;
  onClose: () => void;
}

export const ChannelContextMenu: React.FC<Props> = ({
  state, onClose,
}) => {
  // Outside click closes.
  useEffect(() => {
    if (!state) return;
    const onAny = () => onClose();
    document.addEventListener('click', onAny);
    return () => document.removeEventListener('click', onAny);
  }, [state, onClose]);

  // Esc closes.
  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [state, onClose]);

  if (!state) return null;

  const togglePin = async () => {
    try {
      const r = await api.channelPrefs.pin(
        state.channelId, !state.isPinned,
      );
      // Reflect the new state in the chat store so the list
      // re-orders without a hard refresh. We mutate via the
      // setter the store already exposes (channels mutation).
      useChatStore.setState((s) => ({
        channels: s.channels.map((c) =>
          c.id === state.channelId
            ? ({ ...c, is_pinned: r.is_pinned } as any)
            : c,
        ),
      }));
      toast.success(r.is_pinned ? 'تم التثبيت' : 'تم إلغاء التثبيت');
    } catch (e: any) {
      toast.error('فشل: ' + (e?.message || e));
    } finally {
      onClose();
    }
  };

  const Icon = state.isPinned ? PinOff : Pin;
  const label = state.isPinned ? 'إلغاء التثبيت' : 'تثبيت في الأعلى';

  return (
    <div
      className="fixed z-50 bg-slate-800 rounded-lg shadow-lg
                 border border-slate-700 py-1 min-w-44"
      style={{ top: `${state.y}px`, left: `${state.x}px` }}
      onClick={(e) => e.stopPropagation()}
    >
      <button
        onClick={togglePin}
        className="w-full px-4 py-2 text-start text-sm text-slate-100
                   hover:bg-slate-700 flex items-center gap-2 transition"
      >
        <Icon size={14} />
        {label}
      </button>
    </div>
  );
};
