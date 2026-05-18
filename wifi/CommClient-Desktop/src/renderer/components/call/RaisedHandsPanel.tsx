/**
 * RaisedHandsPanel — FIFO queue of participants with hand raised.
 *
 * Visible only to the host (or a channel moderator). The host can:
 *   - Click "Allow" to spotlight the participant (they need to
 *     unmute themselves; the host can also force-unmute via the
 *     existing HostMenu if the call is in webinar mode).
 *   - Click "Lower" to lower the hand without granting the floor
 *     (e.g. if the question was already asked by someone else).
 *
 * The order respects ``handRaisedAt`` — first raised, first listed.
 * Without this panel, raised hands only show as a corner badge on
 * each tile, which is unusable when 50+ tiles are on screen.
 */

import React, { useMemo } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { useSpotlightStore } from '@/stores/spotlight.store';

const RaisedHandsPanel: React.FC = () => {
  const participants = useCallStore((s) => s.participants);
  const hostId = useCallStore((s) => s.hostId);
  const me = useAuthStore((s) => s.user);
  const toggleSpotlight = useSpotlightStore((s) => s.toggleSpotlight);

  const isHost = !!me && hostId === me.id;

  const raised = useMemo(() => {
    const list = Object.values(participants).filter(
      (p: any) => p.isHandRaised,
    );
    // FIFO: oldest hand first. Fall back to peerId for stable order
    // when timestamps are missing or equal.
    list.sort((a: any, b: any) => {
      const at = a.handRaisedAt ? Date.parse(a.handRaisedAt) : 0;
      const bt = b.handRaisedAt ? Date.parse(b.handRaisedAt) : 0;
      if (at !== bt) return at - bt;
      return String(a.peerId).localeCompare(String(b.peerId));
    });
    return list;
  }, [participants]);

  // Render only for the host AND only when at least one hand is up.
  // Non-hosts already see the per-tile badge, which is enough for
  // them to understand who's queued.
  if (!isHost || raised.length === 0) return null;

  return (
    <div className="absolute top-20 right-4 z-30 w-72 max-h-[60vh]
                    rounded-lg bg-surface-900/95 border border-surface-700
                    shadow-2xl backdrop-blur overflow-hidden flex flex-col">
      <div className="px-3 py-2 bg-yellow-500/20 border-b border-yellow-600/30
                      flex items-center gap-2">
        <span className="text-lg">✋</span>
        <span className="text-sm font-semibold text-yellow-200">
          أيدي مرفوعة
        </span>
        <span className="ms-auto text-xs px-2 py-0.5 rounded-full
                         bg-yellow-500/30 text-yellow-100 font-bold">
          {raised.length}
        </span>
      </div>

      <ul className="overflow-y-auto divide-y divide-surface-800">
        {raised.map((p: any, idx) => (
          <li key={p.peerId} className="px-3 py-2 flex items-center gap-2">
            <span className="text-xs text-text-500 font-mono w-5">
              {idx + 1}.
            </span>
            <span className="flex-1 text-sm text-text-100 truncate">
              {p.displayName || p.peerId.slice(0, 8)}
            </span>
            <button
              onClick={() => toggleSpotlight(p.peerId)}
              className="px-2 py-1 rounded bg-blue-600 hover:bg-blue-500
                         text-white text-xs font-medium"
              title="تركيز على هذا المشارك"
            >
              تركيز
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
};

export default RaisedHandsPanel;
