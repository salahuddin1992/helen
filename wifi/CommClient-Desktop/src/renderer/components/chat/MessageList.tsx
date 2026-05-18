/**
 * MessageList.tsx
 * Virtualized scrollable message list with date grouping, loading states, and reactions.
 * Uses react-virtuoso for efficient rendering of large message lists.
 */

import React, { useEffect, useRef, useMemo, useCallback, useState } from 'react';
import { Virtuoso, VirtuosoHandle } from 'react-virtuoso';
import { t } from '@/i18n';
import type { Message } from '@/types';
import { useChatStore } from '@/stores/chat.store.v2';
import { api } from '@/services/api.client';
import { QuickReactionsBar } from '@/components/chat/reactions/QuickReactionsBar';
import {
  Reply,
  Pin,
  Trash2,
  Edit,
  Share2,
  Copy,
  MessageSquare,
  MoreVertical,
  Check,
  CheckCheck,
  CheckCheck as CheckCheckIcon,
  Bookmark,
} from 'lucide-react';

interface MessageListProps {
  channelId: string;
  messages: Message[];
  onLoadMore: () => void;
  isLoadingMessages: boolean;
  hasMore: boolean;
  currentUserId: string;
  onOpenThread?: (messageId: string) => void;
  onOpenReadReceipts?: (messageId: string) => void;
}

interface ContextMenuState {
  x: number;
  y: number;
  messageId: string;
}

/**
 * Format a date for group headers (e.g., "Monday, March 5")
 */
function formatDateHeader(date: Date): string {
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);

  if (date.toDateString() === today.toDateString()) {
    return 'Today';
  } else if (date.toDateString() === yesterday.toDateString()) {
    return 'Yesterday';
  }

  return date.toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'short',
    day: 'numeric',
  });
}

/**
 * Format a time string (e.g., "2:45 PM")
 */
function formatTime(timestamp: string): string {
  const date = new Date(timestamp);
  return date.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

/**
 * Group messages by date
 */
function groupMessagesByDate(messages: Message[]): Array<{ date: Date; messages: Message[] }> {
  const groups: Map<string, Message[]> = new Map();

  messages.forEach((msg) => {
    const dateStr = new Date(msg.created_at).toDateString();
    if (!groups.has(dateStr)) {
      groups.set(dateStr, []);
    }
    groups.get(dateStr)!.push(msg);
  });

  return Array.from(groups.entries()).map(([dateStr, msgs]) => ({
    date: new Date(dateStr),
    messages: msgs,
  }));
}

/**
 * Individual message component with context menu, receipts, pin/forward indicators
 */
function MessageBubbleInline({
  message,
  isOwn,
  currentUserId,
  onContextMenu,
  onReply,
  onOpenThread,
  onOpenReadReceipts,
  deliveryStatus,
  pinnedMessages,
}: {
  message: Message;
  isOwn: boolean;
  currentUserId: string;
  onContextMenu: (e: React.MouseEvent, messageId: string) => void;
  onReply?: (messageId: string) => void;
  onOpenThread?: (messageId: string) => void;
  onOpenReadReceipts?: (messageId: string) => void;
  deliveryStatus?: 'sending' | 'sent' | 'delivered' | 'read' | 'failed';
  pinnedMessages?: string[];
}) {
  const toggleReaction = useChatStore((s) => s.toggleReaction);
  const isPinned = pinnedMessages?.includes(message.id) || false;

  const renderDeliveryStatus = () => {
    if (!isOwn) return null;
    switch (deliveryStatus) {
      case 'sending':
        return <span className="text-xs text-slate-400">⏳</span>;
      case 'sent':
        return <Check size={14} className="text-slate-400" />;
      case 'delivered':
        return <CheckCheck size={14} className="text-slate-400" />;
      case 'read':
        return <CheckCheck size={14} className="text-blue-400" />;
      case 'failed':
        return <span className="text-xs text-red-400">✕</span>;
      default:
        return null;
    }
  };

  return (
    <div
      className={`flex ${isOwn ? 'justify-end' : 'justify-start'} mb-3 group`}
      onContextMenu={(e) => onContextMenu(e, message.id)}
    >
      <div className={`flex gap-3 max-w-xs ${isOwn ? 'flex-row-reverse' : ''}`}>
        {/* Avatar */}
        {!isOwn && (
          <div className="flex-shrink-0 w-8 h-8 rounded-full bg-gradient-to-br from-slate-400 to-slate-600 flex items-center justify-center text-white text-xs font-semibold">
            {message.sender.display_name.charAt(0).toUpperCase()}
          </div>
        )}

        {/* Message content */}
        <div className={isOwn ? 'flex-row-reverse' : ''}>
          {!isOwn && (
            <p className="text-xs text-slate-400 mb-1 font-medium px-3">
              {message.sender.display_name}
            </p>
          )}

          <div className="relative">
            {/* Pinned indicator */}
            {isPinned && (
              <div className="absolute -top-6 left-0 flex items-center gap-1 text-xs text-amber-400">
                <Pin size={12} />
                <span>Pinned</span>
              </div>
            )}

            {/* Forwarded indicator */}
            {message.type === 'reply' && message.reply_to && (
              <div className="text-xs text-slate-400 italic mb-1 px-3">
                Forwarded
              </div>
            )}

            <div
              className={`px-4 py-2 rounded-lg break-words ${
                isOwn
                  ? 'bg-blue-600 text-white rounded-br-none'
                  : 'bg-slate-800 text-slate-100 rounded-bl-none'
              }`}
            >
              {/* Message type handling */}
              {message.type === 'text' && <p className="text-sm">{message.content}</p>}

              {message.type === 'reply' && message.reply_to && (
                <div>
                  <p className="text-xs opacity-75 italic mb-1 border-l-2 border-current pl-2">
                    Replying to message
                  </p>
                  <p className="text-sm">{message.content}</p>
                </div>
              )}

              {message.type === 'file' && message.file_id && (
                <div className="flex items-center gap-2">
                  <span className="text-2xl">📎</span>
                  <div>
                    <p className="text-sm font-medium truncate">{message.content}</p>
                    <a
                      href={api.getFileUrl(message.file_id!)}
                      download
                      className="text-xs underline hover:opacity-75"
                    >
                      Download
                    </a>
                  </div>
                </div>
              )}

              {message.type === 'image' && message.file_id && (
                <img
                  src={api.getThumbnailUrl(message.file_id!)}
                  alt="Message image"
                  className="max-w-sm h-auto rounded cursor-pointer hover:opacity-75"
                />
              )}
            </div>

            {/* Message actions: Reply, Thread, More menu (hover) */}
            <div className="flex gap-1 mt-1 px-3 opacity-0 group-hover:opacity-100 transition">
              {onReply && (
                <button
                  onClick={() => onReply(message.id)}
                  className="text-xs text-slate-400 hover:text-blue-400 flex items-center gap-1"
                  title="Reply"
                >
                  <Reply size={14} />
                </button>
              )}
              {onOpenThread && (
                <button
                  onClick={() => onOpenThread(message.id)}
                  className="text-xs text-slate-400 hover:text-blue-400 flex items-center gap-1"
                  title="Thread"
                >
                  <MessageSquare size={14} />
                </button>
              )}
            </div>
          </div>

          {/* Timestamp with delivery status */}
          <p className="text-xs text-slate-500 mt-1 px-3 opacity-0 group-hover:opacity-100 transition flex items-center gap-1">
            {formatTime(message.created_at)}
            {renderDeliveryStatus()}
          </p>

          {/* Reactions */}
          {message.reactions && message.reactions.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2 px-3">
              {message.reactions.map((reaction) => (
                <button
                  key={reaction.emoji}
                  onClick={() => toggleReaction(message.id, reaction.emoji)}
                  className="px-2 py-1 bg-slate-700 hover:bg-slate-600 rounded text-sm transition flex items-center gap-1"
                >
                  <span>{reaction.emoji}</span>
                  <span className="text-xs text-slate-300">{reaction.count}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Context menu component for message actions
 */
function MessageContextMenu({
  contextMenu,
  onClose,
  messageId,
  isOwnMessage,
  onReply,
  onEdit,
  onDelete,
  onPin,
  onUnpin,
  onForward,
  onCopyText,
  onOpenThread,
  onSave,
  isPinned,
  onReact,
  appliedReactionEmojis,
}: {
  contextMenu: ContextMenuState | null;
  onClose: () => void;
  messageId: string;
  isOwnMessage: boolean;
  onReply?: (messageId: string) => void;
  onEdit?: (messageId: string) => void;
  onDelete?: (messageId: string) => void;
  onPin?: (messageId: string) => void;
  onUnpin?: (messageId: string) => void;
  onForward?: (messageId: string) => void;
  onCopyText?: (messageId: string) => void;
  onOpenThread?: (messageId: string) => void;
  onSave?: (messageId: string) => void;
  isPinned: boolean;
  onReact?: (messageId: string, emoji: string) => void;
  appliedReactionEmojis?: string[];
}) {
  if (!contextMenu || contextMenu.messageId !== messageId) return null;

  const menuItems = [
    onReply && { label: 'Reply', icon: Reply, action: () => onReply(messageId) },
    onOpenThread && { label: 'Thread', icon: MessageSquare, action: () => onOpenThread(messageId) },
    onForward && { label: 'Forward', icon: Share2, action: () => onForward(messageId) },
    onSave && { label: 'Save', icon: Bookmark, action: () => onSave(messageId) },
    onCopyText && { label: 'Copy Text', icon: Copy, action: () => onCopyText(messageId) },
    (isPinned ? onUnpin : onPin) && {
      label: isPinned ? 'Unpin' : 'Pin',
      icon: Pin,
      action: () => (isPinned ? onUnpin?.(messageId) : onPin?.(messageId)),
    },
    isOwnMessage && onEdit && { label: 'Edit', icon: Edit, action: () => onEdit(messageId) },
    isOwnMessage && onDelete && { label: 'Delete', icon: Trash2, action: () => onDelete(messageId) },
  ].filter(Boolean) as Array<{ label: string; icon: any; action: () => void }>;

  return (
    <div
      className="fixed z-50 bg-slate-800 rounded-lg shadow-lg border border-slate-700 py-1 min-w-40"
      style={{ top: `${contextMenu.y}px`, left: `${contextMenu.x}px` }}
      onClick={(e) => e.stopPropagation()}
    >
      {onReact && (
        <QuickReactionsBar
          appliedEmojis={appliedReactionEmojis || []}
          onPick={(emoji) => {
            onReact(messageId, emoji);
            onClose();
          }}
        />
      )}
      {menuItems.map((item, idx) => {
        const Icon = item.icon;
        return (
          <button
            key={idx}
            className="w-full px-4 py-2 text-left text-sm text-slate-100 hover:bg-slate-700 flex items-center gap-2 transition"
            onClick={() => {
              item.action();
              onClose();
            }}
          >
            <Icon size={16} />
            {item.label}
          </button>
        );
      })}
    </div>
  );
}

export function MessageList({
  channelId,
  messages,
  onLoadMore,
  isLoadingMessages,
  hasMore,
  currentUserId,
  onOpenThread,
  onOpenReadReceipts,
}: MessageListProps) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const prevLengthRef = useRef(messages.length);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  // Forward-to-channel modal state. `forwardId` is the source message id;
  // null = closed. Filtered channel picker with search; replaces the
  // prior window.prompt() that exposed channel ids only.
  const [forwardId, setForwardId] = useState<string | null>(null);
  const [forwardQuery, setForwardQuery] = useState('');

  // Store actions
  const deleteMessage = useChatStore((s) => s.deleteMessage);
  const editMessage = useChatStore((s) => s.editMessage);
  const pinMessage = useChatStore((s) => s.pinMessage);
  // ContextMenu-scoped toggleReaction (the per-message
  // ``MessageItem`` body has its own reference; this one is for the
  // quick-reactions bar mounted inside the menu).
  const toggleReactionFromMenu = useChatStore((s) => s.toggleReaction);
  const unpinMessage = useChatStore((s) => s.unpinMessage);
  const forwardMessage = useChatStore((s) => s.forwardMessage);
  const setReplyTarget = useChatStore((s) => s.setReplyTarget);
  const channels = useChatStore((s) => s.channels);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const deliveryStatuses = useChatStore((s) => s.deliveryStatuses);
  const pinnedMessages = useChatStore((s) => s.pinnedMessages[channelId] || []).map((m) => m.id);

  // Context menu handlers
  const handleContextMenu = useCallback((e: React.MouseEvent, messageId: string) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, messageId });
  }, []);

  const handleDelete = useCallback(async (messageId: string) => {
    await deleteMessage(messageId);
  }, [deleteMessage]);

  const handlePin = useCallback(async (messageId: string) => {
    await pinMessage(messageId);
  }, [pinMessage]);

  const handleUnpin = useCallback(async (messageId: string) => {
    await unpinMessage(messageId);
  }, [unpinMessage]);

  const handleForward = useCallback(
    (messageId: string) => {
      // Open the modal — actual forwarding happens when the user picks
      // a target channel below. Keeping the source id in state lets the
      // modal survive a re-render (e.g. when channels stream in).
      const message = messages.find((m) => m.id === messageId);
      if (!message) return;
      setForwardQuery('');
      setForwardId(messageId);
    },
    [messages]
  );

  const handleSave = useCallback(async (messageId: string) => {
    // POST /api/saved — backend dedups so saving the same message twice
    // is a no-op rather than an error. Optional folder/note can be set
    // later via the SavedMessagesPage edit actions.
    try {
      await api.savedMessages.save({ message_id: messageId });
    } catch (e) {
      // Silently swallow on duplicate (409) or transient errors —
      // user can re-try or check /saved page.
    }
  }, []);

  const handleCopyText = useCallback((messageId: string) => {
    const message = messages.find((m) => m.id === messageId);
    if (message) {
      navigator.clipboard.writeText(message.content);
    }
  }, [messages]);

  const handleReply = useCallback((messageId: string) => {
    const message = messages.find((m) => m.id === messageId);
    if (message) {
      setReplyTarget({
        messageId: message.id,
        channelId,
        content: message.content,
        senderName: message.sender.display_name,
      });
    }
  }, [messages, channelId, setReplyTarget]);

  const handleEdit = useCallback(async (messageId: string) => {
    const message = messages.find((m) => m.id === messageId);
    if (!message) return;
    const newContent = window.prompt('Edit message:', message.content);
    if (newContent !== null && newContent !== message.content) {
      await editMessage(messageId, newContent);
    }
  }, [messages, editMessage]);

  // Close context menu on click outside
  useEffect(() => {
    const handleClick = () => setContextMenu(null);
    if (contextMenu) {
      window.addEventListener('click', handleClick);
      return () => window.removeEventListener('click', handleClick);
    }
  }, [contextMenu]);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (messages.length > prevLengthRef.current) {
      // New messages arrived
      virtuosoRef.current?.scrollToIndex({
        index: messages.length - 1,
        align: 'end',
        behavior: 'smooth',
      });
    }
    prevLengthRef.current = messages.length;
  }, [messages.length]);

  // Group messages by date
  const groupedMessages = useMemo(() => groupMessagesByDate(messages), [messages]);

  // Flatten for virtuoso with date separators
  const items = useMemo(() => {
    const flattened: Array<{ type: 'date' | 'message'; data: any }> = [];

    groupedMessages.forEach((group) => {
      flattened.push({ type: 'date', data: group.date });
      group.messages.forEach((msg) => {
        flattened.push({ type: 'message', data: msg });
      });
    });

    return flattened;
  }, [groupedMessages]);

  const rowRenderer = useCallback(
    (_index: number, item: (typeof items)[0]) => {
      if (item.type === 'date') {
        return (
          <div key={`date-${item.data.toISOString()}`} className="flex justify-center my-4">
            <span className="bg-slate-800 text-slate-400 text-xs font-semibold px-3 py-1 rounded-full">
              {formatDateHeader(item.data)}
            </span>
          </div>
        );
      }

      const message: Message = item.data;
      const isOwn = message.sender.id === currentUserId;
      const deliveryStatus = deliveryStatuses[message.id] || 'sent';

      return (
        <div key={message.id} className="px-4 relative">
          <MessageBubbleInline
            message={message}
            isOwn={isOwn}
            currentUserId={currentUserId}
            onContextMenu={handleContextMenu}
            onReply={handleReply}
            onOpenThread={onOpenThread}
            onOpenReadReceipts={onOpenReadReceipts}
            deliveryStatus={deliveryStatus as any}
            pinnedMessages={pinnedMessages}
          />
          <MessageContextMenu
            contextMenu={contextMenu}
            onClose={() => setContextMenu(null)}
            messageId={message.id}
            isOwnMessage={isOwn}
            onReply={handleReply}
            onEdit={handleEdit}
            onDelete={handleDelete}
            onPin={handlePin}
            onUnpin={handleUnpin}
            onForward={handleForward}
            onCopyText={handleCopyText}
            onSave={handleSave}
            onOpenThread={onOpenThread}
            isPinned={pinnedMessages.includes(message.id)}
            onReact={toggleReactionFromMenu}
            appliedReactionEmojis={
              (message.reactions || []).map((r) => r.emoji)
            }
          />
        </div>
      );
    },
    [currentUserId, deliveryStatuses, pinnedMessages, contextMenu, handleContextMenu, handleReply, handleEdit, handleDelete, handlePin, handleUnpin, handleForward, handleCopyText, onOpenThread, onOpenReadReceipts]
  );

  // Empty state
  if (items.length === 0 && !isLoadingMessages) {
    return (
      <div className="flex-1 flex items-center justify-center flex-col gap-4 text-slate-400 bg-slate-950">
        <div className="text-5xl">💬</div>
        <p className="text-sm">{t('chat.no_messages')}</p>
      </div>
    );
  }

  // Loading state for initial load
  if (isLoadingMessages && items.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-slate-950">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-blue-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="flex-1 bg-slate-950 overflow-hidden">
      <Virtuoso
        ref={virtuosoRef}
        data={items}
        itemContent={rowRenderer}
        startReached={() => hasMore && onLoadMore()}
        overscan={10}
        computeItemKey={(_index, item) =>
          item.type === 'date'
            ? `date-${item.data.toISOString()}`
            : `msg-${item.data.id}`
        }
        className="h-full"
        components={{
          Header: () =>
            isLoadingMessages ? (
              <div className="flex justify-center py-4">
                <div className="animate-spin rounded-full h-5 w-5 border-2 border-blue-500 border-t-transparent" />
              </div>
            ) : hasMore ? (
              <div className="text-center py-4">
                <button className="text-blue-500 hover:text-blue-400 text-sm font-medium">
                  {t('common.loading')}
                </button>
              </div>
            ) : null,
        }}
      />

      {/* Forward-to-channel modal — replaces the prior window.prompt
          picker with a searchable list of channels the user belongs to. */}
      {forwardId && (
        <div
          className="fixed inset-0 z-[100] bg-black/60 flex items-center justify-center p-6"
          onClick={() => setForwardId(null)}
        >
          <div
            className="w-full max-w-md bg-slate-900 border border-slate-700 rounded-lg shadow-2xl overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-4 py-3 border-b border-slate-800 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-white">
                {t('chat.forward_title') || 'Forward to channel'}
              </h2>
              <button
                onClick={() => setForwardId(null)}
                className="p-1 hover:bg-slate-800 rounded text-slate-400"
              >
                ✕
              </button>
            </div>
            <div className="p-3 border-b border-slate-800">
              <input
                autoFocus
                value={forwardQuery}
                onChange={(e) => setForwardQuery(e.target.value)}
                placeholder={t('chat.forward_search') || 'Search channels…'}
                className="w-full px-3 py-1.5 bg-slate-800 border border-slate-700 rounded text-sm text-white outline-none"
              />
            </div>
            <div className="max-h-72 overflow-y-auto">
              {channels
                .filter((c) => c.id !== channelId)
                .filter((c) => {
                  const q = forwardQuery.trim().toLowerCase();
                  if (!q) return true;
                  const name = ((c as any).name || c.id).toLowerCase();
                  return name.includes(q);
                })
                .slice(0, 50)
                .map((c) => (
                  <button
                    key={c.id}
                    onClick={async () => {
                      const sourceId = forwardId;
                      setForwardId(null);
                      try {
                        await forwardMessage(sourceId, c.id);
                      } catch (err) {
                        // Best-effort surface — chat store already logs
                        // the failure path.
                      }
                    }}
                    className="w-full text-left px-4 py-2 hover:bg-slate-800 text-sm text-slate-200 flex justify-between items-center"
                  >
                    <span className="truncate">{(c as any).name || c.id}</span>
                    <span className="text-xs text-slate-500 shrink-0 ml-2">
                      {c.type === 'dm' ? 'DM' : (c as any).member_count + ' members'}
                    </span>
                  </button>
                ))}
              {channels.filter((c) => c.id !== channelId).length === 0 && (
                <div className="px-4 py-6 text-center text-sm text-slate-500">
                  {t('chat.forward_no_other') || 'No other channels available'}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
