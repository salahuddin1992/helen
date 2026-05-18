/**
 * CoHostMount — headless socket listener that mirrors the
 * ``call:cohost_changed`` server events into the call store, plus a
 * tiny chip renderer:
 *   - When the local user is a co-host, show a small badge so they
 *     know the host has elevated them (gives them moderator buttons).
 *   - Inside the existing host menu, the host can promote/demote
 *     via the participant search panel (added separately).
 */

import React, { useEffect } from 'react';
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

const CoHostMount: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const coHostIds = useCallStore((s) => s.coHostIds);
  const me = useAuthStore((s) => s.user);

  useEffect(() => {
    if (!callId) return;
    const off = socketManager.on('call:cohost_changed', (data: any) => {
      if (data?.call_id !== callId) return;
      const { user_id, is_cohost } = data;
      if (typeof user_id !== 'string') return;
      const cur = useCallStore.getState().coHostIds;
      const has = cur.includes(user_id);
      if (is_cohost && !has) {
        useCallStore.setState({ coHostIds: [...cur, user_id] });
      } else if (!is_cohost && has) {
        useCallStore.setState({
          coHostIds: cur.filter((id) => id !== user_id),
        });
      }
    });
    return () => { try { off(); } catch { /* ignore */ } };
  }, [callId]);

  // Reset on call end so a stale list doesn't carry over.
  useEffect(() => {
    if (status === 'idle' || status === 'ended') {
      useCallStore.setState({ coHostIds: [] });
    }
  }, [status]);

  if (!me || !callId) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;
  if (!coHostIds.includes(me.id)) return null;

  return (
    <div className="fixed top-12 left-44 z-30 px-2 py-0.5 rounded-full
                    bg-emerald-500/30 text-emerald-100 text-[10px] font-medium
                    flex items-center gap-1 shadow">
      <span aria-hidden="true">★</span>
      <span>مشرف مساعد</span>
    </div>
  );
};

export default CoHostMount;
