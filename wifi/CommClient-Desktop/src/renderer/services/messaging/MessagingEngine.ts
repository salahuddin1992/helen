/**
 * MessagingEngine — unified orchestrator for the messaging subsystem.
 *
 * Ties together:
 *   - MessageQueue — outbound message queue with retry
 *   - DeliveryTracker — per-message delivery/read state
 *   - SyncManager — reconnection sync
 *
 * Provides a clean API consumed by the Zustand chat store.
 * All socket event registration happens here.
 */

import { socketManager } from '../socket.manager';
import { MessageQueue, OutboundMessage, MessageType } from './MessageQueue';
import { DeliveryTracker, DeliveryStatus } from './DeliveryTracker';
import {
  SyncManager,
  SyncResult,
  SyncedMessage,
  ChannelUnreadInfo,
  ChannelSummary,
} from './SyncManager';

// ── Public Types ────────────────────────────────────

export interface IncomingMessage {
  id: string;
  channelId: string;
  sender: {
    id: string;
    username: string;
    displayName: string;
    avatarUrl: string | null;
  } | null;
  content: string;
  type: string;
  replyTo: string | null;
  fileId: string | null;
  status: string;
  reactions: any[];
  editedAt: string | null;
  createdAt: string;
}

export interface TypingEvent {
  channelId: string;
  userId: string;
  isTyping: boolean;
}

export interface MessageEditedEvent {
  messageId: string;
  channelId: string;
  content: string;
  editedAt: string;
  editorId: string;
}

export interface MessageDeletedEvent {
  messageId: string;
  channelId: string;
  deletedBy: string;
}

export interface ReactionUpdateEvent {
  messageId: string;
  channelId: string;
  reactions: Array<{ emoji: string; count: number; user_ids: string[] }>;
  toggledBy: string;
  emoji: string;
}

export interface MessagePinnedEvent {
  message_id: string;
  pinned_by: string;
  channel_id: string;
}

export interface MessageUnpinnedEvent {
  message_id: string;
  channel_id: string;
}

export interface MessageForwardedEvent {
  original_message_id: string;
  forwarded_to_channel_id: string;
  new_message_id: string;
  forwarded_by: string;
}

export interface MessagingEngineCallbacks {
  /** New message received from another user */
  onIncomingMessage: (msg: IncomingMessage) => void;
  /** Our message was sent (server ACK) — clientId → serverId */
  onMessageSent: (clientId: string, serverId: string, createdAt: string) => void;
  /** Our message failed permanently */
  onMessageFailed: (clientId: string, error: string) => void;
  /** Message delivery status changed */
  onDeliveryStatusChange: (messageId: string, status: DeliveryStatus) => void;
  /** Typing indicator */
  onTyping: (event: TypingEvent) => void;
  /** Message edited */
  onMessageEdited: (event: MessageEditedEvent) => void;
  /** Message deleted */
  onMessageDeleted: (event: MessageDeletedEvent) => void;
  /** Reaction update */
  onReactionUpdate: (event: ReactionUpdateEvent) => void;
  /** Unread counts updated */
  onUnreadUpdate: (unread: Record<string, ChannelUnreadInfo>) => void;
  /** Channel summaries updated */
  onSummariesUpdate: (summaries: ChannelSummary[]) => void;
  /** Reconnection sync completed */
  onSyncComplete: (result: SyncResult) => void;
  /** Channel read by someone */
  onChannelRead: (channelId: string, readerId: string, upToMessageId: string | null) => void;
  /** Queue state changed (pending/sending counts) */
  onQueueStateChange: (pending: number, sending: number) => void;
  /** Message pinned */
  onMessagePinned?: (event: MessagePinnedEvent) => void;
  /** Message unpinned */
  onMessageUnpinned?: (event: MessageUnpinnedEvent) => void;
  /** Message forwarded */
  onMessageForwarded?: (event: MessageForwardedEvent) => void;
  /** Error */
  onError: (error: string) => void;
}

// ── Engine ──────────────────────────────────────────

export class MessagingEngine {
  private queue: MessageQueue;
  private tracker: DeliveryTracker;
  private sync: SyncManager;
  private callbacks: MessagingEngineCallbacks;
  private _socketUnsubs: Array<() => void> = [];
  private _destroyed = false;

  constructor(callbacks: MessagingEngineCallbacks) {
    this.callbacks = callbacks;

    // Initialize sub-components
    this.queue = new MessageQueue({
      onSent: (clientId, serverId, createdAt) => {
        this.tracker.markSent(clientId, serverId);
        this.sync.updateTimestamp(createdAt);
        this.callbacks.onMessageSent(clientId, serverId, createdAt);
      },
      onFailed: (clientId, error) => {
        this.tracker.markFailed(clientId);
        this.callbacks.onMessageFailed(clientId, error);
      },
      onQueueStateChange: (pending, sending) => {
        this.callbacks.onQueueStateChange(pending, sending);
      },
    });

    this.tracker = new DeliveryTracker({
      onStatusChange: (messageId, status) => {
        this.callbacks.onDeliveryStatusChange(messageId, status);
      },
      onChannelRead: (channelId, readerId, upToMessageId) => {
        this.callbacks.onChannelRead(channelId, readerId, upToMessageId);
      },
    });

    this.sync = new SyncManager({
      onSyncComplete: (result) => {
        // Register synced messages with tracker
        for (const [channelId, msgs] of Object.entries(result.channels)) {
          for (const msg of msgs as SyncedMessage[]) {
            this.tracker.trackIncoming(msg.id, channelId);
          }
        }
        this.callbacks.onSyncComplete(result);
      },
      onUnreadUpdate: (unread) => {
        this.callbacks.onUnreadUpdate(unread);
      },
      onSummariesUpdate: (summaries) => {
        this.callbacks.onSummariesUpdate(summaries);
      },
      onSyncError: (error) => {
        this.callbacks.onError(`Sync error: ${error}`);
      },
    });
  }

  // ── Initialization ────────────────────────────────

  init(): void {
    // Guard against double-init. Calling init() twice without a
    // matching destroy() (e.g. a buggy view that mounts initMessaging
    // on render) used to push another full set of socket listeners
    // onto _socketUnsubs, so every incoming server event then fanned
    // out N times into Zustand `set()` calls — wasted CPU plus visible
    // re-render flicker on fast streams.
    if (this._socketUnsubs.length > 0) {
      return;
    }
    this.tracker.init();
    this.sync.init();
    this._registerSocketListeners();
  }

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    this._unregisterSocketListeners();
    this.queue.destroy();
    this.tracker.destroy();
    this.sync.destroy();
  }

  // ── Sending Messages ──────────────────────────────

  /**
   * Send a message. Returns a client-side temporary ID.
   * Message is queued and sent asynchronously.
   */
  sendMessage(
    channelId: string,
    content: string,
    type: MessageType = 'text',
    options?: { replyTo?: string; fileId?: string }
  ): string {
    const clientId = this.queue.enqueue(channelId, content, type, options);
    this.tracker.trackPending(clientId, channelId);
    return clientId;
  }

  /**
   * Retry a failed message.
   */
  retryMessage(clientId: string): void {
    this.queue.retry(clientId);
  }

  /**
   * Cancel a queued message.
   */
  cancelMessage(clientId: string): void {
    this.queue.cancel(clientId);
  }

  // ── Typing Indicators ─────────────────────────────

  /** Emit typing-start unless the user has disabled the typing
   *  indicator in privacy settings. */
  startTyping(channelId: string): void {
    if (!_isTypingEmitAllowed()) return;
    socketManager.emitNoAck('v2_chat_typing_start', { channel_id: channelId });
  }

  stopTyping(channelId: string): void {
    if (!_isTypingEmitAllowed()) return;
    socketManager.emitNoAck('v2_chat_typing_stop', { channel_id: channelId });
  }

  // ── Read / Delivery ───────────────────────────────

  /**
   * Mark a channel as read (user opened the channel or scrolled to bottom).
   */
  markChannelRead(channelId: string, upToMessageId?: string): void {
    this.tracker.markChannelRead(channelId, upToMessageId);
  }

  /**
   * Get delivery status for a message.
   */
  getDeliveryStatus(messageId: string): DeliveryStatus {
    return this.tracker.getStatus(messageId);
  }

  // ── Message Actions ───────────────────────────────

  /**
   * Edit a message.
   */
  async editMessage(messageId: string, content: string): Promise<boolean> {
    try {
      const response = await socketManager.emit('v2_chat_edit_message', {
        message_id: messageId,
        content,
      });
      return !response?.error;
    } catch {
      return false;
    }
  }

  /**
   * Delete a message.
   */
  async deleteMessage(messageId: string): Promise<boolean> {
    try {
      const response = await socketManager.emit('v2_chat_delete_message', {
        message_id: messageId,
      });
      return !response?.error;
    } catch {
      return false;
    }
  }

  /**
   * Toggle a reaction.
   */
  toggleReaction(messageId: string, emoji: string): void {
    socketManager.emitNoAck('v2_chat_reaction', {
      message_id: messageId,
      emoji,
    });
  }

  /**
   * Pin a message in a channel.
   */
  async pinMessage(messageId: string): Promise<void> {
    try {
      await socketManager.emit('v2_chat_pin_message', {
        message_id: messageId,
      });
    } catch (e) {
      this.callbacks.onError(`Failed to pin message: ${e}`);
    }
  }

  /**
   * Unpin a message in a channel.
   */
  async unpinMessage(messageId: string): Promise<void> {
    try {
      await socketManager.emit('v2_chat_unpin_message', {
        message_id: messageId,
      });
    } catch (e) {
      this.callbacks.onError(`Failed to unpin message: ${e}`);
    }
  }

  /**
   * Get pinned messages in a channel.
   */
  async getPinnedMessages(channelId: string): Promise<any[]> {
    try {
      const response = await socketManager.emit('v2_chat_get_pinned_messages', {
        channel_id: channelId,
      });
      return response?.messages || [];
    } catch (e) {
      this.callbacks.onError(`Failed to fetch pinned messages: ${e}`);
      return [];
    }
  }

  /**
   * Forward a message to another channel.
   */
  async forwardMessage(messageId: string, toChannelId: string): Promise<void> {
    try {
      await socketManager.emit('v2_chat_forward_message', {
        message_id: messageId,
        to_channel_id: toChannelId,
      });
    } catch (e) {
      this.callbacks.onError(`Failed to forward message: ${e}`);
    }
  }

  /**
   * Get thread for a message (replies and context).
   */
  async getThread(messageId: string, limit?: number): Promise<any[]> {
    try {
      const response = await socketManager.emit('v2_chat_get_thread', {
        message_id: messageId,
        limit: limit || 50,
      });
      return response?.messages || [];
    } catch (e) {
      this.callbacks.onError(`Failed to fetch thread: ${e}`);
      return [];
    }
  }

  /**
   * Get delivery receipts for a message.
   */
  async getMessageReceipts(messageId: string): Promise<any> {
    try {
      const response = await socketManager.emit('v2_chat_get_message_receipts', {
        message_id: messageId,
      });
      return response || {};
    } catch (e) {
      this.callbacks.onError(`Failed to fetch receipts: ${e}`);
      return {};
    }
  }

  // ── Sync ──────────────────────────────────────────

  /**
   * Request channel summaries (unread + last message).
   */
  async fetchChannelSummaries(): Promise<ChannelSummary[]> {
    return this.sync.fetchChannelSummaries();
  }

  /**
   * Request unread counts.
   */
  async fetchUnreadCounts(): Promise<void> {
    return this.sync.fetchUnreadCounts();
  }

  /**
   * Force a manual resync.
   */
  async forceResync(): Promise<SyncResult | null> {
    return this.sync.forceResync();
  }

  /**
   * Flush the outbound queue (retry all pending).
   */
  flushQueue(): void {
    this.queue.flush();
  }

  /**
   * Get pending outbound messages.
   */
  getPendingMessages(): OutboundMessage[] {
    return this.queue.allPending;
  }

  // ── Private: Socket Listeners ─────────────────────

  private _registerSocketListeners(): void {
    // Incoming message from another user
    this._socketUnsubs.push(
      socketManager.on('v2_chat:new_message', (data: any) => {
        const msg = this._mapIncomingMessage(data);
        this.tracker.trackIncoming(msg.id, msg.channelId);
        this.sync.updateTimestamp(msg.createdAt);
        this.callbacks.onIncomingMessage(msg);
      })
    );

    // Typing indicator
    this._socketUnsubs.push(
      socketManager.on('v2_chat:typing', (data: any) => {
        this.callbacks.onTyping({
          channelId: data.channel_id,
          userId: data.user_id,
          isTyping: data.is_typing,
        });
      })
    );

    // Message edited
    this._socketUnsubs.push(
      socketManager.on('v2_chat:message_edited', (data: any) => {
        this.callbacks.onMessageEdited({
          messageId: data.message_id,
          channelId: data.channel_id,
          content: data.content,
          editedAt: data.edited_at,
          editorId: data.editor_id,
        });
      })
    );

    // Message deleted
    this._socketUnsubs.push(
      socketManager.on('v2_chat:message_deleted', (data: any) => {
        this.callbacks.onMessageDeleted({
          messageId: data.message_id,
          channelId: data.channel_id,
          deletedBy: data.deleted_by,
        });
      })
    );

    // Reaction update
    this._socketUnsubs.push(
      socketManager.on('v2_chat:reaction_update', (data: any) => {
        this.callbacks.onReactionUpdate({
          messageId: data.message_id,
          channelId: data.channel_id,
          reactions: data.reactions || [],
          toggledBy: data.toggled_by,
          emoji: data.emoji,
        });
      })
    );

    // Message pinned
    this._socketUnsubs.push(
      socketManager.on('v2_chat:message_pinned', (data: any) => {
        this.callbacks.onMessagePinned?.({
          message_id: data.message_id,
          pinned_by: data.pinned_by,
          channel_id: data.channel_id,
        });
      })
    );

    // Message unpinned
    this._socketUnsubs.push(
      socketManager.on('v2_chat:message_unpinned', (data: any) => {
        this.callbacks.onMessageUnpinned?.({
          message_id: data.message_id,
          channel_id: data.channel_id,
        });
      })
    );

    // Message forwarded
    this._socketUnsubs.push(
      socketManager.on('v2_chat:message_forwarded', (data: any) => {
        this.callbacks.onMessageForwarded?.({
          original_message_id: data.original_message_id,
          forwarded_to_channel_id: data.forwarded_to_channel_id,
          new_message_id: data.new_message_id,
          forwarded_by: data.forwarded_by,
        });
      })
    );

    // Socket reconnection — flush queue
    this._socketUnsubs.push(
      socketManager.on('connect', () => {
        console.log('[MessagingEngine] Socket reconnected — flushing queue');
        this.queue.flush();
      })
    );
  }

  private _unregisterSocketListeners(): void {
    for (const unsub of this._socketUnsubs) {
      unsub();
    }
    this._socketUnsubs = [];
  }

  private _mapIncomingMessage(data: any): IncomingMessage {
    return {
      id: data.id,
      channelId: data.channel_id,
      sender: data.sender
        ? {
            id: data.sender.id,
            username: data.sender.username,
            displayName: data.sender.display_name,
            avatarUrl: data.sender.avatar_url,
          }
        : null,
      content: data.content,
      type: data.type || 'text',
      replyTo: data.reply_to,
      fileId: data.file_id,
      status: data.status || 'sent',
      reactions: data.reactions || [],
      editedAt: data.edited_at,
      createdAt: data.created_at || new Date().toISOString(),
    };
  }
}

// ── Privacy-gate helper ───────────────────────────────────────
//
// Lazy-resolved at every call site rather than at module-load
// time. This sidesteps cyclic-import timing — the privacy store
// is itself loaded by SettingsView via the same engine in some
// boot orders. ``require`` is wrapped in try/catch because tests
// run this file without the renderer's path aliases.
function _isTypingEmitAllowed(): boolean {
  try {
    const mod = require('@/stores/privacy.store');
    return mod.usePrivacyStore.getState().send_typing_indicator !== false;
  } catch {
    return true;
  }
}
