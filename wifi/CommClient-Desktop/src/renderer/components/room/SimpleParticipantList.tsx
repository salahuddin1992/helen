/**
 * SimpleParticipantList.tsx — Clear participant list with friendly permissions.
 *
 * Replaces technical RBAC terminology with child-friendly labels:
 *   admin     → "Owner" (crown icon, gold)
 *   moderator → "Helper" (shield icon, blue)
 *   member    → (no badge — the default)
 *
 * Permission actions are contextual tap menus, not hidden in settings:
 *   - Owner sees: "Make Helper", "Remove from group"
 *   - Helper sees: "Remove from group" (on members only)
 *   - Member sees: nothing (just the list)
 *
 * Online status is shown with prominent color coding:
 *   - Green dot = online
 *   - Gray dot = offline
 *   - Yellow dot = in a call
 *
 * Design: large 56px rows, clear visual hierarchy, tap-to-act.
 */

import React, { useState, useMemo } from 'react';
import {
  X, AlertCircle, CheckCircle, MoreHorizontal, UserMinus,
  Phone, Mic, MicOff, Video, Search, Lock
} from 'lucide-react';
import { t } from '@/i18n';

// ── Types ────────────────────────────────────────────────

export type ParticipantRole = 'admin' | 'moderator' | 'member';
export type ParticipantStatus = 'online' | 'offline' | 'in_call';

export interface Participant {
  id: string;
  displayName: string;
  username: string;
  avatar?: string;
  role: ParticipantRole;
  status: ParticipantStatus;
  isMuted?: boolean;       // Only relevant when in_call
  hasVideo?: boolean;      // Only relevant when in_call
  isCurrentUser?: boolean;
}

// ── Friendly Labels ──────────────────────────────────────

function roleLabel(role: ParticipantRole): string {
  switch (role) {
    case 'admin': return t('role.owner') || 'Owner';
    case 'moderator': return t('role.helper') || 'Helper';
    default: return '';
  }
}

function roleIcon(role: ParticipantRole) {
  switch (role) {
    case 'admin':
      return <AlertCircle size={12} className="text-yellow-400" />;
    case 'moderator':
      return <Lock size={12} className="text-blue-400" />;
    default:
      return null;
  }
}

function statusColor(status: ParticipantStatus): string {
  switch (status) {
    case 'online': return 'bg-green-500';
    case 'in_call': return 'bg-yellow-500';
    default: return 'bg-gray-600';
  }
}

function statusLabel(status: ParticipantStatus): string {
  switch (status) {
    case 'online': return t('status.online') || 'Online';
    case 'in_call': return t('status.in_call') || 'In a Call';
    default: return t('status.offline') || 'Offline';
  }
}

// ── Component ────────────────────────────────────────────

interface SimpleParticipantListProps {
  isOpen: boolean;
  onClose: () => void;
  participants: Participant[];
  currentUserRole: ParticipantRole;
  groupName: string;
  onMakeHelper?: (userId: string) => void;
  onRemoveHelper?: (userId: string) => void;
  onRemoveMember?: (userId: string) => void;
  onStartDM?: (userId: string) => void;
  onCallUser?: (userId: string) => void;
}

const SimpleParticipantList: React.FC<SimpleParticipantListProps> = ({
  isOpen,
  onClose,
  participants,
  currentUserRole,
  groupName,
  onMakeHelper,
  onRemoveHelper,
  onRemoveMember,
  onStartDM,
  onCallUser,
}) => {
  const [search, setSearch] = useState('');
  const [menuOpenFor, setMenuOpenFor] = useState<string | null>(null);

  const canManage = currentUserRole === 'admin';
  const canModerate = currentUserRole === 'admin' || currentUserRole === 'moderator';

  // Sort: owner first, then helpers, then online, then alphabetical
  const sortedParticipants = useMemo(() => {
    const q = search.toLowerCase().trim();
    let filtered = participants;
    if (q) {
      filtered = participants.filter(
        (p) =>
          p.displayName.toLowerCase().includes(q) ||
          p.username.toLowerCase().includes(q)
      );
    }

    return [...filtered].sort((a, b) => {
      // Role priority
      const rolePriority = { admin: 0, moderator: 1, member: 2 };
      if (rolePriority[a.role] !== rolePriority[b.role]) {
        return rolePriority[a.role] - rolePriority[b.role];
      }
      // Status priority
      const statusPriority = { in_call: 0, online: 1, offline: 2 };
      if (statusPriority[a.status] !== statusPriority[b.status]) {
        return statusPriority[a.status] - statusPriority[b.status];
      }
      return a.displayName.localeCompare(b.displayName);
    });
  }, [participants, search]);

  const onlineCount = participants.filter((p) => p.status !== 'offline').length;
  const inCallCount = participants.filter((p) => p.status === 'in_call').length;

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-sm mx-4 bg-surface-900 rounded-2xl border border-surface-800 shadow-2xl overflow-hidden max-h-[75vh] flex flex-col">

        {/* ─── Header ─── */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-surface-800 flex-shrink-0">
          <div>
            <h3 className="text-base font-semibold text-white">
              {t('group.people') || 'People'}
            </h3>
            <div className="flex items-center gap-3 text-xs text-gray-500 mt-0.5">
              <span>{participants.length} {t('group.total') || 'total'}</span>
              <span className="flex items-center gap-1">
                <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
                {onlineCount} {t('status.online') || 'online'}
              </span>
              {inCallCount > 0 && (
                <span className="flex items-center gap-1">
                  <Phone size={10} className="text-yellow-400" />
                  {inCallCount}
                </span>
              )}
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-full hover:bg-surface-800 flex items-center justify-center text-gray-500 hover:text-gray-300"
          >
            <X size={18} />
          </button>
        </div>

        {/* ─── Search ─── */}
        {participants.length > 6 && (
          <div className="px-5 py-2 flex-shrink-0">
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('group.search_people') || 'Search people...'}
                className="w-full pl-8 pr-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
              />
            </div>
          </div>
        )}

        {/* ─── Participant List ─── */}
        <div className="flex-1 overflow-y-auto px-2 py-1">
          {sortedParticipants.map((person) => (
            <div
              key={person.id}
              className="flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-surface-800/50 transition-colors group relative"
            >
              {/* Avatar + status dot */}
              <div className="relative flex-shrink-0">
                <div className="w-10 h-10 rounded-full bg-gradient-to-br from-surface-600 to-surface-700 flex items-center justify-center text-white text-sm font-bold">
                  {person.displayName.charAt(0).toUpperCase()}
                </div>
                <div className={`absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full border-2 border-surface-900 ${statusColor(person.status)}`} />
              </div>

              {/* Name + role + status */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="text-sm font-medium text-white truncate">
                    {person.displayName}
                  </span>
                  {person.isCurrentUser && (
                    <span className="text-[10px] text-gray-500 flex-shrink-0">
                      ({t('group.you') || 'you'})
                    </span>
                  )}
                  {roleIcon(person.role)}
                  {person.role !== 'member' && (
                    <span className={`text-[10px] font-medium flex-shrink-0 ${
                      person.role === 'admin' ? 'text-yellow-500' : 'text-blue-400'
                    }`}>
                      {roleLabel(person.role)}
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-2 text-xs text-gray-500 mt-0.5">
                  <span>{statusLabel(person.status)}</span>
                  {/* In-call indicators */}
                  {person.status === 'in_call' && (
                    <span className="flex items-center gap-1">
                      {person.isMuted ? (
                        <MicOff size={10} className="text-red-400" />
                      ) : (
                        <Mic size={10} className="text-green-400" />
                      )}
                      {person.hasVideo && (
                        <Video size={10} className="text-blue-400" />
                      )}
                    </span>
                  )}
                </div>
              </div>

              {/* Action button (visible on hover, or when menu open) */}
              {!person.isCurrentUser && (canManage || canModerate || onStartDM || onCallUser) && (
                <div className="flex-shrink-0 relative">
                  <button
                    onClick={() => setMenuOpenFor(menuOpenFor === person.id ? null : person.id)}
                    className="w-8 h-8 rounded-full hover:bg-surface-700 flex items-center justify-center text-gray-600 group-hover:text-gray-400 transition-colors"
                  >
                    <MoreHorizontal size={16} />
                  </button>

                  {/* Context menu */}
                  {menuOpenFor === person.id && (
                    <>
                      {/* Backdrop to close menu */}
                      <div
                        className="fixed inset-0 z-10"
                        onClick={() => setMenuOpenFor(null)}
                      />
                      <div className="absolute right-0 top-full mt-1 z-20 w-48 bg-surface-800 rounded-xl border border-surface-700 shadow-xl overflow-hidden">
                        {/* Chat privately */}
                        {onStartDM && (
                          <button
                            onClick={() => { onStartDM(person.id); setMenuOpenFor(null); onClose(); }}
                            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-gray-300 hover:bg-surface-700 transition-colors"
                          >
                            💬 {t('group.send_message') || 'Send message'}
                          </button>
                        )}

                        {/* Call directly */}
                        {onCallUser && person.status === 'online' && (
                          <button
                            onClick={() => { onCallUser(person.id); setMenuOpenFor(null); onClose(); }}
                            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-gray-300 hover:bg-surface-700 transition-colors"
                          >
                            <Phone size={14} className="text-green-400" />
                            {t('group.call_directly') || 'Call directly'}
                          </button>
                        )}

                        {/* Admin: Make helper */}
                        {canManage && person.role === 'member' && onMakeHelper && (
                          <button
                            onClick={() => { onMakeHelper(person.id); setMenuOpenFor(null); }}
                            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-gray-300 hover:bg-surface-700 transition-colors"
                          >
                            <Lock size={14} className="text-blue-400" />
                            {t('group.make_helper') || 'Make Helper'}
                          </button>
                        )}

                        {/* Admin: Remove helper role */}
                        {canManage && person.role === 'moderator' && onRemoveHelper && (
                          <button
                            onClick={() => { onRemoveHelper(person.id); setMenuOpenFor(null); }}
                            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-gray-300 hover:bg-surface-700 transition-colors"
                          >
                            <Lock size={14} className="text-gray-400" />
                            {t('group.remove_helper') || 'Remove Helper role'}
                          </button>
                        )}

                        {/* Admin/Moderator: Remove from group */}
                        {canModerate && person.role === 'member' && onRemoveMember && (
                          <button
                            onClick={() => { onRemoveMember(person.id); setMenuOpenFor(null); }}
                            className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-red-400 hover:bg-red-500/10 transition-colors"
                          >
                            <UserMinus size={14} />
                            {t('group.remove_from_group') || 'Remove from group'}
                          </button>
                        )}
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        {/* ─── Legend ─── */}
        <div className="px-5 py-3 border-t border-surface-800 flex-shrink-0">
          <div className="flex items-center justify-center gap-4 text-[10px] text-gray-600">
            <span className="flex items-center gap-1">
              <AlertCircle size={10} className="text-yellow-400" />
              {t('role.owner') || 'Owner'}
            </span>
            <span className="flex items-center gap-1">
              <Lock size={10} className="text-blue-400" />
              {t('role.helper') || 'Helper'}
            </span>
            <span className="flex items-center gap-1">
              <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
              {t('status.online') || 'Online'}
            </span>
            <span className="flex items-center gap-1">
              <Phone size={10} className="text-yellow-400" />
              {t('status.in_call') || 'In a Call'}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SimpleParticipantList;
