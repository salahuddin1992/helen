/**
 * GroupActionHub.tsx — Unified floating action bar for group channels.
 *
 * Replaces scattered action buttons with a single, persistent bar at the
 * top of the chat area (below the channel header). Shows contextual
 * actions based on the channel type and the user's permissions.
 *
 * Actions (left-to-right):
 *   [Voice Call]  [Video Call]  [Share Screen]  [People (count)]  [Add People]
 *
 * Design principles:
 *   - Always visible (no hunting for buttons)
 *   - Icon + label (no icon-only ambiguity)
 *   - Color-coded: green = voice, blue = video, purple = screen, gray = people
 *   - Badge on "People" shows online count
 *   - Disabled states clearly grayed out with tooltip
 *
 * This component coordinates opening the QuickCallSheet,
 * SimpleParticipantList, and SimpleCreateGroup/SimpleJoinGroup modals.
 */

import React, { useState } from 'react';
import {
  Phone, Video, AlertCircle, Users, UserPlus,
} from 'lucide-react';
import { t } from '@/i18n';
import QuickCallSheet from './QuickCallSheet';
import SimpleParticipantList, { Participant, ParticipantRole } from './SimpleParticipantList';

// ── Types ────────────────────────────────────────────────

interface GroupActionHubProps {
  channelId: string;
  channelName: string;
  channelType: 'dm' | 'group';
  targetUserId?: string;          // For DM calls
  participantCount: number;
  onlineCount: number;
  currentUserRole: ParticipantRole;
  participants: Participant[];
  hasActiveCall: boolean;
  // Optional callbacks
  onAddPeople?: () => void;
  onMakeHelper?: (userId: string) => void;
  onRemoveHelper?: (userId: string) => void;
  onRemoveMember?: (userId: string) => void;
  onStartDM?: (userId: string) => void;
  onCallUser?: (userId: string) => void;
}

const GroupActionHub: React.FC<GroupActionHubProps> = ({
  channelId,
  channelName,
  channelType,
  targetUserId,
  participantCount,
  onlineCount,
  currentUserRole,
  participants,
  hasActiveCall,
  onAddPeople,
  onMakeHelper,
  onRemoveHelper,
  onRemoveMember,
  onStartDM,
  onCallUser,
}) => {
  const [showCallSheet, setShowCallSheet] = useState(false);
  const [showPeople, setShowPeople] = useState(false);

  const canAddPeople = currentUserRole === 'admin' || currentUserRole === 'moderator';

  return (
    <>
      {/* ─── Action Bar ─── */}
      <div className="flex items-center gap-1.5 px-3 py-2 bg-surface-900/80 border-b border-surface-800">
        {/* Voice Call */}
        <button
          onClick={() => setShowCallSheet(true)}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg hover:bg-green-600/10 text-gray-400 hover:text-green-400 transition-colors text-xs font-medium"
          title={t('call.voice_call') || 'Voice Call'}
        >
          <Phone size={15} />
          <span className="hidden sm:inline">{t('call.audio_call') || 'Call'}</span>
        </button>

        {/* Video Call */}
        <button
          onClick={() => setShowCallSheet(true)}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg hover:bg-blue-600/10 text-gray-400 hover:text-blue-400 transition-colors text-xs font-medium"
          title={t('call.video_call') || 'Video Call'}
        >
          <Video size={15} />
          <span className="hidden sm:inline">{t('call.video_call') || 'Video'}</span>
        </button>

        {/* Share Screen */}
        <button
          onClick={() => setShowCallSheet(true)}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg hover:bg-purple-600/10 text-gray-400 hover:text-purple-400 transition-colors text-xs font-medium"
          title={t('call.share_screen') || 'Share Screen'}
        >
          <AlertCircle size={15} />
          <span className="hidden sm:inline">{t('call.share_screen') || 'Screen'}</span>
        </button>

        {/* Spacer */}
        <div className="flex-1" />

        {/* People (with online badge) */}
        <button
          onClick={() => setShowPeople(true)}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg hover:bg-surface-800 text-gray-400 hover:text-gray-300 transition-colors text-xs font-medium relative"
          title={`${participantCount} ${t('group.members') || 'members'}`}
        >
          <Users size={15} />
          <span>{participantCount}</span>
          {/* Online badge */}
          {onlineCount > 0 && (
            <span className="flex items-center gap-0.5 text-green-500">
              <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
              {onlineCount}
            </span>
          )}
        </button>

        {/* Add People (admin/moderator only) */}
        {canAddPeople && onAddPeople && channelType === 'group' && (
          <button
            onClick={onAddPeople}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg hover:bg-surface-800 text-gray-400 hover:text-blue-400 transition-colors text-xs font-medium"
            title={t('group.add_people') || 'Add People'}
          >
            <UserPlus size={15} />
            <span className="hidden sm:inline">{t('group.add') || 'Add'}</span>
          </button>
        )}
      </div>

      {/* ─── Modals ─── */}
      <QuickCallSheet
        isOpen={showCallSheet}
        onClose={() => setShowCallSheet(false)}
        channelId={channelId}
        channelName={channelName}
        channelType={channelType}
        targetUserId={targetUserId}
        hasActiveCall={hasActiveCall}
      />

      <SimpleParticipantList
        isOpen={showPeople}
        onClose={() => setShowPeople(false)}
        participants={participants}
        currentUserRole={currentUserRole}
        groupName={channelName}
        onMakeHelper={onMakeHelper}
        onRemoveHelper={onRemoveHelper}
        onRemoveMember={onRemoveMember}
        onStartDM={onStartDM}
        onCallUser={onCallUser}
      />
    </>
  );
};

export default GroupActionHub;
