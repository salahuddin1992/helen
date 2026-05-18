/**
 * PinnedMessagesPanel.tsx
 * Panel for viewing and managing pinned messages in a channel
 */

import React, { useEffect, useState } from 'react';
import { X, Pin, Loader } from 'lucide-react';
import { useChatStore } from '@/stores/chat.store.v2';
import type { Message } from '@/types';

export interface PinnedMessagesPanelProps {
  channelId: string;
  currentUserId: string;
  onClose: () => void;
  onJumpToMessage?: (messageId: string) => void;
}

function PinnedMessageItem({
  message,
  currentUserId,
  onJump,
  onUnpin,
  isPinning,
}: {
  message: any;
  currentUserId: string;
  onJump?: (messageId: string) => void;
  onUnpin?: (messageId: string) => void;
  isPinning?: boolean;
}) {
  const isOwnMessage = message.sender.id === currentUserId;

  return (
    <div className="bg-slate-800 rounded-lg p-3 border border-slate-700 hover:border-slate-600 transition">
      <div className="flex justify-between items-start mb-2">
        <div className="flex-1">
          <p className="text-xs text-slate-400 font-medium">{message.sender.display_name}</p>
          <p className="text-sm text-slate-100 mt-1 break-words">{message.content}</p>
          <p className="text-xs text-slate-500 mt-2">
            {new Date(message.created_at).toLocaleString()}
          </p>
        </div>
        <div className="flex gap-1 ml-2">
          {onJump && (
            <button
              onClick={() => onJump(message.id)}
              className="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded transition"
              title="Jump to message"
            >
              Jump
            </button>
          )}
          {isOwnMessage && onUnpin && (
            <button
              onClick={() => onUnpin(message.id)}
              disabled={isPinning}
              className="p-1 text-amber-400 hover:text-amber-300 disabled:opacity-50 transition"
              title="Unpin message"
            >
              <Pin size={16} />
            </button>
          )}
        </div>
      </div>

      {/* Message type indicator */}
      {message.type !== 'text' && (
        <p className="text-xs text-slate-500 italic">
          {message.type === 'image' && '📷 Image'}
          {message.type === 'file' && '📎 File'}
          {message.type === 'reply' && '↩️ Reply'}
        </p>
      )}
    </div>
  );
}

export function PinnedMessagesPanel({
  channelId,
  currentUserId,
  onClose,
  onJumpToMessage,
}: PinnedMessagesPanelProps) {
  const [unpinningId, setUnpinningId] = useState<string | null>(null);

  // Store selectors
  const pinnedMessages = useChatStore((s) => s.pinnedMessages[channelId] || []);
  const isLoadingPins = useChatStore((s) => s.isLoadingPins);
  const loadPinnedMessages = useChatStore((s) => s.loadPinnedMessages);
  const unpinMessage = useChatStore((s) => s.unpinMessage);

  // Load pinned messages on mount
  useEffect(() => {
    loadPinnedMessages(channelId);
  }, [channelId, loadPinnedMessages]);

  const handleUnpin = async (messageId: string) => {
    setUnpinningId(messageId);
    try {
      await unpinMessage(messageId);
    } finally {
      setUnpinningId(null);
    }
  };

  return (
    <div className="fixed right-0 top-0 h-screen w-96 bg-slate-900 border-l border-slate-700 flex flex-col shadow-lg z-40">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-slate-700">
        <div className="flex items-center gap-2">
          <Pin size={20} className="text-amber-400" />
          <div>
            <h2 className="text-lg font-semibold text-white">Pinned Messages</h2>
            <p className="text-xs text-slate-400">
              {pinnedMessages.length} {pinnedMessages.length === 1 ? 'message' : 'messages'}
            </p>
          </div>
        </div>
        <button
          onClick={onClose}
          className="p-2 hover:bg-slate-800 rounded-lg transition text-slate-400 hover:text-white"
          title="Close panel"
        >
          <X size={20} />
        </button>
      </div>

      {/* Content - scrollable */}
      <div className="flex-1 overflow-y-auto">
        {isLoadingPins ? (
          <div className="flex items-center justify-center h-full">
            <Loader className="animate-spin text-blue-500" size={24} />
          </div>
        ) : pinnedMessages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-slate-400 gap-2">
            <Pin size={32} className="opacity-50" />
            <p className="text-sm">No pinned messages yet</p>
            <p className="text-xs text-slate-500">Pin messages to keep them easily accessible</p>
          </div>
        ) : (
          <div className="p-4 space-y-3">
            {pinnedMessages.map((message) => (
              <PinnedMessageItem
                key={message.id}
                message={message}
                currentUserId={currentUserId}
                onJump={onJumpToMessage}
                onUnpin={handleUnpin}
                isPinning={unpinningId === message.id}
              />
            ))}
          </div>
        )}
      </div>

      {/* Footer info */}
      <div className="border-t border-slate-700 p-4 bg-slate-800 text-xs text-slate-400">
        <p>Click "Jump" to navigate to a pinned message in the chat</p>
      </div>
    </div>
  );
}
