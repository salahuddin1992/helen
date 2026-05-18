/**
 * RoomStateBar.tsx — Visual status indicator for the current room/channel.
 *
 * Sits below the channel header and shows the live state of the room:
 *
 * States:
 *   idle      → No indicator shown (clean view)
 *   call      → Green pulsing bar: "Call in progress • 3 people • Join"
 *   typing    → Subtle indicator: "Alice is typing..."
 *   screen    → Purple bar: "Bob is sharing their screen • Watch"
 *
 * Design principles:
 *   - Non-intrusive: slides in/out with animation
 *   - Color-coded: green = call, purple = screen share, gray = typing
 *   - Actionable: "Join" button right on the bar (one-tap)
 *   - Auto-hides when not relevant
 */

import React from 'react';
import {
  Phone, AlertCircle, Users, ArrowRight, Loader2
} from 'lucide-react';
import { t } from '@/i18n';

// ── Types ────────────────────────────────────────────────

export type RoomState = 'idle' | 'call' | 'typing' | 'screen_share';

interface RoomStateBarProps {
  state: RoomState;
  // Call state
  callParticipantCount?: number;
  callDuration?: string;             // "02:34"
  isUserInCall?: boolean;
  onJoinCall?: () => void;
  // Typing state
  typingNames?: string[];            // ["Alice", "Bob"]
  // Screen share state
  screenSharerName?: string;
  onWatchScreen?: () => void;
}

const RoomStateBar: React.FC<RoomStateBarProps> = ({
  state,
  callParticipantCount = 0,
  callDuration,
  isUserInCall = false,
  onJoinCall,
  typingNames = [],
  screenSharerName,
  onWatchScreen,
}) => {
  if (state === 'idle') return null;

  // ── Call in progress ───────────────────────────
  if (state === 'call') {
    return (
      <div className="flex items-center gap-3 px-4 py-2 bg-green-600/10 border-b border-green-600/20 animate-slide-down">
        {/* Pulsing phone icon */}
        <div className="relative flex-shrink-0">
          <Phone size={14} className="text-green-400" />
          <div className="absolute inset-0 rounded-full bg-green-400/30 animate-ping" />
        </div>

        {/* Info */}
        <div className="flex-1 min-w-0 flex items-center gap-2 text-sm">
          <span className="text-green-400 font-medium">
            {t('room.call_active') || 'Call in progress'}
          </span>
          <span className="text-green-600">•</span>
          <span className="flex items-center gap-1 text-green-500/80 text-xs">
            <Users size={12} />
            {callParticipantCount}
          </span>
          {callDuration && (
            <>
              <span className="text-green-600">•</span>
              <span className="text-green-500/60 text-xs font-mono">{callDuration}</span>
            </>
          )}
        </div>

        {/* Action */}
        {!isUserInCall && onJoinCall && (
          <button
            onClick={onJoinCall}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white text-xs font-semibold rounded-lg transition-colors flex-shrink-0"
          >
            {t('call.join') || 'Join'}
            <ArrowRight size={12} />
          </button>
        )}

        {isUserInCall && (
          <span className="text-xs text-green-500/60 flex-shrink-0">
            {t('call.youre_here') || "You're here"}
          </span>
        )}
      </div>
    );
  }

  // ── Screen share ───────────────────────────────
  if (state === 'screen_share') {
    return (
      <div className="flex items-center gap-3 px-4 py-2 bg-purple-600/10 border-b border-purple-600/20 animate-slide-down">
        <AlertCircle size={14} className="text-purple-400 flex-shrink-0" />

        <div className="flex-1 min-w-0 text-sm">
          <span className="text-purple-400">
            {screenSharerName
              ? `${screenSharerName} ${t('room.is_sharing') || 'is sharing their screen'}`
              : (t('room.screen_shared') || 'Screen is being shared')}
          </span>
        </div>

        {onWatchScreen && (
          <button
            onClick={onWatchScreen}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-purple-600 hover:bg-purple-700 text-white text-xs font-semibold rounded-lg transition-colors flex-shrink-0"
          >
            {t('room.watch') || 'Watch'}
            <ArrowRight size={12} />
          </button>
        )}
      </div>
    );
  }

  // ── Typing indicator ───────────────────────────
  if (state === 'typing' && typingNames.length > 0) {
    const typingText = typingNames.length === 1
      ? `${typingNames[0]} ${t('room.is_typing') || 'is typing...'}`
      : typingNames.length === 2
        ? `${typingNames[0]} ${t('room.and') || 'and'} ${typingNames[1]} ${t('room.are_typing') || 'are typing...'}`
        : `${typingNames.length} ${t('room.people_typing') || 'people are typing...'}`;

    return (
      <div className="flex items-center gap-2 px-4 py-1.5 border-b border-surface-800 animate-slide-down">
        {/* Animated dots */}
        <div className="flex gap-0.5">
          <div className="w-1 h-1 rounded-full bg-gray-500 animate-bounce" style={{ animationDelay: '0ms' }} />
          <div className="w-1 h-1 rounded-full bg-gray-500 animate-bounce" style={{ animationDelay: '150ms' }} />
          <div className="w-1 h-1 rounded-full bg-gray-500 animate-bounce" style={{ animationDelay: '300ms' }} />
        </div>
        <span className="text-xs text-gray-500">{typingText}</span>
      </div>
    );
  }

  return null;
};

export default RoomStateBar;
