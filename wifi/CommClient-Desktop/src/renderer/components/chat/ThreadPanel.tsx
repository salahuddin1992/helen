/**
 * ThreadPanel.tsx
 * Side panel for viewing and replying to message threads
 */

import React, { useEffect, useRef, useState } from 'react';
import { X, Send, Loader } from 'lucide-react';
import { useChatStore } from '@/stores/chat.store.v2';
import { MessageBubble } from './MessageBubble';
import type { Message } from '@/types';

export interface ThreadPanelProps {
  parentMessageId: string;
  channelId: string;
  currentUserId: string;
  onClose: () => void;
}

export function ThreadPanel({
  parentMessageId,
  channelId,
  currentUserId,
  onClose,
}: ThreadPanelProps) {
  const [replyText, setReplyText] = useState('');
  const [isSending, setIsSending] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Store selectors
  const threadMessages = useChatStore((s) => s.threadMessages[parentMessageId] || []);
  const isLoadingThread = useChatStore((s) => s.isLoadingThread);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const loadThreadReplies = useChatStore((s) => s.loadThreadReplies);
  const messages = useChatStore((s) => s.messages[channelId] || []);

  // Find parent message
  const parentMessage = messages.find((m) => m.id === parentMessageId);

  // Load thread on mount
  useEffect(() => {
    loadThreadReplies(parentMessageId);
  }, [parentMessageId, loadThreadReplies]);

  // Handle send reply
  const handleSendReply = async () => {
    if (!replyText.trim()) return;

    setIsSending(true);
    try {
      sendMessage(channelId, replyText.trim(), 'reply', {
        replyTo: parentMessageId,
      });
      setReplyText('');
      if (inputRef.current) {
        inputRef.current.focus();
      }
    } finally {
      setIsSending(false);
    }
  };

  // Handle keyboard shortcut (Ctrl/Cmd + Enter to send)
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      handleSendReply();
    }
  };

  return (
    <div className="fixed right-0 top-0 h-screen w-96 bg-slate-900 border-l border-slate-700 flex flex-col shadow-lg z-40">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-slate-700">
        <div>
          <h2 className="text-lg font-semibold text-white">Thread</h2>
          <p className="text-xs text-slate-400">
            {threadMessages.length} {threadMessages.length === 1 ? 'reply' : 'replies'}
          </p>
        </div>
        <button
          onClick={onClose}
          className="p-2 hover:bg-slate-800 rounded-lg transition text-slate-400 hover:text-white"
          title="Close thread"
        >
          <X size={20} />
        </button>
      </div>

      {/* Content - scrollable */}
      <div className="flex-1 overflow-y-auto">
        {isLoadingThread ? (
          <div className="flex items-center justify-center h-full">
            <Loader className="animate-spin text-blue-500" size={24} />
          </div>
        ) : !parentMessage ? (
          <div className="flex items-center justify-center h-full text-slate-400">
            <p>Message not found</p>
          </div>
        ) : (
          <div className="p-4">
            {/* Parent message */}
            <div className="mb-4 pb-4 border-b border-slate-700">
              <p className="text-xs text-slate-500 mb-2 font-medium">Original Message</p>
              <div className="bg-slate-800 rounded-lg p-3">
                <p className="text-xs text-slate-400 mb-1">{parentMessage.sender.display_name}</p>
                <p className="text-sm text-slate-100">{parentMessage.content}</p>
                <p className="text-xs text-slate-500 mt-2">
                  {new Date(parentMessage.created_at).toLocaleString()}
                </p>
              </div>
            </div>

            {/* Thread replies */}
            {threadMessages.length === 0 ? (
              <div className="flex items-center justify-center h-32 text-slate-400">
                <p className="text-sm">No replies yet. Start the conversation!</p>
              </div>
            ) : (
              <div className="space-y-2">
                {threadMessages.map((reply) => (
                  <div key={reply.id} className="mb-3">
                    <MessageBubble
                      message={reply}
                      isOwn={reply.sender.id === currentUserId}
                      currentUserId={currentUserId}
                      onReaction={(messageId, emoji) => {
                        // Handle reaction toggle in thread
                      }}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Input area */}
      {parentMessage && (
        <div className="border-t border-slate-700 p-4 bg-slate-800">
          <div className="flex gap-2">
            <textarea
              ref={inputRef}
              value={replyText}
              onChange={(e) => setReplyText(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Reply to this thread..."
              className="flex-1 bg-slate-700 text-white text-sm rounded-lg px-3 py-2 border border-slate-600 focus:border-blue-500 focus:outline-none resize-none max-h-24"
              rows={2}
            />
            <button
              onClick={handleSendReply}
              disabled={!replyText.trim() || isSending}
              className="flex-shrink-0 bg-blue-600 hover:bg-blue-700 disabled:bg-slate-600 text-white rounded-lg p-2 transition flex items-center justify-center"
              title="Send reply (Ctrl+Enter)"
            >
              {isSending ? (
                <Loader className="animate-spin" size={18} />
              ) : (
                <Send size={18} />
              )}
            </button>
          </div>
          <p className="text-xs text-slate-500 mt-2">
            Ctrl+Enter to send
          </p>
        </div>
      )}
    </div>
  );
}
