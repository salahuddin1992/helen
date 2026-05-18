/**
 * useChannelActiveCall — discovery hook for "Join Existing Call" UX.
 *
 * The backend tracks group calls per channel in CallService._active_calls
 * and persists them to the active_calls / active_call_participants tables.
 * Without this hook, the QuickCallSheet / GroupActionHub UI had no way to
 * know whether a live call was already running in a channel — they
 * accepted `hasActiveCall` / `activeCallParticipants` as props but the
 * props were never wired through.
 *
 * Behaviour
 * ---------
 *   - On mount (or channelId change): GET /api/channels/{id}/active-call
 *     to seed the snapshot.
 *   - Subscribes to two server-emitted socket events:
 *       channel:active_call_started — payload includes call_id, type, host
 *       channel:active_call_ended   — payload includes call_id
 *     so the snapshot stays live without polling.
 *   - Subscribes to call_participant_joined / call_participant_left so
 *     the participant_count badge updates in real time while the call
 *     is live.
 *   - Refetches whenever the socket reconnects (after a brief network
 *     blip) to recover any events the client may have missed during the
 *     dead window.
 *
 * Returns a stable shape consumable by QuickCallSheet:
 *   {
 *     hasActiveCall: boolean
 *     activeCall: ActiveCallSummary | null
 *     activeCallParticipants: CallParticipantPreview[]
 *     loading: boolean
 *     refresh: () => Promise<void>
 *   }
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../services/api.client';
import { socketManager } from '../services/socket.manager';

export interface ActiveCallSummary {
  callId: string;
  callType: 'audio' | 'video';
  routing: 'p2p' | 'mesh' | 'sfu' | 'hybrid';
  status: 'ringing' | 'active';
  startedAt: string | null;
  participantCount: number;
  hostId: string;
  participants: ActiveCallParticipantSnapshot[];
}

export interface ActiveCallParticipantSnapshot {
  userId: string;
  muted: boolean;
  videoOff: boolean;
  sharingScreen: boolean;
  onHold: boolean;
}

// Shape used by QuickCallSheet — keep field names aligned with its
// existing `CallParticipantPreview` interface so the prop is drop-in.
export interface CallParticipantPreview {
  id: string;
  displayName: string;
  avatar?: string;
  isMuted: boolean;
  hasVideo: boolean;
}

interface UseChannelActiveCallOptions {
  /** Optional resolver for displayName/avatar from a userId. Without it, the
   *  preview falls back to the user_id as displayName. */
  resolveUser?: (userId: string) => { displayName: string; avatar?: string } | undefined;
}

interface UseChannelActiveCallResult {
  hasActiveCall: boolean;
  activeCall: ActiveCallSummary | null;
  activeCallParticipants: CallParticipantPreview[];
  loading: boolean;
  refresh: () => Promise<void>;
}

function normalize(raw: any): ActiveCallSummary | null {
  if (!raw || !raw.call_id) return null;
  return {
    callId: raw.call_id,
    callType: raw.call_type,
    routing: raw.routing,
    status: raw.status,
    startedAt: raw.started_at ?? null,
    participantCount: raw.participant_count ?? (raw.participants?.length ?? 0),
    hostId: raw.host_id,
    participants: (raw.participants ?? []).map((p: any) => ({
      userId: p.user_id,
      muted: !!p.muted,
      videoOff: !!p.video_off,
      sharingScreen: !!p.sharing_screen,
      onHold: !!p.on_hold,
    })),
  };
}

export function useChannelActiveCall(
  channelId: string | null,
  opts: UseChannelActiveCallOptions = {}
): UseChannelActiveCallResult {
  const { resolveUser } = opts;
  const [activeCall, setActiveCall] = useState<ActiveCallSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const channelIdRef = useRef<string | null>(channelId);
  channelIdRef.current = channelId;

  // ── Fetch snapshot ──────────────────────────────────────
  const refresh = useCallback(async () => {
    if (!channelIdRef.current) {
      setActiveCall(null);
      return;
    }
    const target = channelIdRef.current;
    setLoading(true);
    try {
      const resp = await api.getChannelActiveCall(target);
      // Guard against stale resolution after channel switched.
      if (channelIdRef.current !== target) return;
      setActiveCall(normalize(resp.active_call));
    } catch (err) {
      // 403 = not a member; treat as "no call visible".
      if (channelIdRef.current === target) setActiveCall(null);
      console.warn('[useChannelActiveCall] fetch failed', err);
    } finally {
      if (channelIdRef.current === target) setLoading(false);
    }
  }, []);

  // ── Initial + on-channel-change fetch ───────────────────
  useEffect(() => {
    if (!channelId) {
      setActiveCall(null);
      return;
    }
    refresh();
  }, [channelId, refresh]);

  // Mirror activeCall into a ref so the socket listeners (defined once
  // per channelId) read the latest call_id without re-subscribing on
  // every state change. MUST be declared BEFORE the socket-listener
  // useEffect that closes over it — TDZ on `const` would otherwise
  // throw on first render (audit fix F1).
  const activeCallRef = useRef<ActiveCallSummary | null>(null);
  useEffect(() => {
    activeCallRef.current = activeCall;
  }, [activeCall]);

  // ── Socket subscriptions ────────────────────────────────
  useEffect(() => {
    if (!channelId) return;
    const unsubs: Array<() => void> = [];

    unsubs.push(
      socketManager.on('channel:active_call_started', (data: any) => {
        if (data?.channel_id !== channelId) return;
        // Seed an initial summary from the broadcast payload then refetch
        // to backfill the participant list (the broadcast carries only
        // headline info to keep the fan-out cheap on large channels).
        setActiveCall((prev) => prev ?? {
          callId: data.call_id,
          callType: data.call_type ?? 'audio',
          routing: data.routing ?? 'mesh',
          status: 'active',
          startedAt: data.started_at ?? null,
          participantCount: data.participant_count ?? 1,
          hostId: data.started_by,
          participants: [],
        });
        refresh();
      })
    );

    unsubs.push(
      socketManager.on('channel:active_call_ended', (data: any) => {
        if (data?.channel_id !== channelId) return;
        if (activeCall && activeCall.callId !== data.call_id) return;
        setActiveCall(null);
      })
    );

    // Live participant_count badge while the call is open.
    unsubs.push(
      socketManager.on('call_participant_joined', (data: any) => {
        if (!activeCallRef.current) return;
        if (data?.call_id !== activeCallRef.current.callId) return;
        setActiveCall((prev) => {
          if (!prev) return prev;
          if (prev.participants.some((p) => p.userId === data.user_id)) return prev;
          return {
            ...prev,
            participantCount: prev.participantCount + 1,
            participants: [
              ...prev.participants,
              { userId: data.user_id, muted: false, videoOff: false, sharingScreen: false, onHold: false },
            ],
          };
        });
      })
    );

    unsubs.push(
      socketManager.on('call_participant_left', (data: any) => {
        if (!activeCallRef.current) return;
        if (data?.call_id !== activeCallRef.current.callId) return;
        setActiveCall((prev) => {
          if (!prev) return prev;
          const filtered = prev.participants.filter((p) => p.userId !== data.user_id);
          if (filtered.length === prev.participants.length) return prev;
          return {
            ...prev,
            participantCount: Math.max(0, prev.participantCount - 1),
            participants: filtered,
          };
        });
      })
    );

    // Recover from socket reconnects by refetching the snapshot — any
    // events emitted during the dead window were delivered to other
    // sockets but not to ours.
    unsubs.push(
      socketManager.on('connect', () => {
        refresh();
      })
    );

    return () => {
      for (const u of unsubs) {
        try { u(); } catch { /* swallow */ }
      }
    };
  }, [channelId, refresh, activeCall]);

  // ── Derive QuickCallSheet's preview shape ───────────────
  const activeCallParticipants: CallParticipantPreview[] = (activeCall?.participants ?? []).map((p) => {
    const meta = resolveUser?.(p.userId);
    return {
      id: p.userId,
      displayName: meta?.displayName ?? p.userId,
      avatar: meta?.avatar,
      isMuted: p.muted,
      hasVideo: !p.videoOff,
    };
  });

  return {
    hasActiveCall: !!activeCall,
    activeCall,
    activeCallParticipants,
    loading,
    refresh,
  };
}
