/**
 * MessageBubble.tsx
 * Dedicated message bubble component with comprehensive features
 * Handles text, files, images, replies, reactions, delivery status, etc.
 */

import React, { useMemo } from 'react';
import {
  Check,
  CheckCheck,
  Pin,
  Edit,
  RefreshCw,
  Star,
  MessageSquare,
  Trash2,
} from 'lucide-react';
import type { Message, ReactionInfo } from '@/types';
import { VoiceMessageBubble } from '@/components/voice/VoiceMessageBubble';
import { getBaseUrl } from '@/services/api.client';
import { openLightbox } from '@/components/common/Lightbox';
import { VideoMessageBubble } from '@/components/chat/media/VideoMessageBubble';
import { FileMessageBubble } from '@/components/chat/media/FileMessageBubble';
import { isVideoFile } from '@/components/chat/media/videoExt';

export interface MessageBubbleProps {
  message: Message;
  isOwn: boolean;
  currentUserId: string;
  onReply?: (messageId: string) => void;
  onEdit?: (messageId: string, content: string) => void;
  onDelete?: (messageId: string) => void;
  onPin?: (messageId: string) => void;
  onUnpin?: (messageId: string) => void;
  onForward?: (messageId: string) => void;
  onReaction?: (messageId: string, emoji: string) => void;
  onOpenThread?: (messageId: string) => void;
  onContextMenu?: (e: React.MouseEvent, messageId: string) => void;
  onRetry?: (messageId: string) => void;
  deliveryStatus?: 'sending' | 'sent' | 'delivered' | 'read' | 'failed';
  isPinned?: boolean;
  showContextMenu?: boolean;
  repliedToContent?: string;
}

function formatTime(timestamp: string): string {
  const date = new Date(timestamp);
  return date.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

function formatDate(timestamp: string): string {
  return new Date(timestamp).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function getInitials(name: string): string {
  return name
    .split(' ')
    .map((n) => n.charAt(0))
    .join('')
    .toUpperCase()
    .slice(0, 2);
}

/**
 * Quick reaction picker button
 */
function QuickReactionButton({
  onReaction,
  messageId,
}: {
  onReaction?: (messageId: string, emoji: string) => void;
  messageId: string;
}) {
  const [showPicker, setShowPicker] = React.useState(false);
  const emojis = ['👍', '❤️', '😂', '😮', '😢', '🔥', '👌', '✨'];

  return (
    <div className="relative">
      <button
        onClick={() => setShowPicker(!showPicker)}
        className="text-xs text-slate-400 hover:text-blue-400 flex items-center gap-1 transition"
        title="Add reaction"
      >
        <Star size={14} />
      </button>
      {showPicker && (
        <div className="absolute bottom-6 left-0 bg-slate-700 rounded-lg p-2 flex gap-1 shadow-lg z-10">
          {emojis.map((emoji) => (
            <button
              key={emoji}
              onClick={() => {
                onReaction?.(messageId, emoji);
                setShowPicker(false);
              }}
              className="text-lg hover:scale-125 transition cursor-pointer"
            >
              {emoji}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Reactions display bar
 */
function ReactionsBar({
  reactions,
  onReaction,
  messageId,
  onShowPicker,
}: {
  reactions: ReactionInfo[];
  onReaction?: (messageId: string, emoji: string) => void;
  messageId: string;
  onShowPicker?: () => void;
}) {
  if (reactions.length === 0 && !onShowPicker) return null;

  return (
    <div className="flex flex-wrap gap-1 mt-2 px-3">
      {reactions.map((reaction) => (
        <button
          key={reaction.emoji}
          onClick={() => onReaction?.(messageId, reaction.emoji)}
          className="px-2 py-1 bg-slate-700 hover:bg-slate-600 rounded text-sm transition flex items-center gap-1 cursor-pointer"
          title={`Reacted by ${reaction.user_ids.length} user(s)`}
        >
          <span>{reaction.emoji}</span>
          <span className="text-xs text-slate-300">{reaction.count}</span>
        </button>
      ))}
      {onShowPicker && (
        <QuickReactionButton onReaction={onReaction} messageId={messageId} />
      )}
    </div>
  );
}

/**
 * Reply preview for replied messages
 */
function ReplyPreview({ repliedToContent }: { repliedToContent?: string }) {
  if (!repliedToContent) return null;

  return (
    <div className="text-xs mb-2 border-l-2 border-slate-500 pl-2 opacity-75">
      <p className="font-medium text-slate-300">In reply to:</p>
      <p className="text-slate-400 truncate">{repliedToContent}</p>
    </div>
  );
}

/**
 * Edited indicator
 */
function EditedIndicator({ editedAt }: { editedAt?: string | null }) {
  if (!editedAt) return null;

  return (
    <p className="text-xs text-slate-500 italic opacity-75 mt-1 px-3">
      Edited at {formatTime(editedAt)}
    </p>
  );
}

/**
 * Delivery status indicators for own messages
 */
function DeliveryStatusIcon({
  status,
  onRetry,
  messageId,
}: {
  status?: 'sending' | 'sent' | 'delivered' | 'read' | 'failed';
  onRetry?: (messageId: string) => void;
  messageId: string;
}) {
  switch (status) {
    case 'sending':
      return (
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-full border-2 border-slate-400 border-t-blue-400 animate-spin" />
        </div>
      );
    case 'sent':
      return <span title="Sent"><Check size={14} className="text-slate-400" /></span>;
    case 'delivered':
      return <span title="Delivered"><CheckCheck size={14} className="text-slate-400" /></span>;
    case 'read':
      return <span title="Read"><CheckCheck size={14} className="text-blue-400" /></span>;
    case 'failed':
      return (
        <button
          onClick={() => onRetry?.(messageId)}
          className="text-red-400 hover:text-red-300 flex items-center gap-1"
          title="Retry sending"
        >
          <RefreshCw size={14} />
        </button>
      );
    default:
      return null;
  }
}

/**
 * Main MessageBubble component
 */
export function MessageBubble({
  message,
  isOwn,
  currentUserId,
  onReply,
  onEdit,
  onDelete,
  onPin,
  onUnpin,
  onForward,
  onReaction,
  onOpenThread,
  onContextMenu,
  onRetry,
  deliveryStatus,
  isPinned,
  showContextMenu,
  repliedToContent,
}: MessageBubbleProps) {
  const isSystemMessage = message.type === 'system';
  const hasReplyTo = message.reply_to && repliedToContent;

  // System message style
  if (isSystemMessage) {
    return (
      <div className="flex justify-center my-4">
        <div className="bg-slate-800 text-slate-400 text-xs px-4 py-2 rounded-full italic">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div
      className={`flex ${isOwn ? 'justify-end' : 'justify-start'} mb-3 group relative`}
      onContextMenu={(e) => onContextMenu?.(e as React.MouseEvent, message.id)}
    >
      <div className={`flex gap-3 max-w-xs ${isOwn ? 'flex-row-reverse' : ''}`}>
        {/* Avatar */}
        {!isOwn && (
          <div className="flex-shrink-0 w-8 h-8 rounded-full bg-gradient-to-br from-blue-400 to-blue-600 flex items-center justify-center text-white text-xs font-semibold">
            {getInitials(message.sender.display_name)}
          </div>
        )}

        {/* Message column */}
        <div className={`flex flex-col ${isOwn ? 'items-end' : 'items-start'}`}>
          {/* Sender name and timestamp */}
          {!isOwn && (
            <p className="text-xs text-slate-400 mb-1 font-medium">
              {message.sender.display_name}
            </p>
          )}

          {/* Main message bubble */}
          <div className="relative">
            {/* Pinned indicator */}
            {isPinned && (
              <div className="absolute -top-6 left-0 flex items-center gap-1 text-xs text-amber-400 font-medium">
                <Pin size={12} />
                Pinned
              </div>
            )}

            {/* Hover quick-react bar — appears above the bubble while
                the message row is hovered. One click adds (or toggles
                via the existing onReaction handler) the reaction so
                users don't have to open the full picker for the most
                common emojis. */}
            {!isSystemMessage && onReaction && (
              <div
                className={`absolute -top-8 ${isOwn ? 'right-0' : 'left-0'}
                  hidden group-hover:flex items-center gap-0.5 bg-slate-800
                  border border-slate-700 rounded-full shadow-lg px-1 py-0.5 z-20`}
              >
                {['👍', '❤️', '😂', '😮', '🔥'].map((emoji) => (
                  <button
                    key={emoji}
                    onClick={() => onReaction(message.id, emoji)}
                    className="text-base px-1 hover:scale-125 transition"
                    title={`React with ${emoji}`}
                  >
                    {emoji}
                  </button>
                ))}
              </div>
            )}

            {/* Bubble background */}
            <div
              className={`px-4 py-2 rounded-lg break-words max-w-sm ${
                isOwn
                  ? 'bg-blue-600 text-white rounded-br-none'
                  : 'bg-slate-800 text-slate-100 rounded-bl-none'
              } ${deliveryStatus === 'failed' ? 'opacity-60' : ''}`}
            >
              {/* Reply preview */}
              {hasReplyTo && <ReplyPreview repliedToContent={repliedToContent} />}

              {/* Content by type */}
              {message.type === 'text' && <p className="text-sm">{message.content}</p>}

              {message.type === 'reply' && (
                <div>
                  <p className="text-xs opacity-75 italic mb-1 border-l-2 border-current pl-2">
                    Reply to message
                  </p>
                  <p className="text-sm">{message.content}</p>
                </div>
              )}

              {message.type === 'file' && message.file_id && (
                isVideoFile(message.content)
                  ? (
                    <VideoMessageBubble
                      fileId={message.file_id}
                      filename={message.content || `video-${message.file_id}`}
                      isOwn={isOwn}
                    />
                  )
                  : (
                    <FileMessageBubble
                      fileId={message.file_id}
                      filename={message.content || `file-${message.file_id}`}
                      isOwn={isOwn}
                    />
                  )
              )}

              {message.type === 'image' && message.file_id && (
                <img
                  src={`${getBaseUrl()}/api/files/${message.file_id}/thumbnail`}
                  alt="Message image"
                  onClick={() => openLightbox({
                    // Full-size on click; thumbnail stays as the inline preview.
                    src: `${getBaseUrl()}/api/files/${message.file_id}`,
                    alt: message.content || 'Image',
                    downloadName: message.content || `image-${message.file_id}`,
                  })}
                  className="max-w-sm h-auto rounded cursor-pointer hover:opacity-75 transition"
                />
              )}

              {message.type === 'voice' && (message.file_id || (message as any).audio_url) && (
                <VoiceMessageBubble
                  senderName={(message.sender as any)?.display_name || (message.sender as any)?.username || 'Voice'}
                  senderAvatar={(message.sender as any)?.avatar_url}
                  // VoicePlayer needs an absolute URL because the
                  // <audio src=...> tag isn't relative-resolved by the
                  // renderer's base. getBaseUrl() returns the active
                  // server origin (LAN, tunnel, or localhost).
                  audioUrl={
                    (message as any).audio_url ||
                    `${getBaseUrl()}/api/files/${message.file_id}`
                  }
                  timestamp={message.created_at}
                  isOwn={isOwn}
                />
              )}
            </div>

            {/* Failure indicator with retry */}
            {deliveryStatus === 'failed' && (
              <div className="mt-1 text-xs text-red-400 flex items-center gap-1">
                <span>Failed to send</span>
                {onRetry && (
                  <button
                    onClick={() => onRetry(message.id)}
                    className="text-red-400 hover:text-red-300 underline"
                  >
                    Retry
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Timestamp and actions (hover) */}
          <div className="flex items-center gap-2 mt-1 px-3 opacity-0 group-hover:opacity-100 transition">
            <p className="text-xs text-slate-500">{formatTime(message.created_at)}</p>

            {/* Delivery status for own messages */}
            {isOwn && (
              <DeliveryStatusIcon
                status={deliveryStatus}
                onRetry={onRetry}
                messageId={message.id}
              />
            )}

            {/* Quick actions */}
            <div className="flex items-center gap-1 ml-1 text-slate-400">
              {onReply && (
                <button
                  onClick={() => onReply(message.id)}
                  className="hover:text-blue-400 transition p-1"
                  title="Reply"
                >
                  <MessageSquare size={14} />
                </button>
              )}
              {onReaction && (
                <QuickReactionButton onReaction={onReaction} messageId={message.id} />
              )}
              {/* Inline delete for own messages — surfaces what was previously
                  buried in the right-click menu so the user can wipe a typo
                  without opening a context menu. Not shown on others' messages
                  because the server enforces ownership anyway. */}
              {isOwn && onDelete && (
                <button
                  onClick={() => {
                    if (window.confirm('Delete this message? This cannot be undone.')) {
                      onDelete(message.id);
                    }
                  }}
                  className="hover:text-red-400 transition p-1"
                  title="Delete message"
                >
                  <Trash2 size={14} />
                </button>
              )}
            </div>
          </div>

          {/* Edited indicator */}
          <EditedIndicator editedAt={message.edited_at} />

          {/* Reactions bar */}
          <ReactionsBar
            reactions={message.reactions || []}
            onReaction={onReaction}
            messageId={message.id}
            onShowPicker={onReaction ? () => {} : undefined}
          />
        </div>
      </div>
    </div>
  );
}
