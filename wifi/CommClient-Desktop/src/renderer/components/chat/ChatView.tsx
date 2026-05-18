/**
 * ChatView.tsx
 * Main chat container with ChannelList, ChannelHeader, MessageList, and MessageInput.
 * Manages layout, channel selection, and modal dialogs for creating DMs/groups.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { t } from '@/i18n';
import { useChatStore } from '@/stores/chat.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { AppLogger } from '@/services/AppLogger';

const _log = AppLogger.create('ChatView');
import { ChannelList } from './ChannelList';
import { ChannelHeader } from './ChannelHeader';
import { MessageList } from './MessageList';
import { MessageInput } from './MessageInput';
import { NewDMDialog } from './dialogs/NewDMDialog';
import { NewGroupDialog } from './dialogs/NewGroupDialog';

/**
 * Empty state when no channel is selected
 */
function EmptyChannelState() {
  return (
    <div className="flex-1 flex flex-col items-center justify-center bg-slate-950 gap-6">
      <div className="text-6xl">💬</div>
      <div className="text-center">
        <h2 className="text-xl font-semibold text-white mb-2">
          {t('chat.no_channels')}
        </h2>
        <p className="text-slate-400 text-sm">
          Start a conversation or create a group to begin chatting.
        </p>
      </div>
    </div>
  );
}

export function ChatView() {
  const [showNewDMDialog, setShowNewDMDialog] = useState(false);
  const [showNewGroupDialog, setShowNewGroupDialog] = useState(false);

  // Keyboard-shortcut bridge — GlobalShortcutsMount dispatches
  // these custom events when the user hits the configured combo
  // for "new DM" / "new group". We open the matching modal here.
  useEffect(() => {
    const onDM = () => setShowNewDMDialog(true);
    const onGroup = () => setShowNewGroupDialog(true);
    window.addEventListener('helen:open-new-dm', onDM);
    window.addEventListener('helen:open-new-group', onGroup);
    return () => {
      window.removeEventListener('helen:open-new-dm', onDM);
      window.removeEventListener('helen:open-new-group', onGroup);
    };
  }, []);

  const {
    channels,
    activeChannelId,
    messages,
    typingUsers,
    isLoadingMessages,
    hasMore,
    setActiveChannel,
    loadMessages,
    createDm,
    createGroup,
  } = useChatStore();

  // Get current user ID from auth store
  const currentUserId = useAuthStore((s) => s.user?.id) || '';

  // Get active channel
  const activeChannel = channels.find((ch) => ch.id === activeChannelId) || null;

  // Get messages for active channel
  const channelMessages = activeChannelId ? (messages[activeChannelId] || []) : [];

  // Get typing users for active channel (filtered to exclude self)
  const channelTypingUsers = activeChannelId
    ? (typingUsers[activeChannelId] || []).filter((uid) => uid !== currentUserId)
    : [];

  // Get member display names for typing indicator
  const typingUserNames = channelTypingUsers
    .map((uid) => {
      const member = activeChannel?.members.find((m) => m.user_id === uid);
      return member?.display_name || 'User';
    })
    .slice(0, 3); // Limit to 3 names for brevity

  // NOTE: messaging engine + presence listeners are initialized once at
  // login by AppBootstrap.onLogin and torn down by AppBootstrap.onLogout.
  // Calling them again here on ChatView mount caused
  //   1. engine.destroy() + recreate on every navigation into /chats,
  //      tearing down the queue/tracker/sync subsystems and replacing
  //      them with empty new ones (visible as "disconnect" / blank chat),
  //   2. duplicate presence subscriptions while ChatView was mounted —
  //      every server presence broadcast fired two React `set()` calls
  //      on the same contacts store, doubling re-render work.
  // The engines persist for the entire authenticated session; this view
  // is just the surface that *uses* them.

  /**
   * Handle load more messages
   */
  const handleLoadMore = useCallback(() => {
    if (activeChannelId && hasMore[activeChannelId]) {
      loadMessages(activeChannelId, true);
    }
  }, [activeChannelId, hasMore, loadMessages]);

  /**
   * Handle create DM
   */
  const handleCreateDm = useCallback(
    async (userId: string) => {
      try {
        const channel = await createDm(userId);
        setActiveChannel(channel.id);
        setShowNewDMDialog(false);
      } catch (error) {
        _log.error('Failed to create DM', error);
      }
    },
    [createDm, setActiveChannel]
  );

  /**
   * Handle create group
   */
  const handleCreateGroup = useCallback(
    async (name: string, memberIds: string[]) => {
      try {
        const channel = await createGroup(name, memberIds);
        setActiveChannel(channel.id);
        setShowNewGroupDialog(false);
      } catch (error) {
        _log.error('Failed to create group', error);
      }
    },
    [createGroup, setActiveChannel]
  );

  return (
    // ``h-full`` respects MainLayout's flex-1 area (height = viewport
    // minus 48px TitleBar). The previous ``h-screen`` measured against
    // 100vh and overflowed past the TitleBar by exactly 48px, which
    // forced the user to scroll the page down to find the message
    // composer. ``min-h-0`` lets the inner flex chain compute heights
    // correctly down to MessageInput.
    <div className="flex h-full min-h-0 bg-slate-950">
      {/* Left: Channel List */}
      <ChannelList
        onNewDM={() => setShowNewDMDialog(true)}
        onNewGroup={() => setShowNewGroupDialog(true)}
      />

      {/* Right: Chat or Empty State.
          ``min-h-0`` is critical — without it the inner flex child
          (MessageList) is allowed to grow past the parent's height
          and push the MessageInput off the bottom of the viewport,
          forcing the user to scroll the *window* down to find the
          composer. With min-h-0 the message list is constrained
          and only its own internal scroll fires.
          ``overflow-hidden`` on the column belt-and-braces guarantees
          even a transient overflow during route changes can't bleed
          past the boundary. */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {activeChannel ? (
          <>
            {/* Header — never shrinks */}
            <div className="flex-shrink-0">
              <ChannelHeader channel={activeChannel} />
            </div>

            {/* Messages — fills remaining space and scrolls internally */}
            <div className="flex-1 min-h-0 flex flex-col">
              <MessageList
                channelId={activeChannelId!}
                messages={channelMessages}
                onLoadMore={handleLoadMore}
                isLoadingMessages={isLoadingMessages}
                hasMore={activeChannelId ? (hasMore[activeChannelId] || false) : false}
                currentUserId={currentUserId}
              />
            </div>

            {/* Input — pinned to the bottom, NEVER shrinks or hides */}
            <div className="flex-shrink-0">
              <MessageInput
                channelId={activeChannelId!}
                typingUsers={typingUserNames}
                currentUsername="You"
              />
            </div>
          </>
        ) : (
          <EmptyChannelState />
        )}
      </div>

      {/* Dialogs */}
      {showNewDMDialog && (
        <NewDMDialog
          onClose={() => setShowNewDMDialog(false)}
          onCreateDm={handleCreateDm}
        />
      )}

      {showNewGroupDialog && (
        <NewGroupDialog
          onClose={() => setShowNewGroupDialog(false)}
          onCreateGroup={handleCreateGroup}
        />
      )}
    </div>
  );
}

export default ChatView;
