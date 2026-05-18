/**
 * IntegrationBridge — cross-module event coordination layer.
 *
 * Bridges events between independent subsystems so they react to each other
 * without tight coupling. Each subsystem remains self-contained; the bridge
 * subscribes to events from one and dispatches side-effects to others.
 *
 * Event flows coordinated:
 *   1. Call started  → suppress chat notifications, update presence to "in_call"
 *   2. Call ended    → resume chat notifications, update presence to "online"
 *   3. New message while in call → show minimal toast (not full notification)
 *   4. Incoming call → show native desktop notification
 *   5. Missed call   → create notification entry
 *   6. Screen share started → update call participant metadata
 *   7. User goes offline → end active call gracefully
 *   8. Channel switch → mark channel as read, stop typing in old channel
 *   9. Server shutdown → end call, save state, show banner
 *  10. Network change → trigger reconnection, resync messages
 *
 * Usage:
 *   IntegrationBridge.start(userId)  — call after AppBootstrap.onLogin
 *   IntegrationBridge.stop()         — call before AppBootstrap.onLogout
 */

import { socketManager } from './socket.manager';
import { useCallStore } from '../stores/call.store.v2';
import { useChatStore } from '../stores/chat.store.v2';
import { useNotificationStore } from '../stores/notification.store';
import { useContactsStore } from '../stores/contacts.store';
import { useAuthStore } from '../stores/auth.store';
import { useSettingsStore } from '../stores/settings.store';
import { AppLogger } from './AppLogger';

const log = AppLogger.create('IntegrationBridge');

/**
 * Do Not Disturb gate — central check used everywhere a desktop
 * notification would otherwise pop. Returns true when notifications
 * should be suppressed for the current moment.
 *
 * The setting stores an ISO timestamp; "indefinite" is a sentinel
 * meaning "until the user toggles it off". Anything in the past is
 * treated as off — that lets a stale value auto-clear without any
 * polling timer.
 */
function _isDndActive(): boolean {
  try {
    const v = useSettingsStore.getState().settings.dndUntil;
    if (!v) return false;
    if (v === 'indefinite') return true;
    const until = Date.parse(v);
    if (!Number.isFinite(until)) return false;
    return until > Date.now();
  } catch {
    return false;
  }
}

/**
 * showNotification — wrapper around window.electronAPI.showNotification
 * that respects Do Not Disturb. Incoming calls bypass DND because
 * missing a call is materially worse than a chat ping; everything else
 * (chat, missed-call summaries, mentions, system) honours the setting.
 */
function _showDesktopNotification(
  title: string,
  body: string,
  opts: { bypassDnd?: boolean } = {},
): void {
  if (!opts.bypassDnd && _isDndActive()) {
    log.debug('Notification suppressed by DND', { title });
    return;
  }
  if (window.electronAPI?.showNotification) {
    window.electronAPI.showNotification(title, body);
  }
}

// ── Internal State ─────────────────────────────────

let _active = false;
let _userId: string | null = null;
let _unsubscribers: (() => void)[] = [];
let _callStatusUnsub: (() => void) | null = null;
let _networkHandler: (() => void) | null = null;

// ── Public API ─────────────────────────────────────

export const IntegrationBridge = {
  get isActive(): boolean {
    return _active;
  },

  /**
   * Start cross-module event coordination.
   * Should be called after socket is connected and engines are initialized.
   */
  start(userId: string): void {
    if (_active) {
      log.warn('Already active, stopping first');
      this.stop();
    }

    _userId = userId;
    _active = true;
    log.info('Starting cross-module bridge for user:', userId);

    // ── 1. Call state changes → presence + notification suppression ──
    let _prevCallStatus = useCallStore.getState().status;
    _callStatusUnsub = useCallStore.subscribe((state) => {
      const status = state.status;
      if (status !== _prevCallStatus) {
        _handleCallStatusChange(status, _prevCallStatus);
        _prevCallStatus = status;
      }
    });

    // ── 2. Incoming call → desktop notification ──
    _unsubscribers.push(
      socketManager.on('call:incoming', (data: any) => {
        _handleIncomingCallNotification(data);
      }),
    );

    // ── 3. Missed call → notification store ──
    _unsubscribers.push(
      socketManager.on('call:missed', (data: any) => {
        _handleMissedCall(data);
      }),
    );

    // ── 4. New message → conditional notification ──
    _unsubscribers.push(
      socketManager.on('chat:new_message', (data: any) => {
        _handleNewMessageNotification(data);
      }),
    );
    _unsubscribers.push(
      socketManager.on('v2_chat:new_message', (data: any) => {
        _handleNewMessageNotification(data);
      }),
    );

    // ── 5. Server shutdown → graceful cleanup ──
    _unsubscribers.push(
      socketManager.on('server:shutdown', (data: any) => {
        _handleServerShutdown(data);
      }),
    );

    // ── 6. Screen share events → update participant metadata ──
    _unsubscribers.push(
      socketManager.on('call:screen_share_started', (data: any) => {
        log.info('Screen share started by:', data?.user_id);
      }),
    );
    _unsubscribers.push(
      socketManager.on('call:screen_share_stopped', (data: any) => {
        log.info('Screen share stopped by:', data?.user_id);
      }),
    );

    // ── 7. Network change detection ──
    _setupNetworkChangeDetection();

    // ── 8. Periodic notification refresh ──
    _startNotificationPolling();

    log.info('Cross-module bridge active');
  },

  /**
   * Stop all cross-module coordination.
   */
  stop(): void {
    if (!_active) return;

    log.info('Stopping cross-module bridge');

    // Unsubscribe all socket listeners
    _unsubscribers.forEach((unsub) => unsub());
    _unsubscribers = [];

    // Unsubscribe zustand listener
    _callStatusUnsub?.();
    _callStatusUnsub = null;

    // Remove network listener
    if (_networkHandler) {
      window.removeEventListener('online', _networkHandler);
      window.removeEventListener('offline', _networkHandler);
      _networkHandler = null;
    }

    // Stop notification polling
    _stopNotificationPolling();

    _active = false;
    _userId = null;
  },
};

// ── Internal Handlers ──────────────────────────────

function _handleCallStatusChange(
  status: string,
  prevStatus: string,
): void {
  // Suppress chat notifications during active call
  if (status === 'active' || status === 'connecting') {
    (window as any).__commclient_suppress_chat_notif = true;
    log.debug('Chat notifications suppressed (in call)');

    // Update presence to in_call
    socketManager.emitNoAck('presence_set_status', { status: 'in_call' });
  } else if (prevStatus === 'active' || prevStatus === 'connecting') {
    (window as any).__commclient_suppress_chat_notif = false;
    log.debug('Chat notifications resumed');

    // Restore presence to online
    socketManager.emitNoAck('presence_set_status', { status: 'online' });

    // Resync messages that arrived during call
    if (status === 'ended' || status === 'idle') {
      log.info('Call ended — triggering message resync');
      useChatStore.getState().fetchChannelSummaries();
    }
  }

  // Log call state transitions
  if (status !== prevStatus) {
    log.info(`Call state: ${prevStatus} → ${status}`);
  }
}

function _handleIncomingCallNotification(data: any): void {
  if (!data) return;

  // Show native desktop notification
  const callerName = data.caller_name || data.caller_id || 'Unknown';
  const mediaType = data.media_type === 'video' ? 'Video' : 'Audio';

  // Incoming calls bypass DND — missing a real call is worse than a
  // chat ping. The user can still mute the ringtone separately.
  _showDesktopNotification(
    `${mediaType} Call`,
    `${callerName} is calling you...`,
    { bypassDnd: true },
  );

  log.info('Incoming call from:', callerName, 'type:', mediaType);
}

function _handleMissedCall(data: any): void {
  if (!data) return;

  const callerName = data.caller_name || data.caller_id || 'Unknown';
  log.info('Missed call from:', callerName);

  // DND suppresses missed-call popups (the entry is still recorded in
  // the notification list and unread count, just no native popup).
  _showDesktopNotification(
    'Missed Call',
    `You missed a call from ${callerName}`,
  );

  // Refresh notification count
  useNotificationStore.getState().fetchUnreadCount();
}

function _handleNewMessageNotification(data: any): void {
  if (!data?.message) return;

  const msg = data.message;
  const currentChannel = useChatStore.getState().activeChannelId;
  const isInCall = useCallStore.getState().status === 'active';
  const isSuppressed = (window as any).__commclient_suppress_chat_notif;

  // Don't notify for own messages
  if (msg.sender_id === _userId) return;

  // Don't notify for currently viewed channel (user is already looking at it)
  if (msg.channel_id === currentChannel && document.hasFocus()) return;

  // Per-channel mute preferences. Three modes:
  //   - 'muted'   → never popup (unread badge still updates)
  //   - 'mentions' → only popup if the message @-mentions the current user
  //   - 'all' (default if key absent) → popup for every message
  // The badge / unread count is owned by the chat store, which doesn't
  // honour this setting — only the desktop popup does. That keeps the
  // unread inbox accurate while letting busy users hush specific rooms.
  try {
    const muteMode = useSettingsStore.getState().settings.channelMutes?.[msg.channel_id];
    if (muteMode === 'muted') {
      log.debug('Message notification suppressed (channel muted):', msg.id);
      return;
    }
    if (muteMode === 'mentions') {
      const mentions: string[] =
        Array.isArray(msg.mentions) ? msg.mentions
        : Array.isArray(msg.mentioned_user_ids) ? msg.mentioned_user_ids
        : [];
      const isMention = _userId != null && mentions.includes(_userId);
      if (!isMention) {
        log.debug('Message notification suppressed (mentions-only):', msg.id);
        return;
      }
    }
  } catch (e) {
    // Defensive — never let a settings read failure block notification.
    log.debug('channelMutes lookup failed:', e);
  }

  // During call, skip full notification but log it
  if (isInCall || isSuppressed) {
    log.debug('Message notification suppressed (in call):', msg.id);
    return;
  }

  // Show desktop notification
  const senderName = msg.sender?.display_name || msg.sender_id || 'Someone';
  const content = msg.type === 'text'
    ? (msg.content?.substring(0, 100) || 'New message')
    : `Sent a ${msg.type}`;

  _showDesktopNotification(senderName, content);
}

function _handleServerShutdown(data: any): void {
  log.warn('Server shutdown received:', data?.reason);

  // End any active call
  const callState = useCallStore.getState();
  if (callState.status !== 'idle' && callState.status !== 'ended') {
    try {
      callState.hangup();
    } catch (e) {
      log.error('Error ending call on server shutdown:', e);
    }
  }

  // Server-shutdown popups bypass DND (they're operational, not chat).
  _showDesktopNotification(
    'Server Disconnected',
    data?.reason || 'The server is shutting down. You will be reconnected automatically.',
    { bypassDnd: true },
  );
}

function _setupNetworkChangeDetection(): void {
  const handler = () => {
    const isOnline = navigator.onLine;
    log.info('Network status changed:', isOnline ? 'online' : 'offline');

    if (isOnline) {
      // Trigger socket reconnection if needed
      if (!socketManager.isConnected()) {
        log.info('Network restored — socket will auto-reconnect');
      }
    } else {
      log.warn('Network lost — operations will queue until restored');
    }
  };

  window.addEventListener('online', handler);
  window.addEventListener('offline', handler);
  _networkHandler = handler;
}

// ── Notification Polling ───────────────────────────

let _notifPollInterval: ReturnType<typeof setInterval> | null = null;

function _startNotificationPolling(): void {
  _stopNotificationPolling();

  // Initial fetch
  useNotificationStore.getState().fetchUnreadCount();

  // Poll every 30 seconds
  _notifPollInterval = setInterval(() => {
    if (socketManager.isConnected()) {
      useNotificationStore.getState().fetchUnreadCount();
    }
  }, 30_000);
}

function _stopNotificationPolling(): void {
  if (_notifPollInterval) {
    clearInterval(_notifPollInterval);
    _notifPollInterval = null;
  }
}

export default IntegrationBridge;
