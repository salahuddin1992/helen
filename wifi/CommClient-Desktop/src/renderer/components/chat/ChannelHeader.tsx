/**
 * ChannelHeader.tsx
 * Top bar showing channel/group name, member count, online status, and call action buttons.
 */

import React, { useCallback, useState } from 'react';
// ExternalLink stands in for a chain-link icon — lucide-react 0.383
// declares ``Link`` but its .d.ts doesn't actually export it.
import { Phone, Video, AlertCircle, Users, Hash, Trash2, Bell, BellOff, AtSign, ExternalLink as LinkIcon, Clock as Hourglass } from 'lucide-react';
import { InviteLinkPanel } from '@/components/chat/invite/InviteLinkPanel';
import { SlowModePanel } from '@/components/chat/slow-mode/SlowModePanel';
import { ChannelTTLPanel } from '@/components/chat/ttl/ChannelTTLPanel';
import { t } from '@/i18n';
import { useCallStore } from '@/stores/call.store.v2';
import { useContactsStore } from '@/stores/contacts.store';
import { useAuthStore } from '@/stores/auth.store';
import { useChatStore } from '@/stores/chat.store.v2';
import { useSettingsStore } from '@/stores/settings.store';
import type { Channel } from '@/types';
import { callController } from '@/services/call/CallController';
import { callErrorLog } from '@/services/call/CallErrorLog';

interface ChannelHeaderProps {
  channel: Channel | null;
  onCallAction?: (type: 'audio' | 'video' | 'screen') => void;
}

/**
 * Get online count for a group channel
 */
function getOnlineCount(channel: Channel, getUserStatus: (id: string) => string): number {
  return channel.members.filter((m) => getUserStatus(m.user_id) === 'online').length;
}

/**
 * Get display name for channel
 */
function getChannelDisplayName(channel: Channel, getCurrentUserId?: () => string): string {
  if (channel.type === 'group') {
    return channel.name || 'Unnamed Group';
  }

  // For DMs, find the other participant's name
  const currentUserId = getCurrentUserId?.() || useAuthStore.getState().user?.id || '';
  const otherMember = channel.members.find((m) => m.user_id !== currentUserId);
  return otherMember?.display_name || 'Unknown User';
}

export function ChannelHeader({
  channel,
  onCallAction,
}: ChannelHeaderProps) {
  const { getUserStatus } = useContactsStore();

  // Live disabled state — derived from the call controller, not a local
  // `useState` toggled in click handlers. Single source of truth. Buttons
  // are disabled whenever a call setup is in flight or active so a
  // double-click can't enqueue a second call.
  const [callBusy, setCallBusy] = useState(false);
  React.useEffect(
    () => callController.subscribe((s) => {
      setCallBusy(
        s.isStartingCall ||
        s.state === 'preparing' ||
        s.state === 'requestingPermissions' ||
        s.state === 'connecting' ||
        s.state === 'connected' ||
        s.state === 'reconnecting',
      );
    }),
    [],
  );

  // Both call handlers route through CallController so:
  //   • The click returns synchronously — no `await` blocks the UI thread.
  //   • Duplicate clicks are absorbed by the controller's `isStartingCall`
  //     lock instead of stacking concurrent flows.
  //   • Each step is bounded by a timeout and retries with backoff.
  //   • Errors land in CallErrorLog and surface in DebugCallPanel.
  // The CallView overlay is rendered by App.tsx as soon as engine.status
  // flips to "active", so we don't navigate manually here.
  const startCall = useCallback((media: 'audio' | 'video') => {
    if (!channel) return;
    try {
      if (channel.type === 'dm') {
        const me = useAuthStore.getState().user?.id || '';
        const otherMember = channel.members.find((m) => m.user_id !== me);
        if (!otherMember) {
          callErrorLog.warn('ChannelHeader', 'DM has no other member; aborting');
          return;
        }
        callController.start({
          targetUserId: otherMember.user_id,
          media,
        });
      } else {
        callController.start({
          channelId: channel.id,
          media,
        });
      }
      onCallAction?.(media);
    } catch (error) {
      // Defensive — `start()` is non-throwing by contract, but a bad
      // import or guard violation should still be visible.
      callErrorLog.error('ChannelHeader', `${media} call dispatch failed`, error);
    }
  }, [channel, onCallAction]);

  const handleAudioCall = useCallback(() => startCall('audio'), [startCall]);
  const handleVideoCall = useCallback(() => startCall('video'), [startCall]);

  // Screen share for now is "video call + share" — same non-blocking path
  // as audio/video. The transient loading state is gone because the start
  // is fire-and-forget; the controller drives lifecycle.
  const handleScreenShare = useCallback(() => {
    startCall('video');
    onCallAction?.('screen');
  }, [startCall, onCallAction]);

  if (!channel) {
    return (
      <div className="h-16 border-b border-slate-800 bg-slate-900 flex items-center justify-center text-slate-400">
        <p className="text-sm">{t('chat.no_channels')}</p>
      </div>
    );
  }

  const displayName = getChannelDisplayName(channel);
  const isGroup = channel.type === 'group';
  const onlineCount = isGroup ? getOnlineCount(channel, getUserStatus) : null;

  // For DMs, get status of other user
  let dmStatus: string | null = null;
  if (!isGroup) {
    const otherMember = channel.members.find(
      (m) => m.user_id !== (useAuthStore.getState().user?.id || '')
    );
    if (otherMember) {
      dmStatus = getUserStatus(otherMember.user_id);
    }
  }

  return (
    <div className="h-16 border-b border-slate-800 bg-slate-900 flex items-center justify-between px-4 gap-3">
      {/* Left: Channel info — ``min-w-0`` lets the channel name
          truncate instead of pushing the action buttons off-screen.
          ``flex-1`` lets it grow but ``min-w-0`` is what actually
          enables truncation on flex children. */}
      <div className="flex items-center gap-3 flex-1 min-w-0">
        {/* Icon */}
        <div className="text-slate-400 flex-shrink-0">
          {isGroup ? (
            <Hash className="w-5 h-5" />
          ) : (
            <div className="w-10 h-10 rounded-full bg-gradient-to-br from-blue-400 to-purple-500 flex items-center justify-center text-white text-sm font-semibold">
              {displayName.charAt(0).toUpperCase()}
            </div>
          )}
        </div>

        {/* Name and metadata — truncate so calls/buttons stay visible.
            For DMs we paint the status text + dot green when the peer
            is online. For groups we colour the "N online" segment
            green so the operator can tell at a glance whether anyone
            is currently reachable. */}
        <div className="flex flex-col min-w-0">
          <h2 className="text-white font-semibold text-base truncate">{displayName}</h2>
          <p className="text-xs truncate flex items-center gap-1.5">
            {isGroup ? (
              <>
                <span className="text-slate-400">{channel.member_count} members,</span>
                <span className={(onlineCount ?? 0) > 0 ? 'text-green-400 flex items-center gap-1' : 'text-slate-400'}>
                  {(onlineCount ?? 0) > 0 && (
                    <span className="relative flex w-1.5 h-1.5">
                      <span className="absolute inset-0 rounded-full bg-green-400 animate-ping opacity-60" />
                      <span className="relative w-1.5 h-1.5 rounded-full bg-green-400" />
                    </span>
                  )}
                  {onlineCount} online
                </span>
              </>
            ) : dmStatus === 'online' ? (
              <span className="text-green-400 flex items-center gap-1">
                <span className="relative flex w-1.5 h-1.5">
                  <span className="absolute inset-0 rounded-full bg-green-400 animate-ping opacity-60" />
                  <span className="relative w-1.5 h-1.5 rounded-full bg-green-400" />
                </span>
                {t('status.online') || 'Online'}
              </span>
            ) : (
              <span className="text-slate-400">
                {dmStatus ? (t(`status.${dmStatus}`) || dmStatus) : 'Offline'}
              </span>
            )}
          </p>
        </div>
      </div>

      {/* Right: Action buttons — ``flex-shrink-0`` guarantees the
          call buttons (audio, video, screen share, members) NEVER
          collapse. They always remain visible regardless of how
          long the channel name is or how narrow the window gets. */}
      <div className="flex items-center gap-2 flex-shrink-0">
        {/* Audio call button */}
        <button
          onClick={handleAudioCall}
          disabled={callBusy}
          className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
          title={t('call.audio_call')}
        >
          <Phone className="w-5 h-5" />
        </button>

        {/* Video call button */}
        <button
          onClick={handleVideoCall}
          disabled={callBusy}
          className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
          title={t('call.video_call')}
        >
          <Video className="w-5 h-5" />
        </button>

        {/* Screen share button */}
        <button
          onClick={handleScreenShare}
          disabled={callBusy}
          className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
          title={t('call.share_screen')}
        >
          {callBusy ? (
            <div className="w-5 h-5 animate-spin rounded-full border-2 border-white border-t-transparent" />
          ) : (
            <AlertCircle className="w-5 h-5" />
          )}
        </button>

        {/* Info button (optional, could show member list, settings, etc.) */}
        {isGroup && (
          <button
            className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition"
            title="Channel members"
          >
            <Users className="w-5 h-5" />
          </button>
        )}

        {/* Per-channel mute toggle — three-way (all → mentions → muted → all).
            UI-only; the server doesn't track this so different devices each
            keep their own mute settings. The unread count is unaffected; only
            desktop popups (via IntegrationBridge) honour the mode. */}
        <ChannelMuteToggle channelId={channel.id} />

        {/* Invite-link panel — only for groups (DMs are 2-party so an
            invite makes no sense). Server still enforces ownership at
            mint time; this hides the button to keep the header clean. */}
        <InviteLinkButton channel={channel} />

        {/* Slow-mode panel — admin-only on groups. */}
        <SlowModeButton channel={channel} />

        {/* Auto-delete (TTL) panel — admin-only on groups. */}
        <TTLButton channel={channel} />

        {/* Delete channel — visible only when current user can perform it
            (creator, site admin, or DM participant). The server enforces
            authz too; this hide-button-when-unauthorized is purely UX. */}
        <DeleteChannelButton channel={channel} />
      </div>
    </div>
  );
}

const ChannelMuteToggle: React.FC<{ channelId: string }> = ({ channelId }) => {
  const settings = useSettingsStore((s) => s.settings);
  const update = useSettingsStore((s) => s.update);
  const mode = settings.channelMutes?.[channelId] ?? 'all';

  const cycle = () => {
    const next: 'all' | 'mentions' | 'muted' =
      mode === 'all' ? 'mentions' : mode === 'mentions' ? 'muted' : 'all';
    const map = { ...(settings.channelMutes || {}) };
    if (next === 'all') delete map[channelId];
    else map[channelId] = next;
    update({ channelMutes: map });
  };

  const Icon = mode === 'muted' ? BellOff : mode === 'mentions' ? AtSign : Bell;
  const tone =
    mode === 'muted' ? 'text-red-400' : mode === 'mentions' ? 'text-yellow-400' : 'text-slate-400';
  const tip =
    mode === 'muted'
      ? t('chat.notifications_muted') || 'Notifications muted (click to enable)'
      : mode === 'mentions'
      ? t('chat.notifications_mentions_only') || 'Mentions only (click to mute)'
      : t('chat.notifications_all') || 'All notifications (click for mentions only)';
  return (
    <button
      onClick={cycle}
      title={tip}
      className={`p-2 ${tone} hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition`}
    >
      <Icon className="w-5 h-5" />
    </button>
  );
};

// ── Delete-channel control ───────────────────────────────────────────
//
// For a group: shown to the creator AND any user with role=admin (so
// site admins can wipe runaway rooms).
// For a DM:    shown to either participant.
// On click: confirm → store.deleteChannel → optimistic removal +
// server fan-out via socket.
// ── Invite-link button — opens the InviteLinkPanel modal ─────────────
//
// Visibility rules:
//   * Groups only — DMs are 2-party, an invite makes no sense.
//   * Channel creator OR site admin — others see no button. Server
//     re-checks ownership when minting.
function InviteLinkButton({ channel }: { channel: Channel }) {
  const me = useAuthStore((s) => s.user);
  const [open, setOpen] = useState(false);

  if (!me) return null;
  const isGroup = (channel.type || '').toLowerCase() === 'group';
  if (!isGroup) return null;
  const canInvite = channel.created_by === me.id || me.role === 'admin';
  if (!canInvite) return null;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="p-2 text-slate-400 hover:text-white bg-slate-800
                   hover:bg-slate-700 rounded-lg transition"
        title={t('chat.invite_link') || 'رابط الدعوة'}
      >
        <LinkIcon className="w-5 h-5" />
      </button>
      {open && (
        <InviteLinkPanel
          channelId={channel.id}
          channelName={channel.name || undefined}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

// ── Slow-mode button — opens the SlowModePanel modal ───────────
function SlowModeButton({ channel }: { channel: Channel }) {
  const me = useAuthStore((s) => s.user);
  const [open, setOpen] = useState(false);

  if (!me) return null;
  const isGroup = (channel.type || '').toLowerCase() === 'group';
  if (!isGroup) return null;
  const canModerate = channel.created_by === me.id || me.role === 'admin';
  if (!canModerate) return null;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="p-2 text-slate-400 hover:text-white bg-slate-800
                   hover:bg-slate-700 rounded-lg transition"
        title={t('chat.slow_mode') || 'وضع البطء'}
      >
        <Hourglass className="w-5 h-5" />
      </button>
      {open && (
        <SlowModePanel
          channelId={channel.id}
          channelName={channel.name || undefined}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

// ── TTL button — opens the ChannelTTLPanel modal ───────────────
function TTLButton({ channel }: { channel: Channel }) {
  const me = useAuthStore((s) => s.user);
  const [open, setOpen] = useState(false);

  if (!me) return null;
  const isGroup = (channel.type || '').toLowerCase() === 'group';
  if (!isGroup) return null;
  const canModerate = channel.created_by === me.id || me.role === 'admin';
  if (!canModerate) return null;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="p-2 text-slate-400 hover:text-white bg-slate-800
                   hover:bg-slate-700 rounded-lg transition"
        title="الحذف التلقائي للرسائل"
      >
        <Trash2 className="w-5 h-5" />
      </button>
      {open && (
        <ChannelTTLPanel
          channelId={channel.id}
          channelName={channel.name || undefined}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function DeleteChannelButton({ channel }: { channel: Channel }) {
  const me = useAuthStore((s) => s.user);
  const deleteChannel = useChatStore((s) => s.deleteChannel);
  const [busy, setBusy] = useState(false);

  if (!me) return null;
  const isDm        = (channel.type || '').toLowerCase() === 'dm';
  const isCreator   = channel.created_by === me.id;
  const isSiteAdmin = me.role === 'admin';
  const canDelete   = isDm || isCreator || isSiteAdmin;
  if (!canDelete) return null;

  const onClick = async () => {
    const label = isDm ? 'this conversation' : `the group "${channel.name || 'Unnamed'}"`;
    if (!window.confirm(`Delete ${label}? This removes it for everyone in it. Cannot be undone.`)) return;
    setBusy(true);
    try {
      await deleteChannel(channel.id);
    } catch (err: any) {
      window.alert('Failed to delete: ' + (err?.message || 'unknown'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      onClick={onClick}
      disabled={busy}
      className="p-2 text-slate-400 hover:text-red-400 bg-slate-800 hover:bg-red-500/10 rounded-lg transition disabled:opacity-50"
      title={isDm
        ? 'Delete conversation'
        : isCreator
            ? 'Delete this group (you created it)'
            : 'Delete this group (site admin)'}
    >
      <Trash2 className="w-5 h-5" />
    </button>
  );
}
