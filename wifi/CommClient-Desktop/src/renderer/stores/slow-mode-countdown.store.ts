/**
 * Slow-mode countdown store — purely client-side reaction to a
 * server-rejected ``slow_mode:<N>`` send.
 *
 * The server (``app/services/channel_slow_mode.py``) raises a
 * ValueError formatted as ``slow_mode:<seconds-to-wait>`` whenever
 * a member exceeds the per-channel rate cap. The chat store catches
 * that string and pumps it through ``setDueIn`` here. The
 * MessageInput reads ``getDueInSec`` to render a banner + disable
 * the send button until the lockout passes.
 *
 * Keeping this in its own file makes the slow-mode UX a tiny,
 * isolated subsystem: it can grow (per-message hint, force-tap toast,
 * etc.) without spreading countdown timers across the chat layer.
 */

import { create } from 'zustand';

interface CountdownState {
  /** Map of channel_id -> absolute due timestamp (ms) until next send
   *  is allowed. ``Date.now()`` >= due means no countdown. */
  dueAtMs: Record<string, number>;
  /** Most-recent rejection error per channel — surfaced as a tooltip /
   *  toast for clarity. Cleared once the timer elapses. */
  lastError: Record<string, string>;

  setDueIn: (channelId: string, seconds: number, error?: string) => void;
  clear: (channelId: string) => void;
  /** Seconds remaining (rounded up). 0 = no active countdown. */
  getDueInSec: (channelId: string) => number;
}

export const useSlowModeCountdownStore = create<CountdownState>(
  (set, get) => ({
    dueAtMs: {},
    lastError: {},
    setDueIn: (channelId, seconds, error) => {
      const ms = Math.max(0, seconds) * 1000;
      const due = Date.now() + ms;
      set((s) => ({
        dueAtMs: { ...s.dueAtMs, [channelId]: due },
        lastError: error
          ? { ...s.lastError, [channelId]: error }
          : s.lastError,
      }));
    },
    clear: (channelId) =>
      set((s) => {
        const due = { ...s.dueAtMs };
        const err = { ...s.lastError };
        delete due[channelId];
        delete err[channelId];
        return { dueAtMs: due, lastError: err };
      }),
    getDueInSec: (channelId) => {
      const due = get().dueAtMs[channelId];
      if (!due) return 0;
      const remainingMs = due - Date.now();
      if (remainingMs <= 0) return 0;
      return Math.ceil(remainingMs / 1000);
    },
  }),
);

/** Parse a rejection error string. Returns the wait-seconds or
 *  ``null`` if the error isn't a slow-mode rejection. */
export function parseSlowModeError(raw: string): number | null {
  if (!raw) return null;
  // Accept either bare ``slow_mode:N`` or anything that contains it
  // (some pipelines wrap the message). Tolerate fractional seconds.
  const m = raw.match(/slow_mode:([\d.]+)/);
  if (!m) return null;
  const n = parseFloat(m[1]);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}
