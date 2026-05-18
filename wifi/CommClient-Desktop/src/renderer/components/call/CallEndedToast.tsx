/**
 * CallEndedToast — brief explanation of why the last call ended.
 *
 * Without this, the call view simply vanishes when the call ends
 * and the user has no idea whether they hung up, the host hung up,
 * the network dropped, or the orphan sweep took the call. The
 * toast appears for ~5 seconds and auto-dismisses; clicking it
 * dismisses immediately.
 *
 * Mounts unconditionally; renders nothing when ``endReason`` is
 * null. The store sets this on every ``onCallEnded`` and clears it
 * on the next call initiate.
 */

import React, { useEffect } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { PhoneOff } from 'lucide-react';

const CallEndedToast: React.FC = () => {
  const endReason = useCallStore((s) => s.endReason);
  const status = useCallStore((s) => s.status);
  const clearEndReason = useCallStore((s) => s.clearEndReason);

  // Auto-dismiss after 5 seconds.
  useEffect(() => {
    if (!endReason) return;
    const t = setTimeout(() => clearEndReason(), 5000);
    return () => clearTimeout(t);
  }, [endReason, clearEndReason]);

  // Hide while a call is active or being placed — the toast is for
  // the post-call moment only.
  if (!endReason) return null;
  if (status === 'active' || status === 'reconnecting' || status === 'connecting' || status === 'ringing') {
    return null;
  }

  return (
    <button
      type="button"
      onClick={clearEndReason}
      className="fixed top-12 right-4 z-50 max-w-sm
                 bg-surface-900/95 border border-surface-700 rounded-lg
                 shadow-2xl backdrop-blur px-4 py-3 flex items-center gap-3
                 text-start text-text-100 hover:bg-surface-800 transition-colors"
    >
      <PhoneOff size={18} className="text-red-400 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium truncate">{endReason}</div>
        <div className="text-xs text-text-500 mt-0.5">انقر للإغلاق</div>
      </div>
    </button>
  );
};

export default CallEndedToast;
