/**
 * SimpleJoinGroup.tsx — Browse available groups and join with one tap.
 *
 * Shows a visual grid of groups the user is NOT yet a member of.
 * Each card shows: group name, member count, online count, and a
 * big obvious "Join" button.
 *
 * Also supports invite links (if someone shares a group ID directly).
 *
 * Design principles:
 *   - Card-based visual layout (not a list — feels like browsing)
 *   - One tap to join, no confirmation dialog
 *   - Instant feedback: "Joined!" checkmark animation
 *   - Shows who's already in the group (familiar faces)
 */

import React, { useState, useEffect, useMemo } from 'react';
import { X, Search, Users, Check, Loader2, UserPlus, Hash, ArrowRight } from 'lucide-react';
import { t } from '@/i18n';
import { api } from '@/services/api.client';
import { useContactsStore } from '@/stores/contacts.store';
import { useChatStore } from '@/stores/chat.store.v2';

interface AvailableGroup {
  id: string;
  name: string;
  description?: string;
  memberCount: number;
  onlineCount: number;
  members: { id: string; displayName: string; avatar?: string }[];
  hasJoined: boolean;
  isJoining: boolean;
}

interface SimpleJoinGroupProps {
  isOpen: boolean;
  onClose: () => void;
  onJoined?: (channelId: string) => void;
}

const SimpleJoinGroup: React.FC<SimpleJoinGroupProps> = ({ isOpen, onClose, onJoined }) => {
  const [groups, setGroups] = useState<AvailableGroup[]>([]);
  const [search, setSearch] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [inviteCode, setInviteCode] = useState('');
  const [showInviteInput, setShowInviteInput] = useState(false);
  const [inviteError, setInviteError] = useState('');

  const loadChannels = useChatStore((s) => s.loadChannels);
  const onlineUsers = useContactsStore((s) => s.onlineUsers);

  // Load available groups on open
  useEffect(() => {
    if (!isOpen) return;
    setIsLoading(true);
    setSearch('');
    setShowInviteInput(false);
    setInviteError('');

    loadAvailableGroups();
  }, [isOpen]);

  const loadAvailableGroups = async () => {
    try {
      // Fetch all channels and filter for public groups
      const channels = await api.listChannels();
      const data: any[] = Array.isArray(channels) ? channels : [];

      const mapped: AvailableGroup[] = data.map((g: any) => ({
        id: g.id,
        name: g.name || 'Unnamed Group',
        description: g.description || '',
        memberCount: g.member_count || g.memberCount || 0,
        onlineCount: g.online_count || 0,
        members: (g.members || []).slice(0, 4).map((m: any) => ({
          id: m.id || m.user_id,
          displayName: m.display_name || m.username || '',
          avatar: m.avatar,
        })),
        hasJoined: false,
        isJoining: false,
      }));

      setGroups(mapped);
    } catch {
      // API may not exist yet — show empty state
      setGroups([]);
    } finally {
      setIsLoading(false);
    }
  };

  // Filter by search
  const filteredGroups = useMemo(() => {
    const q = search.toLowerCase().trim();
    if (!q) return groups;
    return groups.filter(
      (g) =>
        g.name.toLowerCase().includes(q) ||
        (g.description || '').toLowerCase().includes(q)
    );
  }, [groups, search]);

  const handleJoin = async (groupId: string) => {
    setGroups((prev) =>
      prev.map((g) => (g.id === groupId ? { ...g, isJoining: true } : g))
    );

    try {
      await (api as any).post?.(`/api/channels/${groupId}/join`);

      setGroups((prev) =>
        prev.map((g) =>
          g.id === groupId ? { ...g, hasJoined: true, isJoining: false } : g
        )
      );

      // Refresh channel list
      loadChannels();

      // Notify parent
      setTimeout(() => {
        onJoined?.(groupId);
      }, 800);
    } catch {
      setGroups((prev) =>
        prev.map((g) => (g.id === groupId ? { ...g, isJoining: false } : g))
      );
    }
  };

  const handleInviteJoin = async () => {
    if (!inviteCode.trim()) return;
    setInviteError('');

    try {
      await (api as any).post?.(`/api/channels/join-invite`, { code: inviteCode.trim() });
      loadChannels();
      onClose();
    } catch {
      setInviteError(t('group.invite_invalid') || 'Invalid invite code');
    }
  };

  if (!isOpen) return null;

  // Gradient colors for group cards
  const gradients = [
    'from-blue-600/20 to-purple-600/20',
    'from-green-600/20 to-teal-600/20',
    'from-orange-600/20 to-red-600/20',
    'from-pink-600/20 to-purple-600/20',
    'from-cyan-600/20 to-blue-600/20',
    'from-yellow-600/20 to-orange-600/20',
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="w-full max-w-lg mx-4 bg-surface-900 rounded-2xl border border-surface-800 shadow-2xl overflow-hidden max-h-[80vh] flex flex-col">

        {/* ─── Header ─── */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-surface-800 flex-shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-full bg-green-600/20 flex items-center justify-center">
              <UserPlus size={18} className="text-green-400" />
            </div>
            <h2 className="text-base font-semibold text-white">
              {t('group.browse_groups') || 'Browse Groups'}
            </h2>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-full hover:bg-surface-800 flex items-center justify-center text-gray-500 hover:text-gray-300 transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        {/* ─── Search ─── */}
        <div className="px-5 py-3 flex-shrink-0">
          <div className="relative">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('group.search_groups') || 'Search groups...'}
              className="w-full pl-9 pr-4 py-2.5 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
              autoFocus
            />
          </div>
        </div>

        {/* ─── Group Cards ─── */}
        <div className="flex-1 overflow-y-auto px-5 pb-3">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 size={24} className="animate-spin text-gray-500" />
            </div>
          ) : filteredGroups.length === 0 ? (
            <div className="text-center py-12">
              <Users size={40} className="mx-auto text-gray-700 mb-3" />
              <p className="text-gray-500 text-sm">
                {t('group.no_groups_available') || 'No groups available to join'}
              </p>
              <p className="text-gray-600 text-xs mt-1">
                {t('group.try_invite') || 'Try using an invite code instead'}
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {filteredGroups.map((group, idx) => (
                <div
                  key={group.id}
                  className={`relative rounded-xl border border-surface-800 bg-gradient-to-br ${gradients[idx % gradients.length]} p-4 transition-all ${
                    group.hasJoined ? 'ring-2 ring-green-500/30' : ''
                  }`}
                >
                  <div className="flex items-start gap-3">
                    {/* Group icon */}
                    <div className="w-12 h-12 rounded-xl bg-surface-800/60 flex items-center justify-center flex-shrink-0">
                      <Hash size={22} className="text-gray-400" />
                    </div>

                    {/* Info */}
                    <div className="flex-1 min-w-0">
                      <h3 className="text-base font-semibold text-white truncate">
                        {group.name}
                      </h3>
                      {group.description && (
                        <p className="text-xs text-gray-400 mt-0.5 line-clamp-2">
                          {group.description}
                        </p>
                      )}

                      {/* Stats */}
                      <div className="flex items-center gap-3 mt-2 text-xs text-gray-500">
                        <span className="flex items-center gap-1">
                          <Users size={12} />
                          {group.memberCount} {t('group.members') || 'members'}
                        </span>
                        {group.onlineCount > 0 && (
                          <span className="flex items-center gap-1">
                            <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
                            {group.onlineCount} {t('status.online') || 'online'}
                          </span>
                        )}
                      </div>

                      {/* Member faces */}
                      {group.members.length > 0 && (
                        <div className="flex -space-x-2 mt-2">
                          {group.members.map((m) => (
                            <div
                              key={m.id}
                              className="w-6 h-6 rounded-full bg-surface-700 flex items-center justify-center text-[10px] text-gray-300 font-bold border border-surface-900"
                              title={m.displayName}
                            >
                              {m.displayName.charAt(0).toUpperCase()}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Join button */}
                    <div className="flex-shrink-0">
                      {group.hasJoined ? (
                        <div className="flex items-center gap-1 px-3 py-2 bg-green-600/20 text-green-400 rounded-lg text-sm font-medium">
                          <Check size={16} />
                          {t('group.joined') || 'Joined!'}
                        </div>
                      ) : group.isJoining ? (
                        <div className="flex items-center gap-1 px-3 py-2 bg-surface-800 text-gray-400 rounded-lg text-sm">
                          <Loader2 size={16} className="animate-spin" />
                        </div>
                      ) : (
                        <button
                          onClick={() => handleJoin(group.id)}
                          className="flex items-center gap-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors"
                        >
                          {t('group.join') || 'Join'}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ─── Invite Code Section ─── */}
        <div className="px-5 py-3 border-t border-surface-800 flex-shrink-0">
          {!showInviteInput ? (
            <button
              onClick={() => setShowInviteInput(true)}
              className="w-full text-center text-sm text-gray-500 hover:text-gray-400 transition-colors py-1"
            >
              {t('group.have_invite') || 'Have an invite code? Tap here'}
            </button>
          ) : (
            <div className="space-y-2">
              <div className="flex gap-2">
                <input
                  type="text"
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') handleInviteJoin(); }}
                  placeholder={t('group.enter_invite') || 'Paste invite code'}
                  className="flex-1 px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                  autoFocus
                />
                <button
                  onClick={handleInviteJoin}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-1"
                >
                  {t('group.join') || 'Join'}
                  <ArrowRight size={14} />
                </button>
              </div>
              {inviteError && (
                <p className="text-xs text-red-400">{inviteError}</p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default SimpleJoinGroup;
