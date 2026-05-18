/**
 * SlowModeCountdown — banner that sits above MessageInput while a
 * slow-mode lockout is active for the current channel.
 *
 * Driven by ``useSlowModeCountdownStore``: the chat store's
 * ``onMessageFailed`` handler populates the lockout when it sees a
 * ``slow_mode:N`` rejection from the server. We tick locally (one
 * setInterval per mount) and unmount when the timer hits zero.
 *
 * Also exposes a small ``useIsSlowModeLocked(channelId)`` hook so
 * MessageInput can disable the send button while the timer runs.
 */

import React, { useEffect, useState } from 'react';
import { Clock as Hourglass } from 'lucide-react';
import { useSlowModeCountdownStore } from '@/stores/slow-mode-countdown.store';

interface Props {
  channelId: string;
}

function fmt(s: number): string {
  if (s <= 0) return '0s';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}m ${sec}s`;
}

export const SlowModeCountdown: React.FC<Props> = ({ channelId }) => {
  // We don't subscribe to dueAtMs directly because the recompute is
  // time-based; instead we tick a local state every 500ms. This
  // single timer per mounted instance is cheap.
  const [now, setNow] = useState(() => Date.now());
  const dueAt = useSlowModeCountdownStore(
    (s) => s.dueAtMs[channelId] || 0,
  );
  const clear = useSlowModeCountdownStore((s) => s.clear);

  useEffect(() => {
    if (!dueAt) return;
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [dueAt]);

  // Auto-clear store entry once timer elapses, so MessageInput's
  // disabled state lifts.
  useEffect(() => {
    if (dueAt && now >= dueAt) {
      clear(channelId);
    }
  }, [dueAt, now, channelId, clear]);

  if (!dueAt) return null;
  const remainingSec = Math.ceil((dueAt - now) / 1000);
  if (remainingSec <= 0) return null;

  return (
    <div
      className="flex items-center gap-2 px-3 py-1.5 mx-2 mb-2
                 rounded-md bg-amber-700/20 border border-amber-700/40
                 text-amber-200 text-xs"
      role="status"
      aria-live="polite"
    >
      <Hourglass size={13} className="flex-none" />
      <span className="flex-1">
        وضع البطء مفعَّل في هذه القناة — يمكنك الإرسال خلال {' '}
        <span className="font-mono font-semibold">
          {fmt(remainingSec)}
        </span>
      </span>
    </div>
  );
};

/** Convenience selector for MessageInput. Returns true while a
 *  slow-mode lockout is active so the send button can be disabled. */
export function useIsSlowModeLocked(channelId: string): boolean {
  return useSlowModeCountdownStore(
    (s) => (s.dueAtMs[channelId] || 0) > Date.now(),
  );
}
