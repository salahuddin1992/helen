/**
 * ChannelList.tsx
 * Left sidebar showing conversations/channels with search, unread badges, online status.
 * Includes "New DM" and "New Group" buttons.
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Search, Plus, Users, Hash, X, Trash2, Phone } from 'lucide-react';
import { useChatStore } from '@/stores/chat.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { useContactsStore } from '@/stores/contacts.store';
import { api } from '@/services/api.client';
import { t } from '@/i18n';
import type { Channel } from '@/types';
import { useChannelActiveCall } from '@/hooks/useChannelActiveCall';
import { useCallStore } from '@/stores/call.store.v2';
import { ChannelContextMenu } from '@/components/chat/channel-list/ChannelContextMenu';

/**
 * Per-row active-call indicator. Hooks must be at component level, so
 * we render one ChannelLiveBadge per channel row instead of looping
 * useChannelActiveCall in the parent.
 */
const ChannelLiveBadge: React.FC<{ channelId: string }> = ({ channelId }) => {
  const { hasActiveCall, activeCall } = useChannelActiveCall(channelId);
  const joinGroupCall = useCallStore((s) => (s as any).joinGroupCall);
  if (!hasActiveCall || !activeCall) return null;
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        if (typeof joinGroupCall === 'function') {
          joinGroupCall(channelId, activeCall.callType || 'audio');
        }
      }}
      className="ml-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-600/90 hover:bg-red-500 text-white text-[10px] font-semibold shadow-sm"
      title={`Join active call · ${activeCall.participantCount} in call`}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-white animate-pulse" />
      <Phone size={10} />
      <span>LIVE · {activeCall.participantCount}</span>
    </button>
  );
};

interface ChannelListProps {
  onNewDM?: () => void;
  onNewGroup?: () => void;
}

export function ChannelList({ onNewDM, onNewGroup }: ChannelListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [messageSearchQuery, setMessageSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [showSearchResults, setShowSearchResults] = useState(false);
  const searchResultsRef = useRef<HTMLDivElement>(null);
  // Right-click context menu over a channel row — pin / unpin /
  // (future) archive. Null = closed.
  const [channelMenu, setChannelMenu] = useState<
    import('@/components/chat/channel-list/ChannelContextMenu').ChannelContextMenuState | null
  >(null);

  const {
    channels,
    activeChannelId,
    setActiveChannel,
    loadChannels,
    isLoadingChannels,
    channelMeta,
    deleteChannel,
  } = useChatStore();
  const meRole = useAuthStore((s) => s.user?.role);

  const { onlineUsers, getUserStatus } = useContactsStore();

  // Read auth identity *before* any helper that depends on it. The
  // channel filter at line ~90 calls `getChannelDisplayName` which
  // dereferences `currentUserId`; if this `const` lived below those
  // helpers, the filter would hit a temporal-dead-zone ReferenceError
  // and React would unmount the entire chat view (blank screen).
  const currentUserId = useAuthStore((s) => s.user?.id) || '';

  useEffect(() => {
    loadChannels();
  }, [loadChannels]);

  // Close search results on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (searchResultsRef.current && !searchResultsRef.current.contains(e.target as Node)) {
        setShowSearchResults(false);
      }
    }
    if (showSearchResults) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showSearchResults]);

  /**
   * Execute message search
   */
  const handleMessageSearch = useCallback(async () => {
    const q = messageSearchQuery.trim();
    if (!q) return;
    setIsSearching(true);
    setShowSearchResults(true);
    try {
      const result = await api.searchMessages(q);
      setSearchResults(result.messages || result.results || []);
    } catch (e) {
      console.error('[ChannelList] search error:', e);
      setSearchResults([]);
    } finally {
      setIsSearching(false);
    }
  }, [messageSearchQuery]);

  /**
   * Handle clicking a search result - navigate to that channel
   */
  const handleSearchResultClick = useCallback((result: any) => {
    const chId = result.channel_id;
    if (chId) {
      setActiveChannel(chId);
    }
    setShowSearchResults(false);
    setMessageSearchQuery('');
    setSearchResults([]);
  }, [setActiveChannel]);

  // Filter channels based on search query
  const filteredChannels = channels.filter((ch) => {
    const displayName = getChannelDisplayName(ch);
    return displayName.toLowerCase().includes(searchQuery.toLowerCase());
  });

  // Sort: pinned channels first (preserving the activity order
  // among themselves), then by last-activity timestamp.
  const sortedChannels = [...filteredChannels].sort((a, b) => {
    const aPin = (a as any).is_pinned ? 1 : 0;
    const bPin = (b as any).is_pinned ? 1 : 0;
    if (aPin !== bPin) return bPin - aPin;
    const metaA = channelMeta[a.id]?.lastMessage?.createdAt || '';
    const metaB = channelMeta[b.id]?.lastMessage?.createdAt || '';
    if (metaA && metaB) return metaB.localeCompare(metaA);
    if (metaA) return -1;
    if (metaB) return 1;
    return 0;
  });

  /**
   * Get display name for a channel (group name or other user's name for DMs)
   */
  function getChannelDisplayName(channel: Channel): string {
    if (channel.type === 'group') {
      return channel.name || 'Unnamed Group';
    }
    // For DMs, find the other participant's name
    const otherMember = channel.members.find((m) => m.user_id !== getCurrentUserId());
    return otherMember?.display_name || 'Unknown User';
  }

  /**
   * Get last message preview for a channel from channelMeta
   */
  function getLastMessagePreview(channel: Channel): { text: string; time: string | null } {
    const meta = channelMeta[channel.id];
    if (!meta?.lastMessage) return { text: 'No messages yet', time: null };
    const lm = meta.lastMessage;
    const preview = lm.type === 'file'
      ? `${lm.senderName} sent a file`
      : `${lm.senderName}: ${lm.content}`;
    return {
      text: preview.length > 45 ? preview.slice(0, 45) + '...' : preview,
      time: lm.createdAt,
    };
  }

  /**
   * Format timestamp for channel list (short form)
   */
  function formatShortTime(isoString: string | null): string {
    if (!isoString) return '';
    try {
      const date = new Date(isoString);
      const now = new Date();
      const diffMs = now.getTime() - date.getTime();
      const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
      if (diffDays === 0) {
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      } else if (diffDays === 1) {
        return 'Yesterday';
      } else if (diffDays < 7) {
        return date.toLocaleDateString([], { weekday: 'short' });
      }
      return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
    } catch {
      return '';
    }
  }

  /**
   * Get online status for a DM channel
   */
  function getDMStatus(channel: Channel): string {
    const otherMember = channel.members.find((m) => m.user_id !== getCurrentUserId());
    if (!otherMember) return 'offline';
    return getUserStatus(otherMember.user_id);
  }

  // `currentUserId` is declared above near the other hooks so it's
  // initialised before the filter helpers that read it.
  function getCurrentUserId(): string {
    return currentUserId;
  }

  return (
    <div className="flex flex-col h-full w-80 bg-slate-900 border-r border-slate-800">
      {/* Header with action buttons */}
      <div className="p-4 border-b border-slate-800">
        <h2 className="text-xl font-bold text-white mb-4">{t('nav.chats')}</h2>

        {/* Unified search — filters the channel list as you type AND
            searches actual message content (server-side, debounced).
            Previously this section had two stacked inputs ("Search
            messages…" + a channel filter) with separate icons that
            looked like a duplicate. Now one input drives both:
              • As the user types, `setSearchQuery` filters the channel
                list rendered below (instant, client-side).
              • A debounced effect kicks off `handleMessageSearch()` so
                matching messages show up in the dropdown overlay too.
            ``rtl:`` Tailwind variants flip the icon + padding so the
            search glyph sits on the correct (leading) side regardless
            of UI language. */}
        <div className="relative mb-4" ref={searchResultsRef}>
          {/* Flex layout — icon, input, clear button live as flex
              siblings inside a styled wrapper. Flex direction follows
              the document `dir` automatically, so the icon naturally
              sits at the leading edge (right in Arabic, left in
              English) without absolute positioning, RTL CSS variants,
              or padding tricks. The previous implementation pinned
              the icon with `absolute left-3` which (1) didn't flip in
              RTL even with `start-*` because an ancestor was forcing
              `direction: ltr`, and (2) drifted off the input's leading
              edge when the wrapper's positioned ancestor changed. */}
          <div className="flex items-center w-full px-3 py-2 bg-slate-800 rounded-lg border border-slate-700 focus-within:border-blue-500 transition-colors">
            <Search className="w-4 h-4 text-slate-400 shrink-0" />
            <input
              type="text"
              placeholder={t('chat.search') || 'بحث في المحادثات والرسائل…'}
              value={messageSearchQuery}
              onChange={(e) => {
                setMessageSearchQuery(e.target.value);
                setSearchQuery(e.target.value);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleMessageSearch();
              }}
              className="flex-1 min-w-0 ms-2 me-2 bg-transparent text-white text-sm placeholder-slate-500 border-none outline-none"
            />
            {messageSearchQuery && (
              <button
                onClick={() => {
                  setMessageSearchQuery('');
                  setSearchQuery('');
                  setShowSearchResults(false);
                  setSearchResults([]);
                }}
                className="shrink-0 text-slate-400 hover:text-white"
                aria-label="Clear search"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>

          {/* Search results overlay */}
          {showSearchResults && (
            <div className="absolute top-full left-0 right-0 mt-1 bg-slate-800 border border-slate-700 rounded-lg shadow-2xl z-50 max-h-72 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-slate-800">
              {isSearching ? (
                <div className="flex items-center justify-center py-6 text-slate-400 text-sm">
                  <span className="animate-spin inline-block w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full mr-2" />
                  Searching...
                </div>
              ) : searchResults.length === 0 ? (
                <div className="py-4 text-center text-slate-400 text-sm">No results found</div>
              ) : (
                searchResults.map((result: any, idx: number) => (
                  <button
                    key={result.id || idx}
                    onClick={() => handleSearchResultClick(result)}
                    className="w-full text-left px-3 py-2.5 hover:bg-slate-700 border-b border-slate-700 last:border-0 transition"
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-blue-400 font-medium truncate">
                        {result.sender?.display_name || result.sender_name || 'Unknown'}
                      </span>
                      <span className="text-xs text-slate-500">
                        {result.created_at ? formatShortTime(result.created_at) : ''}
                      </span>
                    </div>
                    <p className="text-xs text-slate-300 truncate mt-0.5">{result.content}</p>
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        {/* (Removed) the second search input that filtered channels —
            the unified box above now drives both `messageSearchQuery`
            (server-side message hits) and `searchQuery` (client-side
            channel filter), so this stack of two near-identical search
            rows is gone. */}

        {/* Action buttons */}
        <div className="flex gap-2">
          <button
            onClick={onNewDM}
            className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition"
          >
            <Plus className="w-4 h-4" />
            {t('chat.new_dm')}
          </button>
          <button
            onClick={onNewGroup}
            className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg text-sm font-medium transition"
          >
            <Users className="w-4 h-4" />
            {t('chat.new_group')}
          </button>
        </div>
      </div>

      {/* Channel list */}
      <div className="flex-1 overflow-y-auto">
        {isLoadingChannels ? (
          <div className="flex items-center justify-center h-32 text-slate-400">
            <div className="animate-spin rounded-full h-6 w-6 border-2 border-blue-500 border-t-transparent" />
          </div>
        ) : sortedChannels.length === 0 ? (
          <div className="p-4 text-center text-slate-400 text-sm">
            {searchQuery ? 'No conversations found' : t('chat.no_channels')}
          </div>
        ) : (
          <div className="space-y-1 p-2">
            {sortedChannels.map((channel) => {
              const displayName = getChannelDisplayName(channel);
              const isActive = activeChannelId === channel.id;
              const unreadCount = channelMeta[channel.id]?.unread || 0;
              const isDm = channel.type === 'dm';
              const status = isDm ? getDMStatus(channel) : null;
              const isOnline = isDm && status === 'online';

              // Authorization shown the same way ChannelHeader does:
              // creator, DM participant (always — the user is in it),
              // or site admin can wipe the channel.
              const isDmRow = (channel.type || '').toLowerCase() === 'dm';
              const canDeleteRow =
                isDmRow ||
                channel.created_by === currentUserId ||
                meRole === 'admin';

              return (
                <div
                  key={channel.id}
                  className="group relative"
                ><button
                  onClick={() => setActiveChannel(channel.id)}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setChannelMenu({
                      channelId: channel.id,
                      isPinned: !!(channel as any).is_pinned,
                      x: e.clientX,
                      y: e.clientY,
                    });
                  }}
                  className={`w-full text-left px-3 py-3 rounded-lg transition flex items-start gap-3 ${
                    isActive
                      ? 'bg-slate-800 border border-blue-500'
                      : 'hover:bg-slate-800 border border-transparent'
                  }`}
                >
                  {/* Avatar */}
                  <div className="relative flex-shrink-0 mt-0.5">
                    <div
                      className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-400 to-purple-500 flex items-center justify-center text-white font-semibold text-sm flex-shrink-0"
                      title={displayName}
                    >
                      {displayName.charAt(0).toUpperCase()}
                    </div>
                    {/* Online status dot for DMs */}
                    {isDm && (
                      <div
                        className={`absolute bottom-0 right-0 w-3 h-3 rounded-full border-2 border-slate-900 ${
                          isOnline ? 'bg-green-500' : 'bg-slate-500'
                        }`}
                      />
                    )}
                  </div>

                  {/* Content */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      {/* Icon based on channel type */}
                      {channel.type === 'group' && (
                        <Hash className="w-4 h-4 text-slate-400 flex-shrink-0" />
                      )}
                      <h3 className="font-semibold text-white truncate text-sm flex-1">
                        {displayName}
                      </h3>
                      {/* Active-call badge — visible only when a group
                          call is live in this channel. Click joins it. */}
                      <ChannelLiveBadge channelId={channel.id} />
                      {/* Timestamp of last message */}
                      {(() => {
                        const preview = getLastMessagePreview(channel);
                        return preview.time ? (
                          <span className="text-[10px] text-slate-500 flex-shrink-0">
                            {formatShortTime(preview.time)}
                          </span>
                        ) : null;
                      })()}
                    </div>
                    {/* Last message preview */}
                    <p className="text-xs text-slate-400 truncate mt-0.5">
                      {getLastMessagePreview(channel).text}
                    </p>
                  </div>

                  {/* Unread badge */}
                  {unreadCount > 0 && (
                    <div className="flex-shrink-0 bg-red-500 text-white text-xs font-bold w-5 h-5 rounded-full flex items-center justify-center">
                      {unreadCount > 99 ? '99+' : unreadCount}
                    </div>
                  )}
                </button>
                  {/* Hover-revealed delete (sits OUTSIDE the row's <button>
                      to avoid the nested-button HTML pitfall). Only shown
                      when the user has authorization to wipe this channel. */}
                  {canDeleteRow && (
                    <button
                      onClick={async (e) => {
                        e.stopPropagation();
                        const label = isDmRow
                          ? 'this conversation'
                          : `the group "${displayName}"`;
                        if (!window.confirm(`Delete ${label}? Cannot be undone.`)) return;
                        try {
                          await deleteChannel(channel.id);
                        } catch (err: any) {
                          window.alert('Failed to delete: ' + (err?.message || 'unknown'));
                        }
                      }}
                      className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-md text-slate-400 hover:text-red-400 hover:bg-red-500/15 opacity-0 group-hover:opacity-100 transition-all"
                      title={isDmRow ? 'Delete conversation' : 'Delete group'}
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
      <ChannelContextMenu
        state={channelMenu}
        onClose={() => setChannelMenu(null)}
      />
    </div>
  );
}
