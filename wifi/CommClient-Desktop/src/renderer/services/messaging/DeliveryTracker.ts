/**
 * DeliveryTracker — per-message delivery and read state tracking.
 *
 * Maintains a local map of message delivery states:
 *   - sent      — server ACKed the message
 *   - delivered  — at least one recipient received it
 *   - read       — at least one recipient read it
 *
 * For DMs, "delivered" and "read" refer to the single other participant.
 * For group chats, tracks per-recipient delivery/read counts.
 *
 * Integrates with backend v2_chat:message_delivered and v2_chat:message_read events.
 */

import { socketManager } from '../socket.manager';

export type DeliveryStatus = 'pending' | 'sent' | 'delivered' | 'read' | 'failed';

export interface MessageDeliveryState {
  messageId: string;
  clientId?: string;
  channelId: string;
  status: DeliveryStatus;
  /** Recipient IDs that have received the message */
  deliveredTo: Set<string>;
  /** Recipient IDs that have read the message */
  readBy: Set<string>;
  /** When first delivered (to any recipient) */
  deliveredAt: string | null;
  /** When first read (by any recipient) */
  readAt: string | null;
}

export interface ReceiptDetail {
  messageId: string;
  deliveredCount: number;
  readCount: number;
  totalRecipients: number;
  recipients: Array<{
    userId: string;
    status: 'sent' | 'delivered' | 'read';
    deliveredAt: string | null;
    readAt: string | null;
  }>;
}

export interface DeliveryCallbacks {
  /** A message's delivery status changed */
  onStatusChange: (messageId: string, status: DeliveryStatus) => void;
  /** Read receipt received for a channel */
  onChannelRead: (channelId: string, readerId: string, upToMessageId: string | null) => void;
}

export class DeliveryTracker {
  private states: Map<string, MessageDeliveryState> = new Map();
  private clientToServer: Map<string, string> = new Map(); // clientId → serverId
  private callbacks: DeliveryCallbacks;
  private _socketUnsubs: Array<() => void> = [];
  private _pendingDeliveryConfirmations: string[] = [];
  private _destroyed = false;
  private _pruneTimer: ReturnType<typeof setInterval> | null = null;

  constructor(callbacks: DeliveryCallbacks) {
    this.callbacks = callbacks;
  }

  /**
   * Register socket listeners for delivery events.
   */
  init(): void {
    // Periodically prune the delivery-state map. Without this, the map
    // grew unbounded — every message ever seen accumulated a state
    // entry — eventually slowing the renderer on long-running sessions.
    // 5-minute cadence + 200-per-channel cap is plenty for "show
    // recent receipts" UX without holding stale state forever.
    this._pruneTimer = setInterval(() => {
      try { this.prune(200); } catch { /* never fail the loop */ }
    }, 5 * 60 * 1000);

    this._socketUnsubs.push(
      socketManager.on('v2_chat:message_delivered', (data: any) => {
        this._handleDelivered(data);
      })
    );

    this._socketUnsubs.push(
      socketManager.on('v2_chat:message_read', (data: any) => {
        this._handleRead(data);
      })
    );

    // Listen for socket reconnection to flush pending delivery confirmations
    this._socketUnsubs.push(
      socketManager.on('connect', () => {
        this._flushPendingDeliveryConfirmations();
      })
    );
  }

  /**
   * Track a new outbound message (optimistic — before server ACK).
   */
  trackPending(clientId: string, channelId: string): void {
    this.states.set(`client:${clientId}`, {
      messageId: `client:${clientId}`,
      clientId,
      channelId,
      status: 'pending',
      deliveredTo: new Set(),
      readBy: new Set(),
      deliveredAt: null,
      readAt: null,
    });
  }

  /**
   * Message was ACKed by server — promote from pending to sent.
   */
  markSent(clientId: string, serverId: string): void {
    const pendingKey = `client:${clientId}`;
    const state = this.states.get(pendingKey);

    if (state) {
      // Move from client key to server key
      this.states.delete(pendingKey);
      state.messageId = serverId;
      state.status = 'sent';
      this.states.set(serverId, state);
      this.clientToServer.set(clientId, serverId);
    } else {
      // Track directly as sent
      this.states.set(serverId, {
        messageId: serverId,
        clientId,
        channelId: '',
        status: 'sent',
        deliveredTo: new Set(),
        readBy: new Set(),
        deliveredAt: null,
        readAt: null,
      });
      this.clientToServer.set(clientId, serverId);
    }

    this.callbacks.onStatusChange(serverId, 'sent');
  }

  /**
   * Mark a message as failed (max retries exceeded in queue).
   */
  markFailed(clientId: string): void {
    const pendingKey = `client:${clientId}`;
    const state = this.states.get(pendingKey);
    if (state) {
      state.status = 'failed';
      this.callbacks.onStatusChange(pendingKey, 'failed');
    }
  }

  /**
   * Get the delivery status for a message.
   */
  getStatus(messageId: string): DeliveryStatus {
    return this.states.get(messageId)?.status || 'sent';
  }

  /**
   * Get full delivery state for a message.
   */
  getState(messageId: string): MessageDeliveryState | undefined {
    return this.states.get(messageId);
  }

  /**
   * Get server ID from client ID.
   */
  resolveClientId(clientId: string): string | undefined {
    return this.clientToServer.get(clientId);
  }

  /**
   * Track an incoming message (received from server).
   * Marks as delivered and notifies sender via socket.
   */
  trackIncoming(messageId: string, channelId: string): void {
    // We received this message, so from our perspective it's delivered
    // The backend already handles marking it delivered on receipt
    this.states.set(messageId, {
      messageId,
      channelId,
      status: 'delivered',
      deliveredTo: new Set(),
      readBy: new Set(),
      deliveredAt: new Date().toISOString(),
      readAt: null,
    });
  }

  /**
   * Mark messages in a channel as read (user opened/scrolled the channel).
   *
   * The local state map is for tracking messages WE sent — read status
   * for those flips when the remote reader's `v2_chat:message_read`
   * broadcast comes back through `_handleRead`. The previous version
   * walked every state in the channel and stamped `readAt` with the
   * current time without flipping `status` or firing `onStatusChange`,
   * which was both wrong (set readAt on outbound messages from our
   * clock, not the reader's) and useless (UI never re-rendered). Drop
   * the local mutation; rely on the server round-trip for authoritative
   * state.
   */
  markChannelRead(channelId: string, upToMessageId?: string): void {
    // Privacy gate — when the user has opted out of sending read
    // receipts, we suppress the emit. The server still receives
    // the message, the user still sees it; we just don't tell
    // peers we saw it. Lazy import keeps this module bootable in
    // contexts where the privacy store doesn't exist (tests).
    try {
      const mod = require('@/stores/privacy.store');
      const sending = mod.usePrivacyStore.getState().send_read_receipts;
      if (!sending) return;
    } catch { /* fall through and emit */ }
    socketManager.emitNoAck('v2_chat_mark_read', {
      channel_id: channelId,
      up_to_message_id: upToMessageId,
    });
  }

  /**
   * Bulk mark messages as delivered (used after reconnection sync).
   * Stores failed confirmations for retry when socket reconnects.
   */
  bulkMarkDelivered(messageIds: string[]): void {
    if (messageIds.length === 0) return;

    // If socket is not connected, queue the confirmations
    if (!socketManager.isConnected()) {
      this._pendingDeliveryConfirmations.push(...messageIds);
      return;
    }

    socketManager.emitNoAck('v2_chat_mark_delivered', {
      message_ids: messageIds,
    });
  }

  /**
   * Confirm batch delivery for multiple messages.
   */
  confirmBatchDelivery(messageIds: string[]): void {
    if (messageIds.length === 0) return;

    socketManager.emitNoAck('v2_chat_confirm_batch_delivery', {
      message_ids: messageIds,
    });

    // Update local states
    for (const msgId of messageIds) {
      const state = this.states.get(msgId);
      if (state && (state.status === 'pending' || state.status === 'sent')) {
        state.status = 'delivered';
        state.deliveredAt = new Date().toISOString();
        this.callbacks.onStatusChange(msgId, 'delivered');
      }
    }
  }

  /**
   * Fetch detailed receipt information for a message.
   */
  async fetchReceiptDetails(messageId: string): Promise<ReceiptDetail> {
    try {
      const response = await socketManager.emit('v2_chat_get_receipt_details', {
        message_id: messageId,
      });

      const detail: ReceiptDetail = {
        messageId: response?.message_id || messageId,
        deliveredCount: response?.delivered_count || 0,
        readCount: response?.read_count || 0,
        totalRecipients: response?.total_recipients || 0,
        recipients: response?.recipients || [],
      };

      return detail;
    } catch (e) {
      console.error('[DeliveryTracker] Failed to fetch receipt details:', e);
      return {
        messageId,
        deliveredCount: 0,
        readCount: 0,
        totalRecipients: 0,
        recipients: [],
      };
    }
  }

  /**
   * Send an explicit read receipt acknowledgement for a message.
   */
  acknowledgeRead(channelId: string, messageId: string): void {
    socketManager.emitNoAck('v2_chat_acknowledge_read', {
      channel_id: channelId,
      message_id: messageId,
    });
  }

  // ── Private: Event Handlers ───────────────────────

  private _handleDelivered(data: any): void {
    // Can be single message or batch
    const messageIds: string[] = data.message_ids
      ? data.message_ids
      : data.message_id
        ? [data.message_id]
        : [];
    const deliveredTo: string = data.delivered_to;
    const deliveredAt: string = data.delivered_at;

    // Batch from delivered_to array
    const recipients: string[] = data.delivered_to
      ? Array.isArray(data.delivered_to)
        ? data.delivered_to
        : [data.delivered_to]
      : [];

    for (const msgId of messageIds) {
      const state = this.states.get(msgId);
      if (state) {
        for (const r of recipients) {
          state.deliveredTo.add(r);
        }
        if (!state.deliveredAt) {
          state.deliveredAt = deliveredAt;
        }
        if (state.status === 'sent' || state.status === 'pending') {
          state.status = 'delivered';
          this.callbacks.onStatusChange(msgId, 'delivered');
        }
      }
    }
  }

  private _handleRead(data: any): void {
    const channelId: string = data.channel_id;
    const readerId: string = data.reader_id;
    const upToMessageId: string | null = data.up_to_message_id || null;

    // Determine the high-water mark for the read receipt. If the server
    // sent an `up_to_message_id`, only messages on/before that one have
    // been read — the previous code marked EVERY message in the channel
    // as read, which was wrong any time a reader scrolled mid-history.
    // We use the upToMessage's `deliveredAt` as the cutoff timestamp;
    // messages with deliveredAt <= cutoff are eligible. If we don't
    // have local state for upToMessageId (e.g. the reader caught up on
    // a message we never tracked), fall back to marking everything.
    let cutoffAt: string | null = null;
    if (upToMessageId) {
      const upToState = this.states.get(upToMessageId);
      if (upToState?.deliveredAt) {
        cutoffAt = upToState.deliveredAt;
      }
    }

    for (const [, state] of this.states) {
      if (state.channelId !== channelId) continue;
      if (cutoffAt && state.deliveredAt && state.deliveredAt > cutoffAt) {
        // Past the read high-water mark — reader hasn't seen this yet.
        continue;
      }
      state.readBy.add(readerId);
      if (!state.readAt) {
        state.readAt = data.read_at;
      }
      if (state.status === 'sent' || state.status === 'delivered') {
        state.status = 'read';
        this.callbacks.onStatusChange(state.messageId, 'read');
      }
    }

    this.callbacks.onChannelRead(channelId, readerId, upToMessageId);
  }

  // ── Cleanup ───────────────────────────────────────

  /**
   * Flush pending delivery confirmations when socket reconnects.
   */
  private _flushPendingDeliveryConfirmations(): void {
    if (this._pendingDeliveryConfirmations.length === 0) return;

    if (!socketManager.isConnected()) {
      // Socket went offline before we could send; will retry on next connect
      return;
    }

    const toSend = [...this._pendingDeliveryConfirmations];
    this._pendingDeliveryConfirmations = [];

    socketManager.emitNoAck('v2_chat_mark_delivered', {
      message_ids: toSend,
    });

    console.log(`[DeliveryTracker] Flushed ${toSend.length} pending delivery confirmations`);
  }

  /**
   * Prune old delivery states to prevent memory growth.
   * Keeps only the last N states per channel.
   */
  prune(maxPerChannel: number = 200): void {
    const byChannel = new Map<string, MessageDeliveryState[]>();

    for (const state of this.states.values()) {
      const list = byChannel.get(state.channelId) || [];
      list.push(state);
      byChannel.set(state.channelId, list);
    }

    for (const [, states] of byChannel) {
      if (states.length > maxPerChannel) {
        // Keep the newest
        states.sort((a, b) => (a.deliveredAt || '').localeCompare(b.deliveredAt || ''));
        const toRemove = states.slice(0, states.length - maxPerChannel);
        for (const s of toRemove) {
          this.states.delete(s.messageId);
        }
      }
    }
  }

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    // Stop the periodic prune timer or it would keep firing on a
    // destroyed instance.
    if (this._pruneTimer) {
      clearInterval(this._pruneTimer);
      this._pruneTimer = null;
    }

    for (const unsub of this._socketUnsubs) {
      unsub();
    }
    this._socketUnsubs = [];
    this.states.clear();
    this.clientToServer.clear();
    this._pendingDeliveryConfirmations = [];
  }
}
