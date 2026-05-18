/**
 * MessageInput.tsx
 * Bottom message input with multiline textarea, file attachment, and typing indicators.
 * Sends on Enter, Shift+Enter for newlines.
 */

import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { Send, Paperclip, X, Smile, AtSign, Mic } from 'lucide-react';

// Lucide d.ts in this version doesn't export Calendar/CalendarClock;
// inline SVG to keep types stable.
const Calendar: React.FC<{ className?: string }> = ({ className }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
       className={className} aria-hidden>
    <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
    <line x1="16" y1="2" x2="16" y2="6" />
    <line x1="8" y1="2" x2="8" y2="6" />
    <line x1="3" y1="10" x2="21" y2="10" />
  </svg>
);
import { VoiceRecorder } from '@/components/voice/VoiceRecorder';
import { t } from '@/i18n';
import { api } from '@/services/api.client';
import { socketManager } from '@/services/socket.manager';
import { useChatStore } from '@/stores/chat.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { AppLogger } from '@/services/AppLogger';
import {
  SlowModeCountdown,
  useIsSlowModeLocked,
} from '@/components/chat/slow-mode/SlowModeCountdown';
import { ScheduleMessageDialog } from '@/components/chat/schedule/ScheduleMessageDialog';
import { CustomEmojiPicker } from '@/components/chat/emoji/CustomEmojiPicker';

const _msgLog = AppLogger.create('MessageInput');

const DRAFT_KEY_PREFIX = 'commclient_draft_v1:';

/**
 * Per-channel draft persistence. Stored under
 *   commclient_draft_v1:<channel_id>
 * so each channel has its own. Cap at 5 KB per channel; longer drafts
 * are truncated rather than refused so the user doesn't lose work.
 */
const DraftStore = {
    get(channelId: string): string {
        try {
            return localStorage.getItem(DRAFT_KEY_PREFIX + channelId) || '';
        } catch {
            return '';
        }
    },
    set(channelId: string, value: string): void {
        try {
            const trimmed = value.length > 5_000 ? value.slice(0, 5_000) : value;
            if (trimmed) {
                localStorage.setItem(DRAFT_KEY_PREFIX + channelId, trimmed);
            } else {
                localStorage.removeItem(DRAFT_KEY_PREFIX + channelId);
            }
        } catch { /* quota errors etc — drop draft persistence */ }
    },
    clear(channelId: string): void {
        try { localStorage.removeItem(DRAFT_KEY_PREFIX + channelId); } catch { /* ignore */ }
    },
};

// ── Built-in emoji data ────────────────────────────────
const EMOJI_CATEGORIES: Record<string, string[]> = {
  Smileys: [
    '\u{1F600}','\u{1F603}','\u{1F604}','\u{1F601}','\u{1F605}','\u{1F602}','\u{1F923}','\u{1F60A}','\u{1F607}','\u{1F642}',
    '\u{1F643}','\u{1F609}','\u{1F60D}','\u{1F618}','\u{1F617}','\u{1F61A}','\u{1F60B}','\u{1F61C}','\u{1F92A}','\u{1F914}',
  ],
  Hands: [
    '\u{1F44D}','\u{1F44E}','\u{1F44F}','\u{1F64C}','\u{1F91D}','\u{1F64F}','\u{270C}\u{FE0F}','\u{1F91E}','\u{1F44C}','\u{1F448}',
    '\u{1F449}','\u{1F446}','\u{1F447}','\u{270B}','\u{1F44B}','\u{1F4AA}',
  ],
  Hearts: [
    '\u{2764}\u{FE0F}','\u{1F9E1}','\u{1F49B}','\u{1F49A}','\u{1F499}','\u{1F49C}','\u{1F5A4}','\u{1F90D}','\u{1F90E}','\u{1F498}',
    '\u{1F49D}','\u{1F496}','\u{1F497}','\u{1F493}',
  ],
  Objects: [
    '\u{1F525}','\u{2B50}','\u{1F31F}','\u{1F389}','\u{1F388}','\u{1F381}','\u{1F3B5}','\u{1F3B6}','\u{1F4F7}','\u{1F4BB}',
    '\u{1F4A1}','\u{1F4DA}','\u{270F}\u{FE0F}','\u{1F4DD}','\u{2705}','\u{274C}',
  ],
};

interface MessageInputProps {
  channelId: string;
  typingUsers: string[];
  currentUsername?: string;
}

/**
 * Format typing indicator text
 */
function formatTypingIndicator(userNames: string[]): string {
  if (userNames.length === 0) return '';
  if (userNames.length === 1) return `${userNames[0]} is typing...`;
  if (userNames.length === 2) return `${userNames[0]} and ${userNames[1]} are typing...`;
  return `${userNames.slice(0, 2).join(', ')} and ${userNames.length - 2} more are typing...`;
}

export function MessageInput({
  channelId,
  typingUsers,
  currentUsername = 'You',
}: MessageInputProps) {
  const [content, setContent] = useState(() => DraftStore.get(channelId));
  const [isUploading, setIsUploading] = useState(false);
  const isSlowModeLocked = useIsSlowModeLocked(channelId);
  const [showScheduleDialog, setShowScheduleDialog] = useState(false);
  const [showCustomEmoji, setShowCustomEmoji] = useState(false);
  const [typingTimeout, setTypingTimeout] = useState<ReturnType<typeof setTimeout> | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const sendMessage = useChatStore((s) => s.sendMessage);
  const editMessage = useChatStore((s) => s.editMessage);
  const replyTarget = useChatStore((s) => s.replyTarget);
  const setReplyTarget = useChatStore((s) => s.setReplyTarget);
  // Read the channel message list for "edit-last" via ↑ on empty input.
  const channelMessages = useChatStore((s) => s.messages[channelId] || []);

  // Active edit target. Null = sending a fresh message; non-null =
  // PATCH the existing message instead. The message id + original
  // content is captured at edit-start so a successful edit can clear
  // both the input and this state in one shot.
  const [editTarget, setEditTarget] = useState<{ messageId: string; original: string } | null>(null);
  // Active channel member list — used by mention autocomplete to filter
  // names against the @-trigger query. Falls back to empty array for
  // DMs (single-other-member channels also benefit from autocomplete
  // even though it's a one-row list).
  const channelMembers = useChatStore(
    (s) => s.channels.find((c) => c.id === channelId)?.members || [],
  );
  const myUserId = useAuthStore((s) => s.user?.id);

  const [showEmojiPicker, setShowEmojiPicker] = useState(false);
  const [activeEmojiCategory, setActiveEmojiCategory] = useState('Smileys');
  const emojiPickerRef = useRef<HTMLDivElement>(null);

  // Voice-message mode. When true the input row hides the textarea and
  // shows the hold-to-record control instead. Toggle is driven by the
  // mic icon next to the file attach button.
  const [voiceMode, setVoiceMode] = useState(false);

  // Drag-and-drop file upload. Visual highlight + auto-trigger upload
  // on drop. Same code path as the existing paperclip → file picker
  // (api.uploadFile + sendMessage with type=file).
  const [isDragging, setIsDragging] = useState(false);

  // ── Mention autocomplete state ────────────────────────────────────
  // `mentionQuery` is null when the picker is closed; a string (possibly
  // empty) when an @-trigger is active and we're filtering members.
  // `mentionAnchor` is the absolute index in `content` of the `@` itself.
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionAnchor, setMentionAnchor] = useState<number>(0);
  const [mentionActiveIdx, setMentionActiveIdx] = useState<number>(0);

  // Channel-switch reset — without this, switching from channel A to
  // channel B mid-mention leaves the @-anchor pointing at A's old
  // text position, so the next keystroke filters channel B's members
  // against the wrong anchor and picks the wrong user.
  useEffect(() => {
    setMentionQuery(null);
    setMentionAnchor(0);
    setMentionActiveIdx(0);
  }, [channelId]);

  const mentionResults = useMemo(() => {
    if (mentionQuery == null) return [] as Array<{ user_id: string; display_name: string; username?: string }>;
    const q = mentionQuery.toLowerCase();
    const out = channelMembers
      .filter((m: any) => m.user_id !== myUserId)
      .filter((m: any) => {
        const name = (m.display_name || m.username || '').toLowerCase();
        return q === '' || name.includes(q);
      })
      .slice(0, 8);
    return out as any;
  }, [mentionQuery, channelMembers, myUserId]);

  /**
   * Auto-expand textarea as user types
   */
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      const scrollHeight = Math.min(textareaRef.current.scrollHeight, 104); // Max 4 lines (~26px per line)
      textareaRef.current.style.height = `${scrollHeight}px`;
    }
  }, [content]);

  /**
   * Close emoji picker on outside click
   */
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (emojiPickerRef.current && !emojiPickerRef.current.contains(e.target as Node)) {
        setShowEmojiPicker(false);
      }
    }
    if (showEmojiPicker) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showEmojiPicker]);

  /**
   * Insert emoji at cursor position
   */
  const insertEmoji = useCallback((emoji: string) => {
    const textarea = textareaRef.current;
    if (textarea) {
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const newContent = content.slice(0, start) + emoji + content.slice(end);
      setContent(newContent);
      // Restore cursor position after emoji
      requestAnimationFrame(() => {
        textarea.selectionStart = textarea.selectionEnd = start + emoji.length;
        textarea.focus();
      });
    } else {
      setContent((prev) => prev + emoji);
    }
    setShowEmojiPicker(false);
  }, [content]);

  /**
   * Emit typing indicators + persist draft + drive mention autocomplete.
   *
   * Mention detection: scan from the cursor backwards for an `@` that's
   * either at start-of-string or preceded by whitespace. Everything
   * between the `@` and the cursor (a-zA-Z0-9_ -.) is the live filter
   * query. Whitespace breaks the trigger and closes the picker.
   */
  const handleInput = useCallback(
    (value: string) => {
      setContent(value);
      DraftStore.set(channelId, value);

      // Mention trigger detection. Cursor position needed; pull from the
      // textarea ref. For tests / SSR where the ref isn't attached yet,
      // assume the cursor is at end-of-string.
      const ta = textareaRef.current;
      const caret = ta?.selectionStart ?? value.length;
      const prefix = value.slice(0, caret);
      // Walk backwards from the cursor to find an @ trigger.
      let i = prefix.length - 1;
      let trigger = -1;
      while (i >= 0) {
        const ch = prefix[i];
        if (ch === '@') {
          // Anchored only if the char before is whitespace or start-of-string.
          const before = i === 0 ? ' ' : prefix[i - 1];
          if (/\s/.test(before)) trigger = i;
          break;
        }
        if (/\s/.test(ch)) break;  // whitespace before @ → not a mention
        if (!/[a-zA-Z0-9_.-]/.test(ch)) break;  // non-mention char → bail
        i--;
      }
      if (trigger >= 0) {
        const query = prefix.slice(trigger + 1);
        setMentionAnchor(trigger);
        setMentionQuery(query);
        setMentionActiveIdx(0);
      } else if (mentionQuery !== null) {
        setMentionQuery(null);
      }

      // Clear previous timeout
      if (typingTimeout) clearTimeout(typingTimeout);

      // Honor the user's "send typing indicator" privacy setting.
      // If disabled we skip the emit entirely — peers won't see
      // a "user is typing…" hint for this client.
      const sendTyping = (() => {
        try {
          const mod = require('@/stores/privacy.store');
          return mod.usePrivacyStore.getState().send_typing_indicator !== false;
        } catch { return true; }
      })();

      // Emit typing start on each keystroke (server uses v2 event names)
      if (sendTyping && value.length > 0) {
        socketManager.emitNoAck('v2_chat_typing_start', { channel_id: channelId });
      }

      // Set timeout to emit typing stop. If typing is disabled we
      // never fired the start — but stopTyping is harmless (server
      // ignores when no prior start), so we only skip when the
      // user wants to keep their typing state private even on
      // churn.
      const timeout = setTimeout(() => {
        if (sendTyping) {
          socketManager.emitNoAck('v2_chat_typing_stop', { channel_id: channelId });
        }
      }, 3000);

      setTypingTimeout(timeout);
    },
    [channelId, content.length, typingTimeout, mentionQuery]
  );

  /**
   * Replace the active @-trigger with the selected member's display name
   * and close the picker. Cursor lands after the inserted name + a space.
   */
  const insertMention = useCallback((member: { display_name?: string; username?: string }) => {
    if (mentionQuery == null) return;
    const name = (member.display_name || member.username || '').replace(/\s+/g, '_');
    if (!name) return;
    const before = content.slice(0, mentionAnchor);
    const after = content.slice(mentionAnchor + 1 + mentionQuery.length);
    const next = `${before}@${name} ${after}`;
    setContent(next);
    DraftStore.set(channelId, next);
    setMentionQuery(null);
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (ta) {
        const cursor = mentionAnchor + 1 + name.length + 1;
        ta.selectionStart = ta.selectionEnd = cursor;
        ta.focus();
      }
    });
  }, [content, mentionAnchor, mentionQuery, channelId]);

  /**
   * Handle send on Enter, Shift+Enter for newline.
   * When the mention picker is open, ↑↓/Tab/Enter drive selection
   * instead of sending the message.
   */
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // ↑ on empty input → start editing the user's last text message in
    // this channel. This matches the "Edit your last message" entry in
    // the keyboard-shortcuts modal. Only fires when the input is fully
    // empty AND no mention picker is active (otherwise ↑ is the picker's
    // up-arrow nav).
    if (
      e.key === 'ArrowUp' &&
      !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey &&
      content.length === 0 &&
      mentionQuery == null
    ) {
      const myId = useAuthStore.getState().user?.id;
      // Walk newest-first looking for our own text message.
      for (let i = channelMessages.length - 1; i >= 0; i--) {
        const m = channelMessages[i];
        if ((m.sender as any)?.id === myId && m.type === 'text' && !(m as any).deleted_at) {
          e.preventDefault();
          setContent(m.content);
          setEditTarget({ messageId: m.id, original: m.content });
          requestAnimationFrame(() => {
            const ta = textareaRef.current;
            if (ta) {
              ta.focus();
              ta.selectionStart = ta.selectionEnd = m.content.length;
            }
          });
          return;
        }
      }
    }

    // Mention picker keyboard handling — short-circuits before send.
    if (mentionQuery != null && mentionResults.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setMentionActiveIdx((i) => Math.min(i + 1, mentionResults.length - 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setMentionActiveIdx((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        insertMention(mentionResults[mentionActiveIdx]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMentionQuery(null);
        return;
      }
    }
    // Esc cancels edit mode (when not handled by the picker above).
    if (e.key === 'Escape' && editTarget) {
      e.preventDefault();
      cancelEdit();
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  /**
   * Send message (with optional reply context)
   */
  const handleSend = () => {
    const trimmed = content.trim();
    if (!trimmed || isUploading) return;
    // Honor an active slow-mode lockout before even queueing the
    // message. The server would reject anyway; bailing here saves a
    // round-trip and avoids an extra "failed" pending row.
    if (isSlowModeLocked) return;

    setContent('');
    DraftStore.clear(channelId);  // sent → drop the saved draft
    setMentionQuery(null);
    if (typingTimeout) clearTimeout(typingTimeout);
    socketManager.emitNoAck('v2_chat_typing_stop', { channel_id: channelId });

    // Edit path: PATCH the existing message instead of POSTing a new one.
    // The store's optimistic update propagates the new content; reply
    // / file / image targets aren't editable through this flow.
    if (editTarget) {
      const { messageId, original } = editTarget;
      setEditTarget(null);
      if (trimmed !== original) {
        editMessage(messageId, trimmed).catch((e) => {
          _msgLog.error('edit failed', e);
        });
      }
      return;
    }

    if (replyTarget) {
      sendMessage(channelId, trimmed, 'text', { replyTo: replyTarget.messageId });
      setReplyTarget(null);
    } else {
      sendMessage(channelId, trimmed);
    }
  };

  // Esc cancels an active edit (in addition to the existing mention-picker
  // Esc handling). Wired separately so it doesn't interfere with that.
  const cancelEdit = useCallback(() => {
    setEditTarget(null);
    setContent('');
    DraftStore.clear(channelId);
  }, [channelId]);

  /**
   * Handle file attachment
   */
  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setIsUploading(true);
    try {
      const result = await api.uploadFile(file, channelId);
      // Send a file message with the fileId from the upload result
      sendMessage(channelId, file.name, 'file', { fileId: result.file_id });

      // Reset input
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    } catch (error) {
      _msgLog.error('Failed to upload file', error);
      // Show error toast (would use a toast library in production)
    } finally {
      setIsUploading(false);
    }
  };

  /**
   * Trigger file picker
   */
  const handleAttachClick = () => {
    fileInputRef.current?.click();
  };

  return (
    <div
      className={`border-t border-slate-800 bg-slate-900 p-4 transition-colors ${
        isDragging ? 'ring-2 ring-blue-500 ring-inset bg-slate-800' : ''
      }`}
      onDragEnter={(e) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.dataTransfer.types.includes('Files')) setIsDragging(true);
      }}
      onDragOver={(e) => {
        // Prevent default so drop fires; don't bother updating state
        // every dragover (60Hz) — Enter/Leave handle the highlight.
        e.preventDefault();
        e.stopPropagation();
      }}
      onDragLeave={(e) => {
        e.preventDefault();
        e.stopPropagation();
        // Only clear when leaving the outer container, not a child
        // — the relatedTarget check covers that.
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          setIsDragging(false);
        }
      }}
      onDrop={async (e) => {
        e.preventDefault();
        e.stopPropagation();
        setIsDragging(false);
        const files = Array.from(e.dataTransfer.files || []);
        if (files.length === 0) return;
        // Upload each file sequentially through the same path as the
        // file picker. The first failure breaks the loop so the user
        // sees the error immediately rather than after N retries.
        setIsUploading(true);
        try {
          for (const file of files) {
            const result = await api.uploadFile(file, channelId);
            sendMessage(channelId, file.name, 'file', { fileId: result.file_id });
          }
        } catch (err) {
          _msgLog.error('drag-drop upload failed', err);
        } finally {
          setIsUploading(false);
        }
      }}
    >
      {/* Slow-mode lockout banner — populated by the chat store
          when send rejects with ``slow_mode:N``. Renders nothing
          when no lockout is active. */}
      <SlowModeCountdown channelId={channelId} />

      {/* Reply context banner */}
      {replyTarget && (
        <div className="mb-2 flex items-center gap-2 bg-slate-800 rounded-lg px-3 py-2 border-l-4 border-blue-500">
          <div className="flex-1 min-w-0">
            <span className="text-xs text-blue-400 font-medium">
              Replying to {replyTarget.senderName}
            </span>
            <p className="text-xs text-slate-400 truncate">
              {replyTarget.content.length > 80
                ? replyTarget.content.slice(0, 80) + '...'
                : replyTarget.content}
            </p>
          </div>
          <button
            onClick={() => setReplyTarget(null)}
            className="text-slate-400 hover:text-white p-1 rounded transition"
            title="Cancel reply"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Edit-mode banner — shown while ↑-recall is active. Click X to
          cancel; the keystroke handler also accepts Esc. */}
      {editTarget && (
        <div className="mb-2 flex items-center gap-2 bg-yellow-900/40 rounded-lg px-3 py-2 border-l-4 border-yellow-500">
          <div className="flex-1 min-w-0">
            <span className="text-xs text-yellow-300 font-medium">
              {t('chat.editing_message') || 'Editing message — Esc to cancel'}
            </span>
          </div>
          <button
            onClick={cancelEdit}
            className="text-slate-400 hover:text-white p-1 rounded transition"
            title="Cancel edit"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Typing indicator */}
      {typingUsers.length > 0 && (
        <div className="mb-3 text-xs text-slate-400 italic h-4">
          {formatTypingIndicator(typingUsers)}
        </div>
      )}

      {/* Mention autocomplete picker — appears above the textarea while
          an @-trigger is active and there are matching members. The list
          is keyboard-driven (↑↓/Enter/Tab/Esc) via `handleKeyDown`; the
          mouse fallback is only here for users on a touch device. */}
      {mentionQuery != null && mentionResults.length > 0 && (
        <div className="mb-2 max-h-48 overflow-auto bg-slate-800 border border-slate-700 rounded-lg shadow-lg">
          {mentionResults.map((m: any, i: number) => (
            <button
              key={m.user_id}
              onMouseDown={(e) => {
                // mousedown so the textarea doesn't lose focus before
                // the click fires through.
                e.preventDefault();
                insertMention(m);
              }}
              onMouseEnter={() => setMentionActiveIdx(i)}
              className={`w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 ${
                i === mentionActiveIdx
                  ? 'bg-blue-600 text-white'
                  : 'text-slate-200 hover:bg-slate-700'
              }`}
            >
              <AtSign size={12} className="opacity-60 shrink-0" />
              <span className="truncate">{m.display_name || m.username}</span>
              {m.username && m.display_name && (
                <span className="text-xs text-slate-400 truncate">@{m.username}</span>
              )}
            </button>
          ))}
        </div>
      )}

      {/* Input container */}
      {voiceMode ? (
        <div className="flex items-center justify-center py-3 gap-3">
          <VoiceRecorder
            channelId={channelId}
            onRecordEnd={() => setVoiceMode(false)}
            onError={(err) => {
              _msgLog.error('voice recording failed', err);
              setVoiceMode(false);
            }}
          />
          <button
            onClick={() => setVoiceMode(false)}
            className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition"
            title="Cancel"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
      ) : (
      <div className="flex gap-3 items-end">
        {/* Textarea */}
        <textarea
          ref={textareaRef}
          value={content}
          onChange={(e) => handleInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={t('chat.type_message')}
          className="flex-1 px-4 py-2 bg-slate-800 text-white rounded-lg border border-slate-700 focus:border-blue-500 focus:outline-none resize-none text-sm placeholder-slate-500 max-h-28 scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-slate-800"
          rows={1}
        />

        {/* Voice message mode — opens the hold-to-record interface so the
            user can send an audio note inline. */}
        <button
          onClick={() => setVoiceMode(true)}
          disabled={isUploading}
          className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
          title={t('chat.voice_message') || 'Voice message'}
        >
          <Mic className="w-5 h-5" />
        </button>

        {/* File attachment button */}
        <button
          onClick={handleAttachClick}
          disabled={isUploading}
          className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
          title="Attach file"
        >
          <Paperclip className="w-5 h-5" />
        </button>

        {/* Emoji picker button */}
        {/* Custom emoji picker — server-uploaded shortcodes + Unicode
            favorites. Opens its own popover (positioned by the
            picker itself). */}
        <div className="relative">
          <button
            onClick={() => setShowCustomEmoji((v) => !v)}
            className="p-2 text-slate-300 hover:text-white bg-slate-800
                       hover:bg-slate-700 rounded-lg transition
                       text-base font-bold"
            title="إيموجي مخصّص"
          >
            :)
          </button>
          {showCustomEmoji && (
            <CustomEmojiPicker
              onPick={(token) => {
                insertEmoji(token);
                setShowCustomEmoji(false);
              }}
              onClose={() => setShowCustomEmoji(false)}
            />
          )}
        </div>

        <div className="relative" ref={emojiPickerRef}>
          <button
            onClick={() => setShowEmojiPicker((v) => !v)}
            className="p-2 text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition"
            title="Emoji"
          >
            <Smile className="w-5 h-5" />
          </button>

          {/* Emoji picker popup */}
          {showEmojiPicker && (
            <div className="absolute bottom-12 right-0 w-72 bg-slate-800 border border-slate-700 rounded-xl shadow-2xl z-50 overflow-hidden">
              {/* Category tabs */}
              <div className="flex border-b border-slate-700">
                {Object.keys(EMOJI_CATEGORIES).map((cat) => (
                  <button
                    key={cat}
                    onClick={() => setActiveEmojiCategory(cat)}
                    className={`flex-1 px-2 py-2 text-xs font-medium transition ${
                      activeEmojiCategory === cat
                        ? 'text-blue-400 border-b-2 border-blue-400'
                        : 'text-slate-400 hover:text-white'
                    }`}
                  >
                    {cat}
                  </button>
                ))}
              </div>
              {/* Emoji grid */}
              <div className="p-2 grid grid-cols-8 gap-1 max-h-48 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-slate-800">
                {EMOJI_CATEGORIES[activeEmojiCategory].map((emoji, i) => (
                  <button
                    key={i}
                    onClick={() => insertEmoji(emoji)}
                    className="w-8 h-8 flex items-center justify-center text-lg hover:bg-slate-700 rounded transition"
                  >
                    {emoji}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Schedule button — opens ScheduleMessageDialog with the
            current draft pre-filled. Only shown when there's text
            so the right-rail doesn't churn on every focus. */}
        {content.trim() && (
          <button
            onClick={() => setShowScheduleDialog(true)}
            disabled={isUploading || isSlowModeLocked}
            className="p-2 text-slate-300 hover:text-white bg-slate-800
                       hover:bg-slate-700 rounded-lg transition
                       disabled:opacity-50"
            title="جدولة الرسالة لوقت لاحق"
          >
            <Calendar className="w-5 h-5" />
          </button>
        )}

        {/* Send button */}
        <button
          onClick={handleSend}
          disabled={!content.trim() || isUploading || isSlowModeLocked}
          className="p-2 text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
          title={
            isSlowModeLocked
              ? 'وضع البطء — انتظر حتى انقضاء العدّاد'
              : 'Send message (Enter or Cmd+Enter)'
          }
        >
          <Send className="w-5 h-5" />
        </button>
      </div>
      )}

      {showScheduleDialog && (
        <ScheduleMessageDialog
          channelId={channelId}
          initialContent={content}
          onClose={() => setShowScheduleDialog(false)}
          onScheduled={() => {
            // Clear the live composer after a successful schedule —
            // the draft has moved into the queue, no point keeping
            // the duplicate in the input.
            setContent('');
            DraftStore.clear(channelId);
          }}
        />
      )}

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        onChange={handleFileSelect}
        className="hidden"
        accept="*"
      />

      {/* Upload status */}
      {isUploading && (
        <p className="text-xs text-slate-400 mt-2 flex items-center gap-1">
          <span className="animate-spin inline-block w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full" />
          Uploading...
        </p>
      )}
    </div>
  );
}
