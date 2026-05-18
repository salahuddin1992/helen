/**
 * chat.store.v2.ts — Enhanced Zustand store powered by MessagingEngine.
 *
 * Drop-in replacement for chat.store.ts.
 * Adds: delivery receipts, reconnection sync, message queue, edit/delete,
 * reactions, unread counts from server, channel summaries, typing indicators.
 *
 * To migrate: replace `import { useChatStore } from './chat.store'`
 * with `import { useChatStore } from './chat.store.v2'`
 */

import { create } from 'zustand';
import { api } from '../services/api.client';
import {
  MessagingEngine,
  IncomingMessage,
  DeliveryStatus,
  ChannelUnreadInfo,
  ChannelSummary,
  SyncResult,
  MessageType,
} from '../services/messaging';
import { useAuthStore } from './auth.store';
import { socketManager } from '../services/socket.manager';
import type { Channel, Message, ReactionInfo } from '../types';

// ── Store Types ─────────────────────────────────────

interface ChannelMeta {
  unread: number;
  lastMessage: {
    id: string;
    senderName: string;
    content: string;
    type: string;
    createdAt: string | null;
  } | null;
}

interface PendingMessage {
  clientId: string;
  channelId: string;
  content: string;
  type: MessageType;
  replyTo?: string;
  status: 'sending' | 'failed';
  createdAt: string;
}

interface ChatStoreState {
  // Channels
  channels: Channel[];
  activeChannelId: string | null;
  channelMeta: Record<string, ChannelMeta>;
  isLoadingChannels: boolean;

  // Messages
  messages: Record<string, Message[]>;
  pendingMessages: Record<string, PendingMessage[]>;
  hasMore: Record<string, boolean>;
  isLoadingMessages: boolean;

  // Typing indicators
  typingUsers: Record<string, string[]>;

  // Delivery tracking
  deliveryStatuses: Record<string, DeliveryStatus>;

  // Pinned messages
  pinnedMessages: Record<string, any[]>;
  isLoadingPins: boolean;

  // Reply context
  replyTarget: { messageId: string; channelId: string; content: string; senderName: string } | null;

  // Thread support
  threadMessages: Record<string, any[]>;
  activeThread: string | null;
  isLoadingThread: boolean;

  // Receipt details
  receiptDetails: Record<string, any>;
  channelReadStates: Record<string, any[]>;

  // Sync state
  isSyncing: boolean;
  queuePending: number;
  queueSending: number;

  // Actions — Lifecycle
  initMessaging: () => void;
  destroyMessaging: () => void;

  // Actions — Channels
  loadChannels: () => Promise<void>;
  setActiveChannel: (channelId: string | null) => void;
  createDm: (userId: string) => Promise<Channel>;
  createGroup: (name: string, memberIds: string[]) => Promise<Channel>;
  deleteChannel: (channelId: string) => Promise<void>;

  // Actions — Messages
  loadMessages: (channelId: string, loadMore?: boolean) => Promise<void>;
  sendMessage: (channelId: string, content: string, type?: MessageType, options?: { replyTo?: string; fileId?: string }) => string;
  editMessage: (messageId: string, content: string) => Promise<boolean>;
  deleteMessage: (messageId: string) => Promise<boolean>;
  retryMessage: (clientId: string) => void;

  // Actions — Reactions
  toggleReaction: (messageId: string, emoji: string) => void;

  // Actions — Typing
  startTyping: (channelId: string) => void;
  stopTyping: (channelId: string) => void;

  // Actions — Read
  markChannelRead: (channelId: string) => void;

  // Actions — Pins
  pinMessage: (messageId: string) => Promise<void>;
  unpinMessage: (messageId: string) => Promise<void>;
  loadPinnedMessages: (channelId: string) => Promise<void>;

  // Actions — Threads
  openThread: (messageId: string) => Promise<void>;
  closeThread: () => void;
  loadThreadReplies: (messageId: string) => Promise<void>;

  // Actions — Reply context
  setReplyTarget: (target: { messageId: string; channelId: string; content: string; senderName: string } | null) => void;

  // Actions — Message forwarding
  forwardMessage: (messageId: string, toChannelId: string) => Promise<void>;

  // Actions — Receipt details
  loadReceiptDetails: (messageId: string) => Promise<void>;
  loadChannelReadStates: (channelId: string) => Promise<void>;

  // Actions — Sync
  forceResync: () => Promise<void>;
  fetchChannelSummaries: () => Promise<void>;
}

// ── Engine Singleton ────────────────────────────────

let engine: MessagingEngine | null = null;
let _initializing = false;
// Holds the unsubscriber for the raw `channel:deleted` socket listener
// registered in initMessaging. Without this, every initMessaging call
// (and there are several — AppBootstrap.onLogin + ChatView mount, plus
// any later re-render that re-runs the effect) accumulates a fresh
// listener on the underlying socket. After enough cycles the same
// 'channel:deleted' broadcast fires the React `set` N times, which
// caused visible UI thrash and contributed to the perceived "disconnect"
// when navigating into chats.
let _channelDeletedUnsub: (() => void) | null = null;

// ── Store ───────────────────────────────────────────

export const useChatStore = create<ChatStoreState>((set, get) => ({
  // Initial state
  channels: [],
  activeChannelId: null,
  channelMeta: {},
  isLoadingChannels: false,
  messages: {},
  pendingMessages: {},
  hasMore: {},
  isLoadingMessages: false,
  typingUsers: {},
  deliveryStatuses: {},
  pinnedMessages: {},
  isLoadingPins: false,
  replyTarget: null,
  threadMessages: {},
  activeThread: null,
  isLoadingThread: false,
  receiptDetails: {},
  channelReadStates: {},
  isSyncing: false,
  queuePending: 0,
  queueSending: 0,

  // ── Lifecycle ──────────────────────────────────────

  initMessaging: () => {
    if (_initializing) return;
    _initializing = true;

    if (engine) {
      engine.destroy();
    }

    // Listen for remote channel deletion (creator/admin elsewhere wiped
    // the room). Cleanup any prior subscription first — every call to
    // initMessaging would otherwise stack another listener.
    if (_channelDeletedUnsub) {
      try { _channelDeletedUnsub(); } catch { /* ignore */ }
      _channelDeletedUnsub = null;
    }
    _channelDeletedUnsub = socketManager.on('channel:deleted', (data: { channel_id?: string; deleted_by?: string }) => {
      if (!data?.channel_id) return;
      const cid = data.channel_id;
      set((s) => ({
        channels:        s.channels.filter((c) => c.id !== cid),
        activeChannelId: s.activeChannelId === cid ? null : s.activeChannelId,
        messages:        Object.fromEntries(Object.entries(s.messages).filter(([k]) => k !== cid)),
        channelMeta:     Object.fromEntries(Object.entries(s.channelMeta).filter(([k]) => k !== cid)),
      }));
    });

    engine = new MessagingEngine({
      onIncomingMessage: (msg: IncomingMessage) => {
        const state = get();

        // Map to Message type
        const message: Message = {
          id: msg.id,
          channel_id: msg.channelId,
          sender: msg.sender
            ? {
                id: msg.sender.id,
                username: msg.sender.username,
                display_name: msg.sender.displayName,
                avatar_url: msg.sender.avatarUrl,
              }
            : { id: '', username: '', display_name: '', avatar_url: null },
          content: msg.content,
          type: msg.type as any,
          reply_to: msg.replyTo,
          file_id: msg.fileId,
          status: msg.status as any,
          reactions: msg.reactions || [],
          edited_at: msg.editedAt,
          created_at: msg.createdAt,
        };

        // Add to messages
        const channelMsgs = state.messages[msg.channelId] || [];
        // Dedup check
        if (!channelMsgs.find((m) => m.id === msg.id)) {
          set({
            messages: {
              ...state.messages,
              [msg.channelId]: [...channelMsgs, message],
            },
          });
        }

        // Update channel meta
        const meta = state.channelMeta[msg.channelId] || { unread: 0, lastMessage: null };
        const isActive = state.activeChannelId === msg.channelId;

        set({
          channelMeta: {
            ...state.channelMeta,
            [msg.channelId]: {
              unread: isActive ? 0 : meta.unread + 1,
              lastMessage: {
                id: msg.id,
                senderName: msg.sender?.displayName || msg.sender?.username || 'Unknown',
                content: msg.content,
                type: msg.type,
                createdAt: msg.createdAt,
              },
            },
          },
        });

        // If active channel, auto-mark as read
        if (isActive) {
          engine?.markChannelRead(msg.channelId, msg.id);
        }

        // Native notification + force-bring-to-front when the user
        // isn't currently looking at this channel. The main process
        // no-ops the focus call if the window is already focused, so
        // we don't yank the user out of the chat they're in.
        if (!isActive) {
          const senderName = msg.sender?.displayName || msg.sender?.username || 'Unknown';
          window.electronAPI?.showNotification(
            senderName,
            msg.content.length > 100 ? msg.content.slice(0, 100) + '...' : msg.content
          );
          window.electronAPI?.forceFocusWindow?.();
        }
      },

      onMessageSent: (clientId, serverId, createdAt) => {
        const state = get();

        // Find and remove pending message, create real message
        const allPending = { ...state.pendingMessages };
        let sentPending: PendingMessage | null = null;
        let channelId: string | null = null;

        for (const [chId, pendingList] of Object.entries(allPending)) {
          const idx = pendingList.findIndex((p) => p.clientId === clientId);
          if (idx >= 0) {
            sentPending = pendingList[idx];
            channelId = chId;
            allPending[chId] = pendingList.filter((_, i) => i !== idx);
            break;
          }
        }

        if (sentPending && channelId) {
          // Create real message from pending
          const message: Message = {
            id: serverId,
            channel_id: channelId,
            sender: (() => {
              const authUser = useAuthStore.getState().user;
              return {
                id: authUser?.id || '',
                username: authUser?.username || 'me',
                display_name: authUser?.display_name || 'Me',
                avatar_url: authUser?.avatar_url || null,
              };
            })(),
            content: sentPending.content,
            type: sentPending.type as any,
            reply_to: sentPending.replyTo || null,
            file_id: null,
            status: 'sent',
            reactions: [],
            edited_at: null,
            created_at: createdAt,
          };

          const channelMsgs = state.messages[channelId] || [];

          set({
            messages: {
              ...state.messages,
              [channelId]: [...channelMsgs, message],
            },
            pendingMessages: allPending,
          });

          // Update channel meta
          set({
            channelMeta: {
              ...get().channelMeta,
              [channelId]: {
                ...get().channelMeta[channelId],
                lastMessage: {
                  id: serverId,
                  senderName: 'Me',
                  content: sentPending.content,
                  type: sentPending.type,
                  createdAt,
                },
              },
            },
          });
        }
      },

      onMessageFailed: (clientId, error) => {
        const state = get();
        const allPending = { ...state.pendingMessages };

        let failedChannelId: string | null = null;
        for (const [chId, pendingList] of Object.entries(allPending)) {
          const idx = pendingList.findIndex((p) => p.clientId === clientId);
          if (idx >= 0) {
            failedChannelId = chId;
            allPending[chId] = pendingList.map((p, i) =>
              i === idx ? { ...p, status: 'failed' as const } : p
            );
            break;
          }
        }
        set({ pendingMessages: allPending });

        // Slow-mode rejection? Lift the wait into the dedicated
        // countdown store so MessageInput can render a timer.
        // Lazy-import keeps this store from pulling in the
        // countdown module at file-load time.
        if (failedChannelId && error) {
          import('@/stores/slow-mode-countdown.store').then((mod) => {
            const wait = mod.parseSlowModeError(error);
            if (wait != null) {
              mod.useSlowModeCountdownStore
                .getState()
                .setDueIn(failedChannelId!, wait, error);
            }
          }).catch(() => { /* module load failure is non-fatal */ });
        }
      },

      onDeliveryStatusChange: (messageId, status) => {
        set({
          deliveryStatuses: {
            ...get().deliveryStatuses,
            [messageId]: status,
          },
        });
      },

      onTyping: (event) => {
        const state = get();
        const current = state.typingUsers[event.channelId] || [];

        if (event.isTyping && !current.includes(event.userId)) {
          set({
            typingUsers: {
              ...state.typingUsers,
              [event.channelId]: [...current, event.userId],
            },
          });
        } else if (!event.isTyping) {
          set({
            typingUsers: {
              ...state.typingUsers,
              [event.channelId]: current.filter((id) => id !== event.userId),
            },
          });
        }
      },

      onMessageEdited: (event) => {
        const state = get();
        const channelMsgs = state.messages[event.channelId];
        if (!channelMsgs) return;

        set({
          messages: {
            ...state.messages,
            [event.channelId]: channelMsgs.map((m) =>
              m.id === event.messageId
                ? { ...m, content: event.content, edited_at: event.editedAt }
                : m
            ),
          },
        });
      },

      onMessageDeleted: (event) => {
        const state = get();
        const channelMsgs = state.messages[event.channelId];
        if (!channelMsgs) return;

        set({
          messages: {
            ...state.messages,
            [event.channelId]: channelMsgs.filter((m) => m.id !== event.messageId),
          },
        });
      },

      onReactionUpdate: (event) => {
        const state = get();
        const channelMsgs = state.messages[event.channelId];
        if (!channelMsgs) return;

        set({
          messages: {
            ...state.messages,
            [event.channelId]: channelMsgs.map((m) =>
              m.id === event.messageId
                ? { ...m, reactions: event.reactions as ReactionInfo[] }
                : m
            ),
          },
        });
      },

      onUnreadUpdate: (unread) => {
        const meta = { ...get().channelMeta };
        for (const [chId, info] of Object.entries(unread)) {
          const existing = meta[chId] || { unread: 0, lastMessage: null };
          meta[chId] = {
            unread: info.unread,
            lastMessage: info.last_message
              ? {
                  id: info.last_message.id,
                  senderName: info.last_message.sender_name,
                  content: info.last_message.content,
                  type: info.last_message.type,
                  createdAt: info.last_message.created_at,
                }
              : existing.lastMessage,
          };
        }
        set({ channelMeta: meta });
      },

      onSummariesUpdate: (summaries) => {
        const meta = { ...get().channelMeta };
        for (const s of summaries) {
          meta[s.channel_id] = {
            unread: s.unread,
            lastMessage: s.last_message
              ? {
                  id: s.last_message.id,
                  senderName: s.last_message.sender_name,
                  content: s.last_message.content,
                  type: s.last_message.type,
                  createdAt: s.last_message.created_at,
                }
              : null,
          };
        }
        set({ channelMeta: meta });
      },

      onSyncComplete: (result: SyncResult) => {
        const state = get();
        const updatedMessages = { ...state.messages };

        for (const [channelId, msgs] of Object.entries(result.channels)) {
          const existing = updatedMessages[channelId] || [];
          const existingIds = new Set(existing.map((m) => m.id));

          const newMsgs: Message[] = (msgs as any[])
            .filter((m: any) => !existingIds.has(m.id))
            .map((m: any) => ({
              id: m.id,
              channel_id: m.channel_id,
              sender: m.sender
                ? {
                    id: m.sender.id,
                    username: m.sender.username,
                    display_name: m.sender.display_name,
                    avatar_url: m.sender.avatar_url,
                  }
                : { id: '', username: '', display_name: '', avatar_url: null },
              content: m.content,
              type: m.type || 'text',
              reply_to: m.reply_to,
              file_id: m.file_id,
              status: m.status || 'sent',
              reactions: [],
              edited_at: m.edited_at,
              created_at: m.created_at,
            }));

          if (newMsgs.length > 0) {
            updatedMessages[channelId] = [...existing, ...newMsgs].sort(
              (a, b) => (a.created_at || '').localeCompare(b.created_at || '')
            );
          }
        }

        set({ messages: updatedMessages, isSyncing: false });
      },

      onChannelRead: (channelId, readerId, upToMessageId) => {
        const state = get();
        const channelMsgs = state.messages[channelId];
        if (!channelMsgs) return;

        // Update delivery statuses for all messages up to (and including) the read message
        const updatedStatuses = { ...state.deliveryStatuses };
        let reached = false;
        for (const msg of channelMsgs) {
          if (reached || msg.id === upToMessageId) {
            reached = true;
          }
          // Mark messages sent by the current user as 'read' if the reader is someone else
          const currentUserId = useAuthStore.getState().user?.id;
          if (currentUserId && msg.sender?.id === currentUserId && readerId !== currentUserId) {
            updatedStatuses[msg.id] = 'read' as DeliveryStatus;
          }
          if (msg.id === upToMessageId) break;
        }
        set({ deliveryStatuses: updatedStatuses });
      },

      onQueueStateChange: (pending, sending) => {
        set({ queuePending: pending, queueSending: sending });
      },

      onMessagePinned: (event: any) => {
        const messageId = event.message_id;
        const channelId = event.channel_id;
        const state = get();
        const pinned = state.pinnedMessages[channelId] || [];

        // Find the message to add to pinned
        const messages = state.messages[channelId] || [];
        const messageToPin = messages.find((m) => m.id === messageId);

        if (messageToPin && !pinned.find((p) => p.id === messageId)) {
          set({
            pinnedMessages: {
              ...state.pinnedMessages,
              [channelId]: [...pinned, messageToPin],
            },
          });
        }
      },

      onMessageUnpinned: (event: any) => {
        const messageId = event.message_id;
        const channelId = event.channel_id;
        const state = get();
        const pinned = state.pinnedMessages[channelId] || [];

        set({
          pinnedMessages: {
            ...state.pinnedMessages,
            [channelId]: pinned.filter((p) => p.id !== messageId),
          },
        });
      },

      onError: (error) => {
        console.error('[ChatStore] Error:', error);
      },
    });

    engine.init();
    _initializing = false;
    // Fetch initial data
    engine?.fetchChannelSummaries();
  },

  destroyMessaging: () => {
    if (engine) {
      engine.destroy();
      engine = null;
    }
    // Tear down the raw `channel:deleted` listener too — otherwise it
    // outlives the engine and keeps mutating state for a non-existent
    // chat session.
    if (_channelDeletedUnsub) {
      try { _channelDeletedUnsub(); } catch { /* ignore */ }
      _channelDeletedUnsub = null;
    }
  },

  // ── Channel Actions ────────────────────────────────

  loadChannels: async () => {
    set({ isLoadingChannels: true });
    try {
      const response = await api.listChannels();
      set({ channels: response.channels, isLoadingChannels: false });
      // Fetch summaries to populate meta
      engine?.fetchChannelSummaries();
    } catch (e) {
      console.error('[ChatStore] loadChannels error:', e);
      set({ isLoadingChannels: false });
    }
  },

  setActiveChannel: (channelId) => {
    set({ activeChannelId: channelId });

    if (channelId) {
      // Clear unread
      const meta = { ...get().channelMeta };
      if (meta[channelId]) {
        meta[channelId] = { ...meta[channelId], unread: 0 };
      }
      set({ channelMeta: meta });

      // Load messages if not cached
      if (!get().messages[channelId]) {
        get().loadMessages(channelId);
      }

      // Mark as read
      engine?.markChannelRead(channelId);
    }
  },

  createDm: async (userId) => {
    const response = await api.createChannel({
      type: 'dm',
      member_ids: [userId],
    });
    const state = get();
    if (!state.channels.find((c) => c.id === response.id)) {
      set({ channels: [response, ...state.channels] });
    }
    return response;
  },

  createGroup: async (name, memberIds) => {
    const response = await api.createChannel({
      type: 'group',
      name,
      member_ids: memberIds,
    });
    set({ channels: [response, ...get().channels] });
    return response;
  },

  // Optimistic delete: drop from list immediately so the UI feels
  // responsive, then call the server. On failure we reload channels
  // to recover. Server fans out `channel:deleted` to every other
  // member's socket; the listener registered in initMessaging takes
  // care of mirroring that on remote clients.
  deleteChannel: async (channelId) => {
    const before = get().channels;
    set((s) => ({
      channels: s.channels.filter((c) => c.id !== channelId),
      activeChannelId: s.activeChannelId === channelId ? null : s.activeChannelId,
    }));
    try {
      await api.deleteChannel(channelId);
    } catch (err) {
      console.error('[chat.v2] deleteChannel failed:', err);
      set({ channels: before });
      throw err;
    }
  },

  // ── Message Actions ────────────────────────────────

  loadMessages: async (channelId, loadMore = false) => {
    set({ isLoadingMessages: true });
    try {
      const existing = get().messages[channelId] || [];
      const before = loadMore && existing.length > 0
        ? existing[0].created_at
        : undefined;

      const response = await api.getMessages(channelId, {
        before: before || undefined,
        limit: 50,
      });

      const reversed = [...response.messages].reverse(); // Oldest first

      set({
        messages: {
          ...get().messages,
          [channelId]: loadMore ? [...reversed, ...existing] : reversed,
        },
        hasMore: {
          ...get().hasMore,
          [channelId]: response.has_more,
        },
        isLoadingMessages: false,
      });
    } catch (e) {
      console.error('[ChatStore] loadMessages error:', e);
      set({ isLoadingMessages: false });
    }
  },

  sendMessage: (channelId, content, type = 'text', options) => {
    if (!engine) return '';

    const clientId = engine.sendMessage(channelId, content, type, options);

    // Add optimistic pending message
    const pending: PendingMessage = {
      clientId,
      channelId,
      content,
      type,
      replyTo: options?.replyTo,
      status: 'sending',
      createdAt: new Date().toISOString(),
    };

    const state = get();
    set({
      pendingMessages: {
        ...state.pendingMessages,
        [channelId]: [...(state.pendingMessages[channelId] || []), pending],
      },
    });

    return clientId;
  },

  editMessage: async (messageId, content) => {
    if (!engine) return false;
    const success = await engine.editMessage(messageId, content);
    if (success) {
      // Optimistic update
      const state = get();
      for (const [chId, msgs] of Object.entries(state.messages)) {
        const idx = msgs.findIndex((m) => m.id === messageId);
        if (idx >= 0) {
          set({
            messages: {
              ...state.messages,
              [chId]: msgs.map((m) =>
                m.id === messageId
                  ? { ...m, content, edited_at: new Date().toISOString() }
                  : m
              ),
            },
          });
          break;
        }
      }
    }
    return success;
  },

  deleteMessage: async (messageId) => {
    if (!engine) return false;
    const success = await engine.deleteMessage(messageId);
    if (success) {
      // Optimistic remove
      const state = get();
      for (const [chId, msgs] of Object.entries(state.messages)) {
        const idx = msgs.findIndex((m) => m.id === messageId);
        if (idx >= 0) {
          set({
            messages: {
              ...state.messages,
              [chId]: msgs.filter((m) => m.id !== messageId),
            },
          });
          break;
        }
      }
    }
    return success;
  },

  retryMessage: (clientId) => {
    engine?.retryMessage(clientId);
    // Update pending status
    const state = get();
    const allPending = { ...state.pendingMessages };
    for (const [chId, pendingList] of Object.entries(allPending)) {
      allPending[chId] = pendingList.map((p) =>
        p.clientId === clientId ? { ...p, status: 'sending' as const } : p
      );
    }
    set({ pendingMessages: allPending });
  },

  // ── Reactions ──────────────────────────────────────

  toggleReaction: (messageId, emoji) => {
    engine?.toggleReaction(messageId, emoji);
  },

  // ── Typing ─────────────────────────────────────────

  startTyping: (channelId) => {
    engine?.startTyping(channelId);
  },

  stopTyping: (channelId) => {
    engine?.stopTyping(channelId);
  },

  // ── Read ───────────────────────────────────────────

  markChannelRead: (channelId) => {
    engine?.markChannelRead(channelId);
    // Clear local unread
    const meta = { ...get().channelMeta };
    if (meta[channelId]) {
      meta[channelId] = { ...meta[channelId], unread: 0 };
    }
    set({ channelMeta: meta });
  },

  // ── Sync ───────────────────────────────────────────

  forceResync: async () => {
    set({ isSyncing: true });
    await engine?.forceResync();
    set({ isSyncing: false });
  },

  fetchChannelSummaries: async () => {
    await engine?.fetchChannelSummaries();
  },

  // ── Pin Actions ────────────────────────────────────

  pinMessage: async (messageId) => {
    if (!engine) return;
    try {
      await engine.pinMessage(messageId);
    } catch (e) {
      console.error('[ChatStore] pinMessage error:', e);
    }
  },

  unpinMessage: async (messageId) => {
    if (!engine) return;
    try {
      await engine.unpinMessage(messageId);
    } catch (e) {
      console.error('[ChatStore] unpinMessage error:', e);
    }
  },

  loadPinnedMessages: async (channelId) => {
    set({ isLoadingPins: true });
    try {
      const pinned = await engine?.getPinnedMessages(channelId);
      if (pinned) {
        set({
          pinnedMessages: {
            ...get().pinnedMessages,
            [channelId]: pinned,
          },
          isLoadingPins: false,
        });
      }
    } catch (e) {
      console.error('[ChatStore] loadPinnedMessages error:', e);
      set({ isLoadingPins: false });
    }
  },

  // ── Thread Actions ────────────────────────────────

  openThread: async (messageId) => {
    set({ activeThread: messageId, isLoadingThread: true });
    try {
      const replies = await engine?.getThread(messageId);
      if (replies) {
        set({
          threadMessages: {
            ...get().threadMessages,
            [messageId]: replies,
          },
          isLoadingThread: false,
        });
      }
    } catch (e) {
      console.error('[ChatStore] openThread error:', e);
      set({ isLoadingThread: false });
    }
  },

  closeThread: () => {
    set({ activeThread: null, isLoadingThread: false });
  },

  loadThreadReplies: async (messageId) => {
    set({ isLoadingThread: true });
    try {
      const replies = await engine?.getThread(messageId);
      if (replies) {
        set({
          threadMessages: {
            ...get().threadMessages,
            [messageId]: replies,
          },
          isLoadingThread: false,
        });
      }
    } catch (e) {
      console.error('[ChatStore] loadThreadReplies error:', e);
      set({ isLoadingThread: false });
    }
  },

  // ── Reply Context ──────────────────────────────────

  setReplyTarget: (target) => {
    set({ replyTarget: target });
  },

  // ── Forward Actions ────────────────────────────────

  forwardMessage: async (messageId, toChannelId) => {
    if (!engine) return;
    try {
      const state = get();
      // Find the message in any channel
      let messageToForward: Message | undefined;
      for (const msgs of Object.values(state.messages)) {
        messageToForward = msgs.find((m) => m.id === messageId);
        if (messageToForward) break;
      }

      if (messageToForward) {
        await engine.forwardMessage(messageId, toChannelId);
      }
    } catch (e) {
      console.error('[ChatStore] forwardMessage error:', e);
    }
  },

  // ── Receipt Actions ────────────────────────────────

  loadReceiptDetails: async (messageId) => {
    if (!engine) return;
    try {
      const details = await engine.getMessageReceipts(messageId);
      if (details) {
        set({
          receiptDetails: {
            ...get().receiptDetails,
            [messageId]: details,
          },
        });
      }
    } catch (e) {
      console.error('[ChatStore] loadReceiptDetails error:', e);
    }
  },

  loadChannelReadStates: async (channelId) => {
    try {
      const readStates = await api.getChannelReadStates(channelId);
      if (readStates) {
        set({
          channelReadStates: {
            ...get().channelReadStates,
            [channelId]: readStates,
          },
        });
      }
    } catch (e) {
      console.error('[ChatStore] loadChannelReadStates error:', e);
    }
  },
}));
