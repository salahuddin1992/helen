/**
 * Privacy preferences — purely client-side gates for outbound
 * presence/awareness signals.
 *
 * The server never *requires* these signals; refusing to send them
 * just means peers won't know the local user has read a message,
 * has typed something, or is online. The server still delivers
 * messages addressed to this user as normal.
 *
 * Currently exposed:
 *   * ``send_read_receipts``   — emit ``v2_chat_mark_read`` events.
 *   * ``send_typing_indicator``— emit ``v2_chat_typing_start/stop``.
 *   * ``send_presence``        — broadcast online status to peers.
 *
 * The DeliveryTracker / MessageInput / presence module read the
 * relevant flag before emitting. Defaults are all "true" — opt-out,
 * not opt-in.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface PrivacyState {
  send_read_receipts: boolean;
  send_typing_indicator: boolean;
  send_presence: boolean;
  setSendReadReceipts: (v: boolean) => void;
  setSendTypingIndicator: (v: boolean) => void;
  setSendPresence: (v: boolean) => void;
}

export const usePrivacyStore = create<PrivacyState>()(
  persist(
    (set) => ({
      send_read_receipts: true,
      send_typing_indicator: true,
      send_presence: true,
      setSendReadReceipts: (v) => set({ send_read_receipts: v }),
      setSendTypingIndicator: (v) => set({ send_typing_indicator: v }),
      setSendPresence: (v) => {
        set({ send_presence: v });
        // Best-effort server hint — if the server implements
        // ``presence:set_visible`` it will mark this socket invisible
        // to other users without dropping the connection. Older
        // servers ignore the unknown event silently.
        try {
          const mod = require('@/services/socket.manager');
          mod.socketManager.emitNoAck('presence:set_visible', {
            visible: v,
          });
        } catch { /* socket may not be ready yet */ }
      },
    }),
    { name: 'helen.privacy.v1' },
  ),
);
