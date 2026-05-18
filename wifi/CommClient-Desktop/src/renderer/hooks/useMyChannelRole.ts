/**
 * useMyChannelRole — fetch the current user's per-channel role.
 *
 * Returns "admin" | "moderator" | "member" | null. The null value is
 * the loading state and the "not a member" state — callers should
 * treat both as "no moderation privileges" since the server enforces
 * the actual gate.
 *
 * The previous moderation UI used the global ``User.role`` to decide
 * whether to render the host menu. That meant a global admin who was
 * only a "member" in some channel still saw the moderation buttons,
 * and a per-channel moderator with a non-admin global role saw
 * nothing. Both were wrong. This hook closes the gap by reading
 * ``ChannelMember.role`` from the channel detail endpoint.
 */
import { useEffect, useState } from 'react';
import { api } from '@/services/api.client';
import { useAuthStore } from '@/stores/auth.store';
import { socketManager } from '@/services/socket.manager';

type ChannelRole = 'admin' | 'moderator' | 'member' | null;

export function useMyChannelRole(channelId: string | null): ChannelRole {
  const me = useAuthStore((s) => s.user);
  const [role, setRole] = useState<ChannelRole>(null);

  useEffect(() => {
    if (!channelId || !me?.id) {
      setRole(null);
      return;
    }

    let cancelled = false;

    const refresh = async () => {
      try {
        const channel = await api.getChannel(channelId);
        if (cancelled) return;
        const members: Array<{ user_id: string; role: string }> =
          channel?.members || [];
        const mine = members.find((m) => m.user_id === me.id);
        const r = mine?.role;
        if (r === 'admin' || r === 'moderator' || r === 'member') {
          setRole(r);
        } else {
          setRole(null);
        }
      } catch {
        if (!cancelled) setRole(null);
      }
    };

    refresh();

    // Re-fetch on socket reconnect — role may have changed while we
    // were offline.
    const offConnect = socketManager.on('connect', () => {
      refresh();
    });

    return () => {
      cancelled = true;
      offConnect?.();
    };
  }, [channelId, me?.id]);

  return role;
}
